"""Закрытый Telegram-бот (ТР-17, ТР-19): /today, /live, /match, /value, /team.

Работает через long polling (getUpdates), без вебхуков и без сервера: достаточно Mac.
Отвечает только Telegram ID из TELEGRAM_ALLOWED_IDS.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from . import analysis, rating, sim as simmod
from .analysis import Analysis, Thresholds, market_label

API = "https://api.telegram.org"
MAX_LEN = 4000

HELP = """Анализатор ставок Dota 2

/today — матчи в линии и наша оценка
/live — матчи в эфире
/match N — подробный разбор матча N из /today
/value — сигналы: перевес (EV) не меньше порога
/team название — рейтинг команды
/sim — бумажные ставки: банк, ROI, CLV
/settings — текущие пороги

Коэффициенты: линия Oddin (на ней работает BetBoom), обновляется автоматически.
Вероятности: рейтинг Elo по картам Tier 1–2. Форма, патч, состав и драфт пока не учитываются."""

LATER = "Выгрузка в xlsx появится на следующем шаге."


class ModelCache:
    """Рейтинг пересчитывается, только когда в базе появились новые карты."""

    def __init__(self):
        self._key = None
        self._model: rating.EloModel | None = None

    def get(self, conn: sqlite3.Connection) -> tuple[rating.EloModel, int]:
        from . import history

        history.ensure_schema(conn)
        conn.row_factory = None
        key = conn.execute("SELECT COUNT(*), MAX(match_id) FROM pro_maps").fetchone()
        if key != self._key or self._model is None:
            maps = history.tier_maps(conn)
            conn.row_factory = None
            self._model, _ = rating.fit(maps)
            self._key = key
        return self._model, key[0] or 0


def _local(ts: str | None, tz: ZoneInfo) -> str:
    if not ts:
        return "--:--"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return dt.astimezone(tz).strftime("%d.%m %H:%M")


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _team(name: str, st) -> str:
    return f"{name} ({st.rating:.0f})" if st else f"{name} (нет в истории Tier 1–2)"


class Bot:
    def __init__(self, conn: sqlite3.Connection, allowed_ids: tuple[int, ...], thresholds: Thresholds | None = None,
                 tz: str = "Europe/Moscow", now=None, sim: simmod.SimSettings | None = None):
        self.sim = sim or simmod.SimSettings()
        self.conn = conn
        self.allowed = set(allowed_ids)
        self.thresholds = thresholds or Thresholds()
        self.tz = ZoneInfo(tz)
        self.models = ModelCache()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_list: list[str] = []  # fixture_id по номерам из /today

    # --- данные -------------------------------------------------------------
    def _analyses(self, live_only: bool = False) -> tuple[list[Analysis], int]:
        model, maps = self.models.get(self.conn)
        now = self._now()
        board = analysis.load_board(self.conn, now=now)
        if live_only:
            board = [m for m in board if m.phase == "live"]
        return [analysis.analyze(m, model, int(now.timestamp())) for m in board], maps

    def _line(self, n: int, a: Analysis) -> str:
        m = a.match
        mark = "🔴 " if m.phase == "live" else ""
        head = f"{n}. {mark}{_local(m.start_time, self.tz)} {m.team1} — {m.team2}"
        if m.phase == "live" and m.score:
            head += f" (счёт {m.score})"
        q1, q2 = m.price("match_winner", m.team1), m.price("match_winner", m.team2)
        odds = f"{q1.price:.2f} / {q2.price:.2f}" if q1 and q2 else "нет коэффициентов"
        ours = f"наша {_pct(a.p_series)} / {_pct(1 - a.p_series)}" if a.p_series is not None else "нашей оценки нет"
        best = max(a.picks, key=lambda p: p.ev, default=None)
        sig = f" ✅ {best.team} {market_label(best.market)} EV {best.ev:+.0%}" if best and self.thresholds.ok(best) else ""
        return f"{head}\n   П1/П2 {odds}; {ours}{sig}"

    # --- команды ------------------------------------------------------------
    def today(self, live_only: bool = False) -> str:
        items, maps = self._analyses(live_only)
        if not items:
            return "Сейчас в эфире матчей Dota нет." if live_only else "В линии нет матчей Dota. Проверьте, что запущен сбор: bk collect-odds"
        self._last_list = [a.match.fixture_id for a in items]
        lines = ["Матчи в эфире:" if live_only else "Матчи Dota в линии:"]
        lines += [self._line(i, a) for i, a in enumerate(items, 1)]
        if not maps:
            lines.append("\nИстории матчей нет — оценки не будет. Запустите один раз: bk sync-matches")
        lines.append("\nПодробно: /match N")
        return "\n".join(lines)

    def match(self, arg: str) -> str:
        items, _ = self._analyses()
        if not items:
            return "В линии нет матчей Dota."
        target = None
        if arg.isdigit() and self._last_list and 1 <= int(arg) <= len(self._last_list):
            fid = self._last_list[int(arg) - 1]
            target = next((a for a in items if a.match.fixture_id == fid), None)
        if target is None and arg.isdigit() and 1 <= int(arg) <= len(items):
            target = items[int(arg) - 1]
        if target is None:
            q = analysis.normalize(arg)
            target = next((a for a in items if q and (q in analysis.normalize(a.match.team1) or q in analysis.normalize(a.match.team2))), None)
        if target is None:
            return "Не нашёл матч. Посмотрите номера в /today"
        a, m = target, target.match
        s1, s2 = a.known
        lines = [
            f"{m.team1} — {m.team2}",
            f"{m.tournament or ''}, Bo{m.best_of}, начало {_local(m.start_time, self.tz)}" + (f", в эфире, счёт {m.score}" if m.phase == "live" else ""),
            f"Рейтинг: {_team(m.team1, s1)} vs {_team(m.team2, s2)}",
        ]
        if a.p_map is not None:
            lines.append(f"Наша вероятность: карта {a.p_map:.0%} / {1 - a.p_map:.0%}; серия {a.p_series:.0%} / {1 - a.p_series:.0%}")
        else:
            lines.append("Нашей оценки нет: одной из команд нет в истории Tier 1–2.")
        lines.append("\nЛиния Oddin (коэфф. · вероятность БК без маржи):")
        for market in sorted(m.quotes, key=lambda k: (k != "match_winner", k)):
            parts = [f"{q.outcome} {q.price:.2f} · {_pct(q.fair_prob)}" for q in m.quotes[market].values()]
            lines.append(f"  {market_label(market)}: " + ", ".join(parts))
        if a.picks:
            lines.append("\nПеревес (EV = наша вероятность × коэфф. − 1):")
            for p in sorted(a.picks, key=lambda p: -p.ev):
                flag = "✅" if self.thresholds.ok(p) else "⚠️" if self.thresholds.suspicious(p) and p.ev > 0 else "  "
                lines.append(f"{flag} {market_label(p.market)}: {p.team} @ {p.price:.2f} — наша {p.prob:.0%}, EV {p.ev:+.1%}")
            if any(self.thresholds.suspicious(p) and p.ev > 0 for p in a.picks):
                lines.append(f"⚠️ — модель расходится с рынком больше чем на {self.thresholds.max_gap:.0%}: скорее ошибается модель, ставки нет.")
            if not any(self.thresholds.ok(p) for p in a.picks):
                lines.append("Ставки нет: перевес ниже порога.")
        lines.append("\nРиски: модель видит только результаты карт; форма, патч, замены и драфт пока не учитываются.")
        return "\n".join(lines)

    def value(self) -> str:
        items, _ = self._analyses()
        picks = [(a, p) for a in items for p in a.picks if self.thresholds.ok(p)]
        if not picks:
            return f"Сигналов нет (порог EV {self.thresholds.min_ev:.0%}). Ставки нет."
        lines = ["Сигналы:"]
        for a, p in sorted(picks, key=lambda x: -x[1].ev):
            lines.append(f"• {a.match.team1} — {a.match.team2}, {market_label(p.market)}: {p.team} @ {p.price:.2f}, наша {p.prob:.0%}, EV {p.ev:+.1%}")
        return "\n".join(lines)

    def team(self, query: str) -> str:
        if not query:
            return "Напишите: /team название"
        model, maps = self.models.get(self.conn)
        if not maps:
            return "Истории матчей нет. Запустите: bk sync-matches"
        found = analysis.match_team(model, query)
        if not found:
            return f"Команда «{query}» не найдена в Tier 1–2"
        tid, st = found
        ranked = sorted((s.rating for s in model.teams.values() if s.games >= 20), reverse=True)
        place = sum(1 for r in ranked if r > st.rating) + 1
        return f"{st.name}: рейтинг {st.rating:.0f}, карт в истории {st.games}, место {place} из {len(ranked)} (команды с 20+ картами)"

    def settings(self) -> str:
        t = self.thresholds
        c = self.sim
        return (f"Пороги сигнала: EV ≥ {t.min_ev:.0%}, вероятность ≥ {t.min_prob:.0%}, коэффициент ≥ {t.min_odds:.2f}, "
                f"расхождение с рынком ≤ {t.max_gap:.0%}.\n"
                f"Симулятор: банк {c.bank:.0f}, стратегия {c.strategy} (сумма {c.stake:.0f} / {c.percent:.0%} банка / {c.kelly:.2f} Келли).\n"
                "Меняются в .env: SIGNAL_MIN_EV, SIGNAL_MIN_PROB, SIGNAL_MIN_ODDS, SIGNAL_MAX_GAP, SIM_BANK, SIM_STRATEGY, SIM_STAKE, SIM_PERCENT, SIM_KELLY.")

    def handle(self, user_id: int, text: str) -> str | None:
        if user_id not in self.allowed:
            return None  # чужим не отвечаем
        cmd, _, arg = (text or "").strip().partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        try:
            if cmd in ("/start", "/help"):
                return HELP
            if cmd == "/today":
                return self.today()
            if cmd == "/live":
                return self.today(live_only=True)
            if cmd == "/match":
                return self.match(arg) if arg else "Напишите: /match N (номер из /today)"
            if cmd == "/value":
                return self.value()
            if cmd == "/team":
                return self.team(arg)
            if cmd == "/settings":
                return self.settings()
            if cmd == "/sim":
                return simmod.report(self.conn, self.sim)
            if cmd == "/export":
                return LATER
        except Exception as exc:  # бот не должен падать из-за одной команды
            return f"Ошибка: {exc}"
        return "Не знаю такой команды. /help"


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > size and buf:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    return out + ([buf] if buf else [])


COMMANDS = [
    ("today", "матчи в линии и наша оценка"),
    ("live", "матчи в эфире"),
    ("match", "разбор матча: /match N"),
    ("value", "сигналы с перевесом"),
    ("team", "рейтинг команды"),
    ("sim", "симулятор бумажных ставок"),
    ("settings", "пороги сигнала"),
    ("help", "справка"),
]


def send_message(client: httpx.Client, token: str, chat_id: int, text: str) -> bool:
    ok = True
    for part in chunks(text):
        try:
            client.post(f"{API}/bot{token}/sendMessage", json={"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
        except httpx.HTTPError:
            ok = False
    return ok


def make_telegram_client(proxy: str = "") -> httpx.Client:
    return httpx.Client(timeout=60, proxy=proxy or None)


def check_telegram(token: str, client: httpx.Client) -> str:
    """Проверка связи и токена: getMe."""
    try:
        data = client.get(f"{API}/bot{token}/getMe", timeout=15).json()
    except (httpx.HTTPError, ValueError) as exc:
        return f"нет связи с api.telegram.org: {exc}"
    if not data.get("ok"):
        return f"Telegram отклонил токен: {data.get('description')}"
    return f"ok, бот @{data['result'].get('username')}"


def run_polling(token: str, bot: Bot, client: httpx.Client | None = None, log=print, stop=lambda: False, sleep=time.sleep) -> None:
    client = client or make_telegram_client()
    base = f"{API}/bot{token}"
    try:
        client.post(f"{base}/setMyCommands", json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
    except httpx.HTTPError:
        pass
    offset = 0
    fails = 0
    while not stop():
        try:
            resp = client.get(f"{base}/getUpdates", params={"timeout": 50, "offset": offset})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            fails += 1
            if fails == 1 or fails % 20 == 0:
                log(f"Telegram недоступен ({fails} раз подряд): {exc}. "
                    "Если так постоянно — включите VPN или задайте TELEGRAM_PROXY в .env")
            sleep(min(60, 5 * fails))
            continue
        if fails:
            log("Связь с Telegram восстановлена")
            fails = 0
        if not data.get("ok"):
            log(f"Telegram: {data.get('description')}")
            sleep(5)
            continue
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            user = (msg.get("from") or {}).get("id")
            reply = bot.handle(user, msg.get("text", "")) if user else None
            if reply is None:
                if user:
                    log(f"Сообщение от чужого ID {user} пропущено")
                continue
            for part in chunks(reply):
                try:
                    client.post(f"{base}/sendMessage", json={"chat_id": msg["chat"]["id"], "text": part, "disable_web_page_preview": True})
                except httpx.HTTPError as exc:
                    log(f"Не отправилось: {exc}")
