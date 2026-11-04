"""Liquipedia MediaWiki API. Правила: https://liquipedia.net/api-terms-of-use

Не чаще 1 запроса в 2 секунды (action=parse — 1 в 30 секунд), gzip, User-Agent с контактом.
Данные под CC-BY-SA, указываем источник.
"""

from __future__ import annotations

import time

import httpx

from ..http import request_json

URL = "https://liquipedia.net/dota2/api.php"


class Liquipedia:
    def __init__(self, client: httpx.Client, min_interval: float = 2.0, sleep=time.sleep, clock=time.monotonic):
        self.client = client
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last = -1e9

    def _get(self, interval: float, **params):
        wait = self._last + interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()
        return request_json(self.client, "GET", URL, params={"format": "json", **params})

    def siteinfo(self) -> dict:
        return self._get(self.min_interval, action="query", meta="siteinfo")["query"]["general"]

    def parse_page(self, page: str) -> str:
        """HTML страницы (например, Liquipedia:Matches). Медленный лимит — 30 с."""
        data = self._get(30.0, action="parse", page=page, prop="text")
        return data["parse"]["text"]["*"]
