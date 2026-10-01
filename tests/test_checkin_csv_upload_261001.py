"""Загрузка выгрузки офлайн-сканера в боте (`handlers/admin_checkin.py::checkin_point_pick` +
`services/checkin_csv_import.py`): удалённая сессия не выдаётся за «уже были», кнопка после
перезапуска отвечает «пришлите файл заново», повторный тап не отмечает второй раз, большой файл
получает «⏳ Отмечаю…», записи без времени перечислены в отчёте, UTF-16 и дубли — сквозь весь
путь. Разбор форматов — `tests/test_checkin_csv_formats_261001.py`."""
from __future__ import annotations

import asyncio

from database import db
from handlers import admin_checkin
from handlers.states import CheckinImport
from services.checkin import build_payload
from services import checkin_csv_import
from tests.test_admin_checkin_260924 import (
    ADMIN_ID,
    _db_ready,
    _FakeBot,
    _FakeCallback,
    _FakeDocument,
    _FakeMessage,
    _flat_text,
    _insert_user,
    _new_state,
    _set_season,
)


# ── загрузка в боте ───────────────────────────────────────────────────────────────────────

def _approved(tid: int, name: str) -> str:
    asyncio.run(_insert_user(tid, status="approved", season="YL'26", full_name=name))
    return asyncio.run(db.get_or_create_checkin_token(tid))


def _upload(text_bytes: bytes):
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    asyncio.run(admin_checkin.checkin_import_file_step(message, state, _FakeBot(text_bytes)))
    return state, message


def test_utf16_upload_end_to_end_with_duplicates_counted_once(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    tok = _approved(1, "Иванов Иван")
    qr = build_payload("YL26", "Иванов Иван", "msk", tok)
    text = f"time,text\n2026-10-03 09:00:00,{qr}\n2026-10-03 09:00:05,\"{qr} \"\n{qr}\n"
    state, message = _upload(text.encode("utf-16"))
    assert any("Нашёл кодов: 1" in t for t in _flat_text(message))

    cb = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    report = next(t for t in _flat_text(cb.message) if "Отметки загружены" in t)
    assert "Отмечено новых: 1 · уже были: 0" in report
    assert "Без времени скана" not in report
    assert cb.answers[0] == ("Отмечаю…", False)


def test_untimed_records_are_listed_in_report(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    tok = _approved(1, "Иванов Иван")
    state = _new_state(ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=[{"qr": build_payload("YL26", "И", "msk", tok), "scanned_at": None}]))
    cb = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    report = next(t for t in _flat_text(cb.message) if "Отметки загружены" in t)
    assert "Без времени скана в файле: 1" in report


def test_tap_after_restart_asks_to_resend_file(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)  # перезапуск: ни состояния, ни записей
    for data in ("checkin_point:entry", "checkin_point_city:spb"):
        cb = _FakeCallback(data, ADMIN_ID)
        handler = admin_checkin.checkin_point_pick if data.startswith("checkin_point:") else admin_checkin.checkin_point_city_pick
        asyncio.run(handler(cb, state))
        assert cb.answers, data  # кнопка не висит со спиннером
        assert checkin_csv_import.LOST_FILE_TEXT in _flat_text(cb.message)


def test_second_tap_on_same_picker_does_not_mark_again(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    tok = _approved(1, "Иванов Иван")
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    asyncio.run(state.update_data(checkin_records=[{"qr": build_payload("YL26", "И", "msk", tok), "scanned_at": None}]))
    asyncio.run(admin_checkin.checkin_point_pick(_FakeCallback("checkin_point:entry", ADMIN_ID), state))
    cb2 = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb2, state))
    assert _flat_text(cb2.message) == [checkin_csv_import.LOST_FILE_TEXT]


def test_deleted_session_is_not_reported_as_duplicates(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    tok = _approved(1, "Иванов Иван")
    sid = asyncio.run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    asyncio.run(db.delete_program_session(sid))
    records = [{"qr": build_payload("YL26", "И", "spb", tok), "scanned_at": "2026-10-03 10:05:00"}]
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    asyncio.run(state.update_data(checkin_records=records))

    cb = _FakeCallback(f"checkin_point:session:{sid}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    texts = _flat_text(cb.message)
    assert texts[0].startswith("Сессию не нашёл")
    assert not any("уже были" in t for t in texts)
    kb = cb.message.sent[0][1]
    assert "checkin_point:entry" in [b.callback_data for row in kb.inline_keyboard for b in row]
    # файл не потерян — выбор «Входа» отмечает
    assert asyncio.run(state.get_data())["checkin_records"] == records
    cb2 = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb2, state))
    assert any("Отмечено новых: 1" in t for t in _flat_text(cb2.message))


def test_session_vanishing_mid_import_counts_as_not_marked(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    tok = _approved(1, "Иванов Иван")

    async def _gone(*_a, **_k):
        return {"status": "invalid_point"}

    monkeypatch.setattr(checkin_csv_import, "record_arrival", _gone)
    res = asyncio.run(checkin_csv_import.import_records(
        [{"qr": build_payload("YL26", "И", "spb", tok), "scanned_at": None}], "session:999",
        session=None, bound_city=None, staff_id=ADMIN_ID, bot=None, labels=admin_checkin._DENIAL_LABELS,
    ))
    assert res["point_gone"] == 1 and res["duplicate"] == 0
    lines = asyncio.run(checkin_csv_import.report_lines(res, row_limit=20))
    assert any("Не отмечено: 1" in line and "выберите точку заново" in line for line in lines)


def test_big_file_shows_progress_before_marking(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    n = checkin_csv_import.PROGRESS_THRESHOLD + 1
    records = [{"qr": build_payload("YL26", "Н", "msk", f"nobody{i}"), "scanned_at": None} for i in range(n)]
    state = _new_state(ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=records))
    cb = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    texts = _flat_text(cb.message)
    assert texts[0] == checkin_csv_import.progress_text(n)
    assert "Отметки загружены" in texts[1]
