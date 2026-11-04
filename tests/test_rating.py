import random
import time

import pytest

from bkanalyzer import db, history, rating
from bkanalyzer.cli import predict_report, rating_report


def test_series_probabilities():
    assert rating.series_probabilities(0.5, 3)["win"] == pytest.approx(0.5)
    assert rating.series_probabilities(0.6, 3)["win"] == pytest.approx(0.648)
    assert rating.series_probabilities(0.6, 5)["win"] == pytest.approx(0.68256)
    bo2 = rating.series_probabilities(0.6, 2)
    assert sum(bo2.values()) == pytest.approx(1) and bo2["draw"] == pytest.approx(0.48)


def test_elo_update_and_decay():
    m = rating.EloModel()
    p = m.update(1, 2, now=0)
    assert p == pytest.approx(0.5)
    assert m.teams[1].rating == pytest.approx(1520) and m.teams[2].rating == pytest.approx(1480)
    later = 180 * rating.DAY
    assert m.rating(1, later).rating == pytest.approx(1510)  # половина отклонения ушла за полураспад


def synthetic_maps(n=2000, seed=1):
    rnd = random.Random(seed)
    strength = {t: rnd.gauss(0, 200) for t in range(1, 21)}
    maps, t = [], 1_700_000_000
    for i in range(n):
        a, b = rnd.sample(range(1, 21), 2)
        p = rating.expected(strength[a], strength[b])
        t += 3600
        maps.append({"match_id": i, "start_time": t, "radiant_team_id": a, "dire_team_id": b,
                     "radiant_name": f"T{a}", "dire_name": f"T{b}", "radiant_win": int(rnd.random() < p)})
    return maps, strength


def test_fit_learns_strength_and_is_calibrated():
    maps, strength = synthetic_maps()
    model, ev = rating.fit(maps, eval_since=maps[1000]["start_time"])
    best = max(strength, key=strength.get)
    worst = min(strength, key=strength.get)
    assert model.teams[best].rating > model.teams[worst].rating
    assert ev.n > 900 and ev.brier < 0.24
    for _, n, p, y in ev.buckets:
        if n >= 100:
            assert abs(p - y) < 0.08


class FakeOD:
    def __init__(self, maps):
        self.maps = sorted(maps, key=lambda m: -m["match_id"])
        self.calls = 0

    def pro_matches(self, less_than_match_id=None):
        self.calls += 1
        rows = [m for m in self.maps if less_than_match_id is None or m["match_id"] < less_than_match_id]
        return rows[:100]

    def leagues(self):
        return [{"leagueid": 1, "name": "Tier1 Cup", "tier": "premium"}, {"leagueid": 2, "name": "Amateur", "tier": "amateur"}]


def od_maps(n, now):
    return [{"match_id": i, "start_time": now - (n - i) * 3600, "leagueid": 1 if i % 4 else 2, "radiant_team_id": 10 + i % 3,
             "radiant_name": f"R{i % 3}", "dire_team_id": 20 + i % 2, "dire_name": f"D{i % 2}", "radiant_win": i % 2} for i in range(1, n + 1)]


def test_sync_pro_maps_and_resume(tmp_path):
    now = 2_000_000_000
    conn = db.connect(tmp_path / "bk.sqlite3")
    od = FakeOD(od_maps(350, now))
    history.sync_leagues(conn, od)
    assert history.sync_pro_maps(conn, od, days=10, now=now, sleep=lambda _: None, log=lambda _: None) == 241  # граница 10 дней включительно
    # Повторный запуск с более длинным периодом дозагружает только старое
    assert history.sync_pro_maps(conn, od, days=30, now=now, sleep=lambda _: None, log=lambda _: None) == 109
    assert conn.execute("SELECT COUNT(*) FROM pro_maps").fetchone()[0] == 350
    # Уже всё загружено — ничего нового, быстрый выход
    od.calls = 0
    assert history.sync_pro_maps(conn, od, days=30, now=now, sleep=lambda _: None, log=lambda _: None) == 0
    assert od.calls <= 2
    tier = history.tier_maps(conn)
    assert len(tier) == 350 - 350 // 4  # лиги amateur отброшены


def test_reports(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    history.ensure_schema(conn)
    assert "Сначала: bk sync-matches" in rating_report(conn)
    maps, _ = synthetic_maps(600)
    now = int(time.time())
    shift = now - maps[-1]["start_time"]
    conn.execute("INSERT INTO leagues VALUES (1, 'Cup', 'premium')")
    conn.executemany(
        "INSERT INTO pro_maps (match_id, start_time, league_id, radiant_team_id, radiant_name, dire_team_id, dire_name, radiant_win) VALUES (?,?,?,?,?,?,?,?)",
        [(m["match_id"], m["start_time"] + shift, 1, m["radiant_team_id"], m["radiant_name"], m["dire_team_id"], m["dire_name"], m["radiant_win"]) for m in maps],
    )
    db.upsert_matches(conn, [{"match_id": 7, "team1": "T2", "team2": "T1", "start_time": "x"}], source="betboom")
    db.save_price_changes(conn, [
        {"bookmaker": "betboom", "fixture_id": "7", "market": "Исход матча | Матч", "outcome": "П1", "price": 1.5},
        {"bookmaker": "betboom", "fixture_id": "7", "market": "Исход матча | Матч", "outcome": "П2", "price": 2.6},
    ], source="betboom")
    report = rating_report(conn, eval_days=10)
    assert "Калибровка" in report and "Топ-20" in report
    out = predict_report(conn, "T1", "T2", 3)
    assert "Bo3" in out and "честный коэффициент" in out
    assert "T1: 2.60" in out and "T2: 1.50" in out  # стороны BetBoom переставлены правильно
    assert "не найдена" in predict_report(conn, "Nope", "T2")
