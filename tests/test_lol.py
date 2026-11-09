"""League of Legends: та же схема, что у CS2 — история серий PandaScore, линия OddiWire (sport LOL)."""

from datetime import datetime, timedelta, timezone

import httpx

from bkanalyzer import analysis, cs, db, sim
from bkanalyzer.analysis import Thresholds
from bkanalyzer.bot import Bot, ModelCache
from bkanalyzer.cli import collect_oddiwire_once, live_expected
from bkanalyzer.config import Settings
from bkanalyzer.games import CS2, DOTA, LOL, by_mode
from bkanalyzer.sources.pandascore import PandaScore

from test_cs import NOW, ps_match

ME = 1
T1, GEN = (30, "T1"), (40, "Gen.G")


class FakeLolPS:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    def lol_past(self, page=1, per_page=100):
        self.calls += 1
        return self.pages[page - 1] if page <= len(self.pages) else []

    def cs_past(self, page=1, per_page=100):  # чужую игру не трогаем
        raise AssertionError("LoL sync must not read CS2")


def lol_pages(n=40):
    t = NOW - timedelta(days=60)
    items = []
    for i in range(n):  # T1 выигрывает почти все серии
        win = i % 5 != 0
        items.append(ps_match(5000 + i, (t + timedelta(days=i)).isoformat().replace("+00:00", "Z"), T1, GEN,
                              2 if win else 0, 0 if win else 2))
    items.reverse()
    return [items[:20], items[20:]]


LOL_EVENT = {"event_id": "l1", "sport": "LOL", "event_type": "prematch", "status": "NOT_STARTED", "home": "T1",
             "away": "Gen.G", "starts": "2026-11-05T15:00:00Z", "league": "LCK", "markets": [
                 {"canonical": "match_winner", "market": "Match Winner", "status": "OPEN", "selections": [
                     {"name": "T1", "price": 1.6, "probability": 0.6, "status": "TRADING"},
                     {"name": "Gen.G", "price": 2.3, "probability": 0.4, "status": "TRADING"}]}]}


class FakeOW:
    def __init__(self):
        self.calls = []

    def call(self, name, args=None):
        self.calls.append((name, args))
        return {"events": [LOL_EVENT]} if name == "get_prematch_odds" else {"events": []}


def seeded(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    ps = FakeLolPS(lol_pages())
    assert cs.sync(conn, ps, 365, now=NOW.timestamp(), sleep=lambda *_: None, log=lambda *_: None, game=LOL) == 80
    ow = FakeOW()
    collect_oddiwire_once(conn, ow, log=lambda *_: None, game=LOL)
    assert ("get_prematch_odds", {"sport": "LOL", "tier": 1, "limit": 100}) in ow.calls
    return conn


def test_lol_history_is_separate(tmp_path):
    conn = seeded(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM lol_maps").fetchone()[0] == 80
    cs.ensure_schema(conn, CS2)
    assert conn.execute("SELECT COUNT(*) FROM cs_maps").fetchone()[0] == 0
    # повторная загрузка останавливается на уже загруженном
    ps2 = FakeLolPS(lol_pages())
    assert cs.sync(conn, ps2, 365, now=NOW.timestamp(), sleep=lambda *_: None, log=lambda *_: None, game=LOL) == 0
    assert ps2.calls == 1


def test_lol_board_bets_and_settlement(tmp_path):
    conn = seeded(tmp_path)
    assert analysis.load_board(conn, now=NOW, game=CS2) == []
    board = analysis.load_board(conn, now=NOW, game=LOL)
    assert len(board) == 1 and board[0].game == "lol"
    model, n = ModelCache(game=LOL).get(conn)
    assert n == 80
    a = analysis.analyze(board[0], model, int(NOW.timestamp()))
    assert a.p_series > 0.6
    placed = sim.place_bets(conn, [a], Thresholds(min_ev=0.03, max_gap=0.5, min_games=30), sim.SimSettings(), now=NOW)
    assert placed and placed[0]["mode"] == "lol_prematch" and placed[0]["pick"] == "T1"
    assert by_mode("lol_prematch") is LOL
    start = int(datetime(2026, 11, 5, 15, 10, tzinfo=timezone.utc).timestamp())
    conn.executemany("INSERT INTO lol_maps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [(77771, start, "LCK", "s", 7777, 1, 30, "T1", 40, "Gen.G", 1),
                      (77772, start + 3600, "LCK", "s", 7777, 1, 30, "T1", 40, "Gen.G", 1)])
    conn.commit()
    done = sim.settle(conn, now=NOW + timedelta(hours=6))
    assert done and all(b["status"] == "won" for b in done)
    assert done[0]["closing_price"] == 1.6  # закрытие берётся из линии LoL
    report = sim.report(conn, sim.SimSettings(strategies=("fixed",)))
    assert "LoL до матча: ставок 1" in report and "CS2" not in report


def test_bot_shows_lol(tmp_path):
    conn = seeded(tmp_path)
    bot = Bot(conn, (ME,), Thresholds(0.03, max_gap=0.5), now=lambda: NOW)
    text = bot.handle(ME, "/today").text
    assert "LoL: линия" in text and "T1-Gen.G" in text
    m = bot.handle(ME, "/match 1").text
    assert "(LoL)" in m and "только победу в матче" in m
    assert "LoL: T1" in bot.handle(ME, "/team T1")


def test_live_list_only_when_match_is_near(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    ow = FakeOW()
    assert not live_expected(conn, LOL, now=NOW.timestamp())
    collect_oddiwire_once(conn, ow, log=lambda *_: None, game=LOL, live=False)
    assert [n for n, _ in ow.calls] == ["get_prematch_odds"]
    start = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc).timestamp()
    assert not live_expected(conn, LOL, now=start - 3 * 3600)    # до матча ещё далеко
    assert live_expected(conn, LOL, now=start - 600)              # скоро начнётся
    assert live_expected(conn, LOL, now=start + 2 * 3600)         # идёт
    assert not live_expected(conn, LOL, now=start + 8 * 3600)     # давно закончился
    assert not live_expected(conn, CS2, now=start)                # у CS2 своих матчей нет


def test_games_setting():
    assert Settings().games == (DOTA,)
    assert Settings(pandascore_token="t").games == (DOTA, CS2, LOL)
    assert Settings(pandascore_token="t", games_filter=("dota2", "lol")).games == (DOTA, LOL)


def test_pandascore_lol_endpoint():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json=[])

    PandaScore(httpx.Client(transport=httpx.MockTransport(handler)), "t").lol_past()
    assert seen["path"] == "/lol/matches/past"
