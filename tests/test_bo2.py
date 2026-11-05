import pytest

from bkanalyzer import analysis, db, sim
from bkanalyzer.analysis import Thresholds

from test_bot import ME, NOW, seed
from test_sim import add_series, analyses


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "bk.sqlite3")
    seed(c)
    ts = NOW.isoformat(timespec="seconds")
    # Bo2: у матча Spirit — Tundra есть исход «ничья»
    db.save_feed_rows(c, [
        {"bookmaker": "oddin", "fixture_id": "e1", "market": "match_winner", "outcome": "Spirit", "price": 2.6, "phase": "prematch", "fair_prob": 0.6},
        {"bookmaker": "oddin", "fixture_id": "e1", "market": "match_winner", "outcome": "Draw", "price": 2.5, "phase": "prematch", "fair_prob": 0.3},
        {"bookmaker": "oddin", "fixture_id": "e1", "market": "match_winner", "outcome": "Tundra", "price": 8.0, "phase": "prematch", "fair_prob": 0.1},
    ], source="oddiwire", taken_at=ts)
    return c


def test_bo2_probabilities_and_draw_pick(conn):
    a = next(x for x in analyses(conn) if x.match.fixture_id == "e1")
    assert a.match.best_of == 2 and a.match.draw_outcome == "Draw"
    p = a.p_map
    assert a.p_series == pytest.approx(p * p)
    assert a.p_draw == pytest.approx(2 * p * (1 - p))
    assert a.p_series2 == pytest.approx((1 - p) ** 2)
    assert a.p_series + a.p_draw + a.p_series2 == pytest.approx(1)
    assert {pk.team for pk in a.picks if pk.market == "match_winner"} == {"Spirit", "Draw", "Tundra"}
    assert analysis.draw_from_score(0.7, 1, 0) == pytest.approx(0.3)


def test_bo2_settlement(conn):
    start = int(NOW.timestamp()) + 3600
    add_series(conn, start, ["Team Spirit", "Tundra Esports"], series_id=950, series_type=3)
    maps = sim.find_series(conn, "Spirit", "Tundra", "2026-11-04T22:00:00Z", "2026-11-04T21:00:00Z")
    draw = {"pick": "Draw", "market": "match_winner"}
    team = {"pick": "Spirit", "market": "match_winner"}
    assert sim.settle_result(draw, maps)[0] == "won"
    assert sim.settle_result({**team, "draw_offered": True}, maps)[0] == "lost"  # 1X2: ничья — проигрыш ставки на команду
    assert sim.settle_result(team, maps)[0] == "void"  # двухисходный рынок: ничья — возврат


def test_format_hint_from_history(conn):
    conn.execute("UPDATE pro_maps SET league_name='Big League', series_id=match_id, series_type=0")  # Bo1
    conn.commit()
    hints = analysis.format_hints(conn, int(NOW.timestamp()), days=60)
    assert analysis.tournament_format(hints, "Big League") == 1
    assert analysis.tournament_format(hints, "Unknown Cup") is None
