"""HTTP-клиент с User-Agent, повторами и паузами при лимитах."""

from __future__ import annotations

import time

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """Источник вернул ошибку или недоступен."""


def make_client(user_agent: str, transport: httpx.BaseTransport | None = None, **kw) -> httpx.Client:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip"}
    headers.update(kw.pop("headers", {}))
    return httpx.Client(headers=headers, timeout=kw.pop("timeout", 30.0), transport=transport, **kw)


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 2.0,
    sleep=time.sleep,
    **kw,
):
    """Запрос с повтором на 429/5xx. Возвращает разобранный JSON."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.request(method, url, **kw)
        except httpx.HTTPError as exc:  # сеть
            last = exc
        else:
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                wait = float(resp.headers.get("Retry-After", backoff * 2**attempt))
                sleep(wait)
                continue
            if resp.status_code >= 400:
                raise SourceError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise SourceError(f"{url}: ответ не JSON: {resp.text[:200]}") from exc
        if attempt < retries:
            sleep(backoff * 2**attempt)
    raise SourceError(f"{method} {url}: сеть недоступна ({last})")
