import json

import httpx
import pytest

from bkanalyzer import db
from bkanalyzer.cli import check_sources, collect_odds_once
from bkanalyzer.config import Settings, load_settings
from bkanalyzer.http import SourceError, make_client, request_json
from bkanalyzer.sources.liquipedia import Liquipedia
from bkanalyzer.sources.oddspapi import OddsPapi, find_bookmakers, flatten_odds
from bkanalyzer.bookmakers.betboom import looks_like_odds

ODDS = {
    "bookmakerOdds": {
        "betboom": {"markets": {"101": {"outcomes": {"1": {"players": {"0": {"price": 1.85}}}, "2": {"players": {"0": {"price": 1.95}}}}}}}
    }
}


def router(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "opendota.com/api/proMatches" in url:
        return httpx.Response(200, json=[{"match_id": 1, "radiant_name": "Team A", "dire_name": "Team B", "league_name": "L"}])
    if "opendota.com/api/heroStats" in url:
        return httpx.Response(200, json=[{"id": 1, "8_pick": 10, "8_win": 5}])
    if "opendota.com/api/live" in url:
        return httpx.Response(200, json=[])
    if "stratz.com" in url:
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.headers["User-Agent"] == "STRATZ_API"
        return httpx.Response(200, json={"data": {"constants": {"gameVersions": [{"id": 1, "name": "7.40"}]}}})
    if "liquipedia.net" in url:
        assert "contact" in request.headers["User-Agent"]
        return httpx.Response(200, json={"query": {"general": {"sitename": "Liquipedia"}}})
    if "oddspapi" in url:
        assert request.url.params["apiKey"] == "key"
        if url.split("?")[0].endswith("/sports"):
            return httpx.Response(200, json=[{"sportId": 16, "sportName": "Dota 2"}])
        if url.split("?")[0].endswith("/bookmakers"):
            return httpx.Response(200, json=[{"slug": "betboom", "name": "BetBoom"}, {"slug": "fonbet", "name": "Fonbet"}])
        if url.split("?")[0].endswith("/fixtures"):
            assert request.url.params["sportId"] == "16"
            return httpx.Response(200, json=[{"fixtureId": "f1", "participant1Name": "A", "participant2Name": "B", "startTime": "2026-11-05T12:00:00Z"}])
        if url.split("?")[0].endswith("/odds"):
            return httpx.Response(200, json=ODDS)
    return httpx.Response(404, text="not found")


def settings(**kw):
    base = dict(stratz_token="tok", oddspapi_api_key="key", contact="contact")
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def client():
    return make_client(settings().user_agent, transport=httpx.MockTransport(router))


def test_check_sources_all_ok(client, capsys):
    assert check_sources(settings(), client) == 0
    out = capsys.readouterr().out
    assert "6 из 6" in out
    assert "betboom: есть" in out and "pari: НЕТ" in out


def test_check_sources_reports_missing_token(client, capsys):
    assert check_sources(settings(stratz_token=""), client) == 1
    assert "STRATZ_TOKEN не задан" in capsys.readouterr().out


def test_retry_on_429_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}) if calls["n"] < 3 else httpx.Response(200, json={"ok": 1})

    c = make_client("ua", transport=httpx.MockTransport(handler))
    assert request_json(c, "GET", "https://x.test/", sleep=lambda _: None) == {"ok": 1}
    assert calls["n"] == 3


def test_http_error_raises():
    c = make_client("ua", transport=httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden")))
    with pytest.raises(SourceError, match="403"):
        request_json(c, "GET", "https://x.test/", sleep=lambda _: None)


def test_flatten_odds():
    assert flatten_odds(ODDS) == [
        {"bookmaker": "betboom", "market": "101", "outcome": "1", "price": 1.85},
        {"bookmaker": "betboom", "market": "101", "outcome": "2", "price": 1.95},
    ]


def test_find_bookmakers_case_insensitive():
    found = find_bookmakers([{"slug": "BetBoom"}, {"name": "PARI"}])
    assert found["betboom"] and found["pari"] and found["fonbet"] is None


def test_historical_odds_limit(client):
    with pytest.raises(ValueError):
        OddsPapi(client, "key", "https://api.oddspapi.io/v4").historical_odds("f1", ["a", "b", "c", "d"])


def test_collect_odds_saves_snapshots(client, tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    assert collect_odds_once(settings(), conn, client) == 2
    rows = conn.execute("SELECT bookmaker, fixture_id, participant1, outcome, price FROM odds_snapshots ORDER BY outcome").fetchall()
    assert rows == [("betboom", "f1", "A", "1", 1.85), ("betboom", "f1", "A", "2", 1.95)]


def test_liquipedia_rate_limit():
    t = {"now": 0.0}
    slept = []
    c = make_client("ua contact", transport=httpx.MockTransport(router))
    lp = Liquipedia(c, min_interval=2.0, sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)), clock=lambda: t["now"])
    lp.siteinfo()
    lp.siteinfo()
    assert slept == [2.0]


def test_load_settings_from_env_file(tmp_path, monkeypatch):
    for k in ("STRATZ_TOKEN", "TELEGRAM_ALLOWED_IDS"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("STRATZ_TOKEN=abc\nTELEGRAM_ALLOWED_IDS=1, 2\n")
    s = load_settings(env)
    assert s.stratz_token == "abc" and s.telegram_allowed_ids == (1, 2)


def test_betboom_filter():
    assert looks_like_odds("https://x/api/events", "{}")
    assert not looks_like_odds("https://x/api/user/profile", json.dumps({"name": "x"}))


def test_oddspapi_sample(client):
    from bkanalyzer.cli import oddspapi_sample

    out = oddspapi_sample(settings(), client)
    assert "Матч f1: A — B" in out
    assert "betboom" in out and "рынок 101" in out and "1=1.85" in out


def test_oddspapi_sample_prints_raw_when_unparsed():
    from bkanalyzer.cli import oddspapi_sample

    def handler(request):
        path = request.url.path
        if path.endswith("/bookmakers"):
            return httpx.Response(200, json=[{"slug": "fonbet"}])
        if path.endswith("/fixtures"):
            return httpx.Response(200, json=[{"fixtureId": "old", "startTime": "2000-01-01T00:00:00Z"},
                                             {"fixtureId": "new", "participant1Name": "A", "participant2Name": "B", "startTime": "2999-01-01T00:00:00Z"}])
        if path.endswith("/odds"):
            assert request.url.params["fixtureId"] == "new"
            return httpx.Response(200, json={"weird": {"x": 1}})
        return httpx.Response(404)

    c = make_client("ua", transport=httpx.MockTransport(handler))
    out = oddspapi_sample(settings(), c)
    assert "ещё не начались: 1" in out and "Матч new" in out and "Матч old" not in out
    assert "ключи ответа: ['weird']" in out


def test_keep_awake_is_noop_outside_windows(monkeypatch):
    from bkanalyzer import cli

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli.keep_awake() is False
