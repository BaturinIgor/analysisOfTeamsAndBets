"""SQLite: снимки коэффициентов с меткой времени (основа истории линии)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY,
    taken_at TEXT NOT NULL,          -- UTC ISO-8601
    source TEXT NOT NULL,            -- oddspapi | betboom
    bookmaker TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    start_time TEXT,
    participant1 TEXT,
    participant2 TEXT,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_odds_fixture ON odds_snapshots (fixture_id, bookmaker, market, outcome, taken_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def save_odds(conn: sqlite3.Connection, rows: list[dict], *, source: str, fixture: dict, taken_at: str | None = None) -> int:
    taken_at = taken_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO odds_snapshots
           (taken_at, source, bookmaker, fixture_id, start_time, participant1, participant2, market, outcome, price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                taken_at,
                source,
                r["bookmaker"],
                str(fixture.get("fixtureId") or fixture.get("id")),
                fixture.get("startTime"),
                fixture.get("participant1Name"),
                fixture.get("participant2Name"),
                r["market"],
                r["outcome"],
                r["price"],
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)
