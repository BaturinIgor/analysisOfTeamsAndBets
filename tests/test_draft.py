import random
from datetime import datetime, timezone

import pytest

from bkanalyzer import analysis, db, draft, model as mm, sim
from bkanalyzer.bot import ModelCache
from bkanalyzer.rating import DAY


def maps_with_heroes(n=4000, seed=5):
    """Команды равны по силе, но герои 1–10 сильнее героев 11–20: побеждает драфт."""
    rnd = random.Random(seed)
    maps, t = [], 1_650_000_000
    for i in range(n):
        a, b = rnd.sample(range(1, 11), 2)
        ha, hb = rnd.sample(range(1, 21), 5), None
        hb = rnd.sample([h for h in range(1, 21) if h not in ha], 5)
        sa = sum(1 for h in ha if h <= 10) - sum(1 for h in hb if h <= 10)
        p = 1 / (1 + 10 ** (-sa * 0.15))
        t += 4 * 3600
        maps.append({"match_id": i, "start_time": t, "radiant_team_id": a, "dire_team_id": b,
                     "radiant_name": f"T{a}", "dire_name": f"T{b}", "radiant_win": int(rnd.random() < p),
                     "picks_r": [(a * 100 + k, h) for k, h in enumerate(ha)], "picks_d": [(b * 100 + k, h) for k, h in enumerate(hb)]})
    return maps


def test_draft_model_learns_heroes():
    maps = maps_with_heroes()
    model, rep = mm.train(maps, maps[-1]["start_time"] - 120 * DAY)
    assert rep.draft_eval.n > 300
    assert rep.draft_eval.log_loss < rep.draft_base_eval.log_loss - 0.01
    assert rep.draft_weights[1] > 0.5  # сила героев важна
    strong = [(1, h) for h in range(1, 6)]
    weak = [(2, h) for h in range(11, 16)]
    now = maps[-1]["start_time"] + 3600
    assert model.predict_draft(1, 2, now, strong, weak) > 0.6 > model.predict_draft(1, 2, now, weak, strong)


LIVE = [{"match_id": 77, "game_time": 40, "team_id_radiant": 2, "team_id_dire": 1,
         "team_name_radiant": "Tundra Esports", "team_name_dire": "Team Spirit",
         "players": [{"account_id": 200 + k, "hero_id": 11 + k, "team": 0} for k in range(5)]
                    + [{"account_id": 100 + k, "hero_id": 1 + k, "team": 1} for k in range(5)]},
        {"match_id": 78, "game_time": 0, "team_name_radiant": "X", "team_name_dire": "Y",
         "players": [{"account_id": 1, "hero_id": 0, "team": 0}]}]


def test_parse_and_find():
    ds = draft.parse_live(LIVE)
    assert ds[0].complete and not ds[1].complete
    assert ds[0].picks_d[0] == (100, 1)
    bm = analysis.BoardMatch("e", "Spirit", "Tundra", None, None, "0:0", "live")
    from bkanalyzer import rating
    m = rating.EloModel()
    m.update(1, 2, 0, "Team Spirit", "Tundra Esports")
    found = draft.find_draft(ds, m, bm)
    assert found[0].match_id == 77 and found[1] is False  # Spirit (team1) за dire


def test_draft_step_places_bet(tmp_path):
    from bkanalyzer.cli import draft_step
    from bkanalyzer.config import Settings

    conn = db.connect(tmp_path / "bk.sqlite3")
    # история: Spirit (1) и Tundra (2) равны, герои 1–5 сильнее 11–15
    from bkanalyzer import details, history
    details.ensure_schema(conn)
    conn.execute("INSERT INTO leagues VALUES (1, 'L', 'premium')")
    rnd = random.Random(1)
    t0 = 1_700_000_000
    for i in range(600):
        a, b = (1, 2) if i % 2 else (2, 1)
        strong_side = rnd.random() < 0.5
        win = int(rnd.random() < (0.8 if strong_side else 0.2))
        mid = i + 1
        conn.execute("INSERT INTO pro_maps (match_id, start_time, league_id, radiant_team_id, radiant_name, dire_team_id, dire_name, radiant_win) VALUES (?,?,?,?,?,?,?,?)",
                     (mid, t0 + i * 3600, 1, a, "Team Spirit" if a == 1 else "Tundra Esports", b, "Tundra Esports" if b == 2 else "Team Spirit", win))
        hr, hd = (range(1, 6), range(11, 16)) if strong_side else (range(11, 16), range(1, 6))
        conn.executemany("INSERT INTO map_players VALUES (?,?,?,?)",
                         [(mid, a * 100 + k, 1, h) for k, h in enumerate(hr)] + [(mid, b * 100 + k, 0, h) for k, h in enumerate(hd)])
    conn.commit()
    now = t0 + 700 * 3600
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    db.upsert_matches(conn, [{"match_id": "e1", "team1": "Spirit", "team2": "Tundra", "score": "0:0"}], source="oddiwire", updated_at=stamp)
    db.save_feed_rows(conn, [{"bookmaker": "oddin", "fixture_id": "e1", "market": m, "outcome": o, "price": 1.9, "phase": "live", "fair_prob": 0.5}
                             for m in ("match_winner", "map_1_winner") for o in ("Spirit", "Tundra")], source="oddiwire", taken_at=stamp)

    class OD:
        def live(self):
            return LIVE

    sent = []
    orig = analysis.load_board
    analysis.load_board = lambda c, **kw: orig(c, now=datetime.fromtimestamp(now, timezone.utc))
    try:
        s = Settings(signal_max_gap=0.5, sim_bank=1000, sim_stake=50)
        seen = set()
        bets = draft_step(s, conn, ModelCache(), OD(), None, seen, sent.append, log=lambda *_: None, now=now)
        assert [(b["mode"], b["market"], b["pick"]) for b in bets] == [("draft", "map_1_winner", "Spirit")]
        assert "После драфта" in sent[0] and "Spirit @ 1.90" in sent[0]
        assert draft_step(s, conn, ModelCache(), OD(), None, seen, sent.append, log=lambda *_: None, now=now) == []  # уже видели
    finally:
        analysis.load_board = orig
    assert "После драфта: ставок 1" in sim.report(conn, s.sim())
