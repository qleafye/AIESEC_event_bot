"""Phase 09.2 Plan 03 (CITY-04, CONTEXT B) — delegate-facing consumers wired to the per-city
resolver: main menu buttons (`get_main_menu_kb`) and the four `user_actions.py` info screens
(«ℹ️ Информация», «📞 Контакты», `info_date`, `info_place`).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as every other phase-9/09.x
test file (tests/test_gamification_delegate_phase9.py, tests/test_question_routing_percity.py).

Task 1: `keyboards/builders.py::get_main_menu_kb` resolves the delegate's city once and reads
    `menu_*` toggles through `cities.get_setting_typed_for_city`.
Task 2: `handlers/user_actions.py::show_contacts`/`show_info_menu`/`info_date`/`info_place`
    resolve the delegate's city once per screen and read their text keys through
    `cities.get_setting_for_city`; `show_program`/`show_speakers` stay untouched (RESEARCH
    Pitfall 1 -- their captions are not SETTINGS_SCHEMA keys).
"""
import asyncio

from config import config
from database import db
from cities import per_city_key
from keyboards.builders import get_main_menu_kb, MENU_BUTTONS

ADMIN_ID = 920901
MSK_DELEGATE_ID = 920902
SPB_DELEGATE_ID = 920903
STRANGER_ID = 920904


def _db_ready(tmp_path, name="test_content_percity_consumers.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


def _add_delegate(uid, event_city=None):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-17 00:00:00",
        "event_city": event_city,
    }))


def _set_override(key, code, value):
    ok_key = per_city_key(key, code)
    assert ok_key is not None
    asyncio.run(db.set_setting(ok_key, value))


def _menu_texts(kb):
    return [btn.text for row in kb.keyboard for btn in row]


# ── Task 1: main menu resolves by delegate city ─────────────────────────────────────────

def test_menu_module_off_byte_identical_for_any_raw_toggle_value(tmp_path):
    """Parametrized-in-a-loop: module OFF -> every REACHABLE raw stored value for a menu
    toggle (unset/None, "on", "off", junk) yields the SAME button set as reading the base key
    directly -- no `__city__` lookup ever happens while the module is off. `""` is a
    documented-but-unreachable case handled by the separate test below (09.2-01-SUMMARY.md:
    the write path never persists an empty string for a toggle)."""
    for raw in (None, "on", "off", "junk"):
        _db_ready(tmp_path, name=f"test_menu_offparity_{raw}.db")
        key = "menu_coins"
        if raw is not None:
            asyncio.run(db.set_setting(key, raw))
        # event_city_enabled left at its default ("off") -- module-off contract.
        kb = asyncio.run(get_main_menu_kb(MSK_DELEGATE_ID))
        texts = _menu_texts(kb)
        has_coins = "🪙 Мои монеты" in texts
        expected = raw is None or raw == "on"
        assert has_coins == expected, f"raw={raw!r}: expected has_coins={expected}, got {has_coins}"


def test_menu_empty_string_toggle_resolves_to_default_on_documented(tmp_path):
    """`""` is never actually written by the toggle handler, but if it were, the registry
    `enum` branch's `raw if raw else default` resolves it to "on" (default) -- this is a
    KNOWN, documented divergence from the pre-registry raw idiom `val is None or val == "on"`
    (which would have hidden the button), already recorded in 09.2-01-SUMMARY.md for the
    resolver's own parse-equivalence test."""
    _db_ready(tmp_path, name="test_menu_empty_string.db")
    asyncio.run(db.set_setting("menu_coins", ""))
    kb = asyncio.run(get_main_menu_kb(MSK_DELEGATE_ID))
    assert "🪙 Мои монеты" in _menu_texts(kb)


def test_menu_no_telegram_id_defaults_to_global(tmp_path):
    """Legacy call shape (no argument) -- old tests in test_gamification_delegate_phase9.py
    must keep passing byte-for-byte: city resolves to None, global values used."""
    _db_ready(tmp_path)
    _enable_cities()
    _set_override("menu_coins", "spb", "off")
    kb = asyncio.run(get_main_menu_kb())
    assert "🪙 Мои монеты" in _menu_texts(kb)


def test_menu_city_override_hides_button_for_that_citys_delegate(tmp_path):
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    _set_override("menu_coins", "spb", "off")

    kb = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    texts = _menu_texts(kb)
    assert "🪙 Мои монеты" not in texts
    # Everything else is still there.
    assert "📞 Контакты" in texts


def test_menu_no_override_falls_back_to_global(tmp_path):
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    # No override written -- global default ("on") applies.
    kb = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    assert "🪙 Мои монеты" in _menu_texts(kb)


def test_menu_other_city_delegate_not_affected_by_override(tmp_path):
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(MSK_DELEGATE_ID, event_city="msk")
    _set_override("menu_coins", "spb", "off")
    kb = asyncio.run(get_main_menu_kb(MSK_DELEGATE_ID))
    assert "🪙 Мои монеты" in _menu_texts(kb)


def test_menu_unknown_user_defaults_without_exception(tmp_path):
    """get_user -> None (never-registered id) must not raise; falls back to default city
    (normalize_city(None))."""
    _db_ready(tmp_path)
    _enable_cities()
    kb = asyncio.run(get_main_menu_kb(STRANGER_ID))
    assert isinstance(_menu_texts(kb), list)  # did not raise
    assert "🪙 Мои монеты" in _menu_texts(kb)


def test_menu_city_resolve_failure_falls_back_to_global(tmp_path, monkeypatch):
    """A raised exception during city resolution must not break the menu -- buttons matter
    more than the city (per plan Task 1 behavior)."""
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    _set_override("menu_coins", "spb", "off")

    import keyboards.builders as builders_mod

    async def _boom(_telegram_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(builders_mod, "get_user", _boom)
    kb = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    # code falls back to None -> global value ("on") -- override is NOT applied.
    assert "🪙 Мои монеты" in _menu_texts(kb)


def test_menu_order_and_adjust_unchanged(tmp_path):
    _db_ready(tmp_path)
    kb = asyncio.run(get_main_menu_kb())
    texts = _menu_texts(kb)
    expected_order = [label for _key, label in MENU_BUTTONS]
    assert texts == expected_order
