import csv

import httpx
from openpyxl import load_workbook

from bkanalyzer import db, sim
from bkanalyzer.analysis import Thresholds
from bkanalyzer.bot import Bot, Reply, run_polling

from test_bot import ME, NOW, seed


def make_bot(tmp_path):
    conn = db.connect(tmp_path / "bk.sqlite3")
    seed(conn)
    b = Bot(conn, (ME,), Thresholds(0.05), now=lambda: NOW, sim=sim.SimSettings(bank=1000, strategy="fixed", stake=50),
            export_dir=tmp_path / "exports")
    items, _ = b._analyses()
    sim.place_bets(conn, items, b.thresholds, b.sim, now=NOW)
    conn.execute("UPDATE paper_bets SET status='lost', profit=-stake, closing_price=1.8")
    conn.commit()
    return b


def test_export_xlsx_and_csv(tmp_path):
    b = make_bot(tmp_path)
    r = b.handle(ME, "/export")
    assert isinstance(r, Reply) and r.files[0].suffix == ".xlsx"
    files = sorted(p.name for p in (tmp_path / "exports").iterdir())
    assert [f for f in files if f.endswith(".csv")] and len(files) == 4
    wb = load_workbook(r.files[0])
    assert wb.sheetnames == ["Матчи", "Ставки", "Итоги"]
    m = wb["Матчи"]
    rows = list(m.iter_rows(min_row=2, values_only=True))
    spirit = next(i for i, row in enumerate(rows, 2) if row[7] == "Spirit" and row[6] == "матч")
    assert m.cell(spirit, 1).fill.fgColor.rgb.endswith("C6EFCE")  # положительный EV — зелёный
    bets = wb["Ставки"]
    assert bets.max_row >= 2 and bets.cell(2, 16).value == "проигрыш"
    assert bets.cell(2, 1).fill.fgColor.rgb.endswith("FFC7CE")  # проигрыш — красный
    summary = {row[0]: row for row in wb["Итоги"].iter_rows(min_row=2, values_only=True)}
    assert summary["до матча"][1] == "фикс. сумма" and summary["до матча"][5] >= 1
    csv_bets = next(p for p in (tmp_path / "exports").iterdir() if p.name.endswith("_bets.csv"))
    with csv_bets.open(encoding="utf-8-sig") as f:
        head = next(csv.reader(f, delimiter=";"))
    assert head[0] == "№" and "CLV" in head


def test_polling_sends_document(tmp_path):
    b = make_bot(tmp_path)
    calls = []

    def handler(req: httpx.Request):
        path = req.url.path
        calls.append(path.rsplit("/", 1)[-1])
        if path.endswith("getUpdates"):
            if calls.count("getUpdates") == 1:
                return httpx.Response(200, json={"ok": True, "result": [
                    {"update_id": 1, "message": {"from": {"id": ME}, "chat": {"id": ME}, "text": "/export"}}]})
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    run_polling("T", b, client, log=lambda *_: None, stop=lambda: calls.count("getUpdates") >= 2, sleep=lambda *_: None)
    assert "sendMessage" in calls and calls.count("sendDocument") == 1
