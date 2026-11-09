"""Симулятор бумажных ставок (ТР-20 … ТР-23).

- По каждому сигналу делается бумажная ставка: не больше одной на матч в каждом режиме
  («до матча», «после драфта», «в эфире» считаются раздельно, у каждого свой банк).
- Стратегии идут параллельно на одних и тех же сигналах (SIM_STRATEGIES): у каждой
  стратегии в каждом режиме свой банк, поэтому их можно честно сравнить.
- Сумма — по стратегии: фиксированная, процент банка или Келли.
- Расчёт — по результатам карт из OpenDota (bk sync-matches / автообновление в боте).
- CLV для ставок до матча — по последнему коэффициенту перед стартом.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from .analysis import Analysis, Thresholds, is_draw, market_label, normalize
from .games import base_mode, by_key, by_mode

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_bets (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,              -- prematch | draft | live
    fixture_id TEXT NOT NULL,
    start_time TEXT,
    team1 TEXT, team2 TEXT,
    market TEXT NOT NULL,            -- match_winner | map_N_winner
    pick TEXT NOT NULL,              -- команда
    price REAL NOT NULL,
    prob REAL NOT NULL,
    book_prob REAL,
    stake REAL NOT NULL,
    bank_before REAL NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | won | lost | void
    profit REAL,
    closing_price REAL,
    settled_at TEXT,
    note TEXT,
    UNIQUE (fixture_id, mode, strategy)
);
"""

MODES = ("prematch", "draft", "live", "longshot", "cs_prematch", "cs_live", "cs_longshot",
         "lol_prematch", "lol_live", "lol_longshot")
MODE_LABEL = {"prematch": "до матча", "draft": "после драфта", "live": "в эфире",
              "longshot": "аутсайдеры (на заметку)", "cs_prematch": "CS2 до матча", "cs_live": "CS2 в эфире",
              "cs_longshot": "CS2 аутсайдеры (на заметку)", "lol_prematch": "LoL до матча", "lol_live": "LoL в эфире",
              "lol_longshot": "LoL аутсайдеры (на заметку)"}
# «аутсайдеры»: модель расходится с рынком сильнее SIGNAL_MAX_GAP. Такие ставки не делаем,
# а записываем на заметку фиксированной суммой без уведомлений — чтобы проверить, кто прав.
QUIET_MODES = ("longshot", "cs_longshot", "lol_longshot")
OPTIONAL_MODES = ("cs_prematch", "cs_live", "lol_prematch", "lol_live")  # в отчёте — только если по ним уже есть ставки


STRATEGY_LABEL = {"fixed": "фикс. сумма", "percent": "% банка", "kelly": "Келли"}


@dataclass
class SimSettings:
    bank: float = 25000.0
    strategy: str = "fixed"      # fixed | percent | kelly — текущая (для расчёта суммы)
    stake: float = 1250.0        # для fixed (5% стартового банка)
    percent: float = 0.07        # для percent
    kelly: float = 1.0           # доля Келли (с потолком max_stake_share)
    max_stake_share: float = 0.1  # не больше 10% банка в одной ставке
    strategies: tuple = ()       # параллельные стратегии; пусто — только strategy
    # Эксперимент (SIM_EXPERIMENTS): свои пороги сигнала и свои банки на тех же коэффициентах.
    # Ставки эксперимента лежат в той же таблице, стратегия записана как «профиль:стратегия».
    profile: str = ""
    thresholds: Thresholds | None = None
    experiments: tuple = ()      # у основного профиля: SimSettings экспериментов

    @property
    def key(self) -> str:
        return f"{self.profile}:{self.strategy}" if self.profile else self.strategy

    def all(self) -> list["SimSettings"]:
        return [replace(self, strategy=name, strategies=(), experiments=()) for name in (self.strategies or (self.strategy,))]

    def describe(self) -> str:
        parts = {"fixed": f"фикс. {self.stake:.0f}", "percent": f"{self.percent:.0%} банка", "kelly": f"Келли ×{self.kelly:g}"}
        return ", ".join(parts.get(c.strategy, c.strategy) for c in self.all())


