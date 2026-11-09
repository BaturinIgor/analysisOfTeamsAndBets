"""Теннис: история матчей ATP/WTA с tennis-data.co.uk, линия Pinnacle из PinnWire.

История: бесплатные таблицы tennis-data.co.uk (по году на тур: ATP — /2026/2026.xlsx,
WTA — /2026w/2026.xlsx), обновляются примерно раз в неделю. В них покрытие, раунд,
счёт по сетам, отказы и снятия. Каждый матч — одна строка tennis_maps с теми же
колонками, что у pro_maps (матч = «карта», серия Bo1), поэтому рейтинг, модель и расчёт
бумажных ставок работают тем же кодом. Покрытие идёт в модель вместо «патча»:
признак «форма на покрытии» (винрейт игрока на этом покрытии).

Имена: в истории «Sinner J.», у Pinnacle «Jannik Sinner». При сборе линии имя из линии
переводим в имя из истории (фамилия + инициалы), дальше всё сравнивается как обычно.
"""

from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone

from . import db
from .http import SourceError
from .sources import pinnwire as pw_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS tennis_maps (
    match_id INTEGER PRIMARY KEY,    -- хэш тура, даты и игроков
    start_time INTEGER NOT NULL,     -- день матча, 12:00 UTC (точного времени в таблицах нет)
    league_name TEXT,                -- турнир и город
    tier TEXT,                       -- ATP/WTA и категория
    series_id INTEGER,
    series_type INTEGER,             -- всегда 0: матч целиком = одна «карта»
    radiant_team_id INTEGER,
    radiant_name TEXT,
    dire_team_id INTEGER,
    dire_name TEXT,
    radiant_win INTEGER,
    surface TEXT,                    -- Hard, Clay, Grass, Carpet
    comment TEXT                     -- Completed, Retired, Walkover...
);
CREATE INDEX IF NOT EXISTS ix_tennis_maps_time ON tennis_maps (start_time);
"""

BASE_URL = "http://www.tennis-data.co.uk"
SETTLE_DAYS = 10  # результаты приходят раз в неделю — ставки ждут дольше, чем в киберспорте


def ensure_schema(conn: sqlite3.Connection, game=None) -> None:
    conn.executescript(SCHEMA)


def key(name: str | None) -> str:
    """«Cerúndolo J.M.» → «cerundolojm»: без диакритики, только буквы."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z]", "", s)


def _id(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest()[:12], 16)


def urls(year: int) -> list[tuple[str, str]]:
    return [("ATP", f"{BASE_URL}/{year}/{year}.xlsx"), ("WTA", f"{BASE_URL}/{year}w/{year}.xlsx")]


def _ts(value) -> int | None:
    if isinstance(value, datetime):
        d = value
    elif isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                d = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    else:
        return None
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp())


def parse_rows(tour: str, header: list, rows) -> list[tuple]:
    """Строки tennis_maps из таблицы tennis-data (заголовок + строки значений)."""
    col = {str(h).strip().casefold(): i for i, h in enumerate(header) if h is not None}

    def get(r, *names):
        for n in names:
            i = col.get(n)
            if i is not None and i < len(r) and r[i] not in (None, ""):
                return r[i]
        return None

    out = []
    for r in rows:
        w, l = get(r, "winner"), get(r, "loser")
        start = _ts(get(r, "date"))
        if not w or not l or not start:
            continue
        w, l = str(w).strip(), str(l).strip()
        tour_name = " ".join(str(x) for x in (get(r, "tournament"), get(r, "location")) if x)
        tier = " ".join(str(x) for x in (tour, get(r, "series", "tier")) if x)
        mid = _id(f"{tour}|{start}|{key(w)}|{key(l)}")
        swap = mid % 2 == 1  # порядок игроков случайный: иначе «первый» всегда побеждает
        a, b = (l, w) if swap else (w, l)
        out.append((mid, start, tour_name or None, tier, mid, 0, _id(key(a)), a, _id(key(b)), b, 0 if swap else 1,
                    str(get(r, "surface") or "") or None, str(get(r, "comment") or "Completed")))
    return out


def read_xlsx(data: bytes) -> tuple[list, list]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = list(next(it, []) or [])
    return header, list(it)


def sync(conn: sqlite3.Connection, client, years: int = 3, *, now: float | None = None, log=print, fetch=None) -> int:
    """Загружает таблицы ATP и WTA за последние years лет (текущий год — всегда заново). Возвращает новых матчей."""
    ensure_schema(conn)
    year = datetime.fromtimestamp(now or time.time(), timezone.utc).year
    have = {y for (y,) in conn.execute(
        "SELECT DISTINCT CAST(strftime('%Y', start_time, 'unixepoch') AS INTEGER) FROM tennis_maps")}
    fetch = fetch or (lambda url: _download(client, url))
    added = 0
    for y in range(year - years + 1, year + 1):
        if y in have and y < year:
            continue  # прошлые годы не меняются
        for tour, url in urls(y):
            try:
                header, rows = read_xlsx(fetch(url))
            except SourceError as exc:
                log(f"  теннис {tour} {y}: {exc}")
                continue
            parsed = parse_rows(tour, header, rows)
            before, = conn.execute("SELECT COUNT(*) FROM tennis_maps").fetchone()
            conn.executemany("INSERT OR REPLACE INTO tennis_maps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", parsed)
            conn.commit()
            added += conn.execute("SELECT COUNT(*) FROM tennis_maps").fetchone()[0] - before
            log(f"  теннис {tour} {y}: матчей в таблице {len(parsed)}")
    return added


