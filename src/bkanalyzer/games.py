"""Игры, которые мы считаем: Dota 2 (история OpenDota), CS2 и League of Legends (история PandaScore).

Линия всех — из OddiWire (фид Oddin, как у BetBoom). Строки CS2 в базе хранятся
с source='oddiwire_cs2', а бумажные ставки — в режимах с префиксом «cs_» (у LoL —
'oddiwire_lol' и «lol_»), поэтому у каждой игры свои банки и своя статистика.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    key: str            # dota2 | cs2
    label: str          # как показываем
    sport: str          # id спорта в OddiWire
    source: str         # source в odds_snapshots / bookmaker_matches / feed_markets
    maps_table: str     # история карт
    mode_prefix: str    # префикс режимов симулятора
    map_markets: bool   # считаем ли рынки на отдельные карты
    news_word: str      # добавка к поиску новостей
    pandascore: str = ""  # игра в API PandaScore (история серий); пусто — история не из PandaScore
    sync_cmd: str = ""    # команда загрузки истории


DOTA = Game("dota2", "Dota 2", "DOTA2", "oddiwire", "pro_maps", "", True, "dota")
# CS2: в бесплатной истории PandaScore есть только счёт серии, без порядка карт — рынки на карты не считаем
CS2 = Game("cs2", "CS2", "CS2", "oddiwire_cs2", "cs_maps", "cs_", False, "cs2", "csgo", "bk sync-cs")
# LoL: так же, как CS2 — счёт серий из PandaScore, только победа в матче
LOL = Game("lol", "LoL", "LOL", "oddiwire_lol", "lol_maps", "lol_", False, "league of legends", "lol", "bk sync-lol")
ALL = (DOTA, CS2, LOL)
PANDASCORE = (CS2, LOL)  # игры с историей из PandaScore


def by_key(key: str | None) -> Game:
    return next((g for g in ALL if g.key == key), DOTA)


def by_mode(mode: str) -> Game:
    return next((g for g in ALL if g.mode_prefix and mode.startswith(g.mode_prefix)), DOTA)


def base_mode(mode: str) -> str:
    """cs_prematch → prematch, lol_live → live."""
    g = by_mode(mode)
    return mode[len(g.mode_prefix):] if g.mode_prefix else mode
