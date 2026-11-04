"""OddsPapi: коэффициенты разных БК, в том числе исторические.

Ключ передаётся параметром apiKey. Dota 2 = sportId 16 (по документации на 2026-11).
Структура /odds: bookmakerOdds[slug].markets[market].outcomes[outcome].players["0"].price
"""

from __future__ import annotations

import httpx

from ..http import SourceError, request_json

DOTA2_SPORT_ID = 16
# Возможные слаги интересующих нас БК — точные значения проверяем командой check-sources
WANTED_BOOKMAKERS = ("betboom", "fonbet", "pari")


class OddsPapi:
    def __init__(self, client: httpx.Client, api_key: str, base_url: str):
        if not api_key:
            raise SourceError("ODDSPAPI_API_KEY не задан в .env")
        self.client = client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, **params):
        return request_json(self.client, "GET", f"{self.base_url}{path}", params={"apiKey": self.api_key, **params})

    def sports(self) -> list[dict]:
        return self._get("/sports")

    def bookmakers(self) -> list[dict]:
        return self._get("/bookmakers")

    def markets(self) -> list[dict]:
        """Справочник рынков (id → название), если OddsPapi его отдаёт."""
        return self._get("/markets", sportId=DOTA2_SPORT_ID)

    def fixtures(self, date_from: str, date_to: str, sport_id: int = DOTA2_SPORT_ID) -> list[dict]:
        """Матчи в интервале дат (не больше 10 дней), формат YYYY-MM-DD."""
        return self._get("/fixtures", sportId=sport_id, **{"from": date_from, "to": date_to})

    def odds(self, fixture_id: str, bookmakers: list[str] | None = None) -> dict:
        params = {"fixtureId": fixture_id}
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        return self._get("/odds", **params)

    def historical_odds(self, fixture_id: str, bookmakers: list[str]) -> dict:
        if len(bookmakers) > 3:
            raise ValueError("historical-odds принимает не больше 3 БК за запрос")
        return self._get("/historical-odds", fixtureId=fixture_id, bookmakers=",".join(bookmakers))


def find_bookmakers(bookmakers: list[dict], wanted=WANTED_BOOKMAKERS) -> dict[str, dict | None]:
    """Ищет нужные БК в списке по слагу или названию (без учёта регистра)."""
    found: dict[str, dict | None] = {w: None for w in wanted}
    for bm in bookmakers:
        text = " ".join(str(bm.get(k, "")) for k in ("slug", "name", "bookmaker", "id")).lower()
        for w in wanted:
            if found[w] is None and w in text:
                found[w] = bm
    return found


def flatten_odds(payload: dict) -> list[dict]:
    """Плоский список {bookmaker, market, outcome, price} из ответа /odds."""
    rows = []
    for slug, bm in (payload.get("bookmakerOdds") or {}).items():
        for market_id, market in (bm.get("markets") or {}).items():
            for outcome_id, outcome in (market.get("outcomes") or {}).items():
                player = (outcome.get("players") or {}).get("0") or {}
                if isinstance(player, dict) and "price" in player:
                    rows.append({"bookmaker": slug, "market": market_id, "outcome": outcome_id, "price": float(player["price"])})
    return rows
