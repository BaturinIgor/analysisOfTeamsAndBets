import math
import random
import time

import pytest

from bkanalyzer import model as mm
from bkanalyzer.rating import DAY


def test_fit_logistic_recovers_weights():
    rnd = random.Random(0)
    xs, ys = [], []
    for _ in range(4000):
        x = [rnd.gauss(0, 1), rnd.gauss(0, 1)]
        p = 1 / (1 + math.exp(-(1.5 * x[0] - 0.7 * x[1])))
        xs.append(x)
        ys.append(int(rnd.random() < p))
    w = mm.fit_logistic(xs, ys, l2=0.1)
    assert w[0] == pytest.approx(1.5, abs=0.15) and w[1] == pytest.approx(-0.7, abs=0.15)


def drifting_maps(n=6000, teams=16, seed=3):
    """Сила команд дрейфует: форма важнее старых результатов."""
    rnd = random.Random(seed)
    strength = {t: rnd.gauss(0, 150) for t in range(1, teams + 1)}
    maps, t = [], 1_600_000_000
    for i in range(n):
        if i % 50 == 0:
            for k in strength:
                strength[k] += rnd.gauss(0, 60)
        a, b = rnd.sample(range(1, teams + 1), 2)
        p = 1 / (1 + 10 ** (-(strength[a] - strength[b]) / 400))
        t += 3 * 3600
        maps.append({"match_id": i, "start_time": t, "radiant_team_id": a, "dire_team_id": b,
                     "radiant_name": f"T{a}", "dire_name": f"T{b}", "radiant_win": int(rnd.random() < p)})
    return maps


def test_train_is_honest_and_not_worse_than_elo():
    maps = drifting_maps()
    since = maps[-1]["start_time"] - 120 * DAY
    model, rep = mm.train(maps, since)
    assert rep.model_eval.n == rep.elo_eval.n > 500
    assert rep.train > 1000
    # калиброванная модель не хуже сырого рейтинга
    assert rep.model_eval.log_loss <= rep.elo_eval.log_loss + 0.002
    assert rep.model_eval.log_loss < 0.69
    p = model.predict(1, 2, maps[-1]["start_time"] + 3600)
    assert 0 < p < 1 and model.predict(2, 1, maps[-1]["start_time"] + 3600) == pytest.approx(1 - p, abs=1e-9)
    names = [n for n, _, _ in model.explain(1, 2, maps[-1]["start_time"])]
    assert names == list(mm.FEATURES)


def test_features_use_only_past():
    st = mm.FeatureState()
    t = 1_700_000_000
    x0 = st.features(1, 2, t)
    assert x0 == pytest.approx([0, 0, 0, 0, 0, 0])
    for i in range(5):
        st.update(1, 2, t + i * DAY)
    x = st.features(1, 2, t + 6 * DAY)
    assert x[0] > 0 and x[1] > 0 and x[2] > 0 and x[3] > 0  # рейтинг, форма, личные встречи за команду 1
    assert x[4] == pytest.approx(0) and x[5] == pytest.approx(0)  # одинаковая пауза и опыт


def test_training_speed():
    maps = drifting_maps(n=20000, teams=60)
    t0 = time.monotonic()
    mm.train(maps, maps[-1]["start_time"] - 180 * DAY)
    assert time.monotonic() - t0 < 120
