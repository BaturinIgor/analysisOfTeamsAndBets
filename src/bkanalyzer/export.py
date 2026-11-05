"""Выгрузка в xlsx (листы «Матчи», «Ставки», «Итоги») и параллельно в CSV без цвета.

Зелёным — выигрыши и положительный EV, красным — проигрыши и флаги риска.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sim as simmod
from .analysis import Analysis, Thresholds, market_label

GREEN, RED, GREY = "C6EFCE", "FFC7CE", "EDEDED"
STATUS = {"open": "открыта", "won": "выигрыш", "lost": "проигрыш", "void": "возврат"}


def _local(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def match_rows(analyses: list[Analysis], t: Thresholds, tz: ZoneInfo) -> list[tuple[list, str | None]]:
    """Строки листа «Матчи»: по строке на каждую цену, где есть наша оценка. (значения, цвет)."""
    out = []
    for a in analyses:
        m = a.match
        base = [_local(m.start_time, tz), "в эфире" if m.phase == "live" else "до матча", m.score or "", m.team1, m.team2]
        if not a.picks:
            note = "команда не найдена в истории" if None in a.known else "нет коэффициентов на рынки, которые мы считаем"
            out.append((base + ["", "", None, None, None, None, note], GREY))
            continue
        for p in a.picks:
            flags = []
            if t.suspicious(p):
                flags.append(f"расхождение с рынком > {t.max_gap:.0%}")
            if p.ev >= t.min_ev and not t.ok(p) and not flags:
                flags.append("не прошёл фильтр вероятности/коэффициента")
            signal = t.ok(p)
            note = "сигнал" if signal else "; ".join(flags)
            color = RED if flags else GREEN if p.ev > 0 else None
            out.append((base + [market_label(p.market), p.team, p.price, round(p.prob, 4),
                                round(p.book_prob, 4) if p.book_prob is not None else None, round(p.ev, 4), note], color))
    return out


MATCH_HEAD = ["Начало", "Фаза", "Счёт", "Команда 1", "Команда 2", "Рынок", "Исход", "Коэф.",
              "Наша вероятность", "Вероятность рынка", "EV", "Пометка"]
BET_HEAD = ["№", "Поставлена", "Режим", "Начало", "Команда 1", "Команда 2", "Рынок", "Ставка на", "Коэф.",
            "Наша вероятность", "Вероятность рынка", "EV", "Сумма", "Банк до", "Стратегия", "Статус",
            "Прибыль", "Закрытие", "CLV", "Рассчитана", "Комментарий"]
SUMMARY_HEAD = ["Режим", "Стратегия", "Ставок", "Открыто", "Выигрыш", "Проигрыш", "Возврат", "Поставлено", "Прибыль",
                "ROI", "Банк", "Макс. просадка", "Худшая серия", "Средний CLV"]


def bet_rows(conn: sqlite3.Connection, tz: ZoneInfo) -> list[tuple[list, str | None]]:
    simmod.ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM paper_bets ORDER BY id")]
    conn.row_factory = None
    out = []
    for r in rows:
        clv = r["price"] / r["closing_price"] - 1 if r.get("closing_price") else None
        values = [r["id"], _local(r["created_at"], tz), simmod.MODE_LABEL.get(r["mode"], r["mode"]), _local(r["start_time"], tz),
                  r["team1"], r["team2"], market_label(r["market"]), r["pick"], r["price"], round(r["prob"], 4),
                  round(r["book_prob"], 4) if r["book_prob"] is not None else None, round(r["prob"] * r["price"] - 1, 4),
                  r["stake"], r["bank_before"], r["strategy"], STATUS.get(r["status"], r["status"]),
                  r["profit"], r["closing_price"], round(clv, 4) if clv is not None else None,
                  _local(r["settled_at"], tz), r["note"] or ""]
        out.append((values, {"won": GREEN, "lost": RED, "void": GREY}.get(r["status"])))
    return out


def summary_rows(conn: sqlite3.Connection, cfg: simmod.SimSettings) -> list[tuple[list, str | None]]:
    out = []
    for mode in simmod.MODES:
        for c in cfg.all():
            s = simmod.summary(conn, c, mode)
            values = [simmod.MODE_LABEL[mode], simmod.STRATEGY_LABEL.get(c.strategy, c.strategy), s["bets"], s["open"], s["won"], s["lost"], s["void"], round(s["staked"], 2),
                      round(s["profit"], 2), round(s["roi"], 4) if s["roi"] is not None else None, round(s["bank"], 2),
                      round(s["max_dd"], 4), s["worst_streak"], round(s["clv"], 4) if s["clv"] is not None else None]
            color = None if not (s["won"] + s["lost"]) else GREEN if s["profit"] > 0 else RED if s["profit"] < 0 else None
            out.append((values, color))
    return out


PERCENT_COLS = {"Наша вероятность", "Вероятность рынка", "EV", "CLV", "ROI", "Макс. просадка", "Средний CLV"}


def _sheet(wb, title: str, head: list[str], rows: list[tuple[list, str | None]]) -> None:
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(title)
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for values, color in rows:
        ws.append(values)
        if color:
            fill = PatternFill("solid", fgColor=color)
            for c in ws[ws.max_row]:
                c.fill = fill
    for i, name in enumerate(head, 1):
        letter = get_column_letter(i)
        if name in PERCENT_COLS:
            for c in ws[letter][1:]:
                c.number_format = "0.0%"
        width = max([len(str(name))] + [len(str(v[i - 1])) for v, _ in rows if v[i - 1] is not None])
        ws.column_dimensions[letter].width = min(40, width + 2)
    ws.freeze_panes = "A2"


def _csv(path: Path, head: list[str], rows: list[tuple[list, str | None]]) -> None:
    # utf-8-sig и «;» — чтобы Excel и Numbers сразу открыли кириллицу по колонкам
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(head)
        for values, _ in rows:
            w.writerow(["" if v is None else v for v in values])


def export(conn: sqlite3.Connection, analyses: list[Analysis], thresholds: Thresholds, cfg: simmod.SimSettings,
           out_dir: Path, tz: str = "Europe/Moscow", now: datetime | None = None) -> list[Path]:
    """Пишет bk_ГГГГ-ММ-ДД_ЧЧММ.xlsx и три CSV рядом. Возвращает пути (xlsx первым)."""
    zone = ZoneInfo(tz)
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(zone).strftime("%Y-%m-%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = [("Матчи", "matches", MATCH_HEAD, match_rows(analyses, thresholds, zone)),
              ("Ставки", "bets", BET_HEAD, bet_rows(conn, zone)),
              ("Итоги", "summary", SUMMARY_HEAD, summary_rows(conn, cfg))]
    paths = []
    try:
        from openpyxl import Workbook
    except ImportError:
        Workbook = None
    if Workbook is not None:
        wb = Workbook()
        wb.remove(wb.active)
        for title, _, head, rows in sheets:
            _sheet(wb, title, head, rows)
        xlsx = out_dir / f"bk_{stamp}.xlsx"
        wb.save(xlsx)
        paths.append(xlsx)
    for _, slug, head, rows in sheets:
        p = out_dir / f"bk_{stamp}_{slug}.csv"
        _csv(p, head, rows)
        paths.append(p)
    return paths
