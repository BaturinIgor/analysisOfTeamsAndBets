"""Теннис: история tennis-data.co.uk, линия Pinnacle (PinnWire), Bo1, покрытие как «патч»."""

from datetime import datetime, timedelta, timezone

import httpx

from bkanalyzer import analysis, db, sim, tennis
from bkanalyzer.analysis import Thresholds
from bkanalyzer.bot import Bot, ModelCache
from bkanalyzer.config import Settings
from bkanalyzer.games import DOTA, TENNIS
from bkanalyzer.sources.pinnwire import PinnWire, is_singles, money_line

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
ME = 1
HEADER = ["ATP", "Location", "Tournament", "Date", "Series", "Court", "Surface", "Round", "Best of", "Winner", "Loser",
          "WRank", "LRank", "Comment", "PSW", "PSL"]


def table(n=60):
    """Синнер почти всегда обыгрывает Де Минаура; на грунте чаще проигрывает (для признака покрытия)."""
    rows = []
    t0 = NOW - timedelta(days=90)
    for i in range(n):
        d = t0 + timedelta(days=i)
        surface = "Clay" if i % 3 == 0 else "Hard"
        win = i % 5 != 0
        w, l = ("Sinner J.", "De Minaur A.") if win else ("De Minaur A.", "Sinner J.")
        rows.append((1, "Paris" if surface == "Hard" else "Rome", "Paris Masters" if surface == "Hard" else "Rome Open",
                     d.replace(tzinfo=None), "Masters 1000", "Indoor", surface, "R32", 3, w, l, 1, 8, "Completed", 1.3, 3.5))
    rows.append((1, "Paris", "Paris Masters", (NOW - timedelta(days=3)).replace(tzinfo=None), "Masters 1000", "Indoor", "Hard",
                 "R16", 3, "Sinner J.", "Cerundolo J.M.", 1, 20, "Walkover", None, None))
    return rows


def test_parse_rows_and_names():
    rows = tennis.parse_rows("ATP", HEADER, table())
    assert len(rows) == 61
    assert {r[7] for r in rows} | {r[9] for r in rows} == {"Sinner J.", "De Minaur A.", "Cerundolo J.M."}
    assert {r[10] for r in rows} == {0, 1}  # порядок игроков перемешан
    assert all(datetime.fromtimestamp(r[1], timezone.utc).hour == 12 for r in rows)
    index = {tennis.key(n): n for n in ("Sinner J.", "De Minaur A.", "Cerundolo J.M.", "Zhang Zh.")}
    assert tennis.history_name("Jannik Sinner", index) == "Sinner J."
    assert tennis.history_name("Alex de Minaur", index) == "De Minaur A."
    assert tennis.history_name("Juan Manuel Cerúndolo", index) == "Cerundolo J.M."
    assert tennis.history_name("Zhizhen Zhang", index) == "Zhang Zh."
    assert tennis.history_name("Unknown Player", index) == "Unknown Player"


PW_EVENTS = {"sport_id": 2, "events": [
    {"event_id": 111, "league_name": "ATP Paris", "home": "Jannik Sinner", "away": "Alex de Minaur",
     "starts": "2026-11-05T15:00:00Z", "event_type": "prematch", "periods": {"num_0": {"money_line": {"home": 1.9, "away": 2.0}}}},
    {"event_id": 112, "league_name": "ATP Paris Doubles", "home": "A/B", "away": "C/D", "starts": "2026-11-05T15:00:00Z",
     "periods": {"num_0": {"money_line": {"home": 1.9, "away": 1.9}}}},
    {"event_id": 113, "league_name": "ATP Paris", "home": "X Y", "away": "Z W", "starts": "2026-11-05T15:00:00Z", "periods": {}},
]}


class FakePW:
    calls = 0

    def fixtures(self, sport_id=2):
        self.calls += 1
        return PW_EVENTS


