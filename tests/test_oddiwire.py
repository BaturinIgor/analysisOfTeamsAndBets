import json

import httpx
import pytest

from bkanalyzer.cli import oddiwire_sample
from bkanalyzer.config import Settings
from bkanalyzer.http import SourceError, make_client
from bkanalyzer.sources.oddiwire import OddiWire, find_dota_sport, tool_payload

TOOLS = [
    {"name": "list_sports", "inputSchema": {"properties": {}}},
    {"name": "get_coverage", "inputSchema": {"properties": {}}},
    {"name": "get_prematch_odds", "inputSchema": {"properties": {"sport": {"type": "string"}, "limit": {}}}},
    {"name": "get_my_plan", "inputSchema": {"properties": {}}},
    {"name": "get_live_odds", "inputSchema": {"properties": {"sport": {"type": "string"}, "limit": {}}}},
]


def make_server(sse: bool = False):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((body.get("method"), body.get("params"), dict(request.headers), dict(request.url.params)))
        if "id" not in body:
            return httpx.Response(202)
        m = body["method"]
        if m == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "oddiwire"}}
        elif m == "tools/list":
            result = {"tools": TOOLS}
        else:
            name, args = body["params"]["name"], body["params"]["arguments"]
            data = {
                "list_sports": {"sports": ["CS2", "DOTA2", "DOTA2_DUELS"], "note": "ids"},
                "get_my_plan": {"plan": "demo"},
                "get_coverage": {"args": args, "DOTA2": {"prematch": 7}},
                "get_live_odds": {"fixtures": [], "args": args},
                "get_prematch_odds": {"fixtures": [{"home": "Team Spirit", "away": "Tundra", "odds": [1.7, 2.1]}]},
            }[name]
            result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        headers = {"Mcp-Session-Id": "s1"}
        if sse:
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(msg)}\n\n", headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, json=msg, headers=headers)

    return handler, calls


@pytest.mark.parametrize("sse", [False, True])
def test_call_roundtrip(sse):
    handler, calls = make_server(sse)
    ow = OddiWire(make_client("ua", transport=httpx.MockTransport(handler)))
    assert ow.call("get_my_plan") == {"plan": "demo"}
    methods = [c[0] for c in calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    assert calls[-1][2]["mcp-session-id"] == "s1"
    assert calls[0][3]["key"] == "demo"


def test_api_key_sent():
    handler, calls = make_server()
    OddiWire(make_client("ua", transport=httpx.MockTransport(handler)), api_key="k").tools()
    assert calls[0][2]["authorization"] == "Bearer k"
    assert calls[0][3]["key"] == "k"


def test_errors():
    ow = OddiWire(make_client("ua", transport=httpx.MockTransport(lambda r: httpx.Response(429))))
    with pytest.raises(SourceError, match="лимит"):
        ow.tools()
    err = lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "bad"}})
    with pytest.raises(SourceError, match="bad"):
        OddiWire(make_client("ua", transport=httpx.MockTransport(err))).tools()


def test_payload_and_sport():
    assert tool_payload({"structuredContent": {"a": 1}, "content": []}) == {"a": 1}
    assert tool_payload({"content": [{"type": "text", "text": "plain"}]}) == "plain"
    assert find_dota_sport({"sports": [{"id": "DOTA2", "name": "Dota 2"}]}) == "DOTA2"
    assert find_dota_sport(["CS2", "DOTA_2"]) == "DOTA_2"
    assert find_dota_sport("Valid ids: SOCCER, CS2, DOTA2") == "DOTA2"
    assert find_dota_sport({"sports": []}) is None
    real = {"sports": ["CS2", "DOTA2", "DOTA2_DUELS", "LOL"], "note": "These are the sport ids this wire understands."}
    assert find_dota_sport(real) == "DOTA2"
    assert find_dota_sport({"sports": ["DOTA2_DUELS"]}) is None


def test_sample(tmp_path):
    handler, calls = make_server()
    out = tmp_path / "s.json"
    text = oddiwire_sample(Settings(), make_client("ua", transport=httpx.MockTransport(handler)), out)
    assert "Dota 2 в list_sports: DOTA2" in text
    assert "Team Spirit" in text
    dump = json.loads(out.read_text())
    assert dump["get_coverage"]["args"] == {}
    assert dump["get_live_odds"]["args"] == {"sport": "DOTA2", "limit": 10}
    assert set(dump) == {"tools", "get_my_plan", "list_sports", "get_coverage", "get_prematch_odds", "get_live_odds"}