def ensure_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='paper_bets'").fetchone()
    if row and "UNIQUE (fixture_id, mode)" in row[0]:
        # старая схема: одна стратегия на матч — переносим ставки в новую таблицу
        conn.executescript("ALTER TABLE paper_bets RENAME TO paper_bets_old;" + SCHEMA +
                           "INSERT INTO paper_bets SELECT * FROM paper_bets_old; DROP TABLE paper_bets_old;")
    conn.executescript(SCHEMA)


def bank(conn: sqlite3.Connection, mode: str, cfg: SimSettings) -> float:
    """Банк стратегии в режиме = стартовый + прибыль по рассчитанным − суммы открытых ставок."""
    q = "SELECT COALESCE(SUM({}), 0) FROM paper_bets WHERE mode=? AND strategy=? AND status {}"
    settled = conn.execute(q.format("profit", "IN ('won','lost','void')"), (mode, cfg.key)).fetchone()[0]
    open_stakes = conn.execute(q.format("stake", "= 'open'"), (mode, cfg.key)).fetchone()[0]
    return cfg.bank + settled - open_stakes


def stake_for(cfg: SimSettings, bank_now: float, price: float, prob: float) -> float:
    if bank_now <= 0:
        return 0.0
    if cfg.strategy == "percent":
        amount = bank_now * cfg.percent
    elif cfg.strategy == "kelly":
        edge = prob * price - 1
        amount = bank_now * cfg.kelly * max(0.0, edge / (price - 1)) if price > 1 else 0.0
    else:
        amount = cfg.stake
    return round(min(amount, bank_now * cfg.max_stake_share, bank_now), 2)


def place_pick(conn: sqlite3.Connection, a: Analysis, pick, mode: str, cfg: SimSettings, now: datetime | None = None) -> dict | None:
    """Бумажная ставка по сигналу — по одной на каждую стратегию, если в этом режиме по матчу ещё не ставили.

    Возвращает первую ставку и суммы всех стратегий в поле stakes (для одного уведомления).
    """
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    keys = [c.key for c in cfg.all()]
    if conn.execute(f"SELECT 1 FROM paper_bets WHERE fixture_id=? AND mode=? AND strategy IN ({', '.join('?' * len(keys))})",
                    (a.match.fixture_id, mode, *keys)).fetchone():
        return None
    if result_known(conn, a, pick):
        return None  # исход уже есть в OpenDota — линия устарела, ставить нельзя
    first, stakes = None, {}
    for c in cfg.all():
        b = bank(conn, mode, c)
        stake = stake_for(c, b, pick.price, pick.prob)
        if stake <= 0:
            continue
        row = {
            "created_at": now.isoformat(timespec="seconds"), "mode": mode, "fixture_id": a.match.fixture_id,
            "start_time": a.match.start_time, "team1": a.match.team1, "team2": a.match.team2,
            "market": pick.market, "pick": pick.team, "price": pick.price, "prob": pick.prob,
            "book_prob": pick.book_prob, "stake": stake, "bank_before": b, "strategy": c.key,
        }
        conn.execute(f"INSERT INTO paper_bets ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        stakes[c.strategy] = stake
        first = first or row
    conn.commit()
    return {**first, "ev": pick.ev, "stakes": stakes, "profile": cfg.profile} if first else None


def place_bets(conn: sqlite3.Connection, analyses: list[Analysis], thresholds: Thresholds, cfg: SimSettings,
               now: datetime | None = None) -> list[dict]:
    """Ставит по одному лучшему сигналу на матч в его текущем режиме. Возвращает новые ставки."""
    placed = []
    for a in analyses:
        mode = by_key(a.match.game).mode_prefix + ("live" if a.match.phase == "live" else "prematch")
        picks = [p for p in a.picks if thresholds.ok(p)]
        if picks:
            b = place_pick(conn, a, max(picks, key=lambda p: p.ev), mode, cfg, now)
            if b:
                placed.append(b)
    return placed


def strategies_for(cfg: SimSettings, mode: str) -> list[SimSettings]:
    return [replace(cfg, strategy="fixed", strategies=())] if mode in QUIET_MODES else cfg.all()


def place_longshots(conn: sqlite3.Connection, analyses: list[Analysis], thresholds: Thresholds, cfg: SimSettings,
                    now: datetime | None = None) -> list[dict]:
    """Записывает на заметку пики с перевесом, отсечённые фильтром расхождения с рынком."""
    placed = []
    fixed = replace(cfg, strategy="fixed", strategies=())
    for a in analyses:
        picks = [p for p in a.picks if thresholds.suspicious(p) and p.ev >= thresholds.min_ev]
        if picks:
            b = place_pick(conn, a, max(picks, key=lambda p: p.ev), by_key(a.match.game).mode_prefix + "longshot", fixed, now)
            if b:
                placed.append(b)
    return placed


# --- расчёт -------------------------------------------------------------------


def result_known(conn: sqlite3.Connection, a: Analysis, pick) -> bool:
    try:
        maps = find_series(conn, a.match.team1, a.match.team2, a.match.start_time, datetime.now(timezone.utc).isoformat(),
                           table=by_key(a.match.game).maps_table)
    except sqlite3.OperationalError:  # истории карт ещё нет
        return False
    return bool(maps) and settle_result({"pick": pick.team, "market": pick.market}, maps) is not None

BEST_OF = {0: 1, 1: 3, 2: 5, 3: 2}


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def find_series(conn: sqlite3.Connection, team1: str, team2: str, start_iso: str | None, created_iso: str,
                table: str = "pro_maps") -> list[sqlite3.Row] | None:
    """Карты серии этих команд из истории игры (pro_maps или cs_maps) после начала матча, по возрастанию времени."""
    from . import aliases

    aliases.load(conn)
    t1, t2 = normalize(team1), normalize(team2)
    start = _ts(start_iso) or _ts(created_iso) or 0
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE start_time BETWEEN ? AND ? ORDER BY start_time, match_id",
        (start - 3 * 3600, start + 2 * 86400),
    ).fetchall()
    conn.row_factory = None

    def same(name: str | None, key: str) -> bool:
        n = normalize(name or "")
        return bool(n) and (n == key or (len(key) >= 4 and (key in n or n in key)))

    by_series: dict = {}
    for r in rows:
        names = (r["radiant_name"], r["dire_name"])
        if (same(names[0], t1) and same(names[1], t2)) or (same(names[0], t2) and same(names[1], t1)):
            by_series.setdefault(r["series_id"] or r["match_id"], []).append(r)
    if not by_series:
        return None
    return min(by_series.values(), key=lambda maps: maps[0]["start_time"])


