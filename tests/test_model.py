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
    assert x0 == pytest.approx([0] * len(mm.FEATURES))
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


def test_patch_and_roster_features():
    from bkanalyzer.details import PatchIndex

    t = 1_700_000_000
    st = mm.FeatureState(patches=PatchIndex([("7.36", t - 100 * DAY), ("7.37", t)]))
    core = frozenset({1, 2, 3, 4, 5})
    other = frozenset({11, 12, 13, 14, 15})
    for i in range(4):
        st.update(1, 2, t - 50 * DAY + i, patch=None, lineups=(core, other))   # патч 7.36
    for i in range(3):
        st.update(1, 2, t + i * DAY, lineups=(core, other))                    # патч 7.37
    # у команды 2 замена: в последней карте два новых игрока
    st.update(2, 1, t + 5 * DAY, lineups=(frozenset({11, 12, 13, 98, 99}), core))
    x = dict(zip(mm.FEATURES, st.features(1, 2, t + 6 * DAY)))
    assert st.patch_at(t + 6 * DAY) == "7.37" and st.patch_at(t - 10 * DAY) == "7.36"
    assert x["patch"] > 0             # в 7.37 команда 1 выиграла 3 из 4
    assert x["roster"] == pytest.approx(0.4)  # 1.0 у команды 1 против 0.6 у команды 2


def test_sync_players_batches(tmp_path):
    from bkanalyzer import db, details, history

    conn = db.connect(tmp_path / "bk.sqlite3")
    details.ensure_schema(conn)
    conn.execute("INSERT INTO leagues VALUES (1, 'L', 'premium')")
    conn.executemany("INSERT INTO pro_maps (match_id, start_time, league_id, radiant_team_id, dire_team_id, radiant_win) VALUES (?,?,1,1,2,1)",
                     [(i, 1000 + i) for i in range(1, 6)])
    conn.commit()

    class FakeOD:
        def __init__(self):
            self.sqls = []

        def explorer(self, sql):
            self.sqls.append(sql)
            ids = [int(x) for x in sql.split("IN (")[1].rstrip(")").split(",")]
            return [{"match_id": m, "account_id": 100 + k, "hero_id": k + 1, "player_slot": 0 if k < 5 else 128}
                    for m in ids for k in range(10)]

        def patches(self):
            return [{"name": "7.37", "date": "2024-08-01T00:00:00Z"}, {"name": "7.38", "date": "2025-02-19T00:00:00Z"}]

    od = FakeOD()
    assert details.sync_players(conn, od, batch=2, sleep=lambda *_: None, log=lambda *_: None) == 5
    assert len(od.sqls) == 3
    assert details.sync_players(conn, od, sleep=lambda *_: None, log=lambda *_: None) == 0  # всё уже есть
    lu = details.lineups(conn)
    assert lu[1] == (frozenset(range(100, 105)), frozenset(range(105, 110)))
    assert details.sync_patches(conn, od) == 2
    idx = details.PatchIndex.load(conn)
    assert idx.at(details._parse_date("2025-01-01T00:00:00Z")) == "7.37"
