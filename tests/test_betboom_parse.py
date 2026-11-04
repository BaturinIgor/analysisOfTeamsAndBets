from test_betboom import field_double, field_len, field_varint

from bkanalyzer import db
from bkanalyzer.bookmakers.betboom_parse import parse_frame, snapshot_rows, winner_table


def s(num, text):
    return field_len(num, text.encode())


def team(slot, name):
    return field_len(slot, field_varint(1, 100 + slot) + s(3, name))


def match_msg():
    return (
        field_varint(1, 6413974)
        + s(5, "od:match:3351242")
        + field_varint(10, 39468)
        + s(13, "2026-11-04T19:05:00.000Z")
        + field_len(16, team(1, "GamerLegion") + team(3, "HULIGANI"))
        + s(17, "BO3")
        + field_len(20, s(2, "1:0"))
    )


def outcome_msg(uid, short, price, market="Исход матча", period="Матч", line=None):
    body = s(1, uid) + field_varint(2, 6413974) + s(5, short) + s(6, short) + field_double(10, price) + field_varint(13, 8489) + s(14, market) + s(18, period)
    if line is not None:
        body += field_double(9, line)
    return body


def frame():
    outcomes = (
        field_len(2, outcome_msg("od:match:3351242/1/1", "П1", 1.05))
        + field_len(2, outcome_msg("od:match:3351242/1/2", "П2", 8.5))
        + field_len(2, outcome_msg("od:match:3351242/34/5?map=2&threshold=47.5", "Больше", 1.65, "Тотал убийств на карте", "Карта 2", 47.5))
    )
    return field_len(17, field_len(5, field_len(8, field_len(1, match_msg()) + outcomes)))


def test_parse_frame():
    matches, outcomes = parse_frame(frame())
    assert matches == [{
        "match_id": 6413974, "feed_id": "od:match:3351242", "tournament_id": 39468, "start_time": "2026-11-04T19:05:00.000Z",
        "team1": "GamerLegion", "team2": "HULIGANI", "format": "BO3", "score": "1:0",
    }]
    assert len(outcomes) == 3
    table = winner_table(matches, outcomes)
    assert (table[0]["p1"], table[0]["p2"]) == (1.05, 8.5)
    rows = snapshot_rows(outcomes)
    assert {"bookmaker": "betboom", "fixture_id": "6413974", "market": "Тотал убийств на карте | Карта 2", "outcome": "Больше (47.5)", "price": 1.65} in rows


def test_parse_garbage_returns_empty():
    assert parse_frame(b"\xff\xff\xff") == ([], [])


def test_save_price_changes_dedup(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    matches, outcomes = parse_frame(frame())
    db.upsert_matches(conn, matches, source="betboom")
    rows = snapshot_rows(outcomes)
    assert db.save_price_changes(conn, rows, source="betboom") == 3
    assert db.save_price_changes(conn, rows, source="betboom") == 0
    rows[0] = {**rows[0], "price": 1.07}
    assert db.save_price_changes(conn, rows, source="betboom") == 1
    assert conn.execute("SELECT team1, team2, score FROM bookmaker_matches").fetchone() == ("GamerLegion", "HULIGANI", "1:0")
