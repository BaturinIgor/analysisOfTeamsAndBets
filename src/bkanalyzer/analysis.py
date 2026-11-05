"""Анализ матчей из линии: наша вероятность (рейтинг Elo) против коэффициентов Oddin.

Используется Telegram-ботом: /today, /live, /match, /value.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from . import rating

STOP_WORDS = {"team", "esports", "esport", "gaming", "club", "the", "e-sports", "gg"}


# синонимы из справочника (bk alias): нормализованное название в линии → в истории; заполняет aliases.load
ALIASES: dict[str, str] = {}


def normalize_raw(name: str) -> str:
    """«Team Spirit» → «spirit», «Tundra Esports» → «tundra», «PARIVISION» → «parivision»."""
    words = re.findall(r"[a-z0-9а-яё]+", (name or "").casefold())
    core = [w for w in words if w not in STOP_WORDS]
    return "".join(core or words)


def normalize(name: str) -> str:
    """Нормализованное название с учётом справочника синонимов."""
    key = normalize_raw(name)
    return ALIASES.get(key, key)


def match_team(model, name: str) -> tuple[int, rating.TeamState] | None:
    """Ищет команду из линии в истории OpenDota по нормализованному названию."""
    key = normalize(name)
    if not key:
        return None
    exact = [(tid, st) for tid, st in model.teams.items() if normalize(st.name) == key]
    if exact:
        return max(exact, key=lambda x: x[1].games)
    if len(key) < 4:
        return None
    partial = [(tid, st) for tid, st in model.teams.items() if len(normalize(st.name)) >= 4 and (key in normalize(st.name) or normalize(st.name) in key)]
    return max(partial, key=lambda x: x[1].games) if partial else None


DRAW_NAMES = {"draw", "x", "tie", "ничья", "ничья (x)"}


def is_draw(outcome: str) -> bool:
    return (outcome or "").strip().casefold() in DRAW_NAMES


def series_from_score(p: float, best_of: int, won: int = 0, lost: int = 0) -> float:
    """Вероятность выиграть серию при текущем счёте по картам (карта в игре считается неначатой).

    В Bo2 «выиграть серию» — это 2:0; ничья 1:1 считается отдельно (draw_from_score).
    """
    if best_of == 1:
        return p if won + lost == 0 else float(won > lost)
    if best_of == 2:
        return 0.0 if lost else (p * p if won == 0 else p)
    need = best_of // 2 + 1

    @lru_cache(None)
    def f(a: int, b: int) -> float:
        if a >= need:
            return 1.0
        if b >= need:
            return 0.0
        return p * f(a + 1, b) + (1 - p) * f(a, b + 1)

    return f(won, lost)


def draw_from_score(p: float, won: int = 0, lost: int = 0) -> float:
    """Вероятность ничьей 1:1 в Bo2 при текущем счёте."""
    if won + lost >= 2:
        return float(won == lost == 1)
    if won == 1:
        return 1 - p
    if lost == 1:
        return p
    return 2 * p * (1 - p)


def format_hints(conn: sqlite3.Connection, now: int, days: int = 21) -> dict[str, int]:
    """Нормализованное название турнира → самый частый формат его серий за последние недели (OpenDota)."""
    from .sim import BEST_OF

    try:
        rows = conn.execute(
            """SELECT league_name, series_type, COUNT(DISTINCT series_id) FROM pro_maps
               WHERE start_time >= ? AND league_name IS NOT NULL AND series_id IS NOT NULL
               GROUP BY league_name, series_type""", (now - days * 86400,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    best: dict[str, tuple[int, int]] = {}
    for name, st, n in rows:
        key = normalize_raw(name)
        if key and (key not in best or n > best[key][1]):
            best[key] = (BEST_OF.get(st, 3), n)
    return {k: v[0] for k, v in best.items()}


def tournament_format(hints: dict[str, int], tournament: str | None) -> int | None:
    key = normalize_raw(tournament or "")
    if not key or not hints:
        return None
    if key in hints:
        return hints[key]
    close = [k for k in hints if len(k) >= 6 and (k in key or key in k)]
    return hints[max(close, key=len)] if close else None


@dataclass
class Quote:
    market: str
    outcome: str
    price: float
    fair_prob: float | None
    taken_at: str


@dataclass
class BoardMatch:
    fixture_id: str
    team1: str
    team2: str
    start_time: str | None
    tournament: str | None
    score: str | None
    phase: str
    quotes: dict[str, dict[str, Quote]] = field(default_factory=dict)  # market -> outcome -> quote
    fmt_hint: int | None = None  # формат по прошлым сериям этого турнира в OpenDota

    @property
    def draw_outcome(self) -> str | None:
        """Название исхода «ничья» на победу в матче, если букмекер его даёт (это Bo2)."""
        return next((o for o in self.quotes.get("match_winner", {}) if is_draw(o)), None)

    @property
    def best_of(self) -> int:
        if self.draw_outcome:
            return 2
        maps = [int(m.split("_")[1]) for m in self.quotes if m.startswith("map_")]
        top = max(maps, default=0)
        if top >= 4:
            return 5
        if top == 3 and self.fmt_hint in (1, 2):
            return 3  # третья карта бывает только в Bo3+
        return self.fmt_hint or 3

    @property
    def format_known(self) -> bool:
        return bool(self.draw_outcome or self.fmt_hint or any(m in self.quotes for m in ("map_3_winner", "map_4_winner")))

    @property
    def maps_score(self) -> tuple[int, int]:
        try:
            a, b = (int(x) for x in (self.score or "0:0").split(":"))
            return a, b
        except ValueError:
            return 0, 0

    def price(self, market: str, team: str) -> Quote | None:
        return self.quotes.get(market, {}).get(team)


def load_board(conn: sqlite3.Connection, since_hours: float = 6, now: datetime | None = None) -> list[BoardMatch]:
    """Матчи OddiWire с последними коэффициентами; только те, что обновлялись за since_hours."""
    now = now or datetime.now(timezone.utc)
    cutoff = datetime.fromtimestamp(now.timestamp() - since_hours * 3600, timezone.utc).isoformat(timespec="seconds")
    conn.row_factory = None
    latest = conn.execute("SELECT MAX(updated_at) FROM bookmaker_matches WHERE source='oddiwire'").fetchone()[0]
    if not latest or latest < cutoff:
        return []
    # только матчи из последнего снимка: закончившиеся пропадают из ленты
    matches = conn.execute(
        """SELECT match_id, team1, team2, start_time, tournament_id, score, updated_at FROM bookmaker_matches
           WHERE source='oddiwire' AND updated_at = ? ORDER BY start_time""",
        (latest,),
    ).fetchall()
    out = []
    hints = format_hints(conn, int(now.timestamp()))
    for mid, t1, t2, start, tour, score, seen in matches:
        open_markets = {m for (m,) in conn.execute(
            "SELECT market FROM feed_markets WHERE source='oddiwire' AND fixture_id=? AND last_seen>=?", (mid, seen))}
        tracked = conn.execute("SELECT 1 FROM feed_markets WHERE source='oddiwire' AND fixture_id=? LIMIT 1", (mid,)).fetchone()
        rows = conn.execute(
            """SELECT market, outcome, price, fair_prob, taken_at, phase FROM odds_snapshots
               WHERE source='oddiwire' AND fixture_id=? ORDER BY id""",
            (mid,),
        ).fetchall()
        bm = BoardMatch(mid, t1, t2, start, tour, score, "prematch", fmt_hint=tournament_format(hints, tour))
        last_time = ""
        for market, outcome, price, fair, taken, phase in rows:
            bm.quotes.setdefault(market, {})[outcome] = Quote(market, outcome, price, fair, taken)
            if taken >= last_time:
                last_time, bm.phase = taken, phase or bm.phase
        # закрытые рынки (их не было в последнем снимке) не показываем
        if tracked:
            bm.quotes = {m: q for m, q in bm.quotes.items() if m in open_markets}
        # внутри рынка — только цены из его последнего изменения
        for market, outs in bm.quotes.items():
            newest = max(q.taken_at for q in outs.values())
            bm.quotes[market] = {k: q for k, q in outs.items() if q.taken_at == newest}
        out.append(bm)
    return out


@dataclass
class Pick:
    market: str
    team: str
    price: float
    prob: float
    book_prob: float | None

    @property
    def ev(self) -> float:
        return self.prob * self.price - 1


@dataclass
class Analysis:
    match: BoardMatch
    known: tuple[rating.TeamState | None, rating.TeamState | None]
    p_map: float | None = None
    p_series: float | None = None
    p_draw: float | None = None    # Bo2: ничья 1:1
    p_series2: float | None = None  # вероятность серии для team2 (в Bo2 не равна 1 − p_series)
    picks: list[Pick] = field(default_factory=list)
    factors: list[tuple[str, float, float]] = field(default_factory=list)


def analyze(bm: BoardMatch, model, now: int | None = None) -> Analysis:
    now = now or int(time.time())
    a, b = match_team(model, bm.team1), match_team(model, bm.team2)
    res = Analysis(bm, (a[1] if a else None, b[1] if b else None))
    if not (a and b) or a[0] == b[0]:
        return res
    p = model.predict(a[0], b[0], now)
    if hasattr(model, "explain"):
        res.factors = model.explain(a[0], b[0], now)
    won, lost = bm.maps_score
    res.p_map = p
    bo = bm.best_of
    res.p_series = series_from_score(p, bo, won, lost)
    res.p_series2 = series_from_score(1 - p, bo, lost, won)
    if bo == 2:
        res.p_draw = draw_from_score(p, won, lost)
    # в эфире текущая карта уже идёт — рейтинг не знает, что на ней происходит; берём следующую
    next_map = won + lost + (2 if bm.phase == "live" else 1)
    outcomes = [("match_winner", bm.team1, res.p_series), ("match_winner", bm.team2, res.p_series2)]
    if res.p_draw is not None and bm.draw_outcome:
        outcomes.append(("match_winner", bm.draw_outcome, res.p_draw))
    if bo > 1 or bm.phase != "live":
        outcomes += [(f"map_{next_map}_winner", bm.team1, p), (f"map_{next_map}_winner", bm.team2, 1 - p)]
    for market, team, team_prob in outcomes:
        q = bm.price(market, team)
        if q:
            res.picks.append(Pick(market, team, q.price, team_prob, q.fair_prob))
    return res


@dataclass
class Thresholds:
    min_ev: float = 0.05
    min_prob: float = 0.0
    min_odds: float = 1.0
    max_gap: float = 0.15  # насколько наша вероятность может расходиться с рынком (без маржи)

    def suspicious(self, pick: Pick) -> bool:
        """Модель слишком далеко от рынка: скорее ошибка модели, чем ошибка букмекера."""
        return pick.book_prob is not None and abs(pick.prob - pick.book_prob) > self.max_gap

    def ok(self, pick: Pick) -> bool:
        return (pick.ev >= self.min_ev and pick.prob >= self.min_prob and pick.price >= self.min_odds
                and not self.suspicious(pick))


def market_label(market: str) -> str:
    if market == "match_winner":
        return "матч"
    m = re.match(r"map_(\d+)_winner", market)
    return f"карта {m.group(1)}" if m else market
