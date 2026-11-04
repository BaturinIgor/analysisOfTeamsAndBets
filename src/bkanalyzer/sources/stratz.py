"""STRATZ GraphQL API: https://stratz.com/api"""

from __future__ import annotations

import httpx

from ..http import SourceError, request_json

URL = "https://api.stratz.com/graphql"


class Stratz:
    def __init__(self, client: httpx.Client, token: str):
        if not token:
            raise SourceError("STRATZ_TOKEN не задан в .env")
        self.client = client
        self.token = token

    def query(self, query: str, variables: dict | None = None) -> dict:
        data = request_json(
            self.client,
            "POST",
            URL,
            json={"query": query, "variables": variables or {}},
            # STRATZ требует именно такой User-Agent для запросов к API
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "STRATZ_API"},
        )
        if data.get("errors"):
            raise SourceError(f"STRATZ: {data['errors'][0].get('message')}")
        return data["data"]

    def game_versions(self) -> list[dict]:
        """Список патчей, самый свежий первым."""
        return self.query("{ constants { gameVersions { id name asOfDateTime } } }")["constants"]["gameVersions"]
