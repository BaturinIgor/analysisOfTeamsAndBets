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
