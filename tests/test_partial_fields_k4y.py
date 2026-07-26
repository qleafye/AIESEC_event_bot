"""Quick 260726-k4y: partial FSM answers persisted for the «Незавершённые» sheet tab.

pytest-asyncio unavailable → async helpers driven via asyncio.run(), same style as
tests/test_dropout_lifecycle_block6.py.
"""
import asyncio
import json

from config import config
from database import db


def _use_tmp(tmp_path):
    config.DB_PATH = str(tmp_path / "partial.db")


# ── Task 1: database/db.py persistence ───────────────────────────────────────

def test_set_reg_step_persists_partial_json(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.mark_reg_started(1, "vasya")
        await db.set_reg_step(1, "city", partial_json='{"full_name":"Иванов"}')
        rows = await db.get_incomplete_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row[4] == '{"full_name":"Иванов"}'

    asyncio.run(go())


def test_set_reg_step_without_partial_json_does_not_wipe_snapshot(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.mark_reg_started(1, "vasya")
        await db.set_reg_step(1, "city", partial_json='{"full_name":"Иванов"}')
        # subsequent call without a snapshot (e.g. re-entry) must not reset to NULL
        await db.set_reg_step(1, "email")
        rows = await db.get_incomplete_rows()
        row = rows[0]
        assert row[3] == "email"  # last_step updated
        assert row[4] == '{"full_name":"Иванов"}'  # snapshot preserved

    asyncio.run(go())


def test_get_incomplete_rows_none_partial_data_for_row_without_snapshot(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.mark_reg_started(1, "vasya")
        await db.set_reg_step(1, "city")  # no partial_json at all
        rows = await db.get_incomplete_rows()
        row = rows[0]
        # indices 0..3 unchanged in meaning/order — old positional tests keep passing
        assert row[0] == 1
        assert row[1] == "vasya"
        assert row[3] == "city"
        assert row[4] is None

    asyncio.run(go())


def test_migration_is_idempotent(tmp_path):
    _use_tmp(tmp_path)

    async def go():
        await db.init_db()
        await db.init_db()  # second call on already-migrated DB must not raise

    asyncio.run(go())


# ── Task 2: handlers/registration.py projection helpers ─────────────────────

def test_incomplete_sheet_headers_shape():
    async def go():
        from handlers.registration import incomplete_sheet_headers

        headers = await incomplete_sheet_headers()
        assert headers[:4] == ["ID Telegram", "Username", "Начал регистрацию", "Остановился на"]
        assert "ФИО" in headers
        assert "Статус" not in headers
        assert "Дата регистрации" not in headers
        assert "Детали" not in headers
        assert len(headers) == len(set(headers))  # no duplicates

    asyncio.run(go())


def test_incomplete_sheet_row_projects_answered_fields():
    async def go():
        from handlers.registration import incomplete_sheet_headers, incomplete_sheet_row

        headers = await incomplete_sheet_headers()
        partial_json = json.dumps({"full_name": "Иванов И.И.", "phone": "+7999"}, ensure_ascii=False)
        row = incomplete_sheet_row(1, "vasya", "2026-07-01 10:00:00", "city", partial_json, headers)
        values = dict(zip(headers, row))
        assert values["ФИО"] == "Иванов И.И."
        if "Телефон" in values:
            assert values["Телефон"] == "+7999"
        # 4th cell is the human-readable dropout label
        assert row[3] not in (None, "")
        assert row[3] != "city"  # label, not raw step key

    asyncio.run(go())


def test_incomplete_sheet_row_unanswered_fields_are_dash():
    async def go():
        from handlers.registration import incomplete_sheet_headers, incomplete_sheet_row

        headers = await incomplete_sheet_headers()
        partial_json = json.dumps({"full_name": "Иванов И.И."}, ensure_ascii=False)
        row = incomplete_sheet_row(1, "vasya", "2026-07-01 10:00:00", "city", partial_json, headers)
        values = dict(zip(headers, row))
        # any header beyond the base 4 + ФИО that was not answered must be "-"
        for h in headers[4:]:
            if h == "ФИО":
                continue
            assert values[h] == "-"

    asyncio.run(go())


def test_incomplete_sheet_row_neutralizes_formula_injection():
    async def go():
        from handlers.registration import incomplete_sheet_headers, incomplete_sheet_row

        headers = await incomplete_sheet_headers()
        partial_json = json.dumps({"full_name": '=HYPERLINK("http://x")'}, ensure_ascii=False)
        row = incomplete_sheet_row(1, "vasya", "2026-07-01 10:00:00", "city", partial_json, headers)
        values = dict(zip(headers, row))
        assert values["ФИО"].startswith("'")

    asyncio.run(go())


def test_incomplete_sheet_row_handles_none_and_broken_json():
    async def go():
        from handlers.registration import incomplete_sheet_headers, incomplete_sheet_row

        headers = await incomplete_sheet_headers()
        # None partial_json
        row = incomplete_sheet_row(1, "vasya", "2026-07-01 10:00:00", "city", None, headers)
        assert row  # no exception
        values = dict(zip(headers, row))
        assert values["ФИО"] == "-"

        # broken JSON
        row2 = incomplete_sheet_row(1, "vasya", "2026-07-01 10:00:00", "city", "not json", headers)
        values2 = dict(zip(headers, row2))
        assert values2["ФИО"] == "-"

    asyncio.run(go())
