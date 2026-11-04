"""История про-карт Dota 2 из OpenDota в SQLite (ТР-1, базовая часть).

Берём /proMatches постранично назад во времени и /leagues для уровня турнира.
Tier 1–2 по требованиям = уровни OpenDota 'premium' и 'professional'.
"""

from __future__ import annotations

import sqlite3
import time

from .sources.opendota import OpenDota

TIERS = ("premium", "professional")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    league_id INTEGER PRIMARY KEY,
    name TEXT,
    tier TEXT
);
CREATE TABLE IF NOT EXISTS pro_maps (
    match_id INTEGER PRIMARY KEY,
    start_time INTEGER NOT NULL,     -- unix, UTC
    duration INTEGER,
    league_id INTEGER,
    league_name TEXT,
    series_id INTEGER,
    series_type INTEGER,             -- 0 Bo1, 1 Bo3, 2 Bo5, 3 Bo2 (OpenDota)
    radiant_team_id INTEGER,
    radiant_name TEXT,
    dire_team_id INTEGER,
    dire_name TEXT,
    radiant_score INTEGER,
    dire_score INTEGER,
    radiant_win INTEGER
);
CREATE INDEX IF NOT EXISTS ix_pro_maps_time ON pro_maps (start_time);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def sync_leagues(conn: sqlite3.Connection, od: OpenDota) -> int:
    ensure_schema(conn)
    leagues = od.leagues()
    conn.executemany(
        "INSERT OR REPLACE INTO leagues (league_id, name, tier) VALUES (?, ?, ?)",
        [(lg["leagueid"], lg.get("name"), lg.get("tier")) for lg in leagues],
    )
    conn.commit()
    return len(leagues)


def sync_pro_maps(conn: sqlite3.Connection, od: OpenDota, days: int, *, now: float | None = None, pause: float = 1.1, sleep=time.sleep, log=print) -> int:
    """Загружает про-карты за `days` дней. Повторный запуск дозагружает только новое и недостающее старое."""
    ensure_schema(conn)
    since = (now or time.time()) - days * 86400
    lo_id, hi_id, lo_time = conn.execute("SELECT MIN(match_id), MAX(match_id), MIN(start_time) FROM pro_maps").fetchone()
    covered = lo_time is not None and lo_time <= since
    saved = 0
    cursor = None
    jumped = False
    while True:
        page = od.pro_matches(less_than_match_id=cursor)
        if not page:
            break
        rows = [m for m in page if m.get("start_time", 0) >= since]
        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO pro_maps (match_id, start_time, duration, league_id, league_name, series_id, series_type,
               radiant_team_id, radiant_name, dire_team_id, dire_name, radiant_score, dire_score, radiant_win)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (m["match_id"], m["start_time"], m.get("duration"), m.get("leagueid"), m.get("league_name"), m.get("series_id"),
                 m.get("series_type"), m.get("radiant_team_id"), m.get("radiant_name"), m.get("dire_team_id"), m.get("dire_name"),
                 m.get("radiant_score"), m.get("dire_score"), int(bool(m.get("radiant_win"))))
                for m in rows
            ],
        )
        conn.commit()
        added = conn.total_changes - before
        saved += added
        if min(m.get("start_time", 0) for m in page) < since:
            break
        cursor = min(m["match_id"] for m in page)
        if not jumped and lo_id is not None and cursor <= hi_id:
            # Дошли до уже загруженного: либо всё есть, либо продолжаем от старого края
            if covered:
                break
            cursor, jumped = lo_id, True
        if saved // 1000 != (saved - added) // 1000:
            log(f"  загружено карт: {saved}")
        sleep(pause)  # бесплатный лимит OpenDota ~60 запросов в минуту
    return saved


def tier_maps(conn: sqlite3.Connection, tiers=TIERS, since: int = 0) -> list[sqlite3.Row]:
    """Карты Tier 1–2 по возрастанию времени, только где известны обе команды."""
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(tiers))
    return conn.execute(
        f"""SELECT m.* FROM pro_maps m JOIN leagues l ON l.league_id = m.league_id
            WHERE l.tier IN ({marks}) AND m.radiant_team_id IS NOT NULL AND m.dire_team_id IS NOT NULL AND m.start_time >= ?
            ORDER BY m.start_time, m.match_id""",
        (*tiers, since),
    ).fetchall()
