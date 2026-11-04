from datetime import datetime, timezone

import httpx
import pytest

from bkanalyzer import analysis, db, history, rating
from bkanalyzer.analysis import Thresholds
from bkanalyzer.bot import Bot, chunks, run_polling

NOW = datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc)
ME = 786270397


def seed(conn):
    history.ensure_schema(conn)
    conn.execute("INSERT INTO leagues VALUES (1, 'Big League', 'premium')")
    t = int(NOW.timestamp()) - 30 * 86400
    rows = []
    for i in range(60):  # Spirit почти всегда обыгрывает Tundra
        win = 1 if i % 6 else 0
        rows.append((i + 1, t + i * 3600, 1, 1, "Team Spirit", 2, "Tundra Esports", win))
    conn.executemany(
        "INSERT INTO pro_maps (match_id, start_time, league_id, radiant_team_id, radiant_name, dire_team_id, dire_name, radiant_win) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    ts = NOW.isoformat(timespec="seconds")
    db.upsert_matches(conn, [
        {"match_id": "e1", "feed_id": "od:match:1", "tournament_id": "Big League", "start_time": "2026-11-04T22:00:00Z", "team1": "Spirit", "team2": "Tundra", "score": "0:0"},
        {"match_id": "e2", "feed_id": "od:match:2", "tournament_id": "Small Cup", "start_time": "2026-11-04T20:00:00Z", "team1": "Team Allied", "team2": "Team Lynx", "score": "1:0"},
    ], source="oddiwire", updated_at=ts)
    rows = [
        {"bookmaker": "oddin", "fixture_id": "e1", "market": "match_winner", "outcome": "Spirit", "price": 1.9, "phase": "prematch", "fair_prob": 0.5},
        {"bookmaker": "oddin", "fixture_id": "e1", "market": "match_winner", "outcome": "Tundra", "price": 1.9, "phase": "prematch", "fair_prob": 0.5},
        {"bookmaker": "oddin", "fixture_id": "e2", "market": "match_winner", "outcome": "Team Allied", "price": 1.5, "phase": "live", "fair_prob": 0.64},
        {"bookmaker": "oddin", "fixture_id": "e2", "market": "match_winner", "outcome": "Team Lynx", "price": 2.5, "phase": "live", "fair_prob": 0.36},
        {"bookmaker": "oddin", "fixture_id": "e2", "market": "map_2_winner", "outcome": "Team Allied", "price": 1.7, "phase": "live", "fair_prob": 0.55},
    ]
    db.save_feed_rows(conn, rows, source="oddiwire", taken_at=ts)


@pytest.fixture
def bot(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    seed(conn)
    return Bot(conn, (ME,), Thresholds(0.05), now=lambda: NOW)


def test_normalize_and_match():
    assert analysis.normalize("Team Spirit") == "spirit"
    assert analysis.normalize("Tundra Esports") == "tundra"
    m = rating.EloModel()
    m.update(1, 2, 0, "Team Spirit", "Tundra Esports")
    assert analysis.match_team(m, "Spirit")[0] == 1
    assert analysis.match_team(m, "TUNDRA")[0] == 2
    assert analysis.match_team(m, "OG") is None


def test_series_from_score():
    assert analysis.series_from_score(0.6, 3) == pytest.approx(0.648)
    assert analysis.series_from_score(0.6, 3, 1, 0) == pytest.approx(0.6 + 0.4 * 0.6)
    assert analysis.series_from_score(0.6, 3, 0, 2) == 0.0


def test_whitelist(bot):
    assert bot.handle(123, "/today") is None
    assert "Анализатор" in bot.handle(ME, "/start")


def test_today_and_value(bot):
    text = bot.handle(ME, "/today")
    assert "Spirit — Tundra" in text and "1.90 / 1.90" in text
    assert "✅ Spirit матч" in text  # модель даёт Spirit больше 50% при коэффициенте 1.90
    assert "🔴" in text and "Team Allied — Team Lynx (счёт 1:0)" in text
    assert "нашей оценки нет" in text  # Allied/Lynx нет в Tier 1–2
    val = bot.handle(ME, "/value")
    assert "Spirit @ 1.90" in val and "Tundra" in val
    live = bot.handle(ME, "/live")
    assert "Spirit" not in live and "Team Allied" in live


def test_match_detail(bot):
    bot.handle(ME, "/today")
    assert "Team Allied — Team Lynx" in bot.handle(ME, "/match 1")
    text = bot.handle(ME, "/match 2")
    assert "Рейтинг: Spirit (" in text and "Наша вероятность" in text
    assert "матч: Spirit 1.90 · 50%" in text
    assert "EV +" in text
    other = bot.handle(ME, "/match lynx")
    assert "нет в истории" in other and "карта 2: Team Allied 1.70" in other


def test_team_and_misc(bot):
    assert "Team Spirit: рейтинг" in bot.handle(ME, "/team spirit")
    assert "не найдена" in bot.handle(ME, "/team Secret")
    assert "SIGNAL_MIN_EV" in bot.handle(ME, "/settings")
    assert "следующем этапе" in bot.handle(ME, "/sim")
    assert "Не знаю" in bot.handle(ME, "/foo")


def test_high_threshold_says_no_bet(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    seed(conn)
    b = Bot(conn, (ME,), Thresholds(min_ev=0.05, min_prob=0.95, min_odds=1.5), now=lambda: NOW)
    assert "Ставки нет" in b.handle(ME, "/value")


def test_chunks():
    parts = chunks("\n".join(["x" * 100] * 100), size=1000)
    assert all(len(p) <= 1000 for p in parts) and sum(p.count("x") for p in parts) == 10000


def test_polling(bot):
    sent = []
    state = {"n": 0}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/getUpdates"):
            state["n"] += 1
            result = [
                {"update_id": 5, "message": {"from": {"id": ME}, "chat": {"id": ME}, "text": "/help"}},
                {"update_id": 6, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "/today"}},
            ] if state["n"] == 1 else []
            return httpx.Response(200, json={"ok": True, "result": result})
        if req.url.path.endswith("/sendMessage"):
            import json
            sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    run_polling("TOKEN", bot, client, log=lambda *_: None, stop=lambda: state["n"] >= 2)
    assert len(sent) == 1 and sent[0]["chat_id"] == ME and "/today" in sent[0]["text"]
