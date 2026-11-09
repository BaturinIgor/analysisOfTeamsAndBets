"""Модель вероятности карты: рейтинг Elo + признаки, логистическая регрессия.

Признаки (ТР-6, ТР-7) — всё считается только по картам ДО прогноза:
- elo      — разница рейтингов Elo / 400;
- form10   — разница винрейта за последние 10 карт (со сглаживанием к 50%);
- form30   — разница винрейта за 30 дней;
- h2h      — доля побед в личных встречах за год (со сглаживанием) − 0.5;
- rest     — разница log(1 + дней без игр), не больше 60 дней;
- exp      — разница log(1 + сыгранных карт);
- patch    — разница винрейта в текущем патче (со сглаживанием);
- roster   — разница стабильности состава: сколько из 5 игроков последней карты
             входят в основной состав за 10 карт (замена или стендин снижают её).

Логистическая регрессия заодно калибрует сам Elo: «сырые» вероятности рейтинга
обычно слишком уверенные. Проверка честная: модель учится на старых картах и
проверяется на последних, которых при обучении не видела.

Чего пока нет: драфт (нужен для режима «после драфта» — следующий шаг).
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field

from .rating import DAY, EloModel, Evaluation, evaluate

FEATURES = ("elo", "form10", "form30", "h2h", "rest", "exp", "patch", "roster")


@dataclass
class TeamHistory:
    last10: deque = field(default_factory=lambda: deque(maxlen=10))   # 1/0
    recent: deque = field(default_factory=deque)                      # (время, 1/0) за 30 дней
    last_time: int | None = None
    games: int = 0
    by_patch: dict = field(default_factory=dict)                      # патч -> [побед, карт]
    lineups: deque = field(default_factory=lambda: deque(maxlen=10))  # составы последних карт

    def stability(self) -> float:
        known = [l for l in self.lineups if l]
        if len(known) < 3:
            return 1.0
        core = {acc for acc, _ in Counter(a for l in known for a in l).most_common(5)}
        return len(known[-1] & core) / 5


def _smooth(wins: float, n: int, prior: float = 5.0) -> float:
    return (wins + prior / 2) / (n + prior)


@dataclass
class FeatureState:
    """Всё, что известно о командах к текущему моменту; обновляется по одной карте."""

    elo: EloModel = field(default_factory=EloModel)
    teams: dict[int, TeamHistory] = field(default_factory=dict)
    h2h: dict[tuple[int, int], deque] = field(default_factory=dict)   # (меньший id, больший id) -> (время, победитель)
    patches: object = None                                           # PatchIndex или None
    hero_patch: dict = field(default_factory=dict)    # (патч, герой) -> [побед, игр] в про-матчах
    hero_all: dict = field(default_factory=dict)      # герой -> [побед, игр] за всё время
    player_hero: dict = field(default_factory=dict)   # (игрок, герой) -> игр

    def hero_strength(self, hero: int, patch: str | None) -> float:
        """Логит винрейта героя в патче, сглаженный к его общему винрейту."""
        w_all, n_all = self.hero_all.get(hero, (0, 0))
        base = (w_all + 10) / (n_all + 20)
        w, n = self.hero_patch.get((patch, hero), (0, 0)) if patch else (0, 0)
        p = (w + 20 * base) / (n + 20)
        return math.log(p / (1 - p))

    def draft_features(self, picks_a, picks_b, now: int, patch: str | None = None) -> list[float]:
        """[сила героев a − b, опыт игроков на своих героях a − b]; picks = [(игрок, герой)] × 5."""
        patch = patch or self.patch_at(now)

        def side(picks):
            if not picks:
                return 0.0, 0.0
            hs = sum(self.hero_strength(h, patch) for _, h in picks) / len(picks)
            ex = sum(math.log1p(self.player_hero.get((acc, h), 0)) for acc, h in picks) / len(picks)
            return hs, ex

        (ha, ea), (hb, eb) = side(picks_a), side(picks_b)
        return [ha - hb, ea - eb]

    def update_draft(self, picks_win, picks_lose, now: int, patch: str | None = None) -> None:
        patch = patch or self.patch_at(now)
        for picks, won in ((picks_win, 1), (picks_lose, 0)):
            for acc, hero in picks or ():
                if hero is None:
                    continue
                for key, table in (((patch, hero), self.hero_patch), (hero, self.hero_all)):
                    rec = table.setdefault(key, [0, 0])
                    rec[0] += won
                    rec[1] += 1
                self.player_hero[(acc, hero)] = self.player_hero.get((acc, hero), 0) + 1

    def patch_at(self, now: int) -> str | None:
        return self.patches.at(now) if self.patches is not None else None

    def _team(self, tid: int) -> TeamHistory:
        return self.teams.setdefault(tid, TeamHistory())

    def _team_side(self, tid: int, now: int) -> tuple[float, float, float, float]:
        h = self._team(tid)
        while h.recent and h.recent[0][0] < now - 30 * DAY:
            h.recent.popleft()
        f10 = _smooth(sum(h.last10), len(h.last10))
        f30 = _smooth(sum(w for _, w in h.recent), len(h.recent))
        rest = math.log1p(min(60.0, (now - h.last_time) / DAY)) if h.last_time is not None else math.log1p(60)
        return f10, f30, rest, math.log1p(h.games)

    def features(self, a: int, b: int, now: int, patch: str | None = None) -> list[float]:
        ra, rb = self.elo.rating(a, now).rating, self.elo.rating(b, now).rating
        fa, fb = self._team_side(a, now), self._team_side(b, now)
        key = (min(a, b), max(a, b))
        meetings = [w for t, w in self.h2h.get(key, ()) if t >= now - 365 * DAY]
        h2h = (sum(1 for w in meetings if w == a) + 1) / (len(meetings) + 2) - 0.5
        patch = patch or self.patch_at(now)
        ha, hb = self._team(a), self._team(b)
        pa = _smooth(*ha.by_patch.get(patch, (0, 0)), prior=10) if patch else 0.5
        pb = _smooth(*hb.by_patch.get(patch, (0, 0)), prior=10) if patch else 0.5
        return [(ra - rb) / 400, fa[0] - fb[0], fa[1] - fb[1], h2h, fa[2] - fb[2], fa[3] - fb[3],
                pa - pb, ha.stability() - hb.stability()]

    def update(self, winner: int, loser: int, now: int, wname: str = "", lname: str = "",
               patch: str | None = None, lineups: tuple = (None, None)) -> None:
        self.elo.update(winner, loser, now, wname, lname)
        patch = patch or self.patch_at(now)
        for (tid, won), lineup in zip(((winner, 1), (loser, 0)), lineups):
            h = self._team(tid)
            h.last10.append(won)
            h.recent.append((now, won))
            h.last_time = now
            h.games += 1
            if patch:
                rec = h.by_patch.setdefault(patch, [0, 0])
                rec[0] += won
                rec[1] += 1
            if lineup:
                h.lineups.append(lineup)
        self.h2h.setdefault((min(winner, loser), max(winner, loser)), deque(maxlen=20)).append((now, winner))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Гаусс с выбором главного элемента для маленькой системы."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_logistic(xs: list[list[float]], ys: list[int], l2: float = 1.0, iters: int = 12) -> list[float]:
    """Логистическая регрессия без свободного члена (данные симметричны), метод Ньютона с L2."""
    k = len(xs[0]) if xs else 0
    w = [0.0] * k
    for _ in range(iters):
        grad = [l2 * wi for wi in w]
        hess = [[l2 if i == j else 0.0 for j in range(k)] for i in range(k)]
        for x, y in zip(xs, ys):
            p = _sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            g, s = p - y, p * (1 - p)
            for i in range(k):
                grad[i] += g * x[i]
                sx = s * x[i]
                row = hess[i]
                for j in range(i, k):
                    row[j] += sx * x[j]
        for i in range(k):
            for j in range(i):
                hess[i][j] = hess[j][i]
        step = _solve(hess, grad)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


@dataclass
class MatchModel:
    """Готовая модель: состояние команд + веса. predict(a, b, now) — вероятность, что a выиграет карту."""

    state: FeatureState
    weights: list[float]
    draft_weights: list[float] | None = None   # [базовый логит, сила героев, опыт на героях]

    @property
    def teams(self):
        return self.state.elo.teams

    def logit(self, a: int, b: int, now: int) -> float:
        return sum(w * xi for w, xi in zip(self.weights, self.state.features(a, b, now)))

    def predict_draft(self, a: int, b: int, now: int, picks_a, picks_b) -> float | None:
        """Вероятность карты после драфта; None, если модель драфта не обучена."""
        if not self.draft_weights:
            return None
        x = [self.logit(a, b, now)] + self.state.draft_features(picks_a, picks_b, now)
        return _sigmoid(sum(w * xi for w, xi in zip(self.draft_weights, x)))

    def predict(self, a: int, b: int, now: int, patch: str | None = None) -> float:
        x = self.state.features(a, b, now, patch)
        return _sigmoid(sum(w * xi for w, xi in zip(self.weights, x)))

    def explain(self, a: int, b: int, now: int, patch: str | None = None) -> list[tuple[str, float, float]]:
        """(признак, значение, вклад в логит) — для обоснования в боте."""
        x = self.state.features(a, b, now, patch)
        return [(name, xi, w * xi) for name, xi, w in zip(FEATURES, x, self.weights)]


@dataclass
class Report:
    elo_eval: Evaluation
    model_eval: Evaluation
    weights: list[float]
    k: float
    half_life: float
    train: int
    draft_base_eval: Evaluation = field(default_factory=Evaluation)  # модель без драфта на тех же картах
    draft_eval: Evaluation = field(default_factory=Evaluation)
    draft_weights: list[float] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)  # признаки, прошедшие отбор
    features_eval: Evaluation | None = None  # если признаки проиграли рейтингу: их результат до отката


def fit_masked(samples, mask: list[bool], l2: float = 1.0) -> list[float]:
    """Логистическая регрессия только на отмеченных признаках; у остальных вес 0."""
    idx = [i for i, m in enumerate(mask) if m]
    rows = [(t, [x[i] for i in idx], y, p) for t, x, y, p in samples]
    sub = fit_logistic(*_augment(rows), l2=l2)
    w = [0.0] * len(mask)
    for i, wi in zip(idx, sub):
        w[i] = wi
    return w


def _loss(samples, w) -> float:
    return evaluate([(_sigmoid(sum(wi * xi for wi, xi in zip(w, x))), y) for _, x, y, _ in samples]).log_loss


def select_features(samples, min_gain: float = 0.0005) -> list[bool]:
    """Жадный отбор признаков по времени: учим на первых 75% карт, проверяем на последних 25%.

    Рейтинг Elo входит всегда; признак добавляется, только если заметно снижает log-loss.
    Иначе признаки, повторяющие рейтинг, мешают ему (так было на реальных данных).
    """
    k = len(samples[0][1]) if samples else len(FEATURES)
    mask = [i == 0 for i in range(k)]
    if len(samples) < 400:
        return mask
    cut = int(len(samples) * 0.75)
    inner_train, inner_val = samples[:cut], samples[cut:]
    best = _loss(inner_val, fit_masked(inner_train, mask))
    while True:
        trial = None
        for i in range(1, k):
            if mask[i]:
                continue
            m = mask.copy()
            m[i] = True
            loss = _loss(inner_val, fit_masked(inner_train, m))
            if trial is None or loss < trial[0]:
                trial = (loss, m)
        if trial is None or best - trial[0] < min_gain:
            return mask
        best, mask = trial


def _walk(maps, elo_params: dict, min_games: int, collect_since: int | None = None, patches=None):
    """Один проход по картам: признаки до обновления. Возвращает (state, samples, draft_samples).

    У карты могут быть поля lineup_r / lineup_d (frozenset игроков) и picks_r / picks_d
    ([(игрок, герой)]) — для стабильности состава и драфта.
    """
    st = FeatureState(elo=EloModel(**elo_params), patches=patches)
    samples = []   # (время, признаки за radiant, radiant выиграл, сырой Elo-прогноз)
    dsamples = []  # (время, признаки за radiant, признаки драфта за radiant, radiant выиграл)
    for m in maps:
        r, d, t = m["radiant_team_id"], m["dire_team_id"], m["start_time"]
        rs, ds = st.elo.rating(r, t), st.elo.rating(d, t)
        pr, pd = m.get("picks_r"), m.get("picks_d")
        if rs.games >= min_games and ds.games >= min_games and (collect_since is None or t >= collect_since):
            x = st.features(r, d, t, m.get("patch"))  # теннис: покрытие матча
            samples.append((t, x, int(m["radiant_win"]), 1 / (1 + 10 ** (-x[0]))))
            if pr and pd and len(pr) == 5 and len(pd) == 5:
                dsamples.append((t, x, st.draft_features(pr, pd, t), int(m["radiant_win"])))
        lr, ld = m.get("lineup_r"), m.get("lineup_d")
        if m["radiant_win"]:
            st.update(r, d, t, m["radiant_name"] or "", m["dire_name"] or "", patch=m.get("patch"), lineups=(lr, ld))
            st.update_draft(pr, pd, t)
        else:
            st.update(d, r, t, m["dire_name"] or "", m["radiant_name"] or "", patch=m.get("patch"), lineups=(ld, lr))
            st.update_draft(pd, pr, t)
    return st, samples, dsamples


def _augment(samples):
    xs, ys = [], []
    for _, x, y, _ in samples:
        xs.append(x)
        ys.append(y)
        xs.append([-v for v in x])
        ys.append(1 - y)
    return xs, ys


def train(maps, eval_since: int, min_games: int = 5,
          grid=((24.0, 180.0), (16.0, 180.0), (32.0, 180.0), (24.0, 90.0), (24.0, 365.0)), patches=None) -> tuple[MatchModel, Report]:
    """Подбирает параметры Elo, обучает веса на картах до eval_since, проверяет после, затем учит на всём."""
    maps = list(maps)
    best = None
    for k, hl in grid:
        params = {"k": k, "half_life_days": hl}
        _, samples, dsamples = _walk(maps, params, min_games, patches=patches)
        val = [(p, y) for t, _, y, p in samples if t >= eval_since]
        ev = evaluate(val)
        if val and (best is None or ev.log_loss < best[0].log_loss):
            best = (ev, params, samples, dsamples)
    if best is None:  # мало данных
        st, _, _ = _walk(maps, {}, min_games, patches=patches)
        return MatchModel(st, [1.0 * math.log(10)] + [0.0] * (len(FEATURES) - 1)), Report(Evaluation(), Evaluation(), [], 24, 180, 0)
    elo_ev, params, samples, dsamples = best
    train_s = [s for s in samples if s[0] < eval_since]
    val_s = [s for s in samples if s[0] >= eval_since]
    # отбор признаков только на обучающих картах — проверочные 180 дней в нём не участвуют
    mask = select_features(sorted(train_s, key=lambda r: r[0])) if train_s else [i == 0 for i in range(len(FEATURES))]
    w = fit_masked(train_s, mask) if train_s else [math.log(10)] + [0.0] * (len(FEATURES) - 1)
    model_ev = evaluate([(_sigmoid(sum(wi * xi for wi, xi in zip(w, x))), y) for _, x, y, _ in val_s])
    features_eval = None
    if val_s and any(mask[1:]) and model_ev.log_loss > elo_ev.log_loss:
        # признаки не обогнали рейтинг на свежих картах — работаем на одном рейтинге
        features_eval = model_ev
        mask = [i == 0 for i in range(len(mask))]
        w = fit_masked(train_s, mask)
        model_ev = evaluate([(_sigmoid(sum(wi * xi for wi, xi in zip(w, x))), y) for _, x, y, _ in val_s])
    # рабочая модель: веса на всех картах, состояние — на сегодня
    final_w = fit_masked(samples, mask) if samples else w
    state, _, _ = _walk(maps, params, min_games, collect_since=10**12, patches=patches)
    report = Report(elo_ev, model_ev, final_w, params["k"], params["half_life_days"], len(train_s),
                    selected=[f for f, m in zip(FEATURES, mask) if m], features_eval=features_eval)

    # модель после драфта: базовый логит + сила героев + опыт игроков на героях
    def drows(rows, weights):
        return [(t, [sum(a * b for a, b in zip(weights, x))] + dx, 0, y) for t, x, dx, y in rows]

    dtrain = [r for r in dsamples if r[0] < eval_since]
    dval = [r for r in dsamples if r[0] >= eval_since]
    draft_w = None
    if len(dtrain) >= 200 and dval:
        dw = fit_logistic(*_augment([(t, x, y, 0) for t, x, _, y in drows(dtrain, w)]))
        rows = drows(dval, w)
        report.draft_base_eval = evaluate([(_sigmoid(x[0]), y) for _, x, _, y in rows])
        report.draft_eval = evaluate([(_sigmoid(sum(a * b for a, b in zip(dw, x))), y) for _, x, _, y in rows])
    # рабочая модель драфта — на всех картах с драфтом, даже если на проверку их не хватило
    if len(dsamples) >= 200:
        draft_w = fit_logistic(*_augment([(t, x, y, 0) for t, x, _, y in drows(dsamples, final_w)]))
        report.draft_weights = draft_w
    return MatchModel(state, final_w, draft_w), report


LABELS = {"elo": "рейтинг Elo", "form10": "форма (10 карт)", "form30": "форма (30 дней)", "h2h": "личные встречи",
          "rest": "пауза без игр", "exp": "опыт (число карт)", "patch": "форма в текущем патче",
          "roster": "стабильность состава"}
DRAFT_LABELS = ("модель до драфта", "сила героев в патче", "опыт игроков на героях")
