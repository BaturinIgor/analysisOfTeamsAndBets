import pytest

from bkanalyzer import aliases, analysis, db
from bkanalyzer.bot import Bot, ModelCache

from test_bot import ME, NOW, seed


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "bk.sqlite3")
    seed(c)
    # в линии «Spirit» назван иначе
    c.execute("UPDATE bookmaker_matches SET team1='TS.Ultra' WHERE match_id='e1'")
    c.execute("UPDATE odds_snapshots SET outcome='TS.Ultra' WHERE fixture_id='e1' AND outcome='Spirit'")
    c.commit()
    yield c
    analysis.ALIASES.clear()


def test_alias_links_team_names(conn):
    model, _ = ModelCache().get(conn)
    missing = dict(aliases.unmatched(conn, model, now=NOW))
    assert "TS.Ultra" in missing
    with pytest.raises(ValueError, match="нет в истории"):
        aliases.add(conn, "TS.Ultra", "Team Spirrit")
    assert aliases.add(conn, "TS.Ultra", "Team Spirit") == "Team Spirit"
    assert aliases.listing(conn) == [("TS.Ultra", "Team Spirit")]
    assert analysis.match_team(model, "TS.Ultra")[1].name == "Team Spirit"
    assert "TS.Ultra" not in dict(aliases.unmatched(conn, model, now=NOW))
    text = Bot(conn, (ME,), now=lambda: NOW).handle(ME, "/match ultra")
    assert "Наша вероятность" in text
    assert aliases.remove(conn, "TS.Ultra") and analysis.ALIASES == {}


def test_suggest_close_names(conn):
    names = aliases.history_names(conn)
    assert aliases.suggest("Tundra Esport", names)[0] == "Tundra Esports"
    assert aliases.suggest("Spirrit", names) == ["Team Spirit"]
