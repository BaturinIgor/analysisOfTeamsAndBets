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

MODES = ("prematch", "draft", "live", "longshot")
MODE_LABEL = {"prematch": "до матча", "draft": "после драфта", "live": "в эфире",
              "longshot": "аутсайдеры (на заметку)"}
# «аутсайдеры»: модель расходится с рынком сильнее SIGNAL_MAX_GAP. Такие ставки не делаем,
# а записываем на заметку фиксированной суммой без уведомлений — чтобы проверить, кто прав.
QUIET_MODES = ("longshot",)


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

    def all(self) -> list["SimSettings"]:
        return [replace(self, strategy=name, strategies=()) for name in (self.strategies or (self.strategy,))]

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
    settled = conn.execute(q.format("profit", "IN ('won','lost','void')"), (mode, cfg.strategy)).fetchone()[0]
    open_stakes = conn.execute(q.format("stake", "= 'open'"), (mode, cfg.strategy)).fetchone()[0]
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
    if conn.execute("SELECT 1 FROM paper_bets WHERE fixture_id=? AND mode=?", (a.match.fixture_id, mode)).fetchone():
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
            "book_prob": pick.book_prob, "stake": stake, "bank_before": b, "strategy": c.strategy,
        }
        conn.execute(f"INSERT INTO paper_bets ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        stakes[c.strategy] = stake
        first = first or row
    conn.commit()
    return {**first, "ev": pick.ev, "stakes": stakes} if first else None


def place_bets(conn: sqlite3.Connection, analyses: list[Analysis], thresholds: Thresholds, cfg: SimSettings,
               now: datetime | None = None) -> list[dict]:
    """Ставит по одному лучшему сигналу на матч в его текущем режиме. Возвращает новые ставки."""
    placed = []
    for a in analyses:
        mode = "live" if a.match.phase == "live" else "prematch"
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
            b = place_pick(conn, a, max(picks, key=lambda p: p.ev), "longshot", fixed, now)
            if b:
                placed.append(b)
    return placed


# --- расчёт -------------------------------------------------------------------


def result_known(conn: sqlite3.Connection, a: Analysis, pick) -> bool:
    try:
        maps = find_series(conn, a.match.team1, a.match.team2, a.match.start_time, datetime.now(timezone.utc).isoformat())
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


def find_series(conn: sqlite3.Connection, team1: str, team2: str, start_iso: str | None, created_iso: str) -> list[sqlite3.Row] | None:
    """Карты серии этих команд из OpenDota после начала матча (по возрастанию времени)."""
    from . import aliases

    aliases.load(conn)
    t1, t2 = normalize(team1), normalize(team2)
    start = _ts(start_iso) or _ts(created_iso) or 0
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM pro_maps WHERE start_time BETWEEN ? AND ? ORDER BY start_time",
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
        """SELECT price FROM odds_snapshots WHERE source='oddiwire' AND fixture_id=? AND market=? AND outcome=?
             AND (phase='prematch' OR phase IS NULL) ORDER BY id DESC LIMIT 1""",
        (bet["fixture_id"], bet["market"], bet["pick"]),
    ).fetchone()
    return row[0] if row else None


def settle(conn: sqlite3.Connection, now: datetime | None = None, void_after_days: float = 3) -> list[dict]:
    """Рассчитывает открытые ставки по картам из pro_maps. Возвращает рассчитанные."""
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    conn.row_factory = sqlite3.Row
    bets = [dict(r) for r in conn.execute("SELECT * FROM paper_bets WHERE status='open'").fetchall()]
    conn.row_factory = None
    done = []
    for bet in bets:
        maps = find_series(conn, bet["team1"], bet["team2"], bet["start_time"], bet["created_at"])
        bet["draw_offered"] = bool(conn.execute(
            "SELECT 1 FROM odds_snapshots WHERE fixture_id=? AND market='match_winner' AND LOWER(outcome) IN ('draw','x','tie','ничья') LIMIT 1",
            (bet["fixture_id"],)).fetchone())
        res = settle_result(bet, maps) if maps else None
        if res is None:
            created = datetime.fromisoformat(bet["created_at"])
            if now - created < timedelta(days=void_after_days):
                continue
            res = ("void", "результат не найден в OpenDota за 3 дня")
        status, note = res
        profit = round(bet["stake"] * (bet["price"] - 1), 2) if status == "won" else -bet["stake"] if status == "lost" else 0.0
        close = closing_price(conn, bet) if bet["mode"] == "prematch" else None
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
                                          (mode, cfg.strategy)).fetchall()]
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
        "roi": profit / staked if staked else None, "bank": bank(conn, mode, cfg) + sum(r["stake"] for r in rows if r["status"] == "open"),
        "max_dd": max_dd, "worst_streak": worst, "clv": sum(clv) / len(clv) if clv else None, "rows": rows,
    }


