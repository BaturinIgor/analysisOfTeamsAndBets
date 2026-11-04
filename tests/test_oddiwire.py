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
