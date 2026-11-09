from datetime import datetime, timedelta, timezone

import pytest

from bkanalyzer import analysis, db, history, sim
from bkanalyzer.analysis import Thresholds
from bkanalyzer.bot import Bot, ModelCache

from test_bot import ME, NOW, seed

CFG = sim.SimSettings(bank=1000, strategy="fixed", stake=50)


def analyses(conn):
    model, _ = ModelCache().get(conn)
    return [analysis.analyze(m, model, int(NOW.timestamp())) for m in analysis.load_board(conn, now=NOW)]


def add_series(conn, start, winners, series_id=900, series_type=1, t1="Team Spirit", t2="Tundra Esports", first=0):
    for i, w in enumerate(winners):
        conn.execute(
            "INSERT INTO pro_maps (match_id, start_time, league_id, series_id, series_type, radiant_team_id, radiant_name, dire_team_id, dire_name, radiant_win) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (10_000 + series_id * 10 + first + i, start + i * 3000, 1, series_id, series_type, 1, t1, 2, t2, int(w == t1)),
        )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "bk.sqlite3")
    seed(c)
    return c


def test_stake_strategies():
    assert sim.stake_for(CFG, 1000, 2.0, 0.6) == 50
    assert sim.stake_for(sim.SimSettings(strategy="percent", percent=0.02), 1000, 2.0, 0.6) == 20
    # Келли: edge 0.2 / (2-1) = 0.2, четверть → 5% банка
    assert sim.stake_for(sim.SimSettings(strategy="kelly", kelly=0.25), 1000, 2.0, 0.6) == 50
    assert sim.stake_for(sim.SimSettings(strategy="kelly"), 1000, 2.0, 0.4) == 0
    assert sim.stake_for(sim.SimSettings(stake=500), 1000, 2.0, 0.6) == 100  # не больше 10% банка


def test_place_once_per_match_and_mode(conn):
    placed = sim.place_bets(conn, analyses(conn), Thresholds(0.05), CFG, now=NOW)
    assert [(b["pick"], b["mode"], b["market"]) for b in placed] == [("Spirit", "prematch", "match_winner")]
    assert sim.place_bets(conn, analyses(conn), Thresholds(0.05), CFG, now=NOW) == []
    assert sim.bank(conn, "prematch", CFG) == 950
    s = sim.summary(conn, CFG, "prematch")
    assert (s["bank"], s["in_play"], s["free"]) == (1000, 50, 950)
    assert "банк 1000 (в игре 50, свободно 950)" in sim.report(conn, CFG)


def test_settle_match_and_clv(conn):
    sim.place_bets(conn, analyses(conn), Thresholds(0.05), CFG, now=NOW)
    # линия перед стартом упала до 1.70 → CLV положительный
    db.save_feed_rows(conn, [{"bookmaker": "oddin", "fixture_id": "e1", "market": "match_winner", "outcome": "Spirit", "price": 1.7, "phase": "prematch"}], source="oddiwire")
    start = int(datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc).timestamp())
    add_series(conn, start, ["Team Spirit"])  # 1:0 — серия не закончена
    assert sim.settle(conn, now=NOW) == []
    add_series(conn, start + 7000, ["Team Spirit"], series_id=900, first=1)
    done = sim.settle(conn, now=NOW)
    assert len(done) == 1 and done[0]["status"] == "won" and done[0]["profit"] == 45.0
    s = sim.summary(conn, CFG, "prematch")
    assert s["won"] == 1 and s["roi"] == pytest.approx(0.9) and s["clv"] == pytest.approx(1.9 / 1.7 - 1)
    assert s["bank"] == pytest.approx(1045)


def test_settle_map_and_void(conn):
    bet = {"pick": "Team Allied", "market": "map_2_winner"}
    maps = [{"radiant_name": "Team Allied", "dire_name": "Team Lynx", "radiant_win": 1, "series_type": 1},
            {"radiant_name": "Team Allied", "dire_name": "Team Lynx", "radiant_win": 0, "series_type": 1}]
    assert sim.settle_result(bet, maps[:1]) is None
    assert sim.settle_result(bet, maps)[0] == "lost"
    bo2 = [dict(m, series_type=3) for m in maps]
    assert sim.settle_result({"pick": "Team Allied", "market": "match_winner"}, bo2)[0] == "void"
    # ставка без результата аннулируется через 3 дня
    sim.ensure_schema(conn)
    conn.execute("INSERT INTO paper_bets (created_at, mode, fixture_id, team1, team2, market, pick, price, prob, stake, bank_before, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (NOW.isoformat(), "live", "x", "A", "B", "match_winner", "A", 2.0, 0.6, 50, 1000, "fixed"))
    assert sim.settle(conn, now=NOW + timedelta(days=4))[0]["status"] == "void"


def test_sim_command(conn):
    sim.place_bets(conn, analyses(conn), Thresholds(0.05), CFG, now=NOW)
    text = Bot(conn, (ME,), now=lambda: NOW, sim=CFG).handle(ME, "/sim")
    assert "До матча: ставок 1 (открыто 1" in text and "⏳ Spirit — Tundra" in text and "из 100" in text


def test_simulate_step_notifies(conn, monkeypatch):
    from bkanalyzer.cli import simulate_step
    from bkanalyzer.config import Settings

    # линия в seed() записана временем NOW; сдвигаем «сейчас» модуля анализа не нужно — load_board берёт 6 часов
    orig = analysis.load_board
    monkeypatch.setattr(analysis, "load_board", lambda c, **kw: orig(c, now=NOW))
    sent = []
    simulate_step(Settings(sim_bank=1000, sim_stake=50), conn, ModelCache(), notify=sent.append, log=lambda *_: None)
    assert len(sent) == 1 and sent[0].startswith("📝 Бумажная ставка (до матча): Spirit — Tundra")


def test_no_bet_when_result_known(conn):
    start = int(datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc).timestamp())
    add_series(conn, start, ["Team Spirit", "Team Spirit"])  # серия уже сыграна
    assert sim.place_bets(conn, analyses(conn), Thresholds(0.05), CFG, now=NOW) == []


