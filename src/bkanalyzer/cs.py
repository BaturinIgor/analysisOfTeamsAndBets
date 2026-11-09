"""История CS2 и LoL из PandaScore в SQLite (таблицы cs_maps и lol_maps).

Серию со счётом 2:1 раскладываем на 3 карты (порядок внутри серии неизвестен: победы и
поражения чередуем). Таблицы повторяют колонки pro_maps, поэтому Elo, модель,
формат турнира и расчёт бумажных ставок работают для CS2 и LoL тем же кодом.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from .games import CS2, Game

SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    match_id INTEGER PRIMARY KEY,    -- id серии PandaScore * 10 + номер карты
    start_time INTEGER NOT NULL,
    league_name TEXT,
    tier TEXT,                       -- s, a, b, c, d (PandaScore)
    series_id INTEGER,               -- id серии PandaScore
    series_type INTEGER,             -- как в OpenDota: 0 Bo1, 1 Bo3, 2 Bo5, 3 Bo2
    radiant_team_id INTEGER,         -- «radiant» = команда 1 серии
    radiant_name TEXT,
    dire_team_id INTEGER,
    dire_name TEXT,
    radiant_win INTEGER
);
CREATE INDEX IF NOT EXISTS ix_{table}_time ON {table} (start_time);
CREATE TABLE IF NOT EXISTS cs_sync (       -- до какой даты история уже загружена целиком (ключи всех игр PandaScore)
    key TEXT PRIMARY KEY,
    value INTEGER
);
"""

SERIES_TYPE = {1: 0, 2: 3, 3: 1, 5: 2}


def ensure_schema(conn: sqlite3.Connection, game: Game = CS2) -> None:
    conn.executescript(SCHEMA.format(table=game.maps_table))


def _sync_key(game: Game) -> str:
    # v3: метки прошлых версий писались при неверной сортировке запроса — их не учитываем
    return "backfilled_since_v3" if game is CS2 else f"{game.key}_backfilled_since_v3"


def _fetch(ps, game: Game):
    """Метод клиента PandaScore для игры: cs_past или lol_past."""
    return getattr(ps, "cs_past" if game is CS2 else f"{game.key}_past")


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _date(m: dict) -> str | None:
    """Когда сыграна серия: begin_at бывает пустым даже у сыгранных — берём запасные поля."""
    return m.get("begin_at") or m.get("scheduled_at") or m.get("original_scheduled_at") or m.get("end_at")


def series_maps(m: dict) -> list[tuple]:
    """Строки cs_maps из одной серии PandaScore; пусто — если серия не сыграна (неявка, нет счёта)."""
    if m.get("status") not in (None, "finished") or m.get("forfeit"):
        return []
    opps = [(o.get("opponent") or {}) for o in m.get("opponents") or []]
    if len(opps) != 2 or not all(o.get("id") for o in opps):
        return []
    score = {r.get("team_id"): r.get("score") or 0 for r in m.get("results") or []}
    a, b = opps
    wa, wb = int(score.get(a["id"], 0)), int(score.get(b["id"], 0))
    start = _ts(_date(m))
    if not start or wa + wb == 0 or wa + wb > 7:
        return []
    tour = m.get("tournament") or {}
    league = " ".join(x for x in ((m.get("league") or {}).get("name"), (m.get("serie") or {}).get("full_name")) if x)
    bo = m.get("number_of_games") or (wa + wb)
    seq: list[int] = []
    left = {1: wa, 0: wb}
    turn = 1 if wa >= wb else 0
    while left[0] + left[1]:  # чередуем, последнюю карту выигрывает победитель серии
        if left[turn]:
            seq.append(turn)
            left[turn] -= 1
        turn = 1 - turn
    if wa != wb and seq and seq[-1] != int(wa > wb):
        seq.reverse()
    rows = []
    for i, win in enumerate(seq, 1):
        rows.append((int(m["id"]) * 10 + i, start + (i - 1) * 3600, league or None, tour.get("tier"), int(m["id"]),
                     SERIES_TYPE.get(bo, 1), a["id"], a.get("name"), b["id"], b.get("name"), win))
    return rows


def sync(conn: sqlite3.Connection, ps, days: int = 365, *, now: float | None = None, max_pages: int = 400,
         sleep=time.sleep, pause: float = 0.5, log=print, game: Game = CS2) -> int:
    """Загружает сыгранные серии игры за days дней (от новых к старым), пока не дойдёт до уже загруженных."""
    ensure_schema(conn, game)
    since = (now or time.time()) - days * 86400
    key = _sync_key(game)
    fetch = _fetch(ps, game)
    row = conn.execute("SELECT value FROM cs_sync WHERE key=?", (key,)).fetchone()
    covered = row is not None and row[0] <= since
    saved = seen = kept = 0
    complete = False
    for page in range(1, max_pages + 1):
        items = fetch(page=page)
        if not items:
            complete = True  # старше данных нет
            break
        seen += len(items)
        rows = [r for m in items for r in series_maps(m) if r[1] >= since]
        kept += len({r[4] for r in rows})
        before = conn.total_changes
        conn.executemany(f"INSERT OR IGNORE INTO {game.maps_table} VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        added = conn.total_changes - before
        saved += added
        # у отменённых серий даты может не быть — по ним границу окна не определяем
        stamps = [t for t in (_ts(_date(m)) for m in items) if t]
        if stamps and min(stamps) < since:
            complete = True
            break
        if covered and added == 0 and rows:
            break  # дошли до уже загруженного
        if page % 10 == 0:
            log(f"  {game.label}: страниц {page}, серий {seen} (сыграно {kept}), новых карт {saved}")
        sleep(pause)
    if complete and not covered:
        conn.execute("INSERT OR REPLACE INTO cs_sync VALUES (?, ?)", (key, int(since)))
        conn.commit()
    log(f"  {game.label}: страниц {page if items else page - 1}, серий {seen}, из них сыграно со счётом {kept}")
    return saved


def maps(conn: sqlite3.Connection, since: int = 0, game: Game = CS2) -> list[dict]:
    ensure_schema(conn, game)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {game.maps_table} WHERE start_time >= ? ORDER BY start_time, match_id", (since,))]
    conn.row_factory = None
    return rows
