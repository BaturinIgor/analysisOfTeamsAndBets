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


def normalize(name: str) -> str:
    """«Team Spirit» → «spirit», «Tundra Esports» → «tundra», «PARIVISION» → «parivision»."""
    words = re.findall(r"[a-z0-9а-яё]+", (name or "").casefold())
    core = [w for w in words if w not in STOP_WORDS]
    return "".join(core or words)


def match_team(model: rating.EloModel, name: str) -> tuple[int, rating.TeamState] | None:
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


def series_from_score(p: float, best_of: int, won: int = 0, lost: int = 0) -> float:
    """Вероятность выиграть серию при текущем счёте по картам (карта в игре считается неначатой)."""
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

    @property
    def best_of(self) -> int:
        maps = [int(m.split("_")[1]) for m in self.quotes if m.startswith("map_")]
        top = max(maps, default=0)
        return 5 if top >= 4 else 3 if top >= 2 else 3

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
    matches = conn.execute(
        """SELECT match_id, team1, team2, start_time, tournament_id, score FROM bookmaker_matches
           WHERE source='oddiwire' AND updated_at >= ? ORDER BY start_time""",
        (cutoff,),
    ).fetchall()
    out = []
    for mid, t1, t2, start, tour, score in matches:
        rows = conn.execute(
            """SELECT market, outcome, price, fair_prob, taken_at, phase FROM odds_snapshots
               WHERE source='oddiwire' AND fixture_id=? ORDER BY id""",
            (mid,),
        ).fetchall()
        bm = BoardMatch(mid, t1, t2, start, tour, score, "prematch")
        last_time = ""
        for market, outcome, price, fair, taken, phase in rows:
            bm.quotes.setdefault(market, {})[outcome] = Quote(market, outcome, price, fair, taken)
            if taken >= last_time:
                last_time, bm.phase = taken, phase or bm.phase
        # рынок мог закрыться: оставляем только цены из последнего снимка этого рынка
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
    picks: list[Pick] = field(default_factory=list)


def analyze(bm: BoardMatch, model: rating.EloModel, now: int | None = None) -> Analysis:
    now = now or int(time.time())
    a, b = match_team(model, bm.team1), match_team(model, bm.team2)
    res = Analysis(bm, (a[1] if a else None, b[1] if b else None))
    if not (a and b) or a[0] == b[0]:
        return res
    p = model.predict(a[0], b[0], now)
    won, lost = bm.maps_score
    res.p_map = p
    res.p_series = series_from_score(p, bm.best_of, won, lost)
    for market, prob in (("match_winner", res.p_series), (f"map_{won + lost + 1}_winner", p)):
        for team, team_prob in ((bm.team1, prob), (bm.team2, 1 - prob)):
            q = bm.price(market, team)
            if q:
                res.picks.append(Pick(market, team, q.price, team_prob, q.fair_prob))
    return res


@dataclass
class Thresholds:
    min_ev: float = 0.05
    min_prob: float = 0.0
    min_odds: float = 1.0

    def ok(self, pick: Pick) -> bool:
        return pick.ev >= self.min_ev and pick.prob >= self.min_prob and pick.price >= self.min_odds


def market_label(market: str) -> str:
    if market == "match_winner":
        return "матч"
    m = re.match(r"map_(\d+)_winner", market)
    return f"карта {m.group(1)}" if m else market