def test_sim_reset(tmp_path, monkeypatch):
    from bkanalyzer.cli import main

    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.sqlite3"))
    c = db.connect(tmp_path / "x.sqlite3")
    sim.ensure_schema(c)
    c.execute("INSERT INTO paper_bets (created_at, mode, fixture_id, market, pick, price, prob, stake, bank_before, strategy) VALUES ('t','live','f','m','p',2,0.5,1,1,'fixed')")
    c.commit()
    assert main(["sim-reset"]) == 1
    assert main(["sim-reset", "--yes"]) == 0
    assert c.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0] == 0


def test_parallel_strategies_and_migration(conn):
    # старая схема с UNIQUE (fixture_id, mode) переносится без потери ставок
    conn.execute("DROP TABLE IF EXISTS paper_bets")
    conn.executescript(sim.SCHEMA.replace("UNIQUE (fixture_id, mode, strategy)", "UNIQUE (fixture_id, mode)"))
    conn.execute("INSERT INTO paper_bets (created_at, mode, fixture_id, market, pick, price, prob, stake, bank_before, strategy) "
                 "VALUES ('2026-11-01T00:00:00+00:00', 'prematch', 'old', 'match_winner', 'X', 2.0, 0.6, 10, 1000, 'fixed')")
    conn.commit()
    sim.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0] == 1

    cfg = sim.SimSettings(bank=25000, stake=1250, percent=0.07, kelly=1.0, strategies=("fixed", "percent", "kelly"))
    placed = sim.place_bets(conn, analyses(conn), Thresholds(0.05), cfg, now=NOW)
    assert len(placed) == 1 and set(placed[0]["stakes"]) == {"fixed", "percent", "kelly"}
    assert placed[0]["stakes"]["fixed"] == 1250 and placed[0]["stakes"]["percent"] == 1750
    assert "фикс. сумма 1250" in sim.placed_message(placed[0])
    rows = conn.execute("SELECT strategy, stake FROM paper_bets WHERE fixture_id='e1'").fetchall()
    assert len(rows) == 3
    assert sim.bank(conn, "prematch", sim.replace(cfg, strategy="percent")) == 25000 - 1750
    report = sim.report(conn, cfg)
    assert "фикс. 1250, 7% банка, Келли ×1" in report

    done = [{"fixture_id": "e1", "mode": "prematch", "strategy": st, "profit": p, "status": "won", "note": "2:0",
             "team1": "Spirit", "team2": "Tundra", "market": "match_winner", "pick": "Spirit", "price": 1.9, "stake": 1}
            for st, p in (("fixed", 1125), ("percent", 1575), ("kelly", 900))]
    msgs = sim.settled_messages(done)
    assert len(msgs) == 1 and "Келли +900" in msgs[0]


def test_longshots_recorded_quietly(conn):
    t = Thresholds(min_ev=0.05, max_gap=0.01)  # любой пик — «подозрительный»
    assert sim.place_bets(conn, analyses(conn), t, CFG, now=NOW) == []  # основную ставку не делаем
    noted = sim.place_longshots(conn, analyses(conn), t, sim.replace(CFG, strategies=("fixed", "kelly")), now=NOW)
    assert noted and all(b["mode"] == "longshot" for b in noted)
    assert set(noted[0]["stakes"]) == {"fixed"}  # на заметку — только фиксированной суммой
    conn.execute("UPDATE paper_bets SET status='won', profit=stake*(price-1)")
    conn.commit()
    text = sim.report(conn, CFG)
    assert "Аутсайдеры (на заметку): записей 1, рассчитано 1" in text and "выиграли 100%" in text
    assert "Рассчитано ставок: 0 из 100" in text  # в зачёт 100 ставок не идут


