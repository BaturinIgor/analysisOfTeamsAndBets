"""Игры, которые мы считаем: Dota 2 (история OpenDota) и CS2 (история PandaScore).

Линия обеих — из OddiWire (фид Oddin, как у BetBoom). Строки CS2 в базе хранятся
с source='oddiwire_cs2', а бумажные ставки — в режимах с префиксом «cs_», поэтому
у CS2 свои банки и своя статистика, и ничего не смешивается с Dota.
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


DOTA = Game("dota2", "Dota 2", "DOTA2", "oddiwire", "pro_maps", "", True, "dota")
# CS2: в бесплатной истории PandaScore есть только счёт серии, без порядка карт — рынки на карты не считаем
CS2 = Game("cs2", "CS2", "CS2", "oddiwire_cs2", "cs_maps", "cs_", False, "cs2")
ALL = (DOTA, CS2)


def by_key(key: str | None) -> Game:
    return next((g for g in ALL if g.key == key), DOTA)


def by_mode(mode: str) -> Game:
    return next((g for g in ALL if g.mode_prefix and mode.startswith(g.mode_prefix)), DOTA)


def base_mode(mode: str) -> str:
    """cs_prematch → prematch."""
    g = by_mode(mode)
    return mode[len(g.mode_prefix):] if g.mode_prefix else mode
