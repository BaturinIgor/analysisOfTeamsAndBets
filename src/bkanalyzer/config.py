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
    signal_min_ev: float = 0.03
    signal_min_prob: float = 0.0
    signal_min_odds: float = 1.0
    signal_max_gap: float = 0.10
    signal_min_games: int = 30
    timezone: str = "Europe/Moscow"
    sim_bank: float = 25000.0
    sim_strategy: str = "fixed"
    sim_stake: float = 1250.0
    sim_percent: float = 0.07
    sim_kelly: float = 1.0
    sim_strategies: tuple = ("fixed", "percent", "kelly")

    def sim(self):
        from .sim import SimSettings

        return SimSettings(self.sim_bank, self.sim_strategy, self.sim_stake, self.sim_percent, self.sim_kelly,
                           strategies=self.sim_strategies)

    def thresholds(self):
        from .analysis import Thresholds

        return Thresholds(self.signal_min_ev, self.signal_min_prob, self.signal_min_odds, self.signal_max_gap, self.signal_min_games)
    telegram_bot_token: str = ""
    telegram_allowed_ids: tuple[int, ...] = field(default_factory=tuple)
    telegram_proxy: str = ""
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
        signal_min_ev=float(g("SIGNAL_MIN_EV", "") or 0.03),
        signal_min_prob=float(g("SIGNAL_MIN_PROB", "") or 0.0),
        signal_min_odds=float(g("SIGNAL_MIN_ODDS", "") or 1.0),
        signal_max_gap=float(g("SIGNAL_MAX_GAP", "") or 0.10),
        signal_min_games=int(g("SIGNAL_MIN_GAMES", "") or 30),
        timezone=g("TIMEZONE", "") or "Europe/Moscow",
        sim_bank=float(g("SIM_BANK", "") or 25000),
        sim_strategy=(g("SIM_STRATEGY", "") or "fixed").lower(),
        sim_stake=float(g("SIM_STAKE", "") or 1250),
        sim_percent=float(g("SIM_PERCENT", "") or 0.07),
        sim_kelly=float(g("SIM_KELLY", "") or 1.0),
        sim_strategies=tuple(x.strip().lower() for x in (g("SIM_STRATEGIES", "") or "fixed,percent,kelly").split(",") if x.strip()),
        telegram_bot_token=g("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_ids=_ids(g("TELEGRAM_ALLOWED_IDS", "")),
        telegram_proxy=g("TELEGRAM_PROXY", ""),
        contact=g("CONTACT", "github.com/BaturinIgor/analysisOfTeamsAndBets"),
        db_path=Path(g("DB_PATH", "data/bk.sqlite3")),
    )
