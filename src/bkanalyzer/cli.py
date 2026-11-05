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
from .sources import oddiwire as ow_mod
from .sources.oddiwire import OddiWire, find_dota_sport
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


def collect_oddiwire_once(conn, ow: OddiWire, maps: bool = True, log=print, max_calls: int | None = None) -> int:
    """Один снимок линии Dota из OddiWire: в эфире и до матча.

    Списки берём на tier=1 (там победа в матче). Победа на картах лежит в tier=3,
    её дозапрашиваем get_fixture только по матчам в эфире — после драфта это важнее всего.
    max_calls ограничивает число платных по квоте запросов в этом снимке.
    """
    left = 10**9 if max_calls is None else max_calls
    events: dict[str, dict] = {}
    for tool in ("get_live_odds", "get_prematch_odds"):
        if left <= 0:
            break
        payload = ow.call(tool, {"sport": ow_mod.DOTA2, "tier": 1, "limit": 100})
        left -= 1
        if isinstance(payload, dict) and payload.get("_note"):
            log(f"  {tool}: {payload['_note']}")
        for ev in ow_mod.board_events(payload):
            if ev.get("sport", ow_mod.DOTA2) == ow_mod.DOTA2 and ev.get("event_id"):
                events[str(ev["event_id"])] = ev
    rows: list[dict] = []
    for eid, ev in events.items():
        is_live = ev.get("event_type") == "live" or ev.get("status") == "LIVE"
        if maps and is_live and left > 0:
            try:
                full = ow.call("get_fixture", {"fixture_id": eid, "tier": 3})
                left -= 1
                if isinstance(full, dict):
                    full = full.get("event", full)
                    if isinstance(full, dict) and full.get("markets"):
                        ev = {**ev, "markets": full["markets"]}
            except ow_mod.RateLimited:
                raise
            except SourceError as exc:
                log(f"  {ev.get('name', eid)}: карты не получены ({exc})")
        rows += ow_mod.event_rows(ev)
    stamp = db._now()  # одна метка на весь снимок: по ней видно, какие матчи и рынки сейчас в ленте
    db.upsert_matches(conn, [ow_mod.event_match(e) for e in events.values()], source="oddiwire", updated_at=stamp)
    saved = db.save_feed_rows(conn, rows, source="oddiwire", taken_at=stamp)
    log(f"  матчей: {len(events)}, коэффициентов в снимке: {len(rows)}, новых/изменившихся: {saved}")
    return saved


def next_pause(used: int, limit: int, now_utc=None, min_interval: int = 600, per_snapshot: int = 3) -> float:
    """Пауза до следующего снимка, чтобы остаток дневной квоты растянулся до полуночи UTC."""
    from datetime import datetime, timezone

    now_utc = now_utc or datetime.now(timezone.utc)
    seconds_left = 86400 - (now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second)
    remaining = limit - used
    if remaining < per_snapshot:
        return max(float(min_interval), seconds_left + 60.0)
    snapshots = remaining // per_snapshot
    return max(float(min_interval), seconds_left / snapshots)

def simulate_step(s: Settings, conn, models, od=None, last_sync: list | None = None, notify=None, log=print) -> None:
    """После снимка линии: бумажные ставки по сигналам, обновление результатов и расчёт ставок."""
    from . import analysis, history, sim

    if od is not None and last_sync is not None and time.monotonic() - last_sync[0] > 3600:
        try:
            added = history.sync_pro_maps(conn, od, 730, log=lambda *_: None)
            from . import details

            details.sync_patches(conn, od)
            details.sync_players(conn, od, max_batches=3, log=lambda *_: None)
            last_sync[0] = time.monotonic()
            if added:
                log(f"  новых карт из OpenDota: {added}")
        except SourceError as exc:
            log(f"  OpenDota: {exc}")
    model, _ = models.get(conn)
    board = analysis.load_board(conn)
    now = int(time.time())
    analyses = [analysis.analyze(m, model, now) for m in board]
    cfg = s.sim()
    for b in sim.place_bets(conn, analyses, s.thresholds(), cfg):
        log("  " + sim.placed_message(b))
        if notify:
            notify(sim.placed_message(b))
    for b in sim.settle(conn):
        log("  " + sim.settled_message(b))
        if notify:
            notify(sim.settled_message(b))


