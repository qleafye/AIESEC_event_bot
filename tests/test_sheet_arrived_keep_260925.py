"""День форума 25.09: полная перезапись строки делегата в листе не затирает «Пришёл».

Раньше лямбда колонки «Пришёл» в `SHEET_COLUMNS`/`PARTY_SHEET_COLUMNS` всегда отдавала «-»:
отметку прихода пишет джоба очереди (services/sheet_arrival_sync.py), а любая полная перезапись
строки (правка анкеты, ссылка на резюме, перевод в город — `update_row_by_id`; «♻️ Пересобрать» и
«🔄 Синхронизация» — `build_sheet_batches`) писала «-» поверх времени. Теперь строители строки
берут значение из базы тем же правилом, что очередь (первый вход за форум, «25.09 05:23»):
одиночная строка — `with_arrived_cell`, массовая — один запрос `arrived_cells_map`."""
from __future__ import annotations

import asyncio

from database import db
from handlers import admin_sheets, reg_schema
from handlers.registration import (
    active_sheet_row, party_sheet_headers, party_sheet_row, short_sheet_headers, short_sheet_row,
)
from services import reg_finalize
import services.sheets as sheets
from tests._dbtpl import fast_init_db
from tests.test_sheet_status_city_tab_260819 import _use_tmp_db

SCAN = "2026-09-25 05:23:33"
CELL = "25.09 05:23"


async def _user(tid: int, participant_type: str = "full", arrived: bool = True):
    await db.add_user({
        "telegram_id": tid, "participant_type": participant_type,
        "registration_date": "2026-09-01T00:00:00", "full_name": f"Делегат {tid}",
    })
    if arrived:
        await db.record_checkin(tid, db.CHECKIN_ENTRY_POINT, source="csv", scanned_at=SCAN)
    return await db.get_user(tid)


def _arrived(row: list, headers: list[str]) -> str:
    return row[headers.index(sheets.ARRIVED_HEADER)]


def test_single_row_builders_take_arrival_from_db(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        full = await _user(1)
        fresh = await _user(2, arrived=False)
        party = await _user(3, "party_overnight")
        short = await _user(4, "short")
        headers = await reg_schema.active_sheet_headers()
        assert _arrived(await active_sheet_row(full), headers) == CELL
        assert _arrived(await active_sheet_row(fresh), headers) == "-"
        assert _arrived(await party_sheet_row(party), await party_sheet_headers()) == CELL
        assert _arrived(await short_sheet_row(short), await short_sheet_headers()) == CELL

    asyncio.run(go())


def test_update_row_by_id_keeps_arrival(tmp_path, monkeypatch):
    """Путь правки строки (здесь — ссылка на резюме, тот же `row_fn` + `update_row_by_id`, что у
    правки анкеты и перевода в город): в перезаписанной строке время прихода, не «-»."""
    _use_tmp_db(tmp_path)
    written: list[list] = []

    async def fake_update(tab, telegram_id, row, *a, **kw):
        written.append(row)
        return True

    monkeypatch.setattr(sheets, "update_row_by_id", fake_update)

    async def go():
        fast_init_db()
        full = await _user(7)
        await reg_finalize._apply_resume_url(7, full, "https://cloud.example/s/x")
        return await reg_schema.active_sheet_headers()

    headers = asyncio.run(go())
    assert len(written) == 1
    assert _arrived(written[0], headers) == CELL


def test_rebuild_batches_keep_arrival(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        users = [await _user(11), await _user(12, arrived=False)]
        return await admin_sheets.build_sheet_batches(users)

    main = asyncio.run(go())[0]
    by_id = {u["telegram_id"]: r for u, r in zip(main.users, main.rows)}
    assert _arrived(by_id[11], main.headers) == CELL
    assert _arrived(by_id[12], main.headers) == "-"


def test_rebuild_100_rows_one_db_query(monkeypatch):
    calls = {"map": 0}

    async def fake_map():
        calls["map"] += 1
        return {tid: SCAN for tid in range(1, 101, 2)}  # пришли нечётные

    async def per_row(_tid):
        raise AssertionError("пересборка не должна ходить в базу по строке")

    async def fake_route(*_a):
        return None

    async def fake_code(_c):
        return None

    async def fake_headers(_code=None):
        return ["ID Telegram", sheets.ARRIVED_HEADER]

    monkeypatch.setattr(db, "first_entry_scanned_at_map", fake_map)
    monkeypatch.setattr(db, "first_entry_scanned_at", per_row)
    monkeypatch.setattr(admin_sheets, "city_row_tab", fake_route)
    monkeypatch.setattr(admin_sheets, "sheet_city_code", fake_code)
    monkeypatch.setattr(admin_sheets, "active_sheet_headers", fake_headers)

    users = [{"telegram_id": tid, "participant_type": "full"} for tid in range(1, 101)]
    main = asyncio.run(admin_sheets.build_sheet_batches(users))[0]
    assert calls["map"] == 1
    assert len(main.rows) == 100
    assert main.rows[0][1] == CELL
    assert main.rows[1][1] == "-"
