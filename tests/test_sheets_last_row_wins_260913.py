"""Quick 260913-16n (инцидент 13.09, owner report): на листе МСК за счёт постоянных
re-append'ов накопились дубли строк по одному telegram_id (до 5 на делегата). «🧹 Убрать
дубли» (`_dedupe_sheet_sync`) оставляет ПОСЛЕДНЮЮ (самую свежую) строку и удаляет остальные —
но три хелпера точечной/массовой записи (`_update_row_by_id_in_range`,
`_update_status_in_row_range`, `_bulk_update_status_row_range`) до этого квика писали в
ПЕРВОЕ совпадение col1, то есть в строку, которую дедуп потом удалит. Этот файл фиксирует,
что все три хелпера теперь тоже целятся в последнее совпадение — согласованно с дедупом.

Мок переиспользован импортом (не скопирован): `FakeWorksheet`/`_patch_fake_sheets` из
tests/test_sheet_status_city_tab_260819.py, `RecordingWorksheet` из
tests/test_sheets_update_row.py. Для теста дедупа (нужен `delete_rows`, которого нет у
FakeWorksheet) объявлен локальный подкласс.

pytest-asyncio в окружении нет — async-хелперы гоняются через asyncio.run(), как в
соседних файлах sheets.
"""
import asyncio

import gspread

import services.sheets as sheets
from handlers import admin_cities  # Phase 13 (13-05): cities/dedupe screen
from tests.test_sheet_status_city_tab_260819 import FakeWorksheet, _patch_fake_sheets
from tests.test_sheets_update_row import RecordingWorksheet


# ── _update_row_by_id_in_range: несколько совпадений → пишем в ПОСЛЕДНЕЕ ────────────────────

def test_update_row_by_id_writes_last_match_only():
    header = ["id", "Статус", "Детали"]
    ws = RecordingWorksheet(
        "СПб",
        rows=[
            ["555", "Одобрена", "old row2"],
            ["1", "-", "unrelated"],
            ["555", "Отклонена", "old row4"],
        ],
        header=header,
    )
    new_row = ["555", "Одобрена", "new details"]

    result = sheets._update_row_by_id_in_range(ws, 555, new_row)

    assert result is True
    assert ws.update_calls == [([new_row], "A4:C4")]
    # row 2 (первое совпадение) не тронута
    assert ws.rows[0] == ["555", "Одобрена", "old row2"]


def test_update_row_by_id_no_match_returns_false_no_writes():
    header = ["id", "Статус", "Детали"]
    ws = RecordingWorksheet("СПб", rows=[["1", "-", "x"]], header=header)

    result = sheets._update_row_by_id_in_range(ws, 999, ["999", "-", "y"])

    assert result is False
    assert ws.update_calls == []
    assert ws.update_cell_calls == []


# ── _update_status_in_row_range: несколько совпадений → ровно один update в последнюю ────────
# Квик 260919 (08-sheets-dashboard): production switched from gspread's update_cell (hardcodes
# USER_ENTERED, no RAW override) to an explicit-RAW update() on the single cell.

def test_update_status_in_row_range_writes_last_match_only():
    ws = FakeWorksheet(
        "СПб",
        rows=[
            ["777", "-"],
            ["2", "-"],
            ["777", "-"],
            ["3", "-"],
        ],
    )
    # дубли id=777 в row 2 и row 4 (rows[0] и rows[2])

    result = sheets._update_status_in_row_range(ws, "777", "Одобрена")

    assert result is True
    assert ws.update_cell_calls == []
    assert ws.update_calls == [([["Одобрена"]], "B4")]


# ── _bulk_update_status_row_range: дубли → ровно один диапазон на id, в последнюю строку ────

def test_bulk_update_status_row_range_writes_last_match_only():
    ws = FakeWorksheet(
        "СПб",
        rows=[
            ["555", "-"],  # row 2
            ["1", "-"],  # row 3
            ["555", "-"],  # row 4 — последнее совпадение 555
            ["2", "-"],  # row 5
        ],
    )
    wanted = {"555", "2"}
    id_to_label = {"555": "Одобрена", "2": "Отклонена"}

    updated, found = sheets._bulk_update_status_row_range(ws, wanted, id_to_label)

    assert updated == 2  # число уникальных id, а не число совпавших строк
    assert found == {"555", "2"}
    assert len(ws.batch_update_calls) == 1
    batch = ws.batch_update_calls[0]
    ranges_by_id = {}
    for u in batch:
        row, col = gspread.utils.a1_to_rowcol(u["range"])
        ranges_by_id[row] = u
    # ровно один диапазон для 555, указывающий на row 4 (последнюю), не row 2
    rows_written = [gspread.utils.a1_to_rowcol(u["range"])[0] for u in batch]
    assert rows_written.count(4) == 1
    assert 2 not in rows_written  # первое совпадение 555 не записано


# ── _dedupe_sheet_sync (сторож — поведение уже верное): выживает последняя строка ───────────

class _DedupeWorksheet(FakeWorksheet):
    """FakeWorksheet + delete_rows(n), которого нет у базового класса (тот написан для
    status-cell тестов, где строки не удаляются)."""

    def delete_rows(self, n):
        del self.rows[n - 2]


def test_dedupe_sheet_sync_keeps_last_row(monkeypatch):
    ws = _DedupeWorksheet(
        "main",
        rows=[
            ["7", "-old-1"],  # row 2
            ["9", "-other"],  # row 3
            ["7", "-old-2"],  # row 4
            ["7", "-newest"],  # row 5 — самая свежая, должна выжить
        ],
    )
    monkeypatch.setattr(sheets, "_get_sheet", lambda: ws)

    removed = sheets._dedupe_sheet_sync()

    assert removed == 2
    survivors = [r[0] for r in ws.rows]
    assert survivors.count("7") == 1
    # выжившая строка id=7 — именно «-newest»
    surviving_seven = [r for r in ws.rows if r[0] == "7"][0]
    assert surviving_seven[1] == "-newest"
    assert ["9", "-other"] in ws.rows


# ── сторож текста: менеджер видит, какая строка останется ───────────────────────────────────

class _FakeMessage:
    def __init__(self):
        self.texts = []

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)


class _FakeCallback:
    def __init__(self):
        self.message = _FakeMessage()

    async def answer(self, *args, **kwargs):
        pass


def test_dedupe_sheet_confirm_text_mentions_freshest_row():
    async def go():
        cb = _FakeCallback()
        await admin_cities.dedupe_sheet_confirm(cb)
        return cb.message.texts[-1]

    text = asyncio.run(go())
    assert "самую свежую" in text