def draft_step(s: Settings, conn, models, od, ow, seen: set, notify=None, log=print, now: int | None = None,
               max_game_time: int | None = None) -> list[dict]:
    """Раз в минуту: ищем готовые драфты матчей из линии и ставим «после драфта»."""
    from . import analysis, draft, sim
    from .sources import oddiwire as ow_mod

    now = now or int(time.time())
    max_game_time = draft.MAX_GAME_TIME if max_game_time is None else max_game_time
    live = [m for m in analysis.load_board(conn) if m.phase == "live"]
    if not live:
        return []
    drafts = [d for d in draft.parse_live(od.live()) if d.complete and d.match_id not in seen]
    if not drafts:
        return []
    model, _ = models.get(conn)
    placed = []
    for bm in live:
        found = draft.find_draft(drafts, model, bm)
        if not found:
            continue
        d, team1_radiant = found
        seen.add(d.match_id)
        if d.game_time > max_game_time:
            continue  # опоздали: линия уже учитывает ход игры
        # свежий коэффициент на эту карту (1 запрос к OddiWire)
        if ow is not None:
            try:
                used, limit = ow.budget()
                if used < limit:
                    full = ow.call("get_fixture", {"fixture_id": bm.fixture_id, "tier": 3})
                    full = full.get("event", full) if isinstance(full, dict) else {}
                    ev = {"event_id": bm.fixture_id, "event_type": "live", "status": "LIVE", "home": bm.team1, "away": bm.team2,
                          "starts": bm.start_time, "markets": (full or {}).get("markets") or []}
                    db.save_feed_rows(conn, ow_mod.event_rows(ev), source="oddiwire", taken_at=db._now())
                    bm = next((m for m in analysis.load_board(conn) if m.fixture_id == bm.fixture_id), bm)
            except SourceError as exc:
                log(f"  драфт: свежий коэффициент не получен ({exc})")
        res = draft.draft_pick(model, bm, d, team1_radiant, now)
        if not res:
            continue
        p, picks = res
        best = draft.best_signal(picks, s.thresholds())
        log(f"  драфт {bm.team1} — {bm.team2}: наша {p:.0%}, сигнал: {'да' if best else 'нет'}")
        if best is None:
            continue
        bet = sim.place_pick(conn, draft.as_analysis(bm, picks), best, "draft", s.sim())
        if notify:
            notify(draft.message(bm, p, best, bet is not None))
        if bet:
            placed.append(bet)
    return placed


def run_collector(s: Settings, conn, interval: int = 600, maps: bool = True, once: bool = False, stop=lambda: False, log=print,
                  simulate: bool = False, notify=None) -> int:
    """Цикл сбора OddiWire: держит дневную квоту и ждёт, когда сервис просит подождать."""
    from .sources.oddiwire import RateLimited

    from .bot import ModelCache

    ow = OddiWire(make_client(s.user_agent), s.oddiwire_api_key)
    models, last_sync, drafts_seen = ModelCache(), [0.0], set()
    od = OpenDota(make_client(s.user_agent), s.opendota_api_key) if simulate else None
    if not s.oddiwire_api_key:
        log("ODDIWIRE_API_KEY не задан: работаем на общем демо-ключе (10 запросов за 5 минут на всех).")
    while not stop():
        pause = float(interval)
        try:
            before, limit = ow.budget()
            n = collect_oddiwire_once(conn, ow, maps=maps, max_calls=max(0, limit - before), log=log)
            used, limit = ow.budget()
            # сколько запросов реально ушло на снимок (после полуночи UTC счётчик обнуляется)
            spent = used - before if used >= before else used
            pause = next_pause(used, limit, min_interval=interval, per_snapshot=max(1, spent))
            log(f"{time.strftime('%H:%M:%S')} сохранено {n}; квота {used}/{limit}; следующий снимок через {pause / 60:.0f} мин")
            if simulate:
                simulate_step(s, conn, models, od, last_sync, notify, log)
        except RateLimited as exc:
            pause = exc.retry_after + 5
            log(f"{time.strftime('%H:%M:%S')} {exc}")
        except SourceError as exc:
            log(f"{time.strftime('%H:%M:%S')} ошибка: {exc}")
            ow.reset()
        except Exception as exc:  # сбор не должен останавливаться из-за ошибки анализа
            log(f"{time.strftime('%H:%M:%S')} сбой: {exc!r}")  # сессия MCP могла истечь — следующий снимок откроет новую
        if once:
            return 0
        deadline = time.monotonic() + pause
        next_draft = 0.0
        while time.monotonic() < deadline and not stop():
            if simulate and od is not None and time.monotonic() >= next_draft:
                next_draft = time.monotonic() + 60
                try:
                    draft_step(s, conn, models, od, ow, drafts_seen, notify, log)
                except Exception as exc:  # драфт — дополнительная функция, сбор линии не должен падать
                    log(f"  драфт: {exc!r}")
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    return 0


