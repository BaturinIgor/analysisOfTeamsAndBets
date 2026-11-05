"""Закрытый Telegram-бот (ТР-17, ТР-19): /today, /live, /match, /value, /team.

Работает через long polling (getUpdates), без вебхуков и без сервера: достаточно Mac.
Отвечает только Telegram ID из TELEGRAM_ALLOWED_IDS.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from . import analysis, rating, sim as simmod
from .analysis import Analysis, Thresholds, market_label

API = "https://api.telegram.org"
NOISY_FAILS = 3  # с какого обрыва подряд писать в лог
MAX_LEN = 4000

HELP = """Анализатор ставок Dota 2

Пользуйтесь кнопками внизу экрана. Под списком матчей — кнопки разбора (🔎) и новостей (📰).
Можно просто написать название команды — покажу её матч или рейтинг.

Те же действия командами:

/today — матчи в линии и наша оценка
/live — матчи в эфире
/match N — подробный разбор матча N из /today
/value — сигналы: перевес (EV) не меньше порога
/team название — рейтинг команды
/news N — состав и новости за 7 дней по матчу N (или /news команда)
/model — насколько точна модель на последних 180 днях
/sim — бумажные ставки: банк, ROI, CLV
/settings — текущие пороги
/export — файл xlsx (матчи, ставки, итоги) и CSV

