"""PandaScore: бесплатный тариф «Schedules, Results & Context Data» — 1000 запросов в час.

Ключ бесплатный (регистрация на pandascore.co, без карты), задаётся PANDASCORE_TOKEN.
CS2 в API называется csgo: /csgo/matches/past — сыгранные серии со счётом.
Карты внутри серии на бесплатном тарифе не отдаются, поэтому берём счёт серии (results).
"""

from __future__ import annotations

import httpx

from ..http import SourceError, request_json

BASE_URL = "https://api.pandascore.co"


class PandaScore:
    def __init__(self, client: httpx.Client, token: str):
        if not token:
            raise SourceError("PANDASCORE_TOKEN не задан")
        self.client = client
        self.token = token

    def _get(self, path: str, **params):
        return request_json(self.client, "GET", f"{BASE_URL}{path}", params=params,
                            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})

    def cs_past(self, page: int = 1, per_page: int = 100) -> list[dict]:
        """Сыгранные серии CS2, от новых к старым."""
        # по begin_at нельзя: у отменённых и части сыгранных он пустой, и при сортировке они идут первыми
        data = self._get("/csgo/matches/past", **{"filter[status]": "finished", "sort": "-scheduled_at",
                                                    "page": page, "per_page": per_page})
        return data if isinstance(data, list) else []
