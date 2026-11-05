"""Составы и патчи для про-карт (ТР-5, часть ТР-6).

- Составы (кто играл и на каком герое) берём пакетно через OpenDota Explorer:
  один SQL-запрос на 400 карт вместо 400 запросов /matches.
- Патч определяем по дате карты из списка патчей OpenDota.
"""

from __future__ import annotations

import bisect
import sqlite3
import time
from datetime import datetime

from .history import TIERS, ensure_schema as ensure_history
from .http import SourceError
from .sources.opendota import OpenDota

SCHEMA = """
CREATE TABLE IF NOT EXISTS map_players (
    match_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    is_radiant INTEGER NOT NULL,
    hero_id INTEGER,
    PRIMARY KEY (match_id, account_id)
);
CREATE TABLE IF NOT EXISTS patches (
    name TEXT PRIMARY KEY,
    start_time INTEGER NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    ensure_history(conn)
    conn.executescript(SCHEMA)


def _parse_date(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def sync_patches(conn: sqlite3.Connection, od: OpenDota) -> int:
    ensure_schema(conn)
    rows = []
    for p in od.patches():
        if p.get("name") and p.get("date"):
            rows.append((str(p["name"]), _parse_date(p["date"])))
    conn.executemany("INSERT OR REPLACE INTO patches (name, start_time) VALUES (?, ?)", rows)
    conn.commit()
    return len(rows)


class PatchIndex:
    def __init__(self, rows: list[tuple[str, int]]):
        rows = sorted(rows, key=lambda r: r[1])
        self.names = [r[0] for r in rows]
        self.starts = [r[1] for r in rows]

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "PatchIndex":
        ensure_schema(conn)
        return cls(conn.execute("SELECT name, start_time FROM patches").fetchall())

    def at(self, t: int) -> str | None:
        i = bisect.bisect_right(self.starts, t) - 1
        return self.names[i] if i >= 0 else None


def missing_player_maps(conn: sqlite3.Connection, tiers=TIERS, since: int = 0) -> list[int]:
    marks = ",".join("?" * len(tiers))
    return [r[0] for r in conn.execute(
        f"""SELECT m.match_id FROM pro_maps m JOIN leagues l ON l.league_id = m.league_id
            WHERE l.tier IN ({marks}) AND m.start_time >= ?
              AND NOT EXISTS (SELECT 1 FROM map_players p WHERE p.match_id = m.match_id)
            ORDER BY m.match_id DESC""",
        (*tiers, since),
    )]


def sync_players(conn: sqlite3.Connection, od: OpenDota, *, batch: int = 400, pause: float = 1.5, sleep=time.sleep, log=print,
                 max_batches: int | None = None) -> int:
    """Догружает составы для карт Tier 1–2, у которых их ещё нет. Возвращает число карт с составом."""
    ensure_schema(conn)
    todo = missing_player_maps(conn)
    done = 0
    for n, i in enumerate(range(0, len(todo), batch)):
        if max_batches is not None and n >= max_batches:
            break
        ids = [int(x) for x in todo[i:i + batch]]
        sql = ("SELECT match_id, account_id, hero_id, player_slot FROM player_matches "
               f"WHERE match_id IN ({','.join(map(str, ids))})")
        try:
            rows = od.explorer(sql)
        except SourceError as exc:
            log(f"  составы: {exc}")
            break
        recs = [(int(r["match_id"]), int(r["account_id"]), int((r.get("player_slot") or 0) < 128), r.get("hero_id"))
                for r in rows if r.get("account_id")]
        conn.executemany("INSERT OR IGNORE INTO map_players (match_id, account_id, is_radiant, hero_id) VALUES (?, ?, ?, ?)", recs)
        conn.commit()
        done += len({r[0] for r in recs})
        log(f"  составы: {done} из {len(todo)} карт")
        sleep(pause)
    return done


def lineups(conn: sqlite3.Connection) -> dict[int, tuple[frozenset, frozenset]]:
    """match_id -> (игроки radiant, игроки dire)."""
    ensure_schema(conn)
    out: dict[int, tuple[set, set]] = {}
    for mid, acc, rad in conn.execute("SELECT match_id, account_id, is_radiant FROM map_players"):
        r, d = out.setdefault(mid, (set(), set()))
        (r if rad else d).add(acc)
    return {k: (frozenset(r), frozenset(d)) for k, (r, d) in out.items()}


def picks(conn: sqlite3.Connection) -> dict[int, tuple[list, list]]:
    """match_id -> ([(игрок, герой)] radiant, [(игрок, герой)] dire)."""
    ensure_schema(conn)
    out: dict[int, tuple[list, list]] = {}
    for mid, acc, rad, hero in conn.execute("SELECT match_id, account_id, is_radiant, hero_id FROM map_players ORDER BY match_id"):
        r, d = out.setdefault(mid, ([], []))
        (r if rad else d).append((acc, hero))
    return out
