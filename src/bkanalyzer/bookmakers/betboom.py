"""Разведка публичной линии BetBoom.

У BetBoom нет публичного API. Сайт сам получает коэффициенты из внутренних
JSON-запросов (XHR/fetch) или WebSocket. Режим разведки открывает публичную
страницу в браузере без входа в аккаунт и сохраняет эти ответы на диск, чтобы
по ним написать парсер. Ставки не делаются, авторизация не используется.

Нужен Playwright: pip install -e '.[browser]' && playwright install chromium
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "https://betboom.ru/esport"
# Слова, по которым ответ похож на линию: сохраняем только такие, чтобы не тащить всё подряд
ODDS_HINTS = re.compile(r"dota|odds|coef|koef|market|outcome|event|line|price", re.I)


def looks_like_odds(url: str, body: str) -> bool:
    return bool(ODDS_HINTS.search(url) or ODDS_HINTS.search(body[:5000]))


def _slug(url: str) -> str:
    s = re.sub(r"^https?://", "", url)
    return re.sub(r"[^A-Za-z0-9]+", "_", s)[:120]


def discover(url: str = DEFAULT_URL, out_dir: Path = Path("data/betboom_discovery"), seconds: int = 60, headless: bool = False) -> Path:
    """Открывает страницу, `seconds` секунд записывает JSON-ответы и WebSocket-кадры.

    Пока окно открыто, можно вручную перейти в раздел Dota 2 и открыть пару матчей —
    так запросы линии точно попадут в запись.
    """
    from playwright.sync_api import sync_playwright  # опциональная зависимость

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_dir / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    counter = {"n": 0}

    def save(kind: str, src: str, body: str) -> None:
        if not looks_like_odds(src, body):
            return
        counter["n"] += 1
        name = f"{counter['n']:04d}_{kind}_{_slug(src)}.json"
        (run_dir / name).write_text(body, encoding="utf-8")
        index.append({"file": name, "kind": kind, "url": src, "bytes": len(body)})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(locale="ru-RU")

        def on_response(resp):
            ctype = resp.headers.get("content-type", "")
            if resp.request.resource_type in ("xhr", "fetch") and "json" in ctype:
                try:
                    save("http", resp.url, resp.text())
                except Exception:
                    pass

        def on_ws(ws):
            ws.on("framereceived", lambda payload: save("ws", ws.url, payload if isinstance(payload, str) else payload.decode("utf-8", "replace")))

        page.on("response", on_response)
        page.on("websocket", on_ws)
        page.goto(url, wait_until="domcontentloaded")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
        browser.close()

    (run_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir
