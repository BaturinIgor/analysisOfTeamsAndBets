"""OpenDota API: https://docs.opendota.com/"""

from __future__ import annotations

import httpx

from ..http import request_json

BASE_URL = "https://api.opendota.com/api"


class OpenDota:
    def __init__(self, client: httpx.Client, api_key: str = ""):
        self.client = client
        self.api_key = api_key

    def _get(self, path: str, **params):
        if self.api_key:
            params["api_key"] = self.api_key
        return request_json(self.client, "GET", f"{BASE_URL}{path}", params=params or None)

    def pro_matches(self, less_than_match_id: int | None = None) -> list[dict]:
        """Последние 100 про-матчей (карт), постранично назад по match_id."""
        params = {"less_than_match_id": less_than_match_id} if less_than_match_id else {}
        return self._get("/proMatches", **params)

    def match(self, match_id: int) -> dict:
        """Полная карта: драфт, игроки, radiant_gold_adv по минутам и т.д."""
        return self._get(f"/matches/{match_id}")

    def live(self) -> list[dict]:
        """Идущие сейчас игры (включая про-матчи с league_id)."""
        return self._get("/live")

    def teams(self) -> list[dict]:
        return self._get("/teams")

    def leagues(self) -> list[dict]:
        """Все лиги с уровнем: premium (Tier 1), professional (Tier 2), amateur."""
        return self._get("/leagues")

    def hero_stats(self) -> list[dict]:
        """Статистика героев: pro_pick/pro_win и по рангам (8_pick/8_win = Immortal)."""
        return self._get("/heroStats")