def winner_name(r) -> str:
    return r["radiant_name"] if r["radiant_win"] else r["dire_name"]


def settle_result(bet: dict, maps: list) -> tuple[str, str] | None:
    """('won'|'lost'|'void', пояснение) или None, если результата ещё нет."""
    pick = normalize(bet["pick"])

    def is_pick(name: str) -> bool:
        n = normalize(name or "")
        return n == pick or (len(pick) >= 4 and (pick in n or n in pick))

    if bet["market"].startswith("map_"):
        n = int(bet["market"].split("_")[1])
        if len(maps) < n:
            return None
        w = winner_name(maps[n - 1])
        return ("won" if is_pick(w) else "lost"), f"карту {n} выиграла {w}"
    best_of = 2 if is_draw(bet["pick"]) else BEST_OF.get(maps[0]["series_type"], 3)
    need = best_of // 2 + 1
    wins = sum(1 for r in maps if is_pick(winner_name(r)))
    losses = len(maps) - wins
    score = f"{wins}:{losses}"
    if best_of == 2:
        if len(maps) < 2:
            return None
        if is_draw(bet["pick"]):
            w1, w2 = winner_name(maps[0]), winner_name(maps[1])
            return ("won", "ничья 1:1") if normalize(w1) != normalize(w2) else ("lost", f"без ничьей, {w1} 2:0")
        if wins == losses:
            # ничья: если букмекер давал исход «ничья» (3 исхода), ставка на команду проиграла; иначе возврат
            return ("lost" if bet.get("draw_offered") else "void"), f"ничья {score}"
        return ("won", score) if wins == 2 else ("lost", score)
    if wins >= need:
        return "won", score
    if losses >= need:
        return "lost", score
    return None


def closing_price(conn: sqlite3.Connection, bet: dict) -> float | None:
    row = conn.execute(
        """SELECT price FROM odds_snapshots WHERE source=? AND fixture_id=? AND market=? AND outcome=?
             AND (phase='prematch' OR phase IS NULL) ORDER BY id DESC LIMIT 1""",
        (by_mode(bet["mode"]).source, bet["fixture_id"], bet["market"], bet["pick"]),
    ).fetchone()
    return row[0] if row else None


