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


class RateLimited(SourceError):
    """Сервис просит подождать: retry_after — сколько секунд."""

    def __init__(self, message: str, retry_after: float = 300.0):
        super().__init__(message)
        self.retry_after = retry_after


DEFAULT_DAILY_LIMIT = 100  # бесплатный ключ


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
            try:
                wait = float(resp.headers.get("Retry-After", 300))
            except ValueError:
                wait = 300.0
            raise RateLimited(f"OddiWire: лимит запросов (429), ждём {wait:.0f} с", wait)
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

    def reset(self) -> None:
        self.session_id = None
        self._ready = False

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
        payload = tool_payload(result)
        if isinstance(payload, dict) and payload.get("error") == "rate_limited":
            wait = float(payload.get("retry_after_ms") or 300_000) / 1000
            raise RateLimited(f"OddiWire {name}: лимит «{payload.get('limit')} за {payload.get('window')}», ждём {wait:.0f} с", wait)
        if result.get("isError"):
            raise SourceError(f"OddiWire {name}: {payload}")
        return payload

    def budget(self) -> tuple[int, int]:
        """(использовано сегодня, дневной лимит) по get_my_plan — этот вызов квоту не тратит."""
        plan = self.call("get_my_plan")
        if not isinstance(plan, dict):
            return 0, DEFAULT_DAILY_LIMIT
        used = int((plan.get("usage") or {}).get("today") or 0)
        limit = (plan.get("limits") or {}).get("per_day")
        if plan.get("plan") == "demo" or not limit:
            # демо: 10 запросов за 5 минут на всех; дневного лимита нет, но и квоту делят все
            return 0, 10 * 12 * 24 // 4
        return used, int(limit)


def find_dota_sport(sports) -> str | None:
    """Ищет id Dota 2 в ответе list_sports, например {"sports": ["CS2", "DOTA2", "DOTA2_DUELS"]}.

    Берём сам id (короткая строка без пробелов), не «дуэли» и не описания.
    """
    found: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            name = " ".join(str(v) for v in x.values() if isinstance(v, str)).lower()
            if "dota" in name and "duel" not in name:
                for key in ("id", "sport_id", "sportId", "key", "code"):
                    if isinstance(x.get(key), (str, int)):
                        found.append(str(x[key]))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            for token in x.replace(",", " ").split():
                token = token.strip("\"'`.;:()")
                if "dota" in token.lower() and "duel" not in token.lower():
                    found.append(token)

    walk(sports)
    for cand in found:
        if cand.upper() == "DOTA2":
            return cand
    return found[0] if found else None


DOTA2 = "DOTA2"
BOOKMAKER = "oddin"
MATCH_WINNER = "match_winner"


def market_key(market: dict) -> str | None:
    """Ключ рынка в нашей базе: match_winner или map_N_winner; прочие рынки пропускаем."""
    if market.get("canonical") == MATCH_WINNER:
        return MATCH_WINNER
    name = (market.get("market") or "").strip()
    parts = name.split()
    # "Map 1 Winner - Twoway"
    if len(parts) >= 3 and parts[0].lower() == "map" and parts[1].isdigit() and parts[2].lower() == "winner":
        return f"map_{parts[1]}_winner"
    return None


def feed_id(event: dict) -> str | None:
    """od:match:<id> — тот же id, что у матча в линии BetBoom."""
    for m in event.get("markets") or []:
        for sel in m.get("selections") or []:
            pid = sel.get("provider_id") or ""
            if pid.startswith("od:match:"):
                return pid.split("/", 1)[0]
    return None


def event_rows(event: dict) -> list[dict]:
    """Коэффициенты на победу в матче и на картах из одного события OddiWire."""
    phase = "live" if (event.get("event_type") == "live" or event.get("status") == "LIVE") else "prematch"
    rows = []
    for m in event.get("markets") or []:
        key = market_key(m)
        if key is None or m.get("status") != "OPEN":
            continue
        for sel in m.get("selections") or []:
            price = sel.get("price")
            if not isinstance(price, (int, float)) or sel.get("status") not in (None, "TRADING"):
                continue
            rows.append(
                {
                    "bookmaker": BOOKMAKER,
                    "fixture_id": str(event.get("event_id")),
                    "start_time": event.get("starts"),
                    "participant1": event.get("home"),
                    "participant2": event.get("away"),
                    "market": key,
                    "outcome": sel.get("name"),
                    "price": float(price),
                    "phase": phase,
                    "fair_prob": sel.get("probability"),
                }
            )
    return rows


def event_match(event: dict) -> dict:
    score = event.get("score") or {}
    return {
        "match_id": str(event.get("event_id")),
        "feed_id": feed_id(event),
        "tournament_id": event.get("league"),
        "start_time": event.get("starts"),
        "team1": event.get("home"),
        "team2": event.get("away"),
        "format": None,
        "score": f"{score['home']}:{score['away']}" if score.get("home") is not None and score.get("away") is not None else None,
    }


def board_events(payload) -> list[dict]:
    if isinstance(payload, dict):
        return [e for e in payload.get("events") or [] if isinstance(e, dict)]
    return []
