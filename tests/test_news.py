import httpx

from bkanalyzer import db, details, news
from bkanalyzer.bot import Bot

from test_bot import ME, NOW, seed

NOW_TS = int(NOW.timestamp())

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>Team Spirit will play with a stand-in at the Major - HLTV</title><link>https://example.com/a</link>
<pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate><source url="https://hltv.org">HLTV</source></item>
<item><title>Spirit win group stage - Cybersport.ru</title><link>https://example.com/b</link>
<pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate><source url="https://cybersport.ru">Cybersport.ru</source></item>
<item><title>Old news about Spirit - X</title><link>https://example.com/c</link>
<pubDate>Mon, 01 Sep 2026 10:00:00 GMT</pubDate><source url="https://x">X</source></item>
</channel></rss>"""


def test_parse_rss_and_risk_words():
    items = news.parse_rss(RSS, NOW_TS)
    assert [i.title for i in items] == ["Team Spirit will play with a stand-in at the Major", "Spirit win group stage"]
    assert items[0].risks == ["стендин"] and items[1].risks == []
    assert "виза" in news.risk_kinds("Игрок не получил визу на турнир")
    assert "болезнь" in news.risk_kinds("Капитан заболел")


def add_lineups(conn, team_id=1, standin_last=True):
    details.ensure_schema(conn)
    news.ensure_schema(conn)
    maps = conn.execute("SELECT match_id, radiant_team_id FROM pro_maps WHERE radiant_team_id=? OR dire_team_id=? ORDER BY start_time",
                        (team_id, team_id)).fetchall()
    rows = []
    for i, (mid, rad) in enumerate(maps):
        squad = [101, 102, 103, 104, 105]
        if standin_last and i == len(maps) - 1:
            squad[4] = 999
        rows += [(mid, acc, int(rad == team_id), 1) for acc in squad]
    conn.executemany("INSERT INTO map_players VALUES (?,?,?,?)", rows)
    conn.executemany("INSERT INTO pro_players VALUES (?,?,?)", [(105, "Yatoro", 1), (999, "Standin", None)])
    conn.commit()


def test_roster_risk_finds_standin(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    seed(conn)
    add_lineups(conn)
    last = conn.execute("SELECT MAX(start_time) FROM pro_maps").fetchone()[0]
    r = news.roster_risk(conn, 1, last + 3600)
    assert r and r.newcomers == ["Standin"] and r.missing == ["Yatoro"]
    assert "Standin вместо Yatoro" in r.text("Spirit")
    # драфт с основным составом — риска нет
    assert not news.roster_risk(conn, 1, last + 3600, frozenset({101, 102, 103, 104, 105}))
    # последняя карта давно — про текущий состав не судим
    assert news.roster_risk(conn, 1, last + 10 * 86400) is None


def test_news_command_uses_cache(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    seed(conn)
    calls = []

    def handler(req):
        calls.append(req.url.params["hl"])
        return httpx.Response(200, text=RSS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    b = Bot(conn, (ME,), now=lambda: NOW, news_client=client)
    text = b.handle(ME, "/news Spirit")
    assert "⚠️ стендин: Team Spirit will play with a stand-in" in text and "https://example.com/a" in text
    assert "Tundra" in text and calls == ["ru", "en"] * 2  # матч найден — новости обеих команд
    b.handle(ME, "/news Spirit")
    assert len(calls) == 4  # второй раз — из кэша
    assert "новости (стендин)" in b.handle(ME, "/match Spirit")
