"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    stratz_token: str = ""
    opendota_api_key: str = ""
    oddspapi_api_key: str = ""
    oddspapi_base_url: str = "https://api.oddspapi.io/v4"
    oddspapi_winner_market: str = ""
    oddiwire_api_key: str = ""
    signal_min_ev: float = 0.05
    signal_min_prob: float = 0.0
    signal_min_odds: float = 1.0
    timezone: str = "Europe/Moscow"
    telegram_bot_token: str = ""
    telegram_allowed_ids: tuple[int, ...] = field(default_factory=tuple)
    contact: str = "github.com/BaturinIgor/analysisOfTeamsAndBets"
    db_path: Path = Path("data/bk.sqlite3")

    @property
    def user_agent(self) -> str:
        return f"bkanalyzer/0.1 (personal use; {self.contact})"


def _ids(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in raw.replace(" ", "").split(",") if x)


def load_settings(env_file: str | os.PathLike | None = ".env") -> Settings:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file)
    g = os.environ.get
    return Settings(
        stratz_token=g("STRATZ_TOKEN", ""),
        opendota_api_key=g("OPENDOTA_API_KEY", ""),
        oddspapi_api_key=g("ODDSPAPI_API_KEY", ""),
        oddspapi_base_url=g("ODDSPAPI_BASE_URL", "https://api.oddspapi.io/v4").rstrip("/"),
        oddspapi_winner_market=g("ODDSPAPI_WINNER_MARKET", ""),
        oddiwire_api_key=g("ODDIWIRE_API_KEY", ""),
        signal_min_ev=float(g("SIGNAL_MIN_EV", "") or 0.05),
        signal_min_prob=float(g("SIGNAL_MIN_PROB", "") or 0.0),
        signal_min_odds=float(g("SIGNAL_MIN_ODDS", "") or 1.0),
        timezone=g("TIMEZONE", "") or "Europe/Moscow",
        telegram_bot_token=g("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_ids=_ids(g("TELEGRAM_ALLOWED_IDS", "")),
        contact=g("CONTACT", "github.com/BaturinIgor/analysisOfTeamsAndBets"),
        db_path=Path(g("DB_PATH", "data/bk.sqlite3")),
    )
