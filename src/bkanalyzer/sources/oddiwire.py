"""OddiWire: бесплатная лента коэффициентов Oddin.gg (киберспорт, в том числе Dota 2).

Oddin — поставщик линии, на котором работает BetBoom (в трафике BetBoom матчи
имеют id вида `od:match:<id>`), поэтому цены здесь близки к ценам BetBoom.

Бесплатно: 100 запросов в день (демо без ключа — 10 запросов за 5 минут).
REST-адрес OddiWire не опубликован, поэтому ходим в их публичный MCP-сервер
(https://oddiwire.com/mcp, Streamable HTTP, JSON-RPC 2.0). Инструменты:
list_sports, get_coverage, get_prematch_odds, get_live_odds, get_fixture,
get_my_plan, get_health.
"""

from __future__ import annotations

import json

import httpx

from ..http import SourceError

MCP_URL = "https://oddiwire.com/mcp"
PROTOCOL_VERSION = "2025-06-18"


def _parse_body(resp: httpx.Response) -> dict:
    """Ответ MCP приходит либо JSON, либо потоком SSE (`data: {...}`)."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        last = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    msg = json.loads(chunk)
                    if "id" in msg:
                        last = msg
        if last is None:
            raise SourceError("OddiWire: пустой поток SSE")
        return last
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"OddiWire: ответ не JSON: {resp.text[:200]}") from exc


def tool_payload(result: dict):
    """Достаёт полезные данные из результата tools/call.

    Предпочитаем structuredContent; иначе пробуем разобрать текст как JSON.
    """
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


class OddiWire:
    def __init__(self, client: httpx.Client, api_key: str = "", url: str = MCP_URL):
        self.client = client
        self.api_key = api_key
        self.url = url
        self.session_id: str | None = None
        self._id = 0
        self._ready = False

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, payload: dict) -> httpx.Response:
        params = {"key": self.api_key or "demo"}
        try:
            resp = self.client.post(self.url, json=payload, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"OddiWire недоступен: {exc}") from exc
        if resp.status_code == 429:
            raise SourceError("OddiWire: лимит запросов исчерпан (429), попробуйте позже")
        if resp.status_code >= 400:
            raise SourceError(f"OddiWire HTTP {resp.status_code}: {resp.text[:300]}")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        return resp

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = _parse_body(self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}))
        if "error" in msg:
            raise SourceError(f"OddiWire {method}: {msg['error']}")
        return msg.get("result", {})

    def initialize(self) -> dict:
        info = self._rpc(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "bkanalyzer", "version": "0.1"}},
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True
        return info

    def tools(self) -> list[dict]:
        if not self._ready:
            self.initialize()
        return self._rpc("tools/list").get("tools", [])

    def call(self, name: str, arguments: dict | None = None):
        if not self._ready:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise SourceError(f"OddiWire {name}: {tool_payload(result)}")
        return tool_payload(result)


def find_dota_sport(sports) -> str | None:
    """Ищет id Dota 2 в ответе list_sports, какой бы формы он ни был."""
    def walk(x):
        if isinstance(x, dict):
            yield x
            for v in x.values():
                yield from walk(v)
        elif isinstance(x, list):
            for v in x:
                yield from walk(v)

    for item in walk(sports):
        blob = " ".join(str(v) for v in item.values() if isinstance(v, (str, int)))
        if "dota" in blob.lower():
            for key in ("id", "sport_id", "sportId", "sport", "key", "code"):
                if isinstance(item.get(key), (str, int)):
                    return str(item[key])
    if isinstance(sports, str):
        for token in sports.replace(",", " ").split():
            if "dota" in token.lower():
                return token.strip("\"'`")
    if isinstance(sports, list):
        for v in sports:
            if isinstance(v, str) and "dota" in v.lower():
                return v
    return None