def settle(conn: sqlite3.Connection, now: datetime | None = None, void_after_days: float = 3) -> list[dict]:
    """Рассчитывает открытые ставки по истории карт их игры (Dota — pro_maps, CS2 — cs_maps, LoL — lol_maps). Возвращает рассчитанные."""
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    conn.row_factory = sqlite3.Row
    bets = [dict(r) for r in conn.execute("SELECT * FROM paper_bets WHERE status='open'").fetchall()]
    conn.row_factory = None
    done = []
    for bet in bets:
        game = by_mode(bet["mode"])
        try:
            maps = find_series(conn, bet["team1"], bet["team2"], bet["start_time"], bet["created_at"], table=game.maps_table)
        except sqlite3.OperationalError:  # истории этой игры ещё нет
            maps = None
        bet["draw_offered"] = bool(conn.execute(
            "SELECT 1 FROM odds_snapshots WHERE fixture_id=? AND market='match_winner' AND LOWER(outcome) IN ('draw','x','tie','ничья') LIMIT 1",
            (bet["fixture_id"],)).fetchone())
        res = settle_result(bet, maps) if maps else None
        if res is None:
            created = datetime.fromisoformat(bet["created_at"])
            if now - created < timedelta(days=void_after_days):
                continue
            res = ("void", f"результат не найден в истории {game.label} за 3 дня")
        status, note = res
        profit = round(bet["stake"] * (bet["price"] - 1), 2) if status == "won" else -bet["stake"] if status == "lost" else 0.0
        close = closing_price(conn, bet) if base_mode(bet["mode"]) == "prematch" else None
        conn.execute(
            "UPDATE paper_bets SET status=?, profit=?, closing_price=?, settled_at=?, note=? WHERE id=?",
            (status, profit, close, now.isoformat(timespec="seconds"), note, bet["id"]),
        )
        done.append({**bet, "status": status, "profit": profit, "note": note, "closing_price": close})
    conn.commit()
    return done


# --- итоги --------------------------------------------------------------------

def summary(conn: sqlite3.Connection, cfg: SimSettings, mode: str) -> dict:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM paper_bets WHERE mode=? AND strategy=? ORDER BY id",
                                          (mode, cfg.key)).fetchall()]
    conn.row_factory = None
    settled = [r for r in rows if r["status"] in ("won", "lost")]
    staked = sum(r["stake"] for r in settled)
    profit = sum(r["profit"] for r in rows if r["status"] in ("won", "lost", "void"))
    # просадка и серии по рассчитанным ставкам в порядке расчёта
    peak = curve = cfg.bank
    max_dd = 0.0
    streak = worst = 0
    for r in sorted(settled, key=lambda r: (r["settled_at"] or "", r["id"])):
        curve += r["profit"]
        peak = max(peak, curve)
        max_dd = max(max_dd, (peak - curve) / peak if peak else 0)
        streak = streak + 1 if r["status"] == "lost" else 0
        worst = max(worst, streak)
    clv = [r["price"] / r["closing_price"] - 1 for r in settled if r.get("closing_price")]
    return {
        "mode": mode, "strategy": cfg.strategy, "bets": len(rows), "open": sum(r["status"] == "open" for r in rows),
        "won": sum(r["status"] == "won" for r in rows), "lost": sum(r["status"] == "lost" for r in rows),
        "void": sum(r["status"] == "void" for r in rows), "staked": staked, "profit": profit,
        "roi": profit / staked if staked else None,
        # bank — весь банк (свободные деньги + стоящие на открытых ставках); free — можно ставить сейчас
        "bank": bank(conn, mode, cfg) + sum(r["stake"] for r in rows if r["status"] == "open"),
        "in_play": sum(r["stake"] for r in rows if r["status"] == "open"), "free": bank(conn, mode, cfg),
        "max_dd": max_dd, "worst_streak": worst, "clv": sum(clv) / len(clv) if clv else None, "rows": rows,
    }


