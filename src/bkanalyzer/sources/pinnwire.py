"""PinnWire: линия Pinnacle (самый точный рынок в мире) — теннис, футбол, хоккей и др.

Бесплатный ключ (pinnwire.com → Trial): 100 запросов в день, 20 в минуту, без карты.
Ключ задаётся PINNWIRE_API_KEY. Без ключа есть общий демо-ключ (50 в день на всех) — для
постоянного сбора его не хватит, поэтому без ключа теннис не собираем.

REST (https://pinnwire.com, заголовок x-api-key):
- GET /kit/v1/prematch/fixtures?sport_id=2 — все матчи до начала (теннис = 2);
- GET /v1/me — тариф и расход (квоту не тратит).
Событие: {event_id, league_name, home, away, starts, event_type,
          periods: {num_0: {money_line: {home, away}}}} — цены десятичные.
"""

from __future__ import annotations

import httpx

from ..http import SourceError
from .oddiwire import RateLimited

BASE_URL = "https://pinnwire.com"
TENNIS = 2
BOOKMAKER = "pinnacle"
DEFAULT_DAILY_LIMIT = 100


class PinnWire:
    def __init__(self, client: httpx.Client, api_key: str, base_url: str = BASE_URL):
        if not api_key:
            raise SourceError("PINNWIRE_API_KEY не задан")
        self.client = client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, **params):
        try:
            resp = self.client.get(f"{self.base_url}{path}", params=params, headers={"x-api-key": self.api_key})
        except httpx.HTTPError as exc:
            raise SourceError(f"PinnWire недоступен: {exc}") from exc
        if resp.status_code == 429:
            try:
                wait = float(resp.headers.get("Retry-After", 300))
            except ValueError:
                wait = 300.0
            raise RateLimited(f"PinnWire: лимит запросов (429), ждём {wait:.0f} с", wait)
        if resp.status_code >= 400:
            raise SourceError(f"PinnWire HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"PinnWire: ответ не JSON: {resp.text[:200]}") from exc

    def fixtures(self, sport_id: int = TENNIS) -> dict:
        """Матчи до начала с коэффициентами (1 запрос из квоты)."""
        data = self._get("/kit/v1/prematch/fixtures", sport_id=sport_id)
        return data if isinstance(data, dict) else {"events": data if isinstance(data, list) else []}

    def me(self) -> dict:
        data = self._get("/v1/me")
        return data if isinstance(data, dict) else {}

    def budget(self) -> tuple[int, int]:
        """(использовано сегодня, дневной лимит) по /v1/me; поля читаем осторожно — формат может отличаться."""
        info = self.me()
        flat: dict = {}

        def walk(x, prefix=""):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, f"{prefix}{k}.".lower())
            elif isinstance(x, (int, float)) and not isinstance(x, bool):
                flat[prefix.rstrip(".")] = int(x)

        walk(info)
        used = next((v for k, v in flat.items() if ("today" in k or "day" in k) and ("used" in k or "usage" in k or "count" in k)), None)
        if used is None:
            used = next((v for k, v in flat.items() if k.endswith("today")), 0)
        limit = next((v for k, v in flat.items() if ("per_day" in k or "daily" in k or k.endswith("day_limit"))), None)
        return int(used or 0), int(limit or DEFAULT_DAILY_LIMIT)


def _no_vig(a: float, b: float) -> tuple[float, float]:
    ia, ib = 1 / a, 1 / b
    s = ia + ib
    return ia / s, ib / s


def is_singles(event: dict) -> bool:
    """Парные матчи («A/B»), сеты, геймы и спецпредложения пропускаем."""
    home, away = str(event.get("home") or ""), str(event.get("away") or "")
    if not home or not away or "/" in home or "/" in away:
        return False
    text = f"{home} {away} {event.get('league_name') or ''}".casefold()
    return not any(w in text for w in ("doubles", "(sets)", "(games)", "set winner", "specials"))


def money_line(event: dict) -> tuple[float, float] | None:
    periods = event.get("periods") or {}
    full = periods.get("num_0") or periods.get("0") or {}
    ml = full.get("money_line") or {}
    try:
        home, away = float(ml["home"]), float(ml["away"])
    except (KeyError, TypeError, ValueError):
        return None
    return (home, away) if home > 1 and away > 1 else None


def events(payload) -> list[dict]:
    if isinstance(payload, dict):
        return [e for e in payload.get("events") or [] if isinstance(e, dict)]
    return []