Коэффициенты: линия Oddin (на ней работает BetBoom), обновляется автоматически.
Вероятности: рейтинг Elo + форма + личные встречи + патч + стабильность состава по картам Tier 1–2, обучено на истории.
После драфта — ещё сила героев в патче и опыт игроков на них."""



@dataclass
class Reply:
    """Ответ бота: текст, файлы (sendDocument), кнопки под сообщением и/или меню внизу экрана."""

    text: str
    files: list[Path] = field(default_factory=list)
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # [(надпись, callback_data)]
    menu: bool = False

    def __contains__(self, item: str) -> bool:
        return item in self.text


# меню внизу экрана: надпись кнопки → команда
MENU = [["📋 Матчи", "🔴 Эфир"], ["✅ Сигналы", "📊 Симулятор"], ["📈 Модель", "📁 Выгрузка"], ["⚙️ Настройки", "❓ Помощь"]]
MENU_CMD = {"📋 Матчи": "/today", "🔴 Эфир": "/live", "✅ Сигналы": "/value", "📊 Симулятор": "/sim",
            "📈 Модель": "/model", "📁 Выгрузка": "/export", "⚙️ Настройки": "/settings", "❓ Помощь": "/help"}


def reply_markup(reply: Reply) -> dict | None:
    if reply.buttons:
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in reply.buttons]}
    if reply.menu:
        return {"keyboard": [[{"text": t} for t in row] for row in MENU], "resize_keyboard": True, "is_persistent": True}
    return None


class ModelCache:
    """Модель переобучается, только когда в базе появились новые карты."""

    def __init__(self, eval_days: int = 180):
        self._key = None
        self._model = None
        self.report = None
        self.eval_days = eval_days

    def get(self, conn: sqlite3.Connection):
        from . import history, model as mm

        history.ensure_schema(conn)
        conn.row_factory = None
        from . import aliases, details

        aliases.load(conn)  # справочник синонимов меняется без переобучения

        details.ensure_schema(conn)
        key = conn.execute("SELECT (SELECT COUNT(*) FROM pro_maps), (SELECT MAX(match_id) FROM pro_maps), "
                           "(SELECT COUNT(*) FROM map_players), (SELECT COUNT(*) FROM patches)").fetchone()
        if key != self._key or self._model is None:
            from . import details

            maps = [dict(r) for r in history.tier_maps(conn)]
            conn.row_factory = None
            all_picks = details.picks(conn)
            for m in maps:
                pr, pd = all_picks.get(m["match_id"], (None, None))
                m["picks_r"], m["picks_d"] = pr, pd
                m["lineup_r"] = frozenset(a for a, _ in pr) if pr else None
                m["lineup_d"] = frozenset(a for a, _ in pd) if pd else None
            patches = details.PatchIndex.load(conn)
            since = (maps[-1]["start_time"] - self.eval_days * 86400) if maps else 0
            self._model, self.report = mm.train(maps, since, patches=patches if patches.names else None)
            self._key = key
        return self._model, key[0] or 0  # число карт в базе


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
                 tz: str = "Europe/Moscow", now=None, sim: simmod.SimSettings | None = None,
                 export_dir: Path = Path("data/exports"), news_client: httpx.Client | None = None):
        self.sim = sim or simmod.SimSettings()
        self.conn = conn
        self.allowed = set(allowed_ids)
        self.thresholds = thresholds or Thresholds()
        self.tz = ZoneInfo(tz)
        self.tz_name = tz
        self.export_dir = Path(export_dir)
        self.news_client = news_client  # None — новости только из кэша (в тестах)
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
    def today(self, live_only: bool = False) -> str | Reply:
        items, maps = self._analyses(live_only)
        if not items:
            return "Сейчас в эфире матчей Dota нет." if live_only else "В линии нет матчей Dota. Проверьте, что запущен сбор: bk collect-odds"
        self._last_list = [a.match.fixture_id for a in items]
        lines = ["Матчи в эфире:" if live_only else "Матчи Dota в линии:"]
        lines += [self._line(i, a) for i, a in enumerate(items, 1)]
        if not maps:
            lines.append("\nИстории матчей нет — оценки не будет. Запустите один раз: bk sync-matches")
        lines.append("\nПодробно — кнопки ниже (🔎 разбор, 📰 состав и новости).")
        buttons = []
        for i, a in enumerate(items[:20], 1):
            fid = a.match.fixture_id
            label = f"{i}. {a.match.team1} — {a.match.team2}"
            buttons.append([(label[:40], f"m:{fid}"), ("📰", f"n:{fid}")])
        return Reply("\n".join(lines), buttons=buttons)

    def _find(self, items: list[Analysis], arg: str) -> Analysis | None:
        target = next((a for a in items if a.match.fixture_id == arg), None)  # из кнопки
        if arg.isdigit() and self._last_list and 1 <= int(arg) <= len(self._last_list):
            fid = self._last_list[int(arg) - 1]
            target = next((a for a in items if a.match.fixture_id == fid), None)
        if target is None and arg.isdigit() and 1 <= int(arg) <= len(items):
            target = items[int(arg) - 1]
        if target is None:
            q = analysis.normalize_raw(arg)
            target = next((a for a in items if q and any(q in f(t) for t in (a.match.team1, a.match.team2)
                                                         for f in (analysis.normalize_raw, analysis.normalize))), None)
        return target

    def risks(self, m, now: int, fetch: bool = True) -> list[str]:
        """Состав по нашим данным и новости с флагами риска для обеих команд."""
        from . import news

        model, _ = self.models.get(self.conn)
        lines = []
        for team in (m.team1, m.team2):
            found = analysis.match_team(model, team)
            r = news.roster_risk(self.conn, found[0], now) if found else None
            if r:
                lines.append("⚠️ " + r.text(team))
        for team in (m.team1, m.team2):
            items = news.team_news(self.conn, self.news_client if fetch else None, team, now)
            risky = [it for it in items if it.risks]
            if risky:
                it = risky[0]
                lines.append(f"⚠️ {team}, новости ({', '.join(it.risks)}): {it.title}\n    {it.link}")
        return lines

    def news(self, arg: str) -> str:
        from . import news

        if not arg:
            return "Напишите: /news N (номер матча из /today) или /news название команды"
        now = int(self._now().timestamp())
        items, _ = self._analyses()
        target = self._find(items, arg) if items else None
        teams = [target.match.team1, target.match.team2] if target else [arg]
        lines = []
        model, _ = self.models.get(self.conn)
        for team in teams:
            found = analysis.match_team(model, team)
            r = news.roster_risk(self.conn, found[0], now) if found else None
            if r:
                lines.append("⚠️ " + r.text(team))
            lines += news.news_block(team, news.team_news(self.conn, self.news_client, team, now))
            lines.append("")
        if target:
            fid = target.match.fixture_id
            return Reply("\n".join(lines).strip(), buttons=[[("🔎 Разбор матча", f"m:{fid}"), ("🔄 Обновить", f"n:{fid}")]])
        return "\n".join(lines).strip()

    def match(self, arg: str) -> str | Reply:
        items, _ = self._analyses()
        if not items:
            return "В линии нет матчей Dota."
        target = self._find(items, arg)
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
            if a.factors:
                from .model import LABELS

                top = sorted(a.factors, key=lambda f: -abs(f[2]))[:4]
                lines.append("Главные факторы (+ за " + m.team1 + ", − за " + m.team2 + "):")
                lines += [f"  {LABELS.get(n, n)}: {c:+.2f}" for n, _, c in top if abs(c) >= 0.01]
        else:
            missing = [t for t, st in ((m.team1, s1), (m.team2, s2)) if st is None]
            lines.append(f"Нашей оценки нет: не нашёл в истории Tier 1–2 {', '.join(missing) or 'одну из команд'}. "
                         "Если это переименованная команда, свяжите названия на Mac: bk unmatched, затем bk alias")
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
        risk = self.risks(m, int(self._now().timestamp()))
        lines.append("\nРиски:" + ("\n" + "\n".join(risk) if risk else " по составу и новостям за 7 дней ничего не нашёл."))
        lines.append("Модель не знает героев до драфта (после драфта бот пересчитает карту сам и пришлёт сигнал, если он есть). "
                     "Подробнее — кнопка «Новости» ниже.")
        fid = m.fixture_id
        return Reply("\n".join(lines), buttons=[[("📰 Состав и новости", f"n:{fid}"), ("🔄 Обновить", f"m:{fid}")]])

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

    def model_report(self) -> str:
        from .model import FEATURES, LABELS

        _, maps = self.models.get(self.conn)
        r = self.models.report
        if not maps or r is None or not r.model_eval.n:
            return "Мало истории для проверки. Запустите bk sync-matches"
        lines = [f"Проверка на последних {self.models.eval_days} днях ({r.model_eval.n} карт, обучение на {r.train} более ранних):",
                 f"  только рейтинг: точность {r.elo_eval.accuracy:.1%}, Brier {r.elo_eval.brier:.4f}, log-loss {r.elo_eval.log_loss:.4f}",
                 f"  модель:         точность {r.model_eval.accuracy:.1%}, Brier {r.model_eval.brier:.4f}, log-loss {r.model_eval.log_loss:.4f}",
                 "  (меньше Brier и log-loss — лучше; монетка: 0.25 и 0.693)",
                 "Калибровка модели (прогноз фаворита → факт):"]
        lines += [f"  {rng}: карт {n}, прогноз {p:.0%}, факт {y:.0%}" for rng, n, p, y in r.model_eval.buckets]
        if r.draft_eval.n:
            from .model import DRAFT_LABELS

            lines.append(f"После драфта ({r.draft_eval.n} карт с героями): без драфта log-loss {r.draft_base_eval.log_loss:.4f}, "
                         f"с драфтом {r.draft_eval.log_loss:.4f}; точность {r.draft_base_eval.accuracy:.1%} → {r.draft_eval.accuracy:.1%}")
            lines.append("  Веса драфта: " + ", ".join(f"{n} {w:+.2f}" for n, w in zip(DRAFT_LABELS, r.draft_weights)))
        else:
            lines.append("Модели драфта пока нет: нужны составы с героями (bk sync-matches).")
        lines.append(f"Elo: K={r.k:.0f}, полураспад {r.half_life:.0f} дн. Веса: " + ", ".join(
            f"{LABELS[f]} {w:+.2f}" for f, w in zip(FEATURES, r.weights) if f in (r.selected or FEATURES)))
        dropped = [LABELS[f] for f in FEATURES if r.selected and f not in r.selected]
        if dropped:
            lines.append("Отброшены (не улучшали прогноз): " + ", ".join(dropped))
        if r.features_eval is not None:
            lines.append(f"Признаки команд проиграли рейтингу на последних днях (log-loss {r.features_eval.log_loss:.4f}), "
                         "поэтому до драфта работает рейтинг Elo, а драфт добавляется сверху.")
        return "\n".join(lines)

    def settings(self) -> str:
        t = self.thresholds
        c = self.sim
        return (f"Пороги сигнала: EV ≥ {t.min_ev:.0%}, вероятность ≥ {t.min_prob:.0%}, коэффициент ≥ {t.min_odds:.2f}, "
                f"расхождение с рынком ≤ {t.max_gap:.0%}.\n"
                f"Симулятор: банк {c.bank:.0f}, стратегия {c.strategy} (сумма {c.stake:.0f} / {c.percent:.0%} банка / {c.kelly:.2f} Келли).\n"
                "Меняются в .env: SIGNAL_MIN_EV, SIGNAL_MIN_PROB, SIGNAL_MIN_ODDS, SIGNAL_MAX_GAP, SIM_BANK, SIM_STRATEGY, SIM_STAKE, SIM_PERCENT, SIM_KELLY.")

    def export(self) -> Reply:
        from . import export as ex

        items, _ = self._analyses()
        paths = ex.export(self.conn, items, self.thresholds, self.sim, self.export_dir, self.tz_name, self._now())
        xlsx = [p for p in paths if p.suffix == ".xlsx"]
        text = f"Выгрузка: {len(items)} матчей в линии, ставки симулятора и итоги. Файлы на Mac: {self.export_dir.resolve()}"
        if not xlsx:
            text += "\nxlsx не собран: нет библиотеки openpyxl. Установите: pip install -e . — пока отправляю CSV."
        return Reply(text, xlsx or paths)

    def handle(self, user_id: int, text: str) -> str | Reply | None:
        if user_id not in self.allowed:
            return None  # чужим не отвечаем
        cmd, _, arg = (text or "").strip().partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in MENU_CMD:
            cmd, arg = MENU_CMD[cmd], ""
        elif (text or "").strip() in MENU_CMD:
            cmd, arg = MENU_CMD[text.strip()], ""
        try:
            if cmd in ("/start", "/help"):
                return Reply(HELP, menu=True)
            if cmd == "/today":
                return self.today()
            if cmd == "/live":
                return self.today(live_only=True)
            if cmd == "/match":
                return self.match(arg) if arg else "Напишите: /match N (номер из /today)"
            if cmd == "/value":
                return self.value()
            if cmd == "/news":
                return self.news(arg)
            if cmd == "/team":
                return self.team(arg)
            if cmd == "/model":
                return self.model_report()
            if cmd == "/settings":
                return self.settings()
            if cmd == "/sim":
                return simmod.report(self.conn, self.sim)
            if cmd == "/export":
                return self.export()
            if not cmd.startswith("/"):
                # просто текст: ищем матч в линии с такой командой, иначе — рейтинг команды
                query = (text or "").strip()
                items, _ = self._analyses()
                if items and self._find(items, query) and not query.isdigit():
                    return self.match(query)
                return self.team(query)
        except Exception as exc:  # бот не должен падать из-за одной команды
            return f"Ошибка: {exc}"
        return "Не знаю такой команды. Нажмите ❓ Помощь"

    def callback(self, user_id: int, data: str) -> str | Reply | None:
        """Нажатие кнопки под сообщением: m:<матч> — разбор, n:<матч> — состав и новости."""
        if user_id not in self.allowed:
            return None
        kind, _, fid = (data or "").partition(":")
        try:
            if kind == "m":
                return self.match(fid)
            if kind == "n":
                return self.news(fid)
        except Exception as exc:
            return f"Ошибка: {exc}"
        return None


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
    ("news", "состав и новости: /news N"),
    ("model", "точность модели"),
    ("sim", "симулятор бумажных ставок"),
    ("settings", "пороги сигнала"),
    ("export", "выгрузка в xlsx и csv"),
    ("help", "справка"),
]


def send_message(client: httpx.Client, token: str, chat_id: int, text: str) -> bool:
    for part in chunks(text):
        try:
            resp = client.post(f"{API}/bot{token}/sendMessage", json={"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
        except httpx.HTTPError:
            return False
        if resp.status_code >= 500 or resp.status_code == 429:
            return False  # временная ошибка Telegram — попробуем позже
    return True


class Outbox:
    """Уведомления, которые не ушли из-за обрыва связи с Telegram: отправляются, когда связь вернётся."""

    def __init__(self, client: httpx.Client, token: str, chats, limit: int = 200):
        import threading

        self.client, self.token, self.chats, self.limit = client, token, tuple(chats), limit
        self.pending: list[tuple[int, str]] = []
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            self.pending += [(c, text) for c in self.chats]
            del self.pending[:-self.limit]
        self.flush()

    def flush(self) -> int:
        """Отправляет по порядку; на первой неудаче останавливается. Возвращает число отправленных."""
        sent = 0
        with self._lock:
            while self.pending:
                chat, text = self.pending[0]
                if not send_message(self.client, self.token, chat, text):
                    break
                self.pending.pop(0)
                sent += 1
        return sent


def send_reply(client: httpx.Client, token: str, chat_id: int, reply: str | Reply, log=print) -> None:
    """Текст частями; кнопки — к последней части; затем файлы."""
    r = reply if isinstance(reply, Reply) else Reply(reply)
    parts = chunks(r.text) or [""]
    markup = reply_markup(r)
    for i, part in enumerate(parts):
        body = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
        if markup and i == len(parts) - 1:
            body["reply_markup"] = markup
        try:
            client.post(f"{API}/bot{token}/sendMessage", json=body)
        except httpx.HTTPError as exc:
            log(f"Не отправилось: {exc}")
    for path in r.files:
        if not send_document(client, token, chat_id, path):
            log(f"Файл не отправился: {path}")


def send_document(client: httpx.Client, token: str, chat_id: int, path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            resp = client.post(f"{API}/bot{token}/sendDocument", data={"chat_id": str(chat_id)}, files={"document": (path.name, f)})
        return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


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


def run_polling(token: str, bot: Bot, client: httpx.Client | None = None, log=print, stop=lambda: False, sleep=time.sleep,
                outbox: Outbox | None = None) -> None:
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
            # короткий long poll: VPN и провайдеры рвут долгие простаивающие соединения
            resp = client.get(f"{base}/getUpdates", params={"timeout": 25, "offset": offset})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            fails += 1
            # одиночные обрывы — норма через VPN: молча переподключаемся, пишем только о затяжных
            if fails == NOISY_FAILS or (fails > NOISY_FAILS and fails % 20 == 0):
                log(f"Telegram недоступен ({fails} раз подряд): {exc}. "
                    "Если так постоянно — включите VPN или задайте TELEGRAM_PROXY в .env")
            sleep(1 if fails < NOISY_FAILS else min(60, 5 * fails))
            continue
        if fails >= NOISY_FAILS:
            log("Связь с Telegram восстановлена")
        fails = 0
        if outbox is not None and outbox.pending:
            n = outbox.flush()
            if n:
                log(f"Доставлено отложенных уведомлений: {n}")
        if not data.get("ok"):
            log(f"Telegram: {data.get('description')}")
            sleep(5)
            continue
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if cq:
                try:  # убираем «часики» на кнопке
                    client.post(f"{base}/answerCallbackQuery", json={"callback_query_id": cq.get("id")})
                except httpx.HTTPError:
                    pass
                user = (cq.get("from") or {}).get("id")
                chat = ((cq.get("message") or {}).get("chat") or {}).get("id") or user
                reply = bot.callback(user, cq.get("data", "")) if user else None
            else:
                msg = upd.get("message") or {}
                user = (msg.get("from") or {}).get("id")
                chat = (msg.get("chat") or {}).get("id")
                reply = bot.handle(user, msg.get("text", "")) if user else None
            if reply is None:
                if user and user not in bot.allowed:
                    log(f"Сообщение от чужого ID {user} пропущено")
                continue
            send_reply(client, token, chat, reply, log)
