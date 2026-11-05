"""Режим «после драфта» (ТР-18, ТР-21).

Раз в минуту смотрим OpenDota /live: там идущие про-игры с героями игроков.
Как только у матча из линии Oddin выбраны все 10 героев и игра только началась,
считаем вероятность карты с учётом драфта, берём свежий коэффициент на эту карту
(один запрос к OddiWire) и, если есть перевес, делаем бумажную ставку и шлём уведомление.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import analysis
from .analysis import Analysis, BoardMatch, Pick, Thresholds, market_label

MAX_GAME_TIME = 300  # секунд игрового времени: позже линия уже учитывает ход игры


@dataclass
class LiveDraft:
    match_id: int
    game_time: int
    team_id_r: int | None
    team_id_d: int | None
    name_r: str
    name_d: str
    picks_r: list
    picks_d: list

    @property
    def complete(self) -> bool:
        return len(self.picks_r) == 5 and len(self.picks_d) == 5


def _is_radiant(p: dict) -> bool | None:
    if "is_radiant" in p:
        return bool(p["is_radiant"])
    if p.get("team") in (0, 1):
        return p["team"] == 0
    if isinstance(p.get("player_slot"), int):
        return p["player_slot"] < 128
    return None


def parse_live(payload) -> list[LiveDraft]:
    out = []
    for g in payload or []:
        if not isinstance(g, dict) or not (g.get("team_id_radiant") or g.get("team_name_radiant")):
            continue
        pr, pd = [], []
        for p in g.get("players") or []:
            side, hero = _is_radiant(p), p.get("hero_id")
            if side is None or not hero:
                continue
            (pr if side else pd).append((p.get("account_id"), int(hero)))
        out.append(LiveDraft(int(g.get("match_id") or 0), int(g.get("game_time") or 0),
                             g.get("team_id_radiant"), g.get("team_id_dire"),
                             g.get("team_name_radiant") or "", g.get("team_name_dire") or "", pr[:5], pd[:5]))
    return out


def find_draft(drafts: list[LiveDraft], model, bm: BoardMatch) -> tuple[LiveDraft, bool] | None:
    """Ищет игру этого матча; второе значение — True, если team1 играет за radiant."""
    a, b = analysis.match_team(model, bm.team1), analysis.match_team(model, bm.team2)
    n1, n2 = analysis.normalize(bm.team1), analysis.normalize(bm.team2)
    for d in drafts:
        ids = (d.team_id_r, d.team_id_d)
        if a and b and ids == (a[0], b[0]):
            return d, True
        if a and b and ids == (b[0], a[0]):
            return d, False
        nr, nd = analysis.normalize(d.name_r), analysis.normalize(d.name_d)
        if (nr, nd) == (n1, n2):
            return d, True
        if (nr, nd) == (n2, n1):
            return d, False
    return None


def draft_pick(model, bm: BoardMatch, d: LiveDraft, team1_radiant: bool, now: int) -> tuple[float, list[Pick]] | None:
    """Вероятность текущей карты для team1 после драфта и пики против линии Oddin на эту карту."""
    a, b = analysis.match_team(model, bm.team1), analysis.match_team(model, bm.team2)
    if not (a and b) or not hasattr(model, "predict_draft"):
        return None
    p1, p2 = (d.picks_r, d.picks_d) if team1_radiant else (d.picks_d, d.picks_r)
    p = model.predict_draft(a[0], b[0], now, p1, p2)
    if p is None:
        return None
    won, lost = bm.maps_score
    market = f"map_{won + lost + 1}_winner"
    picks = []
    for team, prob in ((bm.team1, p), (bm.team2, 1 - p)):
        q = bm.price(market, team)
        if q:
            picks.append(Pick(market, team, q.price, prob, q.fair_prob))
    return p, picks


def message(bm: BoardMatch, p: float, pick: Pick | None, placed: bool) -> str:
    head = f"🧩 После драфта: {bm.team1} — {bm.team2}, наша оценка карты {p:.0%} / {1 - p:.0%}"
    if pick is None:
        return head + ". Коэффициентов на карту нет."
    tail = "бумажная ставка сделана" if placed else "сигнал есть, но ставка по матчу уже была"
    return f"{head}\n✅ {market_label(pick.market)}: {pick.team} @ {pick.price:.2f}, EV {pick.ev:+.1%} — {tail}"


def roster_note(conn, model, bm: BoardMatch, d: LiveDraft, team1_radiant: bool, now: int) -> str:
    """Предупреждение, если в этой карте играет не основной состав."""
    from . import news

    notes = []
    for team, picks in ((bm.team1, d.picks_r if team1_radiant else d.picks_d), (bm.team2, d.picks_d if team1_radiant else d.picks_r)):
        found = analysis.match_team(model, team)
        lineup = frozenset(acc for acc, _ in picks if acc)
        if not found or len(lineup) < 5:
            continue
        r = news.roster_risk(conn, found[0], now, lineup)
        if r:
            notes.append("⚠️ " + r.text(team))
    return ("\n" + "\n".join(notes)) if notes else ""


def best_signal(picks: list[Pick], thresholds: Thresholds) -> Pick | None:
    good = [p for p in picks if thresholds.ok(p)]
    return max(good, key=lambda p: p.ev) if good else None


def as_analysis(bm: BoardMatch, picks: list[Pick]) -> Analysis:
    return Analysis(bm, (None, None), picks=picks)
