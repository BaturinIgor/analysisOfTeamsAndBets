from datetime import datetime, timedelta, timezone

from bkanalyzer import analysis, cs, db, sim
from bkanalyzer.analysis import Thresholds
from bkanalyzer.bot import Bot, ModelCache
from bkanalyzer.cli import collect_oddiwire_once
from bkanalyzer.games import CS2

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
ME = 1


def ps_match(mid, begin, a, b, sa, sb, bo=3, **kw):
    return {"id": mid, "status": "finished", "begin_at": begin, "number_of_games": bo, "forfeit": False,
            "opponents": [{"opponent": {"id": a[0], "name": a[1]}}, {"opponent": {"id": b[0], "name": b[1]}}],
            "results": [{"team_id": a[0], "score": sa}, {"team_id": b[0], "score": sb}],
            "league": {"name": "ESL"}, "serie": {"full_name": "Pro League 2026"}, "tournament": {"tier": "a"}, **kw}


VIT, FAZE = (10, "Team Vitality"), (20, "FaZe Clan")


def test_series_maps_order_and_skips():
    rows = cs.series_maps(ps_match(7, "2026-11-01T10:00:00Z", VIT, FAZE, 1, 2))
    assert len(rows) == 3 and [r[-1] for r in rows] == [0, 1, 0]  # последнюю карту выиграл победитель серии (FaZe)
    assert rows[0][0] == 71 and rows[0][4] == 7 and rows[0][5] == 1  # Bo3 → series_type 1 как в OpenDota
    assert cs.series_maps(ps_match(8, "2026-11-01T10:00:00Z", VIT, FAZE, 1, 0, forfeit=True)) == []
    assert cs.series_maps(ps_match(9, "2026-11-01T10:00:00Z", VIT, FAZE, 0, 0)) == []


class FakePS:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    def cs_past(self, page=1, per_page=100):
        self.calls += 1
        return self.pages[page - 1] if page <= len(self.pages) else []


def history_pages(n=40):
    t = NOW - timedelta(days=60)
    items = []
    for i in range(n):  # Vitality выигрывает почти все серии
        win = i % 5 != 0
        items.append(ps_match(1000 + i, (t + timedelta(days=i)).isoformat().replace("+00:00", "Z"), VIT, FAZE,
                              2 if win else 0, 0 if win else 2))
    items.reverse()  # от новых к старым
    return [items[:20], items[20:]]


def test_sync_stops_at_known(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    ps = FakePS(history_pages())
    added = cs.sync(conn, ps, 365, now=NOW.timestamp(), sleep=lambda *_: None)
    assert added == 80 and ps.calls == 3
    ps2 = FakePS(history_pages())
    assert cs.sync(conn, ps2, 365, now=NOW.timestamp(), sleep=lambda *_: None) == 0 and ps2.calls == 1


CS_EVENT = {"event_id": "c1", "sport": "CS2", "event_type": "prematch", "status": "NOT_STARTED", "home": "Vitality",
            "away": "FaZe", "starts": "2026-11-05T15:00:00Z", "league": "ESL Pro League 2026", "markets": [
                {"canonical": "match_winner", "market": "Match Winner", "status": "OPEN", "selections": [
                    {"name": "Vitality", "price": 1.6, "probability": 0.6, "status": "TRADING"},
                    {"name": "FaZe", "price": 2.3, "probability": 0.4, "status": "TRADING"}]},
                {"market": "Map 1 Winner - Twoway", "status": "OPEN", "selections": [
                    {"name": "Vitality", "price": 1.7, "probability": 0.57, "status": "TRADING"},
                    {"name": "FaZe", "price": 2.1, "probability": 0.43, "status": "TRADING"}]}]}


class FakeOW:
    def __init__(self):
        self.calls = []

    def call(self, name, args=None):
        self.calls.append((name, args))
        return {"events": [CS_EVENT]} if name == "get_prematch_odds" else {"events": []}


def seeded(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    cs.sync(conn, FakePS(history_pages()), 365, now=NOW.timestamp(), sleep=lambda *_: None)
    ow = FakeOW()
    collect_oddiwire_once(conn, ow, log=lambda *_: None, game=CS2)
    assert ("get_prematch_odds", {"sport": "CS2", "tier": 1, "limit": 100}) in ow.calls
    assert not any(n == "get_fixture" for n, _ in ow.calls)  # карты CS2 не запрашиваем
    # строки CS2 лежат отдельно от Dota
    db.upsert_matches(conn, [], source="oddiwire")
    return conn


def test_cs_board_analysis_and_bets(tmp_path):
    conn = seeded(tmp_path)
    assert analysis.load_board(conn, now=NOW) == []  # в линии Dota пусто
    board = analysis.load_board(conn, now=NOW, game=CS2)
    assert len(board) == 1 and board[0].game == "cs2" and board[0].fmt_hint == 3
    model, n = ModelCache(game=CS2).get(conn)
    assert n == 80
    a = analysis.analyze(board[0], model, int(NOW.timestamp()))
    assert a.p_series > 0.6 and {p.market for p in a.picks} == {"match_winner"}  # рынок карты пропущен
    t = Thresholds(min_ev=0.03, max_gap=0.5, min_games=30)
    placed = sim.place_bets(conn, [a], t, sim.SimSettings(), now=NOW)
    assert placed and placed[0]["mode"] == "cs_prematch" and placed[0]["pick"] == "Vitality"
    # результат серии приходит из PandaScore → ставка рассчитывается по cs_maps
    start = int(datetime(2026, 11, 5, 15, 10, tzinfo=timezone.utc).timestamp())
    conn.executemany("INSERT INTO cs_maps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [(99991, start, "ESL", "a", 9999, 1, 10, "Team Vitality", 20, "FaZe Clan", 1),
                      (99992, start + 3600, "ESL", "a", 9999, 1, 10, "Team Vitality", 20, "FaZe Clan", 1)])
    conn.commit()
    done = sim.settle(conn, now=NOW + timedelta(hours=6))
    assert done and all(b["status"] == "won" for b in done)
    report = sim.report(conn, sim.SimSettings(strategies=("fixed",)))
    assert "CS2 до матча: ставок 1" in report and "CS2 в эфире" not in report


def test_bot_shows_cs_section(tmp_path):
    conn = seeded(tmp_path)
    bot = Bot(conn, (ME,), Thresholds(0.03, max_gap=0.5), now=lambda: NOW)
    text = bot.handle(ME, "/today").text
    assert "Матчи CS2 в линии:" in text and "Vitality — FaZe" in text
    m = bot.handle(ME, "/match 1").text
    assert "(CS2)" in m and "только победу в матче" in m and "героев" not in m
    assert "CS2: Team Vitality" in bot.handle(ME, "/team Vitality")
