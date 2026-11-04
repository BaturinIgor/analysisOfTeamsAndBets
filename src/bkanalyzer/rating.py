"""Рейтинг команд Elo по картам и вероятность серии.

Это базовая модель этапа 1: она считает силу команды только по результатам карт.
Форма, патч, состав и драфт добавятся признаками поверх неё (ТР-6…ТР-9).

Проверка честная: прогноз на каждую карту делается до того, как рейтинг узнал её исход.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

BASE = 1500.0
DAY = 86400


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(ra - rb) / 400.0))


def series_probabilities(p: float, best_of: int) -> dict[str, float]:
    """Из вероятности выиграть карту p — вероятности исходов серии для команды 1."""
    q = 1 - p
    if best_of == 1:
        return {"win": p, "lose": q}
    if best_of == 2:
        return {"win": p * p, "draw": 2 * p * q, "lose": q * q}
    need = best_of // 2 + 1
    win = sum(math.comb(need - 1 + k, k) * p**need * q**k for k in range(need))
    return {"win": win, "lose": 1 - win}


@dataclass
class TeamState:
    rating: float = BASE
    games: int = 0
    last_time: int | None = None
    name: str = ""


@dataclass
class EloModel:
    k_new: float = 40.0          # K для команд с малым числом карт
    k: float = 24.0
    new_games: int = 20
    half_life_days: float = 180.0  # без игр рейтинг наполовину возвращается к среднему за это время
    teams: dict[int, TeamState] = field(default_factory=dict)

    def _state(self, team_id: int, now: int, name: str = "") -> TeamState:
        st = self.teams.setdefault(team_id, TeamState())
        if st.last_time is not None and now > st.last_time:
            decay = 0.5 ** ((now - st.last_time) / DAY / self.half_life_days)
            st.rating = BASE + (st.rating - BASE) * decay
            st.last_time = now
        if name:
            st.name = name
        return st

    def rating(self, team_id: int, now: int) -> TeamState:
        return self._state(team_id, now)

    def predict(self, a: int, b: int, now: int) -> float:
        return expected(self._state(a, now).rating, self._state(b, now).rating)

    def update(self, winner: int, loser: int, now: int, winner_name: str = "", loser_name: str = "") -> float:
        """Обновляет рейтинги; возвращает вероятность победы победителя ДО обновления."""
        w, l = self._state(winner, now, winner_name), self._state(loser, now, loser_name)
        p = expected(w.rating, l.rating)
        kw = self.k_new if w.games < self.new_games else self.k
        kl = self.k_new if l.games < self.new_games else self.k
        w.rating += kw * (1 - p)
        l.rating -= kl * (1 - p)
        for st in (w, l):
            st.games += 1
            st.last_time = now
        return p


@dataclass
class Evaluation:
    n: int = 0
    brier: float = 0.0
    log_loss: float = 0.0
    accuracy: float = 0.0
    buckets: list[tuple[str, int, float, float]] = field(default_factory=list)  # (диапазон, карт, средний прогноз, факт)


def fit(maps, model: EloModel | None = None, min_games: int = 5, eval_since: int = 0) -> tuple[EloModel, Evaluation]:
    """Проходит карты по времени; оценивает прогнозы для карт после eval_since с опытными командами."""
    model = model or EloModel()
    preds: list[tuple[float, int]] = []  # (прогноз за radiant, radiant выиграл)
    for m in maps:
        r, d, t = m["radiant_team_id"], m["dire_team_id"], m["start_time"]
        rs, ds = model.rating(r, t), model.rating(d, t)
        if t >= eval_since and rs.games >= min_games and ds.games >= min_games:
            preds.append((expected(rs.rating, ds.rating), int(m["radiant_win"])))
        if m["radiant_win"]:
            model.update(r, d, t, m["radiant_name"] or "", m["dire_name"] or "")
        else:
            model.update(d, r, t, m["dire_name"] or "", m["radiant_name"] or "")
    return model, evaluate(preds)


def evaluate(preds: list[tuple[float, int]]) -> Evaluation:
    ev = Evaluation(n=len(preds))
    if not preds:
        return ev
    eps = 1e-6
    ev.brier = sum((p - y) ** 2 for p, y in preds) / len(preds)
    ev.log_loss = -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in preds) / len(preds)
    ev.accuracy = sum((p >= 0.5) == bool(y) for p, y in preds) / len(preds)
    # Калибровка по уверенности фаворита: 50–60%, 60–70%, ..., 90–100%
    fav = [(p, y) if p >= 0.5 else (1 - p, 1 - y) for p, y in preds]
    for lo in range(50, 100, 10):
        part = [(p, y) for p, y in fav if lo / 100 <= p < (lo + 10) / 100 or (lo == 90 and p == 1.0)]
        if part:
            ev.buckets.append((f"{lo}–{lo + 10}%", len(part), sum(p for p, _ in part) / len(part), sum(y for _, y in part) / len(part)))
    return ev


def find_team(model: EloModel, query: str) -> list[tuple[int, TeamState]]:
    q = query.casefold().strip()
    exact = [(tid, st) for tid, st in model.teams.items() if st.name.casefold() == q]
    if exact:
        return exact
    return sorted(((tid, st) for tid, st in model.teams.items() if q in st.name.casefold()), key=lambda x: -x[1].games)
