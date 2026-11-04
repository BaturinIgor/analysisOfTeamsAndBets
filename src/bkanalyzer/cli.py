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

    p_bc = sub.add_parser("betboom-collect", help="держать линию BetBoom открытой и писать изменения коэффициентов в базу")
    p_bc.add_argument("--url", default=None)
    p_bc.add_argument("--minutes", type=int, default=0, help="0 — пока не остановите Ctrl+C")
    p_bc.add_argument("--headless", action="store_true")

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

    if args.cmd == "betboom-collect":
        from .bookmakers import betboom

        conn = db.connect(Path(s.db_path))
        betboom.collect(conn, args.url or betboom.DOTA_URL, minutes=args.minutes, headless=args.headless)
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