def run_bot(s: Settings, collect: bool = True, interval: int = 600) -> int:
    import threading

    from .bot import Bot, run_polling

    if not s.telegram_bot_token:
        print("Нет TELEGRAM_BOT_TOKEN в .env")
        return 1
    if not s.telegram_allowed_ids:
        print("Нет TELEGRAM_ALLOWED_IDS в .env — бот никому не ответит")
        return 1
    from .bot import Outbox, check_telegram, make_telegram_client

    tg = make_telegram_client(s.telegram_proxy)
    outbox = None
    if collect:
        outbox = Outbox(make_telegram_client(s.telegram_proxy), s.telegram_bot_token, s.telegram_allowed_ids)

        def collector():
            run_collector(s, db.connect(Path(s.db_path)), interval, log=lambda m: print(f"[линия] {m}"), simulate=True, notify=outbox.add)

        threading.Thread(target=collector, daemon=True).start()
    bot = Bot(db.connect(Path(s.db_path)), s.telegram_allowed_ids, s.thresholds(), s.timezone, sim=s.sim(),
              export_dir=Path(s.db_path).parent / "exports")
    status = check_telegram(s.telegram_bot_token, tg)
    print(f"Telegram{' через прокси' if s.telegram_proxy else ''}: {status}")
    print("Бот запущен. Напишите ему /today в Telegram. Остановить: Ctrl+C")
    try:
        run_polling(s.telegram_bot_token, bot, tg, outbox=outbox)
    except KeyboardInterrupt:
        pass
    return 0


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
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    upcoming = [fx for fx in fixtures if str(fx.get("startTime") or "") > now[:19]] or fixtures
    lines = [f"Матчей Dota на 3 дня: {len(fixtures)}, ещё не начались: {len([f for f in fixtures if str(f.get('startTime') or '') > now[:19]])}"]
    shown = 0
    for fx in upcoming[:5]:
        fid = str(fx.get("fixtureId") or fx.get("id"))
        lines.append(f"Матч {fid}: {fx.get('participant1Name')} — {fx.get('participant2Name')}, старт {fx.get('startTime')}")
        try:
            payload = op.odds(fid, found or None)
        except SourceError as exc:
            lines.append(f"  ошибка: {exc}")
            continue
        rows = flatten_odds(payload)
        if not rows:
            # Структура ответа не совпала с ожидаемой или коэффициентов нет — печатаем начало ответа как есть
            keys = list(payload) if isinstance(payload, dict) else type(payload).__name__
            lines.append(f"  коэффициентов не разобрано; ключи ответа: {keys}")
            lines.append("  " + json.dumps(payload, ensure_ascii=False)[:1500])
            continue
        by_market: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            by_market.setdefault((r["bookmaker"], r["market"]), []).append(f"{r['outcome']}={r['price']}")
        for (bm, mid), outs in sorted(by_market.items())[:40]:
            lines.append(f"  {bm:<8} рынок {mid:<8} {names.get(mid, ''):<30} {' '.join(outs)}")
        shown += 1
        if shown >= 2:
            break
    return "\n".join(lines)


def _sport_arg(tools: list[dict], tool: str) -> str | None:
    """Имя параметра «вид спорта» у инструмента OddiWire по его inputSchema."""
    for t in tools:
        if t.get("name") == tool:
            for prop in (t.get("inputSchema") or {}).get("properties", {}):
                if "sport" in prop.lower():
                    return prop
    return None


