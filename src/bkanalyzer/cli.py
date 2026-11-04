"""Командная строка: bk check-sources | bk betboom-discover | bk collect-odds"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from . import db
from .config import Settings, load_settings
from .http import SourceError, make_client
from .sources.liquipedia import Liquipedia
from .sources.oddspapi import OddsPapi, find_bookmakers, flatten_odds
from .sources.opendota import OpenDota
from .sources.stratz import Stratz


def _check(name: str, fn) -> bool:
    try:
        detail = fn()
    except Exception as exc:  # отчёт должен дойти до конца, даже если источник упал
        print(f"[FAIL] {name}: {exc}")
        return False
    print(f"[ OK ] {name}: {detail}")
    return True


def check_sources(s: Settings, client=None) -> int:
    client = client or make_client(s.user_agent)
    od = OpenDota(client, s.opendota_api_key)
    results = []

    def od_pro():
        m = od.pro_matches()
        last = m[0] if m else {}
        return f"{len(m)} последних про-карт, свежая: {last.get('radiant_name')} vs {last.get('dire_name')} ({last.get('league_name')})"

    def od_heroes():
        h = od.hero_stats()
        return f"{len(h)} героев, поля Immortal: {'8_pick' in (h[0] if h else {})}"

    results.append(_check("OpenDota /proMatches", od_pro))
    results.append(_check("OpenDota /heroStats", od_heroes))
    results.append(_check("OpenDota /live", lambda: f"{len(od.live())} игр в эфире"))

    def stratz():
        v = Stratz(client, s.stratz_token).game_versions()
        return f"последний патч {v[0]['name']}" if v else "пустой список патчей"

    results.append(_check("STRATZ GraphQL", stratz))
    results.append(_check("Liquipedia API", lambda: Liquipedia(client).siteinfo().get("sitename")))

    def oddspapi():
        op = OddsPapi(client, s.oddspapi_api_key, s.oddspapi_base_url)
        sports = op.sports()
        dota = [x for x in sports if "dota" in str(x).lower()]
        found = find_bookmakers(op.bookmakers())
        bm = ", ".join(f"{k}: {'есть' if v else 'НЕТ'}" for k, v in found.items())
        today = date.today()
        fx = op.fixtures(today.isoformat(), (today + timedelta(days=2)).isoformat())
        return f"Dota в списке спортов: {dota[:1] or 'не найдена'}; БК — {bm}; матчей Dota на 3 дня: {len(fx)}"

    results.append(_check("OddsPapi", oddspapi))
    ok = sum(results)
    print(f"\nИтого: {ok} из {len(results)} проверок прошли.")
    return 0 if ok == len(results) else 1


def collect_odds_once(s: Settings, conn, client=None) -> int:
    client = client or make_client(s.user_agent)
    op = OddsPapi(client, s.oddspapi_api_key, s.oddspapi_base_url)
    found = [v.get("slug") for v in find_bookmakers(op.bookmakers()).values() if v and v.get("slug")]
    today = date.today()
    saved = 0
    for fx in op.fixtures(today.isoformat(), (today + timedelta(days=1)).isoformat()):
        fid = fx.get("fixtureId") or fx.get("id")
        if not fid:
            continue
        try:
            rows = flatten_odds(op.odds(str(fid), found or None))
        except SourceError as exc:
            print(f"  {fid}: {exc}")
            continue
        saved += db.save_odds(conn, rows, source="oddspapi", fixture=fx)
    return saved


def oddspapi_sample(s: Settings, client=None) -> str:
    """Рынки и исходы одного ближайшего матча Dota в OddsPapi — по ним настраиваем ODDSPAPI_WINNER_MARKET."""
    client = client or make_client(s.user_agent)
    op = OddsPapi(client, s.oddspapi_api_key, s.oddspapi_base_url)
    found = [v.get("slug") for v in find_bookmakers(op.bookmakers()).values() if v and v.get("slug")]
    today = date.today()
    fixtures = op.fixtures(today.isoformat(), (today + timedelta(days=3)).isoformat())
    if not fixtures:
        return "Матчей Dota на ближайшие 3 дня в OddsPapi нет"
    names: dict[str, str] = {}
    try:
        markets = op.markets()
        for m in markets if isinstance(markets, list) else []:
            mid = str(m.get("marketId") or m.get("id") or "")
            names[mid] = str(m.get("marketName") or m.get("name") or "")
    except SourceError:
        pass
    lines = []
    for fx in fixtures[:2]:
        fid = str(fx.get("fixtureId") or fx.get("id"))
        lines.append(f"Матч {fid}: {fx.get('participant1Name')} — {fx.get('participant2Name')}, старт {fx.get('startTime')}")
        rows = flatten_odds(op.odds(fid, found or None))
        by_market: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            by_market.setdefault((r["bookmaker"], r["market"]), []).append(f"{r['outcome']}={r['price']}")
        for (bm, mid), outs in sorted(by_market.items())[:40]:
            lines.append(f"  {bm:<8} рынок {mid:<8} {names.get(mid, ''):<30} {' '.join(outs)}")
    return "\n".join(lines)


def inspect_run(run_dir: Path) -> str:
    """Сводка по папке разведки: сколько кадров в каждую сторону и какие строки в них видны."""
    import json

    from .bookmakers.protowire import WireError, decode, strings

    index = json.loads((run_dir / "index.json").read_text(encoding="utf-8"))
    by_url: dict[tuple[str, str], int] = {}
    seen: list[str] = []
    for item in index:
        key = (item["url"].split("?")[0], item.get("dir", "in"))
        by_url[key] = by_url.get(key, 0) + 1
        path = run_dir / item["file"]
        if path.suffix == ".bin":
            try:
                for s in strings(decode(path.read_bytes())):
                    if len(s) > 2 and not s.startswith("http") and s not in seen:
                        seen.append(s)
            except WireError:
                pass
    lines = [f"Записей: {len(index)}"]
    lines += [f"  {d:>3} {n:>5}  {u}" for (u, d), n in sorted(by_url.items())]
    lines.append("Строки из кадров (первые 40): " + "; ".join(seen[:40]))
    return "\n".join(lines)


def parse_run(run_dir: Path, conn=None) -> str:
    """Матчи Dota 2 с коэффициентами на победу из записи разведки BetBoom."""
    from .bookmakers.betboom_parse import parse_frame, snapshot_rows, winner_table

    matches: dict = {}
    outcomes: dict = {}
    for path in sorted(run_dir.glob("*_in_*.bin")):
        m, o = parse_frame(path.read_bytes())
        matches.update({x["match_id"]: x for x in m})
        outcomes.update({x["uid"]: x for x in o})
    if conn is not None:
        db.upsert_matches(conn, list(matches.values()), source="betboom")
        db.save_price_changes(conn, snapshot_rows(list(outcomes.values())), source="betboom")
    lines = [f"Матчей: {len(matches)}, исходов: {len(outcomes)}"]
    for r in winner_table(list(matches.values()), list(outcomes.values())):
        p1 = f"{r['p1']:.2f}" if r["p1"] else "  —  "
        p2 = f"{r['p2']:.2f}" if r["p2"] else "  —  "
        lines.append(f"  {(r['start_time'] or '')[:16]}  {r['team1']} — {r['team2']}  [{r['format'] or '?'}]  П1 {p1}  П2 {p2}")
    return "\n".join(lines)


def _fitted(conn, eval_days: int = 180):
    from . import history, rating

    history.ensure_schema(conn)
    maps = history.tier_maps(conn)
    since = (maps[-1]["start_time"] - eval_days * 86400) if maps else 0
    return rating.fit(maps, eval_since=since), len(maps)


def rating_report(conn, eval_days: int = 180, top: int = 20) -> str:
    (model, ev), total = _fitted(conn, eval_days)
    if not total:
        return "В базе нет карт Tier 1–2. Сначала: bk sync-matches"
    lines = [
        f"Карт Tier 1–2 в базе: {total}, команд: {len(model.teams)}",
        f"Проверка на последних {eval_days} днях: {ev.n} карт",
        f"  точность {ev.accuracy:.1%}, Brier {ev.brier:.4f} (0.25 = монетка), log-loss {ev.log_loss:.4f} (0.693 = монетка)",
        "  Калибровка (прогноз фаворита → как часто он реально выигрывал):",
    ]
    lines += [f"    {rng:>8}: карт {n:>5}, прогноз {p:.1%}, факт {y:.1%}" for rng, n, p, y in ev.buckets]
    lines.append(f"Топ-{top} по рейтингу (не меньше 20 карт):")
    ranked = sorted((st for st in model.teams.values() if st.games >= 20), key=lambda st: -st.rating)[:top]
    lines += [f"  {i:>2}. {st.rating:7.1f}  {st.name} ({st.games} карт)" for i, st in enumerate(ranked, 1)]
    return "\n".join(lines)


def _latest_betboom_prices(conn, t1: str, t2: str) -> dict | None:
    row = conn.execute(
        """SELECT match_id, team1, team2 FROM bookmaker_matches
           WHERE source='betboom' AND ((lower(team1)=lower(?) AND lower(team2)=lower(?)) OR (lower(team1)=lower(?) AND lower(team2)=lower(?)))
           ORDER BY updated_at DESC LIMIT 1""",
        (t1, t2, t2, t1),
    ).fetchone()
    if not row:
        return None
    prices = {}
    for side in ("П1", "П2"):
        r = conn.execute(
            """SELECT price FROM odds_snapshots WHERE source='betboom' AND fixture_id=? AND market='Исход матча | Матч' AND outcome=?
               ORDER BY id DESC LIMIT 1""",
            (row[0], side),
        ).fetchone()
        prices[side] = r[0] if r else None
    swapped = row[1].lower() != t1.lower()
    return {"t1": prices["П2"] if swapped else prices["П1"], "t2": prices["П1"] if swapped else prices["П2"]}


def predict_report(conn, team1: str, team2: str, best_of: int = 3, winner_market: str | None = None) -> str:
    from . import rating

    (model, _), total = _fitted(conn)
    if not total:
        return "В базе нет карт Tier 1–2. Сначала: bk sync-matches"
    picked = []
    for q in (team1, team2):
        found = rating.find_team(model, q)
        if not found:
            return f"Команда «{q}» не найдена в истории Tier 1–2"
        picked.append(found[0])
    (a, sa), (b, sb) = picked
    import time as _t

    now = int(_t.time())
    p = model.predict(a, b, now)
    probs = rating.series_probabilities(p, best_of)
    lines = [
        f"{sa.name} ({sa.rating:.0f}, {sa.games} карт) — {sb.name} ({sb.rating:.0f}, {sb.games} карт), Bo{best_of}",
        f"  карта: {p:.1%} / {1 - p:.1%}",
    ]
    names = {"win": sa.name, "lose": sb.name, "draw": "ничья"}
    for k, v in probs.items():
        lines.append(f"  {names[k]}: {v:.1%} (честный коэффициент {1 / v:.2f})" if v > 0 else f"  {names[k]}: 0%")
    quotes = []
    bb = _latest_betboom_prices(conn, sa.name, sb.name)
    if bb and (bb["t1"] or bb["t2"]):
        quotes.append(("betboom", bb["t1"], bb["t2"]))
    quotes += db.latest_winner_odds(conn, sa.name, sb.name, winner_market or "")
    if quotes:
        lines.append("  Последние коэффициенты на матч и перевес (EV) нашей оценки:")
        for bookmaker, p1, p2 in quotes:
            parts = [f"{name} {price:.2f} ({prob * price - 1:+.1%})" for name, price, prob in ((sa.name, p1, probs["win"]), (sb.name, p2, probs["lose"])) if price]
            lines.append(f"    {bookmaker}: " + ", ".join(parts))
    else:
        lines.append("  Коэффициентов по этому матчу в базе нет: bk collect-odds или bk odds-add")
    lines.append("  Только рейтинг по результатам карт; форма, патч, состав и драфт пока не учитываются.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bk", description="Анализатор ставок Dota 2 — этап 0")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-sources", help="проверить доступ ко всем источникам данных")

    p_bb = sub.add_parser("betboom-discover", help="записать JSON-запросы публичной линии BetBoom")
    p_bb.add_argument("--url", default=None)
    p_bb.add_argument("--seconds", type=int, default=120)
    p_bb.add_argument("--headless", action="store_true")

    p_bi = sub.add_parser("betboom-inspect", help="кратко показать, что записала разведка BetBoom")
    p_bi.add_argument("run_dir", type=Path)

    p_bp = sub.add_parser("betboom-parse", help="разобрать запись разведки: матчи и коэффициенты П1/П2")
    p_bp.add_argument("run_dir", type=Path)
    p_bp.add_argument("--save", action="store_true", help="сохранить коэффициенты в базу")

    p_oa = sub.add_parser("odds-add", help="вручную записать коэффициенты на победу в матче (например, BetBoom)")
    p_oa.add_argument("team1")
    p_oa.add_argument("team2")
    p_oa.add_argument("--p1", type=float, required=True, help="коэффициент на победу team1")
    p_oa.add_argument("--p2", type=float, required=True, help="коэффициент на победу team2")
    p_oa.add_argument("--bookmaker", default="betboom")

    sub.add_parser("oddspapi-sample", help="показать рынки OddsPapi на одном матче Dota (чтобы найти рынок «победа в матче»)")

    p_sm = sub.add_parser("sync-matches", help="загрузить историю про-карт из OpenDota")
    p_sm.add_argument("--days", type=int, default=730)

    p_re = sub.add_parser("rating-eval", help="проверить рейтинг на истории и показать топ команд")
    p_re.add_argument("--eval-days", type=int, default=180, help="за сколько последних дней оценивать прогнозы")
    p_re.add_argument("--top", type=int, default=20)

    p_pr = sub.add_parser("predict", help="вероятность победы в серии по рейтингу")
    p_pr.add_argument("team1")
    p_pr.add_argument("team2")
    p_pr.add_argument("--bo", type=int, default=3, choices=[1, 2, 3, 5])

    p_co = sub.add_parser("collect-odds", help="сохранять снимки коэффициентов OddsPapi в SQLite")
    p_co.add_argument("--interval", type=int, default=600, help="секунд между снимками")
    p_co.add_argument("--once", action="store_true")

    args = parser.parse_args(argv)
    s = load_settings()

    if args.cmd == "check-sources":
        return check_sources(s)

    if args.cmd == "betboom-discover":
        from .bookmakers import betboom

        run_dir = betboom.discover(args.url or betboom.DEFAULT_URL, seconds=args.seconds, headless=args.headless)
        print(f"Готово. Ответы сохранены в {run_dir}. Проверьте: bk betboom-inspect {run_dir} — затем заархивируйте папку и пришлите в тред.")
        return 0

    if args.cmd == "betboom-inspect":
        print(inspect_run(args.run_dir))
        return 0

    if args.cmd == "betboom-parse":
        conn = db.connect(Path(s.db_path)) if args.save else None
        print(parse_run(args.run_dir, conn))
        return 0

    if args.cmd == "odds-add":
        conn = db.connect(Path(s.db_path))
        db.add_manual_winner_odds(conn, args.bookmaker, args.team1, args.team2, args.p1, args.p2)
        print(f"Записано: {args.bookmaker} {args.team1} {args.p1} — {args.team2} {args.p2}")
        return 0

    if args.cmd == "oddspapi-sample":
        print(oddspapi_sample(s))
        return 0

    if args.cmd == "sync-matches":
        from . import history

        conn = db.connect(Path(s.db_path))
        od = OpenDota(make_client(s.user_agent), s.opendota_api_key)
        print(f"Лиг: {history.sync_leagues(conn, od)}")
        print(f"Новых карт: {history.sync_pro_maps(conn, od, args.days)}")
        return 0

    if args.cmd == "rating-eval":
        print(rating_report(db.connect(Path(s.db_path)), args.eval_days, args.top))
        return 0

    if args.cmd == "predict":
        print(predict_report(db.connect(Path(s.db_path)), args.team1, args.team2, args.bo, s.oddspapi_winner_market))
        return 0

    if args.cmd == "collect-odds":
        conn = db.connect(Path(s.db_path))
        while True:
            try:
                n = collect_odds_once(s, conn)
                print(f"{time.strftime('%H:%M:%S')} сохранено {n} коэффициентов")
            except SourceError as exc:
                print(f"{time.strftime('%H:%M:%S')} ошибка: {exc}", file=sys.stderr)
            if args.once:
                return 0
            time.sleep(args.interval)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