def bet_line(r: dict, with_stake: bool = True) -> str:
    icon = {"open": "⏳", "won": "✅", "lost": "❌", "void": "↩️"}[r["status"]]
    if not with_stake:
        return f"{icon} {r['team1']} — {r['team2']}, {market_label(r['market'])}: {r['pick']} @ {r['price']:.2f}"
    tail = f" → {r['profit']:+.0f}" if r["status"] != "open" else ""
    return f"{icon} {r['team1']} — {r['team2']}, {market_label(r['market'])}: {r['pick']} @ {r['price']:.2f}, ставка {r['stake']:.0f}{tail}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def report(conn: sqlite3.Connection, cfg: SimSettings) -> str:
    strategies = cfg.all()
    lines = [f"Симулятор: старт {cfg.bank:.0f} у каждой стратегии; {cfg.describe()}"]
    total = 0
    for mode in MODES:
        sums = [summary(conn, c, mode) for c in strategies_for(cfg, mode)]
        main = sums[0]
        if mode in OPTIONAL_MODES and not main["bets"]:
            continue
        if mode in QUIET_MODES:
            settled = [r for r in main["rows"] if r["status"] in ("won", "lost")]
            if not main["bets"]:
                continue
            lines.append(f"\n{_cap(MODE_LABEL[mode])}: записей {main['bets']}, рассчитано {len(settled)}")
            if settled:
                hit = sum(r["status"] == "won" for r in settled) / len(settled)
                ours = sum(r["prob"] for r in settled) / len(settled)
                book = [r["book_prob"] for r in settled if r["book_prob"] is not None]
                book_txt = f", рынок давал {sum(book) / len(book):.0%}" if book else ""
                lines.append(f"  выиграли {hit:.0%}; модель давала {ours:.0%}{book_txt}; "
                             f"прибыль фикс. суммой {main['profit']:+.0f}")
            continue
        lines.append(f"\n{_cap(MODE_LABEL[mode])}: ставок {main['bets']} (открыто {main['open']}, ✅ {main['won']}, ❌ {main['lost']}, ↩️ {main['void']})")
        total += main["won"] + main["lost"]
        for s in sums if main["bets"] else []:
            label = STRATEGY_LABEL.get(s["strategy"], s["strategy"]) + ": " if len(sums) > 1 else ""
            play = f" (в игре {s['in_play']:.0f}, свободно {s['free']:.0f})" if s["in_play"] else ""
            if s["won"] + s["lost"]:
                lines.append(f"  {label}банк {s['bank']:.0f}{play}, прибыль {s['profit']:+.0f}, ROI {s['roi']:+.1%}, "
                             f"просадка {s['max_dd']:.1%}, худшая серия {s['worst_streak']}")
            else:
                lines.append(f"  {label}банк {s['bank']:.0f}{play}")
        if main["won"] + main["lost"]:
            if main["clv"] is not None:
                lines.append(f"  средний CLV {main['clv']:+.1%} (положительный — берём коэффициент лучше закрытия)")
        for r in main["rows"][-5:]:
            lines.append("  " + bet_line(r, with_stake=len(sums) == 1))
    lines.append(f"\nРассчитано ставок: {total} из 100 нужных для вывода о перевесе.")
    if cfg.experiments:
        lines.append(compare_report(conn, cfg))
    return "\n".join(lines)


def _money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


MIN_BETS_FOR_VERDICT = 30


SHORT_MODE = {"prematch": "Dota до", "draft": "Dota драфт", "live": "Dota эфир", "cs_prematch": "CS2 до",
              "cs_live": "CS2 эфир", "lol_prematch": "LoL до", "lol_live": "LoL эфир", "longshot": "Dota",
              "cs_longshot": "CS2", "lol_longshot": "LoL"}
MODE_W = 10  # ширина столбца режима в таблицах
SHORT_STRATEGY = {"fixed": "фикс", "percent": "%банка", "kelly": "Келли"}


def _rank(conn: sqlite3.Connection, cfg: SimSettings) -> list[tuple]:
    """(прибыль, профиль, стратегия, рассчитано, ROI, CLV) по всем режимам, кроме «на заметку»; лучшие сверху."""
    rows = []
    for prof in (cfg, *cfg.experiments):
        for c in prof.all():
            sums = [summary(conn, c, m) for m in MODES if m not in QUIET_MODES]
            settled = sum(s["won"] + s["lost"] for s in sums)
            if not settled:
                continue
            profit = sum(s["profit"] for s in sums)
            staked = sum(s["staked"] for s in sums)
            clv = [r["price"] / r["closing_price"] - 1 for s in sums for r in s["rows"]
                   if r["status"] in ("won", "lost") and r.get("closing_price")]
            rows.append((profit, prof.profile or "Основной", c.strategy, settled, profit / staked if staked else 0.0,
                         sum(clv) / len(clv) if clv else None))
    return sorted(rows, key=lambda r: r[0], reverse=True)