def test_low_history_pick_is_suspicious():
    from bkanalyzer.analysis import Pick, Thresholds
    t = Thresholds(min_ev=0.03, max_gap=0.10, min_games=30)
    p = Pick("match", "A", 2.9, 0.48, 0.45, 10)
    assert t.suspicious(p) and not t.ok(p)
    assert "мало истории" in t.reason(p)
    big_gap = Pick("match", "A", 2.9, 0.48, 0.32, 200)
    assert "расходится" in t.reason(big_gap)
    good = Pick("match", "A", 2.2, 0.50, 0.44, 200)
    assert t.reason(good) is None and t.ok(good)


def test_experiments_bet_separately_on_same_odds():
    from bkanalyzer.analysis import Thresholds
    from bkanalyzer.config import Settings

    s = Settings(sim_experiments="Фавориты: prob=0.5; Строгий: prob=0.5 ev=0.04 gap=0.06 games=50")
    cfg = s.sim()
    assert [e.profile for e in cfg.experiments] == ["Фавориты", "Строгий"]
    fav, strict = cfg.experiments
    from dataclasses import replace

    assert fav.thresholds == replace(s.thresholds(), min_prob=0.5)
    assert (strict.thresholds.min_ev, strict.thresholds.max_gap, strict.thresholds.min_games) == (0.04, 0.06, 50)
    assert [c.key for c in fav.all()] == ["Фавориты:fixed", "Фавориты:percent", "Фавориты:kelly"]
    assert isinstance(cfg.thresholds, Thresholds)


def test_experiment_bank_and_report_are_separate(conn):
    from dataclasses import replace

    base = replace(CFG, strategies=("fixed", "percent"), thresholds=Thresholds(0.05))
    exp = replace(base, profile="Фавориты", thresholds=Thresholds(0.05, min_prob=0.5))
    never = replace(base, profile="Никогда", thresholds=Thresholds(0.05, min_prob=0.99))
    cfg = replace(base, experiments=(exp, never))
    assert len(sim.place_bets(conn, analyses(conn), cfg.thresholds, cfg, now=NOW)) == 1
    assert len(sim.place_bets(conn, analyses(conn), exp.thresholds, exp, now=NOW)) == 1  # тот же матч, свой профиль
    assert sim.place_bets(conn, analyses(conn), exp.thresholds, exp, now=NOW) == []
    assert sim.place_bets(conn, analyses(conn), never.thresholds, never, now=NOW) == []
    rows = [r[0] for r in conn.execute("SELECT strategy FROM paper_bets ORDER BY id")]
    assert rows == ["fixed", "percent", "Фавориты:fixed", "Фавориты:percent"]
    assert sim.bank(conn, "prematch", exp.all()[0]) == 950
    assert sim.summary(conn, cfg.all()[0], "prematch")["bets"] == 1
    text = sim.report(conn, cfg)
    assert "Сравнение настроек" in text and "Фавориты: EV ≥ 5%, наша ≥ 50%" in text and "ставок пока нет" in text
    from bkanalyzer import export
    labels = [v[1] for v, _ in export.summary_rows(conn, cfg)]
    assert "Фавориты · фикс. сумма" in labels and not any(l.startswith("Никогда") for l in labels)


def test_parse_experiments_errors_and_empty():
    import pytest

    from bkanalyzer.analysis import Thresholds

    assert sim.parse_experiments("", Thresholds()) == []
    with pytest.raises(ValueError):
        sim.parse_experiments("X: foo=1", Thresholds())


def test_banks_report_and_button(conn):
    from dataclasses import replace

    base = replace(CFG, strategies=("fixed", "percent"), thresholds=Thresholds(0.05))
    exp = replace(base, profile="Фавориты", thresholds=Thresholds(0.05, min_prob=0.5))
    cfg = replace(base, experiments=(exp,))
    sim.place_bets(conn, analyses(conn), cfg.thresholds, cfg, now=NOW)
    text = sim.banks_report(conn, cfg)
    assert "<b>Основной</b>" in text and "Фавориты: ставок пока нет" in text
    assert "Лучшая стратегия: пока нет рассчитанных ставок" in text
    assert "<pre>" in text and "до     0-0      +0     +0" in text and "открыто ставок: 1" in text
    b = Bot(conn, (ME,), Thresholds(0.05), now=lambda: NOW, sim=cfg)
    r = b.handle(ME, "💰 Банки")
    assert r.html and "<pre>" in r.text


def test_leaderboard_orders_by_profit(conn):
    from dataclasses import replace

    cfg = replace(CFG, strategies=("fixed", "percent"), percent=0.2, thresholds=Thresholds(0.05))
    sim.place_bets(conn, analyses(conn), cfg.thresholds, cfg, now=NOW)
    conn.execute("UPDATE paper_bets SET status='won', profit=stake*(price-1)")
    conn.commit()
    lines = sim.leaderboard(conn, cfg)
    assert lines[0].startswith("🥇 Основной · % банка") and lines[1].startswith("🥈 Основной · фикс. сумма")
    assert "Рано судить" in lines[-1]
    text = sim.banks_report(conn, cfg)
    assert "ИТОГО  1-0" in text and "ROI" in text
