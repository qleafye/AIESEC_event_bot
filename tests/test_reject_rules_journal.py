"""Phase 31 Plan 05 (D-18/D-19/D-24/D-29): `services/reject_journal.py` — сервисный слой
журнала автоотказов поверх аксессоров плана 31-02.

Два пласта, по задаче плана:
- Задача 1: сентинел `AUTO_DECIDED_BY`, `record_auto_reject` (счётчик попыток),
  `return_to_moderation` (атомарный возврат, право на город, изоляция от других заявок).
- Задача 2: `journal_page` (согласованные счётчик/список), `journal_line` (HTML-экранирование),
  `export_csv` (Excel-RU: `;`, utf-8-sig, защита от формульной инъекции на стороне аксессора).

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_reject_rules_db.py::_ready`.
"""
from __future__ import annotations

import asyncio
import csv
import io

from config import config
from database import db
from services import reject_journal as rj


def _ready(tmp_path, name="test_reject_rules_journal.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [900001]


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, status=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
    }))
    if status is not None:
        _run(_set_user_field(tid, "status", status))


async def _set_user_field(tid, field, value):
    async with db._connect() as conn:
        await conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, tid))
        await conn.commit()


async def _get_user_field(tid, field):
    async with db._connect() as conn:
        async with conn.execute(f"SELECT {field} FROM users WHERE telegram_id = ?", (tid,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


def _bind_manager(manager_id, city, admin_id=900001):
    _run(db.add_staff(manager_id, "reg_manager", admin_id))
    _run(db.set_staff_city(manager_id, city))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: сентинел, счётчик попыток, возврат на модерацию
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_auto_decided_by_is_negative_and_not_zero():
    assert rj.AUTO_DECIDED_BY == -1
    assert rj.AUTO_DECIDED_BY < 0
    assert bool(rj.AUTO_DECIDED_BY) is True


def test_attempt_counter_increments(tmp_path):
    """Три срабатывания подряд -> ОДНА живая строка журнала, attempt_count == 3 (D-24)."""
    _ready(tmp_path)
    _seed_user(3001, event_city="msk", status="rejected")

    first_id = _run(rj.record_auto_reject(3001, [1], ["Не подходит по критерию А."]))
    second_id = _run(rj.record_auto_reject(3001, [1, 2], ["Не подходит по критерию А.", "Б."]))
    third_id = _run(rj.record_auto_reject(3001, [1, 2, 3], ["А.", "Б.", "В."]))

    assert first_id == second_id == third_id
    entry = _run(db.get_auto_reject_log_entry(third_id))
    assert entry["attempt_count"] == 3


def test_return_to_moderation_reverts_status_and_clears_three_columns(tmp_path):
    _ready(tmp_path)
    _seed_user(3002, event_city="msk", status="rejected")
    _run(_set_user_field(3002, "auto_reject_rule_ids", "[1]"))
    _run(_set_user_field(3002, "auto_rejected_at", "2026-09-20 10:00:00"))
    _run(_set_user_field(3002, "auto_rule_note", "правило: Москва 1-2 курс"))
    entry_id = _run(rj.record_auto_reject(3002, [1], ["Не подходит."]))

    entry, error = _run(rj.return_to_moderation(900001, entry_id))
    assert error is None
    assert entry is not None
    assert entry["returned_to_moderation_at"] is not None

    assert _run(_get_user_field(3002, "status")) == "pending"
    assert _run(_get_user_field(3002, "auto_reject_rule_ids")) is None
    assert _run(_get_user_field(3002, "auto_rejected_at")) is None
    assert _run(_get_user_field(3002, "auto_rule_note")) is None


def test_return_to_moderation_double_return_wins_once(tmp_path):
    """Двойной тап по «вернуть на модерацию» срабатывает ровно один раз."""
    _ready(tmp_path)
    _seed_user(3003, event_city="msk", status="rejected")
    entry_id = _run(rj.record_auto_reject(3003, [1], ["Не подходит."]))

    first_entry, first_error = _run(rj.return_to_moderation(900001, entry_id))
    assert first_error is None
    assert first_entry is not None

    second_entry, second_error = _run(rj.return_to_moderation(900001, entry_id))
    assert second_entry is None
    assert second_error == "Эту заявку уже вернули на модерацию"

    # ничего не изменилось повторным вызовом — статус остаётся тем, что оставил первый возврат.
    assert _run(_get_user_field(3003, "status")) == "pending"


def test_return_to_moderation_missing_entry_gives_reason(tmp_path):
    _ready(tmp_path)
    entry, error = _run(rj.return_to_moderation(900001, 999999))
    assert entry is None
    assert error == "Запись недоступна — обновите список"


def test_return_to_moderation_rejects_manager_outside_city(tmp_path):
    """Возврат чужого города отклоняется (T-31-05-02: право на город проверяется ВНУТРИ
    return_to_moderation, до claim)."""
    _ready(tmp_path)
    _seed_user(3004, event_city="msk", status="rejected")
    entry_id = _run(rj.record_auto_reject(3004, [1], ["Не подходит."]))
    _bind_manager(910001, "spb")

    entry, error = _run(rj.return_to_moderation(910001, entry_id))
    assert entry is None
    assert error == "Эта заявка не из вашего города"
    # заявка не тронута отклонённой попыткой возврата
    assert _run(_get_user_field(3004, "status")) == "rejected"
    live = _run(db.get_auto_reject_log_entry(entry_id))
    assert live["returned_to_moderation_at"] is None


def test_return_to_moderation_allows_manager_of_matching_city(tmp_path):
    _ready(tmp_path)
    _seed_user(3005, event_city="spb", status="rejected")
    entry_id = _run(rj.record_auto_reject(3005, [1], ["Не подходит."]))
    _bind_manager(910002, "spb")

    entry, error = _run(rj.return_to_moderation(910002, entry_id))
    assert error is None
    assert entry is not None


def test_return_to_moderation_isolation_does_not_touch_other_delegate(tmp_path):
    """Возврат одной заявки не трогает ни статус, ни колонки ВТОРОГО посеянного делегата
    (изоляция — прямой урок инцидента 06.09)."""
    _ready(tmp_path)
    _seed_user(3006, event_city="msk", status="rejected")
    _seed_user(3007, event_city="msk", status="rejected")
    _run(_set_user_field(3007, "auto_reject_rule_ids", "[9]"))
    _run(_set_user_field(3007, "auto_rejected_at", "2026-09-20 09:00:00"))
    _run(_set_user_field(3007, "auto_rule_note", "не трогать"))
    entry_id_a = _run(rj.record_auto_reject(3006, [1], ["Не подходит."]))
    entry_id_b = _run(rj.record_auto_reject(3007, [9], ["Не подходит вовсе."]))

    entry, error = _run(rj.return_to_moderation(900001, entry_id_a))
    assert error is None
    assert entry["id"] == entry_id_a

    # делегат 3007 (вторая заявка) остался ровно таким, каким был — статус и колонки не тронуты.
    assert _run(_get_user_field(3007, "status")) == "rejected"
    assert _run(_get_user_field(3007, "auto_reject_rule_ids")) == "[9]"
    assert _run(_get_user_field(3007, "auto_rejected_at")) == "2026-09-20 09:00:00"
    assert _run(_get_user_field(3007, "auto_rule_note")) == "не трогать"
    live_b = _run(db.get_auto_reject_log_entry(entry_id_b))
    assert live_b["returned_to_moderation_at"] is None


def test_new_trigger_after_return_opens_a_new_row_with_attempt_one(tmp_path):
    _ready(tmp_path)
    _seed_user(3008, event_city="msk", status="rejected")
    first_id = _run(rj.record_auto_reject(3008, [1], ["Не подходит."]))
    _run(rj.return_to_moderation(900001, first_id))

    second_id = _run(rj.record_auto_reject(3008, [1], ["Не подходит снова."]))
    assert second_id != first_id
    entry = _run(db.get_auto_reject_log_entry(second_id))
    assert entry["attempt_count"] == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: страница журнала, строка экрана, выгрузка CSV
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_journal_page_empty_gives_empty_list_and_zero(tmp_path):
    _ready(tmp_path)
    rows, total = _run(rj.journal_page(900001))
    assert rows == []
    assert total == 0


def test_journal_page_count_and_length_are_consistent_at_twelve_rows(tmp_path):
    """12 записей, JOURNAL_PAGE == 10 — первая страница отдаёт 10 строк, счётчик знает про
    все 12 (правило queue_page: список и счётчик по одному набору фильтров)."""
    _ready(tmp_path)
    assert rj.JOURNAL_PAGE == 10
    for i in range(12):
        tid = 3100 + i
        _seed_user(tid, event_city="msk", status="rejected")
        _run(rj.record_auto_reject(tid, [1], ["Не подходит."]))

    rows, total = _run(rj.journal_page(900001))
    assert total == 12
    assert len(rows) == 10

    rows_page_two, total_two = _run(rj.journal_page(900001, offset=10))
    assert total_two == 12
    assert len(rows_page_two) == 2


def test_journal_page_include_returned_false_hides_returned_rows(tmp_path):
    _ready(tmp_path)
    _seed_user(3200, event_city="msk", status="rejected")
    _seed_user(3201, event_city="msk", status="rejected")
    entry_id = _run(rj.record_auto_reject(3200, [1], ["Не подходит."]))
    _run(rj.record_auto_reject(3201, [1], ["Не подходит."]))
    _run(rj.return_to_moderation(900001, entry_id))

    rows, total = _run(rj.journal_page(900001))
    assert total == 1
    assert len(rows) == 1
    assert rows[0]["telegram_id"] == 3201

    rows_all, total_all = _run(rj.journal_page(900001, include_returned=True))
    assert total_all == 2
    assert len(rows_all) == 2


def test_journal_line_escapes_full_name_and_rule_text():
    entry = {
        "full_name": "Иван <b>",
        "username": "masha",
        "last_triggered_at": "2026-09-20 14:12:00",
        "attempt_count": 2,
        "reject_texts": '["Правило М&M не подходит по треку."]',
        "returned_to_moderation_at": None,
    }
    line = rj.journal_line(entry)
    assert "&lt;b&gt;" in line
    assert "<b>" not in line
    assert "&amp;M" in line
    assert "попыток: 2" in line
    assert "@masha" in line
    assert "20.09 14:12" in line


def test_journal_line_marks_returned_rows():
    entry = {
        "full_name": "Пётр",
        "username": None,
        "last_triggered_at": "2026-09-20 14:12:00",
        "attempt_count": 1,
        "reject_texts": "[]",
        "returned_to_moderation_at": "2026-09-20 15:00:00",
    }
    line = rj.journal_line(entry)
    assert "возвращена на модерацию" in line
    assert "(без ника)" in line


def test_csv_export_reads_back_with_semicolon_and_neutralises_formula_injection(tmp_path):
    _ready(tmp_path)
    _run(db.add_user({
        "telegram_id": 3300,
        "full_name": "=HYPERLINK(\"http://evil\")",
        "registration_date": "2026-01-01 00:00:00",
        "event_city": "msk",
    }))
    _run(_set_user_field(3300, "status", "rejected"))
    _seed_user(3301, event_city="msk", status="rejected")
    _run(rj.record_auto_reject(3300, [1], ["Не подходит."]))
    _run(rj.record_auto_reject(3301, [1], ["Не подходит."]))

    filename, file_bytes = _run(rj.export_csv(900001))
    assert filename.endswith(".csv")

    text = file_bytes.decode("utf-8-sig")
    reader = list(csv.reader(io.StringIO(text), delimiter=';'))
    header, *data_rows = reader
    assert len(data_rows) == 2

    name_col = header.index("ФИО")
    names = [row[name_col] for row in data_rows]
    malicious = [n for n in names if "HYPERLINK" in n][0]
    assert not malicious.startswith("=")