def _download(client, url: str) -> bytes:
    import httpx

    try:
        resp = client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"{url}: сеть недоступна ({exc})") from exc
    if resp.status_code == 404:
        raise SourceError(f"{url}: файла ещё нет (404)")
    if resp.status_code >= 400:
        raise SourceError(f"{url}: HTTP {resp.status_code}")
    return resp.content


def maps(conn: sqlite3.Connection, since: int = 0, game=None) -> list[dict]:
    """Сыгранные до конца матчи (без отказов и снятий) по времени; покрытие — в поле patch для модели."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM tennis_maps WHERE start_time >= ? AND (comment IS NULL OR comment='Completed') "
        "ORDER BY start_time, match_id", (since,))]
    conn.row_factory = None
    for r in rows:
        r["patch"] = r["surface"]
    return rows


# --- имена ---------------------------------------------------------------------

def name_index(conn: sqlite3.Connection, since_days: int = 730) -> dict[str, str]:
    """Ключ имени → имя из истории (если ключ у двух игроков — берём того, у кого больше матчей)."""
    ensure_schema(conn)
    since = int(time.time()) - since_days * 86400
    counts = Counter()
    for a, b in conn.execute("SELECT radiant_name, dire_name FROM tennis_maps WHERE start_time >= ?", (since,)):
        counts[a] += 1
        counts[b] += 1
    out: dict[str, str] = {}
    for name, _ in counts.most_common():
        out.setdefault(key(name), name)
    return out


def history_name(full: str, index: dict[str, str]) -> str:
    """«Jannik Sinner» → «Sinner J.», «Alex de Minaur» → «De Minaur A.»; не нашли — имя как есть."""
    tokens = [t for t in re.split(r"[\s]+", full.strip()) if t]
    if key(full) in index:
        return index[key(full)]
    cands = []
    for i in range(1, len(tokens)):
        for first, last in ((tokens[:i], tokens[i:]), (tokens[i:], tokens[:i])):
            surname = key(" ".join(last))
            initials = "".join(key(f)[:1] for f in first)
            cands.append((surname + initials, surname + initials[:1]))
    for exact, _ in cands:
        if exact in index:
            return index[exact]
    for _, prefix in cands:  # «Zhang Zh.» и подобные: фамилия + первая буква имени, если такой игрок один
        found = [k for k in index if k.startswith(prefix)]
        if len(found) == 1:
            return index[found[0]]
    return full


# --- покрытие ------------------------------------------------------------------

def surface_hints(conn: sqlite3.Connection, now: int, days: int = 21) -> tuple[dict[str, str], str | None]:
    """(город или турнир → покрытие за последние недели, самое частое покрытие сейчас)."""
    ensure_schema(conn)
    rows = conn.execute("SELECT league_name, surface FROM tennis_maps WHERE start_time >= ? AND surface IS NOT NULL",
                        (now - days * 86400,)).fetchall()
    places: dict[str, Counter] = {}
    total = Counter()
    for league, surface in rows:
        total[surface] += 1
        for word in re.findall(r"[a-z]{4,}", key_words(league)):
            places.setdefault(word, Counter())[surface] += 1
    return {w: c.most_common(1)[0][0] for w, c in places.items()}, (total.most_common(1)[0][0] if total else None)


def key_words(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z]+", " ", s)


GENERIC = {"open", "masters", "challenger", "finals", "cup", "international", "championships", "atp", "wta", "women", "men",
           "qualifiers", "qualifying", "tour", "grand", "slam"}


def surface_for(tournament: str | None, hints: tuple[dict[str, str], str | None]) -> str | None:
    places, default = hints
    for word in re.findall(r"[a-z]{4,}", key_words(tournament)):
        if word not in GENERIC and word in places:
            return places[word]
    return default


# --- линия ---------------------------------------------------------------------

def collect_once(conn: sqlite3.Connection, pw, game, log=print) -> int:
    """Один снимок линии Pinnacle по теннису (1 запрос). Возвращает новых/изменившихся цен."""
    payload = pw.fixtures(pw_mod.TENNIS)
    index = name_index(conn)
    matches, rows = [], []
    for ev in pw_mod.events(payload):
        if not pw_mod.is_singles(ev) or not ev.get("event_id") or ev.get("event_type") not in (None, "prematch"):
            continue
        prices = pw_mod.money_line(ev)
        if not prices:
            continue
        p1, p2 = history_name(str(ev["home"]), index), history_name(str(ev["away"]), index)
        if p1 == p2:
            continue
        fid, start = str(ev["event_id"]), ev.get("starts") or ev.get("start_ts")
        fair = pw_mod._no_vig(*prices)
        matches.append({"match_id": fid, "tournament_id": ev.get("league_name"), "start_time": start, "team1": p1, "team2": p2})
        for name, price, prob in ((p1, prices[0], fair[0]), (p2, prices[1], fair[1])):
            rows.append({"bookmaker": pw_mod.BOOKMAKER, "fixture_id": fid, "start_time": start, "participant1": p1,
                         "participant2": p2, "market": "match_winner", "outcome": name, "price": price,
                         "phase": "prematch", "fair_prob": prob})
    stamp = db._now()
    db.upsert_matches(conn, matches, source=game.source, updated_at=stamp)
    saved = db.save_feed_rows(conn, rows, source=game.source, taken_at=stamp)
    known = sum(1 for m in matches if key(m["team1"]) in index and key(m["team2"]) in index)
    log(f"  {game.label}: матчей {len(matches)} (оба игрока найдены в истории: {known}), новых/изменившихся цен {saved}")
    return saved