def leaderboard(conn: sqlite3.Connection, cfg: SimSettings) -> list[str]:
    rows = _rank(conn, cfg)
    if not rows:
        return ["🏆 Лучшая стратегия: пока нет рассчитанных ставок"]
    out = []
    for i, (profit, name, strat, n, roi, clv) in enumerate(rows[:3], 1):
        clv_txt = f", CLV {clv:+.1%}" if clv is not None else ""
        out.append(f"{'🥇🥈🥉'[i - 1]} {name} · {STRATEGY_LABEL.get(strat, strat)}: {profit:+.0f} (ROI {roi:+.1%}, ставок {n}{clv_txt})")
    best = rows[0]
    if best[3] < MIN_BETS_FOR_VERDICT:
        out.append(f"Рано судить: у лидера {best[3]} ставок, вывод — от {MIN_BETS_FOR_VERDICT}.")
    elif best[5] is not None and best[5] > 0:
        out.append(f"Лучшая: {best[1]} · {STRATEGY_LABEL.get(best[2], best[2])}, CLV в плюсе.")
    else:
        out.append(f"Лидер {best[1]} · {STRATEGY_LABEL.get(best[2], best[2])}, но CLV не в плюсе — возможно, везение.")
    return out


def banks_report(conn: sqlite3.Connection, cfg: SimSettings) -> str:
    """HTML для Telegram: рейтинг и по таблице на профиль (строки — режимы, столбцы — стратегии, в ячейках прибыль)."""
    from html import escape

    out = [f"💰 <b>Банки</b> (старт {_money(cfg.bank)} у каждого)", ""]
    out += [escape(x) for x in leaderboard(conn, cfg)]
    empty = []
    for prof in (cfg, *cfg.experiments):
        name = prof.profile or "Основной"
        strategies = prof.all()
        table = []
        totals = [[0.0, 0.0] for _ in strategies]  # прибыль, поставлено
        won = lost = in_play = 0
        for mode in MODES:
            if mode in QUIET_MODES:
                continue
            sums = [summary(conn, c, mode) for c in strategies]
            if not sums[0]["bets"]:
                continue
            m = sums[0]
            won, lost, in_play = won + m["won"], lost + m["lost"], in_play + m["open"]
            for tot, s in zip(totals, sums):
                tot[0] += s["profit"]
                tot[1] += s["staked"]
            table.append(f"{SHORT_MODE.get(mode, mode):<{MODE_W}}{m['won']:>2}-{m['lost']:<2}" + "".join(f"{s['profit']:>+7.0f}" for s in sums))
        if not table:
            empty.append(name)
            continue
        head = " " * (MODE_W + 5) + "".join(f"{SHORT_STRATEGY.get(c.strategy, c.strategy):>7}" for c in strategies)
        total = f"{'ИТОГО':<{MODE_W}}{won:>2}-{lost:<2}" + "".join(f"{p:>+7.0f}" for p, _ in totals)
        roi = f"{'ROI':<{MODE_W + 5}}" + "".join(f"{(p / st if st else 0):>+7.0%}" for p, st in totals)
        out += ["", f"<b>{escape(name)}</b>: {escape(describe_thresholds(prof.thresholds))}",
                "<pre>" + escape("\n".join([head, *table, "-" * len(head), total, roi])) + "</pre>"]
        if in_play:
            out.append(f"⏳ открыто ставок: {in_play}")
    if empty:
        out += ["", escape(", ".join(empty)) + ": ставок пока нет"]
    out += ["", "В ячейках — прибыль; 0-1 — выигрыши и проигрыши. Банки по режимам — в 📊 Симулятор."]
    return "\n".join(out)


def describe_thresholds(t: Thresholds | None) -> str:
    if t is None:
        return ""
    parts = [f"EV ≥ {t.min_ev:.0%}"]
    if t.min_prob > 0:
        parts.append(f"наша ≥ {t.min_prob:.0%}")
    if t.min_odds > 1:
        parts.append(f"кф ≥ {t.min_odds:g}")
    parts.append(f"разрыв ≤ {t.max_gap * 100:.0f} п.п.")
    if t.min_games:
        parts.append(f"≥ {t.min_games} карт")
    return ", ".join(parts)


