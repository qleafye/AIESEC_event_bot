"""Выгрузка анкеты во вкладку «Опрос <id>»: колонки, статусы, fail-soft, общая вкладка «Опросы»
анкет не содержит."""
import asyncio

from database import db
from database import survey_db as sdb
from services.comms import polls as polls_svc
from services.comms import survey_report as rep
from tests.test_polls_260822 import ADMIN_ID, _ready, _add_user


def _run(c):
    return asyncio.run(c)


def _setup():
    _run(_add_user(1, "Иван Петров", "@ivan", city="msk"))
    _run(_add_user(2, "Мария", "@masha", city="msk"))
    pid = _run(sdb.create_survey_draft("Анкета", ADMIN_ID))
    q1 = _run(sdb.add_survey_question(
        pid, "multi", "Что понравилось",
        options=[{"t": "А", "more": "Почему?"}, {"t": "В", "more": None}]))
    q2 = _run(sdb.add_survey_question(pid, "table", "Оценки", rows=["Еда", "Зал", "Звук"]))
    q3 = _run(sdb.add_survey_question(pid, "text", "Комментарий"))
    _run(db.set_poll_status(pid, "open"))
    return pid, q1, q2, q3


def test_sheet_columns_and_rows(tmp_path):
    _ready(tmp_path)
    pid, q1, q2, q3 = _setup()
    _run(sdb.start_survey_progress(pid, 1, q1))
    _run(sdb.upsert_survey_answer(pid, 1, q1, "", [0, 1]))
    _run(sdb.upsert_survey_answer(pid, 1, q1, "m0", "вкусно"))
    _run(sdb.upsert_survey_answer(pid, 1, q2, "r0", 5))
    _run(sdb.upsert_survey_answer(pid, 1, q2, "r1", 0))
    _run(sdb.upsert_survey_answer(pid, 1, q3, "", "=HYPERLINK(\"http://x\")"))
    _run(sdb.finish_survey(pid, 1, q1))
    _run(sdb.start_survey_progress(pid, 2, q1))  # не закончил, ничего не ответил
    headers, rows = _run(rep.build_survey_sheet(pid))
    assert headers[:6] == ["Дата", "Telegram ID", "ФИО", "@ник", "Город", "Статус"]
    assert len(headers) == 6 + 1 + 1 + 3 + 1  # multi + уточнение + 3 строки таблицы + текст
    assert any("уточнение: А" in h for h in headers)
    assert any(h.endswith("— Зал") for h in headers)
    assert len(rows) == 2
    done = next(r for r in rows if r[1] == 1)
    part = next(r for r in rows if r[1] == 2)
    assert done[5] == "завершил" and part[5] == "не закончил"
    assert done[2] == "Иван Петров" and done[3] == "@ivan"
    assert done[6] == "А; В" and done[7] == "вкусно"
    assert done[8] == "5" and done[9] == "Не могу оценить" and done[10] == ""
    assert part[6:] == [""] * (len(headers) - 6)


def test_cells_pass_through_sheet_safe(tmp_path, monkeypatch):
    _ready(tmp_path)
    pid, q1, q2, q3 = _setup()
    _run(sdb.start_survey_progress(pid, 1, q1))
    _run(sdb.upsert_survey_answer(pid, 1, q3, "", "=HYPERLINK(\"http://x\")"))
    monkeypatch.setattr(rep, "_sheet_safe", lambda v: f"'{v}" if str(v).startswith("=") else v)
    _, rows = _run(rep.build_survey_sheet(pid))
    assert rows[0][-1].startswith("'=HYPERLINK")


def test_export_uses_own_tab_and_fails_soft(tmp_path, monkeypatch):
    _ready(tmp_path)
    import services.sheets.sheets as sheets
    calls = []

    async def fake_sync(title, headers, rows):
        calls.append((title, headers, rows))
        return len(rows)
    monkeypatch.setattr(sheets, "sync_named_worksheet", fake_sync)

    async def no_general():
        raise AssertionError("общая вкладка «Опросы» не должна вызываться")
    monkeypatch.setattr(polls_svc, "export_polls_to_sheet", no_general)
    pid, q1, _, _ = _setup()
    _run(sdb.start_survey_progress(pid, 1, q1))
    assert _run(rep.export_survey_to_sheet(pid)) == 1
    assert calls[0][0] == f"Опрос {pid}"

    async def boom(title, headers, rows):
        raise RuntimeError("simulated gspread failure")
    monkeypatch.setattr(sheets, "sync_named_worksheet", boom)
    assert _run(rep.export_survey_to_sheet(pid)) == -1


def test_general_polls_tab_skips_surveys(tmp_path):
    _ready(tmp_path)
    pid, q1, _, _ = _setup()
    _run(sdb.start_survey_progress(pid, 1, q1))
    _run(sdb.create_survey_draft("Черновик", ADMIN_ID))
    ordinary = _run(db.create_poll("Обычный?", ["да", "нет"], is_anonymous=True, allows_multiple=False,
                                   created_by=ADMIN_ID, city=None, audience=[],
                                   scheduled_at="2030-01-01 10:00:00"))
    rows = _run(polls_svc.build_polls_sheet_rows())
    flat = " ".join(str(c) for r in rows for c in r)
    assert "Обычный?" in flat and "Анкета" not in flat and "Черновик" not in flat
    assert _run(polls_svc.build_polls_sheet_rows([pid])) == []
    assert ordinary