def bet_line(r: dict, with_stake: bool = True) -> str:
    icon = {"open": "⏳", "won": "✅", "lost": "❌", "void": "↩️"}[r["status"]]
    if not with_stake:
        return f"{icon} {r['team1']} — {r['team2']}, {market_label(r['market'])}: {r['pick']} @ {r['price']:.2f}"
    tail = f" → {r['profit']:+.0f}" if r["status"] != "open" else ""
    return f"{icon} {r['team1']} — {r['team2']}, {market_label(r['market'])}: {r['pick']} @ {r['price']:.2f}, ставка {r['stake']:.0f}{tail}"


def report(conn: sqlite3.Connection, cfg: SimSettings) -> str:
    strategies = cfg.all()
    lines = [f"Симулятор: старт {cfg.bank:.0f} у каждой стратегии; {cfg.describe()}"]
    total = 0
    for mode in MODES:
        sums = [summary(conn, c, mode) for c in strategies_for(cfg, mode)]
        main = sums[0]
        if mode in QUIET_MODES:
            settled = [r for r in main["rows"] if r["status"] in ("won", "lost")]
            if not main["bets"]:
                continue
            lines.append(f"\n{MODE_LABEL[mode].capitalize()}: записей {main['bets']}, рассчитано {len(settled)}")
            if settled:
                hit = sum(r["status"] == "won" for r in settled) / len(settled)
                ours = sum(r["prob"] for r in settled) / len(settled)
                book = [r["book_prob"] for r in settled if r["book_prob"] is not None]
                book_txt = f", рынок давал {sum(book) / len(book):.0%}" if book else ""
                lines.append(f"  выиграли {hit:.0%}; модель давала {ours:.0%}{book_txt}; "
                             f"прибыль фикс. суммой {main['profit']:+.0f}")
            continue
        lines.append(f"\n{MODE_LABEL[mode].capitalize()}: ставок {main['bets']} (открыто {main['open']}, ✅ {main['won']}, ❌ {main['lost']}, ↩️ {main['void']})")
        total += main["won"] + main["lost"]
        if main["won"] + main["lost"]:
            for s in sums:
                label = STRATEGY_LABEL.get(s["strategy"], s["strategy"]) + ": " if len(sums) > 1 else ""
                lines.append(f"  {label}банк {s['bank']:.0f}, прибыль {s['profit']:+.0f}, ROI {s['roi']:+.1%}, "
                             f"просадка {s['max_dd']:.1%}, худшая серия {s['worst_streak']}")
            if main["clv"] is not None:
                lines.append(f"  средний CLV {main['clv']:+.1%} (положительный — берём коэффициент лучше закрытия)")
        for r in main["rows"][-5:]:
            lines.append("  " + bet_line(r, with_stake=len(sums) == 1))
    lines.append(f"\nРассчитано ставок: {total} из 100 нужных для вывода о перевесе.")
    return "\n".join(lines)


def placed_message(b: dict) -> str:
    stakes = b.get("stakes") or {b["strategy"]: b["stake"]}
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
        profits = ", ".join(f"{STRATEGY_LABEL.get(x['strategy'], x['strategy'])} {x['profit']:+.0f}" for x in bets)
        out.append(f"{bet_line(b, with_stake=False)} ({b['note']}); прибыль: {profits}")
    return out