def oddiwire_sample(s: Settings, client=None, out: Path | None = Path("data/oddiwire_sample.json")) -> str:
    """Разведка OddiWire: инструменты, тариф, виды спорта и линия Dota до матча.

    Полный ответ сохраняется в data/oddiwire_sample.json — его и нужно прислать.
    """
    import json

    ow = OddiWire(client or make_client(s.user_agent), s.oddiwire_api_key)
    dump: dict = {}
    lines: list[str] = []
    try:
        tools = ow.tools()
        dump["tools"] = tools
        lines.append("Инструменты: " + ", ".join(t.get("name", "?") for t in tools))
        for name, args in (("get_my_plan", {}), ("list_sports", {})):
            try:
                dump[name] = ow.call(name, args)
            except SourceError as exc:
                dump[name] = f"ошибка: {exc}"
        sport = find_dota_sport(dump.get("list_sports"))
        lines.append(f"Dota 2 в list_sports: {sport or 'не найдена'}")
        if sport:
            for name in ("get_coverage", "get_prematch_odds", "get_live_odds"):
                arg = _sport_arg(tools, name)
                args = {arg: sport} if arg else {}
                if name != "get_coverage":
                    args["limit"] = 10
                try:
                    dump[name] = ow.call(name, args)
                except SourceError as exc:
                    dump[name] = f"ошибка: {exc}"
    except SourceError as exc:
        lines.append(f"[FAIL] {exc}")
    for key, val in dump.items():
        if key == "tools":
            continue
        text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        lines.append(f"--- {key} ({len(text)} символов) ---\n{text[:1500]}")
    if out is not None and dump:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        lines.append(f"Полный ответ: {out} — пришлите этот файл в тред.")
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

    sub.add_parser("oddiwire-sample", help="проверить OddiWire (линия Oddin, как у BetBoom): инструменты и линия Dota")

    p_sm = sub.add_parser("sync-matches", help="загрузить историю про-карт из OpenDota")
    p_sm.add_argument("--days", type=int, default=730)
    p_sm.add_argument("--no-details", action="store_true", help="не загружать составы и патчи")

    p_re = sub.add_parser("rating-eval", help="проверить рейтинг на истории и показать топ команд")
    p_re.add_argument("--eval-days", type=int, default=180, help="за сколько последних дней оценивать прогнозы")
    p_re.add_argument("--top", type=int, default=20)

    p_me = sub.add_parser("model-eval", help="сравнить модель с чистым рейтингом на последних днях")
    p_me.add_argument("--eval-days", type=int, default=180)

    p_pr = sub.add_parser("predict", help="вероятность победы в серии по рейтингу")
    p_pr.add_argument("team1")
    p_pr.add_argument("team2")
    p_pr.add_argument("--bo", type=int, default=3, choices=[1, 2, 3, 5])

    p_co = sub.add_parser("collect-odds", help="сохранять снимки коэффициентов Dota (OddiWire) в SQLite")
    p_co.add_argument("--interval", type=int, default=600, help="секунд между снимками")
    p_co.add_argument("--source", choices=["oddiwire", "oddspapi"], default="oddiwire")
    p_co.add_argument("--no-maps", action="store_true", help="не запрашивать коэффициенты на карты (меньше запросов)")

    p_sr = sub.add_parser("sim-reset", help="удалить все бумажные ставки и начать симулятор заново")
    p_sr.add_argument("--yes", action="store_true", help="подтверждение")

    p_al = sub.add_parser("alias", help="справочник синонимов: название в линии → название в истории OpenDota")
    p_al.add_argument("name", nargs="?", help="как команда названа в линии")
    p_al.add_argument("target", nargs="?", help="как она названа в OpenDota")
    p_al.add_argument("--list", action="store_true", help="показать справочник")
    p_al.add_argument("--remove", action="store_true", help="удалить синоним для name")
    sub.add_parser("unmatched", help="команды из линии, которых нет в истории, и похожие названия")

    p_ex = sub.add_parser("export", help="выгрузка в xlsx (матчи, ставки, итоги) и CSV")
    p_ex.add_argument("--out", type=Path, default=None, help="папка для файлов (по умолчанию data/exports)")

    p_bot = sub.add_parser("bot", help="запустить Telegram-бота (и сбор коэффициентов в фоне)")
    p_bot.add_argument("--no-collect", action="store_true", help="не собирать коэффициенты в этом же процессе")
    p_bot.add_argument("--interval", type=int, default=600, help="минимальная пауза между снимками линии, секунд")
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

    if args.cmd == "oddiwire-sample":
        print(oddiwire_sample(s))
        return 0

    if args.cmd == "sync-matches":
        from . import history

        conn = db.connect(Path(s.db_path))
        od = OpenDota(make_client(s.user_agent), s.opendota_api_key)
        print(f"Лиг: {history.sync_leagues(conn, od)}")
        print(f"Новых карт: {history.sync_pro_maps(conn, od, args.days)}")
        if not args.no_details:
            from . import details

            print(f"Патчей: {details.sync_patches(conn, od)}")
            print("Составы Tier 1–2 (пакетами по 400 карт):")
            print(f"Карт с составом добавлено: {details.sync_players(conn, od)}")
        return 0

    if args.cmd == "rating-eval":
        print(rating_report(db.connect(Path(s.db_path)), args.eval_days, args.top))
        return 0

    if args.cmd == "predict":
        print(predict_report(db.connect(Path(s.db_path)), args.team1, args.team2, args.bo, s.oddspapi_winner_market))
        return 0

    if args.cmd == "collect-odds":
        conn = db.connect(Path(s.db_path))
        if args.source == "oddspapi":
            while True:
                try:
                    print(f"{time.strftime('%H:%M:%S')} сохранено {collect_odds_once(s, conn)} коэффициентов")
                except SourceError as exc:
                    print(f"{time.strftime('%H:%M:%S')} ошибка: {exc}", file=sys.stderr)
                if args.once:
                    return 0
                time.sleep(args.interval)
        return run_collector(s, conn, args.interval, maps=not args.no_maps, once=args.once)

    if args.cmd == "model-eval":
        from .bot import Bot, ModelCache

        conn = db.connect(Path(s.db_path))
        b = Bot(conn, (0,))
        b.models = ModelCache(args.eval_days)
        t0 = time.monotonic()
        print(b.model_report())
        print(f"(обучение заняло {time.monotonic() - t0:.0f} с)")
        return 0

    if args.cmd == "sim-reset":
        from . import sim

        conn = db.connect(Path(s.db_path))
        sim.ensure_schema(conn)
        n = conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0]
        if not args.yes:
            print(f"Будет удалено бумажных ставок: {n}. Повторите с --yes")
            return 1
        conn.execute("DELETE FROM paper_bets")
        conn.commit()
        print(f"Удалено бумажных ставок: {n}. Симулятор начнётся заново.")
        return 0

    if args.cmd == "alias":
        from . import aliases

        conn = db.connect(Path(s.db_path))
        if args.list or not args.name:
            rows = aliases.listing(conn)
            print("\n".join(f"{a} → {t}" for a, t in rows) if rows else "Справочник пуст. Добавить: bk alias \"В линии\" \"В OpenDota\"")
            return 0
        if args.remove:
            print("Удалено" if aliases.remove(conn, args.name) else "Такого синонима нет")
            return 0
        if not args.target:
            print("Укажите оба названия: bk alias \"В линии\" \"В OpenDota\"")
            return 1
        try:
            print(f"Записано: {args.name} → {aliases.add(conn, args.name, args.target)}")
        except ValueError as exc:
            print(f"Не записано: {exc}")
            return 1
        return 0

    if args.cmd == "unmatched":
        from . import aliases
        from .bot import ModelCache

        conn = db.connect(Path(s.db_path))
        model, _ = ModelCache().get(conn)
        rows = aliases.unmatched(conn, model)
        if not rows:
            print("Все команды из линии найдены в истории.")
        for team, hint in rows:
            print(f"{team}: " + (f"похожие — {', '.join(hint)}" if hint else "похожих нет (возможно, не Tier 1–2 или новая команда)"))
        if rows:
            print('Связать: bk alias "Название в линии" "Название в OpenDota"')
        return 0

    if args.cmd == "export":
        from .bot import Bot

        out = args.out or Path(s.db_path).parent / "exports"
        b = Bot(db.connect(Path(s.db_path)), (0,), s.thresholds(), s.timezone, sim=s.sim(), export_dir=out)
        r = b.export()
        print(r.text)
        for p in r.files:
            print(f"  {p}")
        return 0

    if args.cmd == "bot":
        return run_bot(s, collect=not args.no_collect, interval=args.interval)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
