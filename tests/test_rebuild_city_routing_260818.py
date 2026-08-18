"""UAT 17.08 fast-fix: «♻️ Пересобрать таблицу» must route rows by city exactly like the live
append does. Before this fix the rebuild wrote EVERY user into the main tab, so after one
rebuild the main tab (Moscow) also held СПб/Тюмень rows while live appends kept sending those
cities to their own tabs -- prod drift observed 2026-08-17 (139 rows in the main tab = all
users, incl. 12 spb + 11 tyumen). Contract:
  * cities module ON  -> main tab gets only rows whose city_row_tab() is None (default city /
    NULL city); every other city tab is full-refreshed with its own rows;
  * cities module OFF -> city_row_tab() is always None -> old behaviour byte-identical
    (all rows to main, no named-tab sync at all).
Handler called directly (established idiom, see test_rebuild_confirm_260813_sdl.py)."""
import asyncio

from handlers import admin as admin_mod
from handlers import admin_settings  # Phase 13 (13-06): settings moved out of admin.py
from tests.test_rebuild_confirm_260813_sdl import _FakeCallback, ADMIN_ID


def _users():
    return [
        {"telegram_id": 1, "event_city": "msk", "participant_type": "full"},
        {"telegram_id": 2, "event_city": None, "participant_type": "full"},
        {"telegram_id": 3, "event_city": "spb", "participant_type": "full"},
        {"telegram_id": 4, "event_city": "spb", "participant_type": "full"},
        {"telegram_id": 5, "event_city": "tyumen", "participant_type": "full"},
    ]


def _wire(monkeypatch, route):
    main_calls, named_calls = [], []

    async def fake_headers():
        return ["ID"]

    async def fake_users():
        return _users()

    async def fake_rebuild(headers, rows):
        main_calls.append(rows)
        return len(rows)

    async def fake_sync(title, headers, rows):
        named_calls.append((title, rows))
        return len(rows)

    async def fake_schema(headers):
        return None

    async def fake_kb(uid):
        return None

    monkeypatch.setattr(admin_settings, "active_sheet_headers", fake_headers)
    monkeypatch.setattr(admin_settings, "get_all_users_dicts", fake_users)
    monkeypatch.setattr(admin_settings, "rebuild_main_sheet", fake_rebuild)
    monkeypatch.setattr(admin_settings, "sync_named_worksheet", fake_sync)
    monkeypatch.setattr(admin_settings, "set_sheet_schema", fake_schema)
    monkeypatch.setattr(admin_settings, "admin_keyboard_for", fake_kb)
    monkeypatch.setattr(admin_settings, "_sheet_value_map", lambda u: {"ID": u["telegram_id"]})
    monkeypatch.setattr(admin_settings, "city_row_tab", route)
    return main_calls, named_calls


def test_rebuild_routes_rows_by_city_when_module_on(monkeypatch):
    async def route(event_city, participant_type):
        return {"spb": "СПб", "tyumen": "Тюмень"}.get(event_city)

    main_calls, named_calls = _wire(monkeypatch, route)
    cb = _FakeCallback(ADMIN_ID)
    asyncio.run(admin_settings.rebuild_sheet(cb))

    assert main_calls == [[[1], [2]]]  # msk + NULL city only
    assert sorted(named_calls) == [("СПб", [[3], [4]]), ("Тюмень", [[5]])]
    assert "СПб" in cb.message.text and "Тюмень" in cb.message.text
    assert "пересобрана" in cb.message.text.lower()


def test_rebuild_module_off_is_old_behaviour(monkeypatch):
    async def route(event_city, participant_type):
        return None  # cities module off / no per-city base -> everything to main

    main_calls, named_calls = _wire(monkeypatch, route)
    cb = _FakeCallback(ADMIN_ID)
    asyncio.run(admin_settings.rebuild_sheet(cb))

    assert main_calls == [[[1], [2], [3], [4], [5]]]
    assert named_calls == []
    assert "Городские вкладки" not in cb.message.text


def test_rebuild_refused_main_does_not_touch_city_tabs(monkeypatch):
    async def route(event_city, participant_type):
        return {"spb": "СПб"}.get(event_city)

    main_calls, named_calls = _wire(monkeypatch, route)

    async def refused(headers, rows):
        return admin_mod.REFUSED_UNPINNED_TAB

    monkeypatch.setattr(admin_settings, "rebuild_main_sheet", refused)
    cb = _FakeCallback(ADMIN_ID)
    asyncio.run(admin_settings.rebuild_sheet(cb))
    assert named_calls == []  # main refused -> nothing else is wiped either