def _cell(x: float | None, fmt: str, width: int) -> str:
    return (format(x, fmt) if x is not None else "—").rjust(width)


def report_html(conn: sqlite3.Connection, cfg: SimSettings) -> str:
    """/sim в Telegram: таблицы в <pre> (HTML). Прибыль — по первой стратегии; все банки — в 💰 Банки."""
    from html import escape

    first = cfg.all()[0]
    label = STRATEGY_LABEL.get(first.strategy, first.strategy)
    out = [f"📊 <b>Симулятор</b> (старт {_money(cfg.bank)} у каждой стратегии)"]
    rows, quiet, total = [], [], 0
    for mode in MODES:
        s = summary(conn, first if mode not in QUIET_MODES else strategies_for(cfg, mode)[0], mode)
        if not s["bets"]:
            continue
        name = SHORT_MODE.get(mode, mode)[:MODE_W]
        settled = [r for r in s["rows"] if r["status"] in ("won", "lost")]
        if mode in QUIET_MODES:
            book = [r["book_prob"] for r in settled if r["book_prob"] is not None]
            quiet.append(f"{name:<6}{len(settled):>4}"
                         + _cell(sum(r['status'] == 'won' for r in settled) / len(settled) if settled else None, ".0%", 6)
                         + _cell(sum(r['prob'] for r in settled) / len(settled) if settled else None, ".0%", 7)
                         + _cell(sum(book) / len(book) if book else None, ".0%", 6))
            continue
        total += s["won"] + s["lost"]
        rows.append(f"{name:<{MODE_W}}{s['won']:>2}-{s['lost']:<2}{s['open']:>3}{s['profit']:>+7.0f}"
                    + _cell(s["roi"] if settled else None, "+.0%", 6) + _cell(s["clv"], "+.0%", 5))
    if rows:
        head = f"{'Режим':<{MODE_W}}{'В-П':^5}{'Отк':>3}{'Приб.':>7}{'ROI':>6}{'CLV':>5}"
        out += ["", "<pre>" + escape("\n".join([head, *rows])) + "</pre>",
                f"Прибыль — {escape(label)}; ROI и CLV — по рассчитанным. Все стратегии — 💰 Банки."]
    else:
        out += ["", "Бумажных ставок пока нет."]
    if quiet:
        head = f"{'Игра':<6}{'Ст.':>4}{'Вышло':>6}{'Модель':>7}{'Рынок':>6}"
        out += ["", "<b>На заметку</b> (модель далеко от рынка, без ставок):",
                "<pre>" + escape("\n".join([head, *quiet])) + "</pre>"]
    if cfg.experiments:
        prof_rows = []
        for prof in (cfg, *cfg.experiments):
            c = prof.all()[0]
            sums = [summary(conn, c, m) for m in MODES if m not in QUIET_MODES]
            won, lost = sum(s["won"] for s in sums), sum(s["lost"] for s in sums)
            profit, staked = sum(s["profit"] for s in sums), sum(s["staked"] for s in sums)
            clv = [r["price"] / r["closing_price"] - 1 for s in sums for r in s["rows"]
                   if r["status"] in ("won", "lost") and r.get("closing_price")]
            prof_rows.append(f"{(prof.profile or 'Основной')[:9]:<9}{won:>3}-{lost:<3}{profit:>+7.0f}"
                             + _cell(profit / staked if staked else None, "+.0%", 6)
                             + _cell(sum(clv) / len(clv) if clv else None, "+.0%", 5))
        head = f"{'Профиль':<9}{'В-П':^7}{'Приб.':>7}{'ROI':>6}{'CLV':>5}"
        out += ["", "⚖️ <b>Сравнение настроек</b> (те же коэффициенты, разные пороги):",
                "<pre>" + escape("\n".join([head, *prof_rows])) + "</pre>"]
    recent = conn.execute(
        "SELECT * FROM paper_bets WHERE strategy=? AND mode NOT IN ({}) ORDER BY id DESC LIMIT 5".format(
            ",".join("?" * len(QUIET_MODES))), (first.key, *QUIET_MODES)).fetchall() if total or rows else []
    if recent:
        cols = [d[0] for d in conn.execute("SELECT * FROM paper_bets LIMIT 0").description]
        out += ["", "<b>Последние ставки:</b>"] + [escape(bet_line(dict(zip(cols, r)))) for r in recent]
    out.append(f"\nРассчитано ставок: {total} из 100 нужных для вывода о перевесе.")
    return "\n".join(out)


