"""Разведка публичной линии BetBoom.

У BetBoom нет публичного API. По первой разведке (2026-11-04) сайт получает
линию с бэкенда sporthub.bet через WebSocket (wss://ru-ws.sporthub.bet/api/...)
бинарными кадрами в формате protobuf. Режим разведки открывает публичную
страницу в браузере без входа в аккаунт и сохраняет как есть:

- все кадры WebSocket от хостов BetBoom/sporthub в обе стороны (отправленные
  кадры — это подписки, по ним видно, как запросить линию конкретного матча);
- JSON-ответы этих хостов.

Ставки не делаются, авторизация не используется.

Нужен Playwright: pip install -e '.[browser]' && playwright install chromium
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "https://betboom.ru/esport"
BOOKMAKER_HOSTS = re.compile(r"(betboom|sporthub)", re.I)
# Для прочих хостов сохраняем только то, что похоже на линию
ODDS_HINTS = re.compile(r"dota|odds|coef|koef|market|outcome|event|line|price", re.I)


def looks_like_odds(url: str, body: str) -> bool:
    return bool(ODDS_HINTS.search(url) or ODDS_HINTS.search(body[:5000]))


def is_bookmaker_host(url: str) -> bool:
    return bool(BOOKMAKER_HOSTS.search(url))


def _slug(url: str) -> str:
    s = re.sub(r"^\w+://", "", url)
    return re.sub(r"[^A-Za-z0-9]+", "_", s)[:100]


class Recorder:
    """Пишет кадры и ответы на диск и ведёт index.json."""

    def __init__(self, run_dir: Path, clock=time.monotonic):
        self.run_dir = run_dir
        self.index: list[dict] = []
        self._clock = clock
        self._t0 = clock()

    def save(self, kind: str, url: str, payload: str | bytes, direction: str = "in") -> str | None:
        if not is_bookmaker_host(url):
            text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
            if not looks_like_odds(url, text):
                return None
        n = len(self.index) + 1
        ext = "bin" if isinstance(payload, bytes) else "txt"
        name = f"{n:05d}_{kind}_{direction}_{_slug(url)}.{ext}"
        path = self.run_dir / name
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
        self.index.append(
            {"file": name, "kind": kind, "dir": direction, "url": url, "bytes": len(payload), "t": round(self._clock() - self._t0, 3)}
        )
        return name

    def write_index(self) -> None:
        (self.run_dir / "index.json").write_text(json.dumps(self.index, ensure_ascii=False, indent=2), encoding="utf-8")


def discover(url: str = DEFAULT_URL, out_dir: Path = Path("data/betboom_discovery"), seconds: int = 120, headless: bool = False) -> Path:
    """Открывает страницу и `seconds` секунд записывает трафик линии.

    Пока окно открыто, перейдите в киберспорт → Dota 2 и откройте 2–3 матча
    (лучше один, который идёт сейчас) — так подписки на коэффициенты попадут в запись.
    """
    from playwright.sync_api import sync_playwright  # опциональная зависимость

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_dir / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    rec = Recorder(run_dir)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page(locale="ru-RU")

            def on_response(resp):
                ctype = resp.headers.get("content-type", "")
                if resp.request.resource_type in ("xhr", "fetch") and ("json" in ctype or "protobuf" in ctype or "octet" in ctype):
                    try:
                        rec.save("http", resp.url, resp.body())
                    except Exception:
                        pass

            def on_ws(ws):
                ws.on("framereceived", lambda payload: rec.save("ws", ws.url, payload, "in"))
                ws.on("framesent", lambda payload: rec.save("ws", ws.url, payload, "out"))

            page.on("response", on_response)
            page.on("websocket", on_ws)
            page.goto(url, wait_until="domcontentloaded")
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    page.wait_for_timeout(1000)
                except Exception:  # окно закрыли вручную — сохраняем то, что успели
                    break
            try:
                browser.close()
            except Exception:
                pass
    finally:
        rec.write_index()
    return run_dir
