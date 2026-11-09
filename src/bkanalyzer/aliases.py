"""Справочник синонимов названий команд (ТР-4): название в линии → название в истории OpenDota.

Правится руками: `bk alias "Название в линии" "Название в OpenDota"`, `bk alias --list`,
`bk alias --remove "Название"`. Подсказки по ненайденным командам: `bk unmatched`.
"""

from __future__ import annotations

import difflib
import sqlite3

from . import analysis

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_aliases (
    alias TEXT PRIMARY KEY,      -- нормализованное название из линии
    alias_name TEXT NOT NULL,    -- как написано в линии
    target TEXT NOT NULL,        -- нормализованное название в истории
    target_name TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def load(conn: sqlite3.Connection) -> dict[str, str]:
    """Загружает синонимы в analysis.ALIASES (их использует normalize) и возвращает их."""
    ensure_schema(conn)
    conn.row_factory = None
    found = {a: t for a, t in conn.execute("SELECT alias, target FROM team_aliases") if a != t}
    analysis.ALIASES.clear()
    analysis.ALIASES.update(found)
    return found


def history_names(conn: sqlite3.Connection, tables=("pro_maps", "cs_maps", "lol_maps")) -> dict[str, tuple[str, int]]:
    """Нормализованное название → (как пишется, сколько карт) по истории Dota, CS2 и LoL."""
    conn.row_factory = None
    out: dict[str, tuple[str, int]] = {}
    rows = []
    for t in tables:
        try:
            rows += conn.execute(
                f"SELECT name, COUNT(*) FROM (SELECT radiant_name AS name FROM {t} UNION ALL SELECT dire_name FROM {t}) "
                "WHERE name IS NOT NULL GROUP BY name").fetchall()
        except sqlite3.OperationalError:  # этой истории ещё нет
            continue
    for name, n in rows:
        key = analysis.normalize_raw(name)
        if key and (key not in out or n > out[key][1]):
            out[key] = (name, n)
    return out


def add(conn: sqlite3.Connection, alias_name: str, target_name: str) -> str:
    ensure_schema(conn)
    alias, target = analysis.normalize_raw(alias_name), analysis.normalize_raw(target_name)
    if not alias or not target:
        raise ValueError("пустое название")
    names = history_names(conn)
    if target not in names:
        hint = suggest(target_name, names)
        raise ValueError(f"«{target_name}» нет в истории (OpenDota / PandaScore)" + (f". Похожие: {', '.join(hint)}" if hint else ""))
    conn.execute("INSERT OR REPLACE INTO team_aliases VALUES (?,?,?,?)", (alias, alias_name, target, names[target][0]))
    conn.commit()
    load(conn)
    return names[target][0]


def remove(conn: sqlite3.Connection, alias_name: str) -> bool:
    ensure_schema(conn)
    cur = conn.execute("DELETE FROM team_aliases WHERE alias=?", (analysis.normalize_raw(alias_name),))
    conn.commit()
    load(conn)
    return cur.rowcount > 0


def listing(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    ensure_schema(conn)
    return conn.execute("SELECT alias_name, target_name FROM team_aliases ORDER BY alias_name").fetchall()


def suggest(name: str, names: dict[str, tuple[str, int]], n: int = 3) -> list[str]:
    key = analysis.normalize_raw(name)
    close = difflib.get_close_matches(key, list(names), n=n * 3, cutoff=0.6)
    close.sort(key=lambda k: -names[k][1])
    return [names[k][0] for k in close[:n]]


def unmatched(conn: sqlite3.Connection, model, game=None, now=None) -> list[tuple[str, list[str]]]:
    """Команды из текущей линии игры, которых нет в истории модели, и похожие названия."""
    from .games import DOTA

    game = game or DOTA
    load(conn)
    names = history_names(conn, (game.maps_table,))
    seen, out = set(), []
    for bm in analysis.load_board(conn, since_hours=48, now=now, game=game):
        for team in (bm.team1, bm.team2):
            if team in seen:
                continue
            seen.add(team)
            if analysis.match_team(model, team) is None:
                out.append((team, suggest(team, names)))
    return out