def compare_report(conn: sqlite3.Connection, cfg: SimSettings) -> str:
    """Основной профиль и эксперименты рядом: одни и те же коэффициенты, разные пороги."""
    lines = ["\n⚖️ Сравнение настроек (у каждой свои банки по {:.0f})".format(cfg.bank)]
    for prof in (cfg, *cfg.experiments):
        name = prof.profile or "Основной"
        lines.append(f"\n{name}: {describe_thresholds(prof.thresholds)}")
        any_bets = False
        for mode in MODES:
            if mode in QUIET_MODES:
                continue
            sums = [summary(conn, c, mode) for c in prof.all()]
            main = sums[0]
            if not main["bets"]:
                continue
            any_bets = True
            head = f"  {MODE_LABEL[mode]}: {main['bets']} (✅ {main['won']} ❌ {main['lost']}, открыто {main['open']})"
            if main["won"] + main["lost"]:
                money = ", ".join(f"{STRATEGY_LABEL.get(s['strategy'], s['strategy'])} {s['profit']:+.0f}" for s in sums)
                head += f"; {money}; ROI {main['roi']:+.1%}"
                if main["clv"] is not None:
                    head += f", CLV {main['clv']:+.1%}"
            lines.append(head)
        if not any_bets:
            lines.append("  ставок пока нет")
    return "\n".join(lines)


EXP_KEYS = {"ev": "min_ev", "prob": "min_prob", "odds": "min_odds", "gap": "max_gap", "games": "min_games"}


def parse_experiments(raw: str, base: Thresholds) -> list[tuple[str, Thresholds]]:
    """«Фавориты: prob=0.5; Строгий: prob=0.5 ev=0.04 gap=0.06 games=50» → [(имя, пороги)].

    Не указанные пороги берутся из основных (SIGNAL_*).
    """
    out = []
    for chunk in raw.split(";"):
        if not chunk.strip():
            continue
        name, _, params = chunk.partition(":")
        name = name.strip()
        if not name:
            raise ValueError(f"SIM_EXPERIMENTS: нет имени в «{chunk.strip()}»")
        changes = {}
        for item in params.split():
            k, _, v = item.partition("=")
            if k not in EXP_KEYS or not v:
                raise ValueError(f"SIM_EXPERIMENTS: непонятно «{item}» (можно {', '.join(EXP_KEYS)})")
            changes[EXP_KEYS[k]] = int(v) if k == "games" else float(v)
        out.append((name, replace(base, **changes)))
    return out


def placed_message(b: dict) -> str:
    stakes = b.get("stakes") or {b["strategy"].rpartition(":")[2]: b["stake"]}
    amount = (f"сумма {b['stake']:.0f}" if len(stakes) == 1 else
              "суммы: " + ", ".join(f"{STRATEGY_LABEL.get(k, k)} {v:.0f}" for k, v in stakes.items()))
    return (f"📝 Бумажная ставка ({MODE_LABEL[b['mode']]}): {b['team1']} — {b['team2']}, {market_label(b['market'])}: "
            f"{b['pick']} @ {b['price']:.2f}, наша {b['prob']:.0%}, EV {b['ev']:+.1%}, {amount}")


def settled_message(b: dict) -> str:
    return f"{bet_line(b)} ({b['note']})"


def settled_messages(done: list[dict]) -> list[str]:
    """Одно сообщение на матч и режим, даже если стратегий несколько."""
    groups: dict[tuple, list[dict]] = {}
    for b in done:
        groups.setdefault((b["fixture_id"], b["mode"]), []).append(b)
    out = []
    for bets in groups.values():
        if len(bets) == 1:
            out.append(settled_message(bets[0]))
            continue
        b = bets[0]
        profits = ", ".join(f"{STRATEGY_LABEL.get(x['strategy'].rpartition(':')[2], x['strategy'])} {x['profit']:+.0f}" for x in bets)
        out.append(f"{bet_line(b, with_stake=False)} ({b['note']}); прибыль: {profits}")
    return out