def seeded(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    tennis.ensure_schema(conn)
    conn.executemany("INSERT INTO tennis_maps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tennis.parse_rows("ATP", HEADER, table()))
    conn.commit()
    tennis.collect_once(conn, FakePW(), TENNIS, log=lambda *_: None)
    return conn


def test_pinnwire_filters():
    evs = PW_EVENTS["events"]
    assert is_singles(evs[0]) and not is_singles(evs[1])
    assert is_singles({**evs[0], "units": "Sets"}) and not is_singles({**evs[0], "units": "Games"})
    assert money_line(evs[0]) == (1.9, 2.0) and money_line(evs[2]) is None


def test_board_model_bet_and_settlement(tmp_path, monkeypatch):
    conn = seeded(tmp_path)
    monkeypatch.setattr(tennis.time, "time", lambda: NOW.timestamp())
    board = analysis.load_board(conn, now=NOW, game=TENNIS)
    assert len(board) == 1
    bm = board[0]
    assert (bm.team1, bm.team2) == ("Sinner J.", "De Minaur A.") and bm.best_of == 1 and bm.cond == "Hard"
    model, n = ModelCache(game=TENNIS).get(conn)
    assert n == 61
    a = analysis.analyze(bm, model, int(NOW.timestamp()))
    assert a.p_series > 0.6 and [p.market for p in a.picks] == ["match_winner", "match_winner"]
    placed = sim.place_bets(conn, [a], Thresholds(min_ev=0.03, max_gap=0.5, min_games=30), sim.SimSettings(), now=NOW)
    assert placed and placed[0]["mode"] == "ten_prematch" and placed[0]["pick"] == "Sinner J."
    # через 5 дней результата ещё нет (таблицы раз в неделю) — ставка ждёт, а не возвращается
    assert sim.settle(conn, now=NOW + timedelta(days=5)) == []
    # результат: день матча 12:00 UTC, а матч начинался в 15:00 — всё равно находим
    day = int(datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp())
    conn.execute("INSERT INTO tennis_maps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (9, day, "Paris Masters Paris", "ATP", 9, 0, 1, "Sinner J.", 2, "De Minaur A.", 1, "Hard", "Completed"))
    conn.commit()
    done = sim.settle(conn, now=NOW + timedelta(days=6))
    assert done and all(b["status"] == "won" for b in done) and done[0]["closing_price"] == 1.9
    assert "Теннис до" in sim.report_html(conn, sim.SimSettings(strategies=("fixed",)))


def test_retired_match_is_void(tmp_path):
    conn = seeded(tmp_path)
    sim.ensure_schema(conn)
    conn.execute("""INSERT INTO paper_bets (created_at, fixture_id, mode, team1, team2, start_time, market, pick, price, prob,
                    stake, bank_before, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (NOW.isoformat(), "111", "ten_prematch", "Sinner J.", "De Minaur A.", "2026-11-05T15:00:00Z", "match_winner",
                  "Sinner J.", 1.9, 0.6, 50, 1000, "fixed"))
    day = int(datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp())
    conn.execute("INSERT INTO tennis_maps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (9, day, "Paris", "ATP", 9, 0, 1, "Sinner J.", 2, "De Minaur A.", 0, "Hard", "Retired"))
    conn.commit()
    done = sim.settle(conn, now=NOW + timedelta(days=1))
    assert done[0]["status"] == "void" and "Retired" in done[0]["note"]


def test_bot_shows_tennis(tmp_path, monkeypatch):
    conn = seeded(tmp_path)
    bot = Bot(conn, (ME,), Thresholds(0.03, max_gap=0.5), now=lambda: NOW)
    text = bot.handle(ME, "/today").text
    assert "Теннис: линия" in text and "Sinner-Minaur" in text
    m = bot.handle(ME, "/match 1").text
    assert "покрытие Hard" in m and "Линия Pinnacle" in m and "Bo" not in m.split("\n")[1]


def test_sync_reads_years(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    seen = []

    def fetch(url):
        seen.append(url)
        return b"x"

    import bkanalyzer.tennis as t

    orig = t.read_xlsx
    t.read_xlsx = lambda data: (HEADER, table(5))
    try:
        added = tennis.sync(conn, None, 2, now=NOW.timestamp(), log=lambda *_: None, fetch=fetch)
    finally:
        t.read_xlsx = orig
    assert added == 12 and "http://www.tennis-data.co.uk/2026w/2026.xlsx" in seen and len(seen) == 4


def test_settings_and_pinnwire_client():
    assert TENNIS not in Settings(pandascore_token="t").games
    assert Settings(pinnwire_api_key="k").games == (DOTA, TENNIS)
    seen = {}

    def handler(req):
        seen["key"], seen["path"], seen["sport"] = req.headers.get("x-api-key"), req.url.path, req.url.params.get("sport_id")
        return httpx.Response(200, json=PW_EVENTS)

    pw = PinnWire(httpx.Client(transport=httpx.MockTransport(handler)), "k")
    assert len(pw.fixtures()["events"]) == 3
    assert seen == {"key": "k", "path": "/kit/v1/prematch/fixtures", "sport": "2"}
