"""Разбор кадров линии BetBoom (бэкенд sporthub.bet, фид Oddin, protobuf без схемы).

Поля определены по записи 2026-11-04 (tree_ws/v1):

Матч — сообщение, где поле 5 = 'od:match:<id>' и есть поле 16 (команды):
  1 id матча в BetBoom · 5 id в фиде · 10 id турнира · 13 старт (UTC)
  16.1.3 / 16.3.3 команды · 17 формат (BO3) · 20.2 счёт по картам

Исход — сообщение, где поле 1 = 'od:match:<id>/<рынок>/<исход>?...' и поле 10 — коэффициент:
  2 id матча · 5 название · 6 краткое (П1, П2, Больше) · 9 линия (тотал/фора)
  10 коэффициент · 13 id рынка · 14 рынок · 18 период (Матч, Карта 2)

Если BetBoom поменяет формат, разбор вернёт пустые списки — это видно по выводу
команды, тогда нужна новая разведка.
"""

from __future__ import annotations

from collections import defaultdict

from .protowire import WireError, decode

MATCH_WINNER = "Исход матча"
MAP_WINNER = "Исход"


def _fields(msg: list) -> dict[int, list]:
    dd: dict[int, list] = defaultdict(list)
    for f, v in msg:
        dd[f].append(v)
    return dd


def _walk(msg: list):
    yield _fields(msg)
    for _, v in msg:
        if isinstance(v, list):
            yield from _walk(v)


def _first(dd: dict, key: int, default=None):
    vals = dd.get(key)
    return vals[0] if vals else default


def _team(dd: dict, slot: int) -> str | None:
    teams = _first(dd, 16)
    if not isinstance(teams, list):
        return None
    team = _first(_fields(teams), slot)
    return _first(_fields(team), 3) if isinstance(team, list) else None


def parse_frame(buf: bytes) -> tuple[list[dict], list[dict]]:
    """Матчи и исходы из одного кадра. Битый кадр → два пустых списка."""
    try:
        tree = decode(buf)
    except WireError:
        return [], []
    matches: dict[int, dict] = {}
    outcomes: dict[str, dict] = {}
    for dd in _walk(tree):
        feed = _first(dd, 5)
        if isinstance(feed, str) and feed.startswith("od:match:") and 16 in dd and isinstance(_first(dd, 1), int):
            score = _first(dd, 20)
            matches[_first(dd, 1)] = {
                "match_id": _first(dd, 1),
                "feed_id": feed,
                "tournament_id": _first(dd, 10),
                "start_time": _first(dd, 13),
                "team1": _team(dd, 1),
                "team2": _team(dd, 3),
                "format": _first(dd, 17) or None,
                "score": _first(_fields(score), 2) if isinstance(score, list) else None,
            }
        uid = _first(dd, 1)
        price = _first(dd, 10)
        if isinstance(uid, str) and uid.startswith("od:match:") and isinstance(price, float) and isinstance(_first(dd, 2), int):
            line = _first(dd, 9)
            outcomes[uid] = {
                "uid": uid,
                "match_id": _first(dd, 2),
                "market_id": _first(dd, 13),
                "market": _first(dd, 14, ""),
                "period": _first(dd, 18, ""),
                "name": _first(dd, 5, ""),
                "short": _first(dd, 6, ""),
                "line": line if isinstance(line, float) else None,
                "price": price,
            }
    return list(matches.values()), list(outcomes.values())


def snapshot_rows(outcomes: list[dict]) -> list[dict]:
    """Строки для таблицы odds_snapshots."""
    rows = []
    for o in outcomes:
        outcome = o["short"] or o["name"]
        if o["line"] is not None and str(o["line"]) not in outcome:
            outcome = f"{outcome} ({o['line']:g})"
        rows.append({"bookmaker": "betboom", "fixture_id": str(o["match_id"]), "market": f"{o['market']} | {o['period']}", "outcome": outcome, "price": o["price"]})
    return rows


def winner_table(matches: list[dict], outcomes: list[dict]) -> list[dict]:
    """По каждому матчу коэффициенты П1/П2 на матч."""
    by_match: dict[int, dict] = defaultdict(dict)
    for o in outcomes:
        if o["market"] == MATCH_WINNER and o["short"] in ("П1", "П2", "X"):
            by_match[o["match_id"]][o["short"]] = o["price"]
    out = []
    for m in sorted(matches, key=lambda m: m["start_time"] or ""):
        prices = by_match.get(m["match_id"], {})
        out.append({**m, "p1": prices.get("П1"), "p2": prices.get("П2")})
    return out
