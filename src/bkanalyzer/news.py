"""Риски перед ставкой (ТР-10): состав по нашим данным и новости за 7 дней.

1. Состав. По картам OpenDota знаем, кто играл за команду. «Основа» — игроки, сыгравшие
   хотя бы 40% карт команды за 60 дней. Если в последней карте (или в текущем драфте)
   есть игрок не из основы, а кого-то из основы нет — это стендин или замена.
2. Новости. Google News RSS (бесплатно, без ключа) по названию команды на русском и
   английском за 7 дней. Заголовки с «stand-in», «замена», «виза», «болезнь» и т. п.
   помечаются как риск. Ответы кэшируются на 6 часов.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

SCHEMA = """
CREATE TABLE IF NOT EXISTS pro_players (
    account_id INTEGER PRIMARY KEY,
    name TEXT,
    team_id INTEGER
);
CREATE TABLE IF NOT EXISTS news_cache (
    query TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    items TEXT NOT NULL
);
"""

CORE_SHARE = 0.4          # доля карт команды, чтобы считаться основой
CORE_WINDOW = 60 * 86400  # окно для основы
RECENT = 3 * 86400        # последняя карта не старше — иначе про текущий состав ничего не скажем
NEWS_TTL = 6 * 3600
NEWS_DAYS = 7

RISK_WORDS = {
    "стендин": ("stand-in", "standin", "стендин", "stand in"),
    "замена/состав": ("replace", "substitut", "roster", "bench", "kicked", "parts ways", "part ways", "leaves", "departs",
                      "transfer", "замен", "состав", "покинул", "покидает", "уход", "трансфер", "скамейк"),
    "виза": ("visa", "виз"),
    "болезнь": ("sick", "illness", " ill ", "injur", "hospital", "болезн", "заболел", "травм"),
    "скандал": ("ban ", "banned", "suspend", "match-fixing", "matchfixing", "дисквалиф", "договорн", "скандал"),
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# --- состав --------------------------------------------------------------------

def sync_pro_players(conn: sqlite3.Connection, od) -> int:
    """Имена про-игроков из OpenDota /proPlayers (один запрос)."""
    ensure_schema(conn)
    rows = [(p["account_id"], p.get("name") or p.get("personaname"), p.get("team_id"))
            for p in od.pro_players() or [] if p.get("account_id")]
    conn.executemany("INSERT OR REPLACE INTO pro_players VALUES (?,?,?)", rows)
    conn.commit()
    return len(rows)


def player_name(conn: sqlite3.Connection, account_id: int) -> str:
    try:
        row = conn.execute("SELECT name FROM pro_players WHERE account_id=?", (account_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    return row[0] if row and row[0] else f"id {account_id}"


def team_maps(conn: sqlite3.Connection, team_id: int, since: int) -> list[tuple[int, int, frozenset]]:
    """[(match_id, start_time, состав команды)] по возрастанию времени — только карты с известным составом."""
    conn.row_factory = None
    rows = conn.execute(
        """SELECT m.match_id, m.start_time, p.account_id FROM pro_maps m
           JOIN map_players p ON p.match_id = m.match_id
             AND p.is_radiant = CASE WHEN m.radiant_team_id = ? THEN 1 ELSE 0 END
           WHERE (m.radiant_team_id = ? OR m.dire_team_id = ?) AND m.start_time >= ?
           ORDER BY m.start_time, m.match_id""",
        (team_id, team_id, team_id, since),
    ).fetchall()
    out: dict[int, tuple[int, set]] = {}
    for mid, t, acc in rows:
        out.setdefault(mid, (t, set()))[1].add(acc)
    return [(mid, t, frozenset(ps)) for mid, (t, ps) in out.items()]


@dataclass
class RosterRisk:
    newcomers: list[str] = field(default_factory=list)   # играют, но не из основы
    missing: list[str] = field(default_factory=list)     # основа, которой нет
    source: str = ""                                     # «в этой карте» / «в последней карте 04.11»

    def __bool__(self) -> bool:
        return bool(self.newcomers)

    def text(self, team: str) -> str:
        s = f"{team}: {self.source} играет {', '.join(self.newcomers)}"
        if self.missing:
            s += f" вместо {', '.join(self.missing)}"
        return s + " (не основной состав)"


def roster_risk(conn: sqlite3.Connection, team_id: int, now: int, lineup: frozenset | None = None) -> RosterRisk | None:
    """Сравнивает текущий состав (из драфта или последней карты) с основой за 60 дней."""
    try:
        maps = team_maps(conn, team_id, now - CORE_WINDOW)
    except sqlite3.OperationalError:
        return None
    if len(maps) < 5:
        return None
    if lineup is None:
        mid, t, lineup = maps[-1]
        if now - t > RECENT:
            return None
        source = "в последней карте " + time.strftime("%d.%m", time.gmtime(t))
        history = maps[:-1]
    else:
        source = "в этой карте"
        history = maps
    counts: dict[int, int] = {}
    for _, _, ps in history:
        for p in ps:
            counts[p] = counts.get(p, 0) + 1
    core = {p for p, n in counts.items() if n >= CORE_SHARE * len(history)}
    if len(core) < 4:
        return None  # основа не устоялась — сравнивать не с чем
    new = sorted(lineup - core)
    gone = sorted(core - lineup)
    return RosterRisk([player_name(conn, p) for p in new], [player_name(conn, p) for p in gone], source)


# --- новости -------------------------------------------------------------------

@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: int  # unix
    risks: list[str] = field(default_factory=list)


def risk_kinds(title: str) -> list[str]:
    t = f" {title.casefold()} "
    return [kind for kind, words in RISK_WORDS.items() if any(w in t for w in words)]


def parse_rss(text: str, now: int, days: int = NEWS_DAYS) -> list[NewsItem]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        try:
            ts = int(parsedate_to_datetime(item.findtext("pubDate") or "").timestamp())
        except (TypeError, ValueError):
            ts = 0
        if ts and now - ts > days * 86400:
            continue
        out.append(NewsItem(title, (item.findtext("link") or "").strip(), source, ts, risk_kinds(title)))
    return out


FEEDS = (("ru", "RU", "RU:ru"), ("en", "US", "US:en"))


def _query(team: str) -> str:
    return f'"{team}" dota'


def fetch_news(client: httpx.Client, team: str, now: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for hl, gl, ceid in FEEDS:
        try:
            resp = client.get("https://news.google.com/rss/search",
                              params={"q": f"{_query(team)} when:{NEWS_DAYS}d", "hl": hl, "gl": gl, "ceid": ceid})
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            items += parse_rss(resp.text, now)
    # одна новость в двух лентах — оставляем одну
    seen, uniq = set(), []
    for it in sorted(items, key=lambda i: -i.published):
        key = re.sub(r"\W+", "", it.title.casefold())[:60]
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    return uniq


def team_news(conn: sqlite3.Connection, client: httpx.Client | None, team: str, now: int | None = None) -> list[NewsItem]:
    """Новости за 7 дней с кэшем на 6 часов. Без клиента — только из кэша."""
    ensure_schema(conn)
    now = now or int(time.time())
    q = _query(team).casefold()
    row = conn.execute("SELECT fetched_at, items FROM news_cache WHERE query=?", (q,)).fetchone()
    if row and (now - row[0] < NEWS_TTL or client is None):
        return [NewsItem(**d) for d in json.loads(row[1])]
    if client is None:
        return []
    items = fetch_news(client, team, now)
    conn.execute("INSERT OR REPLACE INTO news_cache VALUES (?,?,?)",
                 (q, now, json.dumps([it.__dict__ for it in items], ensure_ascii=False)))
    conn.commit()
    return items


def news_block(team: str, items: list[NewsItem], limit: int = 5) -> list[str]:
    if not items:
        return [f"{team}: новостей за {NEWS_DAYS} дней не нашёл"]
    risky = [it for it in items if it.risks]
    shown = (risky + [it for it in items if not it.risks])[:limit]
    lines = [f"{team}: новостей {len(items)}" + (f", с флагом риска {len(risky)}" if risky else "")]
    for it in shown:
        flag = f"⚠️ {', '.join(it.risks)}: " if it.risks else "• "
        when = time.strftime("%d.%m", time.gmtime(it.published)) if it.published else ""
        lines.append(f"  {flag}{it.title} ({it.source}{', ' + when if when else ''})\n    {it.link}")
    return lines
