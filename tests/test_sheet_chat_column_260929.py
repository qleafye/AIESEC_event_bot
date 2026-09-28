"""Колонка «В чате» в листе делегатов (29.09).

- значение ячейки (`services.chat_tracking.chat_cell_values`): одобрен + в чате своего города ->
  «да», запись есть без присутствия -> «нет», записи нет -> «не проверено», не одобрен или чат
  города не привязан -> «-»; чат одного города не протекает делегатам другого (D-7);
- схема: «В чате» — последняя колонка главной, городской и короткой шапки, старая шапка —
  строгий префикс новой (ловушка прода 19.09: колонка посреди сдвигает строки); party без неё;
- стартовый пересчёт перезаписывает старый снимок Тюмени без «В чате»;
- строители строк (одиночная строка и пересборка) кладут значение из базы, а не «-»."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import cities
from config import config
from database import db
from handlers import reg_schema
from handlers import registration as reg
from services import chat_tracking
from services.sheets import ARRIVED_HEADER, CHAT_HEADER
from tests._dbtpl import fast_init_db

MSK_CHAT = -100111
TYUMEN_CHAT = -100333


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "chatcol.db")
    fast_init_db()


@contextmanager
def _cities_on(tmp_path):
    _ready(tmp_path)
    saved = list(cities.CITIES)
    try:
        _run(cities.seed_cities_if_empty())
        _run(cities.reload_cities())
        _run(db.set_setting("event_city_enabled", "on"))
        yield
    finally:
        cities.set_cities_for_test(saved)


async def _user(tid, city, status="approved"):
    await db.add_user({"telegram_id": tid, "event_city": city, "participant_type": "full",
                       "registration_date": "2026-09-01T00:00:00"})
    await db.set_user_status(tid, status)


async def _bind(city, chat_id):
    await chat_tracking.bind_chat(None, chat_id, "Чат", city)


# ── значение ячейки ────────────────────────────────────────────────────────────────────────

def test_cell_values_yes_no_unknown_dash_module_off(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind(None, MSK_CHAT)
        await _user(1, None)
        await _user(2, None)
        await _user(3, None)
        await _user(4, None, status="pending")
        await db.upsert_chat_member(MSK_CHAT, 1, "member", source="refresh")
        await db.upsert_chat_member(MSK_CHAT, 2, "left", source="refresh")
        await db.upsert_chat_member(MSK_CHAT, 4, "member", source="refresh")
        return await chat_tracking.chat_cell_values([1, 2, 3, 4, 999])

    values = _run(go())
    assert values[1] == chat_tracking.CHAT_CELL_YES == "да"
    assert values[2] == chat_tracking.CHAT_CELL_NO == "нет"
    assert values[3] == chat_tracking.CHAT_CELL_UNKNOWN == "не проверено"
    assert values[4] == "-"
    assert values[999] == "-"


def test_cell_values_no_bound_chat_is_dash(tmp_path):
    _ready(tmp_path)

    async def go():
        await _user(1, None)
        return await chat_tracking.chat_cell_values([1])

    assert _run(go()) == {1: "-"}


def test_cell_values_city_chat_does_not_leak(tmp_path):
    """Модуль городов включён, чат привязан только у Тюмени: москвич -> «-», тюменец,
    сидящий в «чужом» чате, но не в своём, — не «да»."""
    with _cities_on(tmp_path):
        async def go():
            await _bind("tyumen", TYUMEN_CHAT)
            await _user(10, "msk")
            await _user(11, "tyumen")
            await _user(12, "tyumen")
            await _user(13, None)  # NULL-город = Москва по normalize_city
            await db.upsert_chat_member(TYUMEN_CHAT, 10, "member", source="refresh")
            await db.upsert_chat_member(MSK_CHAT, 11, "member", source="refresh")
            await db.upsert_chat_member(TYUMEN_CHAT, 12, "administrator", source="refresh")
            return await chat_tracking.chat_cell_values([10, 11, 12, 13])

        values = _run(go())
    assert values[10] == "-"
    assert values[11] == "не проверено"
    assert values[12] == "да"
    assert values[13] == "-"


def test_cell_values_default_city_catches_null(tmp_path):
    with _cities_on(tmp_path):
        async def go():
            await _bind("msk", MSK_CHAT)
            await _user(20, None)
            await _user(21, "msk")
            await _user(22, "spb")
            await db.upsert_chat_member(MSK_CHAT, 20, "member", source="refresh")
            await db.upsert_chat_member(MSK_CHAT, 21, "kicked", source="refresh")
            await db.upsert_chat_member(MSK_CHAT, 22, "member", source="refresh")
            return await chat_tracking.chat_cell_values([20, 21, 22])

        values = _run(go())
    assert values == {20: "да", 21: "нет", 22: "-"}


def test_chat_cells_map_covers_approved(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind(None, MSK_CHAT)
        await _user(1, None)
        await _user(2, None, status="pending")
        await db.upsert_chat_member(MSK_CHAT, 1, "member", source="refresh")
        return await chat_tracking.chat_cells_map()

    values = _run(go())
    assert values.get(1) == "да"
    assert values.get(2, "-") == "-"


# ── схема ─────────────────────────────────────────────────────────────────────────────────

def test_static_headers_chat_last():
    assert reg_schema.SHEET_HEADERS[-1] == CHAT_HEADER == "В чате"
    assert reg_schema.SHEET_HEADERS[-2] == ARRIVED_HEADER


def _prefix_ok(new: list[str]) -> bool:
    old = [h for h in new if h != CHAT_HEADER]
    return new[-1] == CHAT_HEADER and len(new) == len(old) + 1 and new[:len(old)] == old


def test_active_short_party_headers(tmp_path):
    with _cities_on(tmp_path):
        async def go():
            await db.set_setting("reg_q_resume__city__tyumen", "off")
            return (
                await reg.active_sheet_headers(None),
                await reg.active_sheet_headers("tyumen"),
                await reg.short_sheet_headers(),
                await reg.party_sheet_headers(),
            )

        main_h, tyumen_h, short_h, party_h = _run(go())
    for headers in (main_h, tyumen_h, short_h):
        assert _prefix_ok(headers), headers
        assert headers[-2] == ARRIVED_HEADER
    assert CHAT_HEADER not in party_h


def test_startup_rewrites_old_tyumen_snapshot(tmp_path, monkeypatch):
    import main

    written = []

    async def fake_ensure(tab_name, headers):
        written.append((tab_name, headers))

    monkeypatch.setattr(main.sheets_service, "ensure_named_sheet_header", fake_ensure)
    with _cities_on(tmp_path):
        async def go():
            await db.set_setting("registration_mode", "full")
            live = await reg.active_sheet_headers("tyumen")
            old = [h for h in live if h != CHAT_HEADER]
            await db.set_setting("sheet_header_schema__city__tyumen", json.dumps(old, ensure_ascii=False))
            await main._maybe_ensure_city_sheet_headers()
            return old, await reg.get_sheet_schema("tyumen")

        old, snap = _run(go())
    assert snap[-1] == CHAT_HEADER
    assert snap[:-1] == old
    assert any(tab == "Тюмень" and h[-1] == CHAT_HEADER for tab, h in written)


# ── строители строк ───────────────────────────────────────────────────────────────────────

def test_active_sheet_row_takes_value_from_db(tmp_path):
    _ready(tmp_path)

    async def go():
        await _bind(None, MSK_CHAT)
        await _user(1, None)
        await db.upsert_chat_member(MSK_CHAT, 1, "left", source="refresh")
        headers = await reg.get_sheet_schema()
        row = await reg.active_sheet_row({"telegram_id": 1, "full_name": "Иван"})
        short = await reg.short_sheet_row({"telegram_id": 1, "full_name": "Иван"})
        return headers, row, await reg.short_sheet_headers(), short

    headers, row, short_h, short = _run(go())
    assert row[headers.index(CHAT_HEADER)] == "нет"
    assert short[short_h.index(CHAT_HEADER)] == "нет"


def test_rebuild_batches_take_value_from_db(tmp_path):
    _ready(tmp_path)
    from handlers import admin_sheets

    async def go():
        await _bind(None, MSK_CHAT)
        await _user(1, None)
        await _user(2, None)
        await db.upsert_chat_member(MSK_CHAT, 1, "member", source="refresh")
        users = [await db.get_user(1), await db.get_user(2)]
        return await admin_sheets.build_sheet_batches(users)

    batches = _run(go())
    main_batch = batches[0]
    col = main_batch.headers.index(CHAT_HEADER)
    assert [r[col] for r in main_batch.rows] == ["да", "не проверено"]