from bkanalyzer import db
from bkanalyzer.cli import collect_oddiwire_once
from bkanalyzer.sources import oddiwire as owm


def sel(name, price, prob, pid):
    return {"name": name, "status": "TRADING", "price": price, "probability": prob, "provider_id": pid}


EVENT = {
    "event_id": "a749",
    "sport": "DOTA2",
    "league": "Asgard Championship Season 2",
    "event_type": "live",
    "status": "LIVE",
    "name": "Team Allied vs Team Lynx",
    "home": "Team Allied",
    "away": "Team Lynx",
    "starts": "2026-11-04T20:01:24.000Z",
    "score": {"home": "0", "away": "0"},
    "markets": [
        {"market": "Match Winner - Twoway", "canonical": "match_winner", "tier": 1, "status": "OPEN", "selections": [
            sel("Team Allied", 2.2, 0.404, "od:match:3386795/1/1?variant=way:two"),
            sel("Team Lynx", 1.58, 0.596, "od:match:3386795/1/2?variant=way:two")]},
        {"market": "Match Handicap", "canonical": "main_handicap", "tier": 1, "status": "OPEN", "selections": [
            sel("-1.5", 1.32, 0.718, "od:match:3386795/2/1?handicap=-1.5")]},
    ],
}
MAPS = [
    {"market": "Map 1 Winner - Twoway", "canonical": None, "tier": 3, "status": "OPEN", "selections": [
        sel("Team Allied", 1.68, 0.555, "x"), sel("Team Lynx", 2.05, 0.445, "y")]},
    {"market": "Map 2 Winner - Twoway", "canonical": None, "tier": 3, "status": "SUSPENDED", "selections": [
        sel("Team Allied", 2.4, 0.363, "x")]},
]


def test_event_rows():
    rows = owm.event_rows(EVENT)
    assert [(r["market"], r["outcome"], r["price"]) for r in rows] == [("match_winner", "Team Allied", 2.2), ("match_winner", "Team Lynx", 1.58)]
    assert rows[0]["phase"] == "live" and rows[0]["fair_prob"] == 0.404
    assert owm.feed_id(EVENT) == "od:match:3386795"
    assert owm.event_match(EVENT)["score"] == "0:0"
    rows = owm.event_rows({**EVENT, "markets": EVENT["markets"] + MAPS})
    assert {r["market"] for r in rows} == {"match_winner", "map_1_winner"}


class FakeOW:
    def __init__(self, live, prematch=(), fixture_markets=None):
        self.live, self.prematch, self.fixture_markets, self.calls = live, prematch, fixture_markets, []

    def call(self, name, args=None):
        self.calls.append((name, args))
        if name == "get_live_odds":
            return {"count": len(self.live), "events": list(self.live)}
        if name == "get_prematch_odds":
            return {"count": 0, "events": list(self.prematch)}
        if name == "get_fixture":
            return {**EVENT, "markets": EVENT["markets"] + (self.fixture_markets or [])}
        raise AssertionError(name)


def test_collect_and_latest(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    ow = FakeOW([EVENT], fixture_markets=MAPS)
    assert collect_oddiwire_once(conn, ow, log=lambda *_: None) == 4
    assert ("get_live_odds", {"sport": "DOTA2", "tier": 1, "limit": 100}) in ow.calls
    assert ("get_fixture", {"fixture_id": "a749", "tier": 3}) in ow.calls
    # повторный снимок без изменений ничего не добавляет
    assert collect_oddiwire_once(conn, ow, log=lambda *_: None) == 0
    feed = conn.execute("SELECT feed_id, team1 FROM bookmaker_matches WHERE source='oddiwire'").fetchone()
    assert feed == ("od:match:3386795", "Team Allied")
    # порядок команд в запросе обратный — коэффициенты должны развернуться
    assert db.latest_winner_odds(conn, "team lynx", "Team Allied") == [("oddin (линия BetBoom)", 1.58, 2.2)]


def test_migration_adds_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE odds_snapshots (id INTEGER PRIMARY KEY, taken_at TEXT NOT NULL, source TEXT NOT NULL, bookmaker TEXT NOT NULL, fixture_id TEXT NOT NULL, start_time TEXT, participant1 TEXT, participant2 TEXT, market TEXT NOT NULL, outcome TEXT NOT NULL, price REAL NOT NULL)")
    old.commit()
    old.close()
    cols = {r[1] for r in db.connect(path).execute("PRAGMA table_info(odds_snapshots)")}
    assert {"phase", "fair_prob"} <= cols
