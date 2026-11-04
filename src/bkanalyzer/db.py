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
CREATE TABLE IF NOT EXISTS bookmaker_matches (
    source TEXT NOT NULL,
    match_id TEXT NOT NULL,
    feed_id TEXT,
    tournament_id TEXT,
    start_time TEXT,
    team1 TEXT,
    team2 TEXT,
    format TEXT,
    score TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, match_id)
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_matches(conn: sqlite3.Connection, matches: list[dict], *, source: str, updated_at: str | None = None) -> None:
    updated_at = updated_at or _now()
    conn.executemany(
        """INSERT INTO bookmaker_matches (source, match_id, feed_id, tournament_id, start_time, team1, team2, format, score, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source, match_id) DO UPDATE SET
             start_time=excluded.start_time, team1=excluded.team1, team2=excluded.team2,
             format=excluded.format, score=COALESCE(excluded.score, score), updated_at=excluded.updated_at""",
        [
            (source, str(m["match_id"]), m.get("feed_id"), str(m.get("tournament_id")), m.get("start_time"),
             m.get("team1"), m.get("team2"), m.get("format"), m.get("score"), updated_at)
            for m in matches
        ],
    )
    conn.commit()


def save_price_changes(conn: sqlite3.Connection, rows: list[dict], *, source: str, taken_at: str | None = None) -> int:
    """Сохраняет только изменившиеся коэффициенты (по fixture_id, рынку и исходу)."""
    taken_at = taken_at or _now()
    new = []
    for r in rows:
        last = conn.execute(
            """SELECT price FROM odds_snapshots WHERE source=? AND bookmaker=? AND fixture_id=? AND market=? AND outcome=?
               ORDER BY id DESC LIMIT 1""",
            (source, r["bookmaker"], r["fixture_id"], r["market"], r["outcome"]),
        ).fetchone()
        if last is None or last[0] != r["price"]:
            new.append((taken_at, source, r["bookmaker"], r["fixture_id"], r["market"], r["outcome"], r["price"]))
    conn.executemany(
        "INSERT INTO odds_snapshots (taken_at, source, bookmaker, fixture_id, market, outcome, price) VALUES (?, ?, ?, ?, ?, ?, ?)",
        new,
    )
    conn.commit()
    return len(new)
