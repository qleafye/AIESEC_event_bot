"""Phase 09.2 Plan 06 (C, CITY-05) tests: «🔘 Кнопки главного меню» screen migrated onto the
settings registry + its per-city sub-flow («🏙 Кнопки по городу»).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_admin_percity_ui.py / tests/test_settings_groups_c0x.py.

Sections mirror the plan's tasks:
    Task 1 — global screen on the registry (truth table parity), «🏙 Кнопки по городу» entry
        row (module on/off), the menu_city list screen (labels, marks, no codes, visibility),
        and ADMIN_CAPS coverage for the 5 new menu_city* prefixes.
    Task 2 — per-city toggle/clear round-trip, isolation from the global key, unknown-code/
        unknown-key refusal, bound-manager gate, and the delegate-facing consumer round-trip
        through keyboards.builders.get_main_menu_kb.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability, role_caps_key, role_enabled_key
from keyboards.builders import get_main_menu_kb, MENU_BUTTONS
import cities


ADMIN_ID = 920601
MANAGER_ID = 920602
OTHER_MANAGER_ID = 920603
SPB_DELEGATE_ID = 920604
MSK_DELEGATE_ID = 920605


def _admin_ready(tmp_path, db_name="test_admin_percity_menu.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _kb_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: global screen on the registry + «🏙 Кнопки по городу» entry
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_render_menu_text_truth_table_matches_legacy_idiom(tmp_path):
    """Byte-in-byte parity with the old `val is None or val == "on"` idiom for the 5
    reachable raw values -- None/absent, "", "on" -> ✅; "off"/garbage -> ❌."""
    _admin_ready(tmp_path)
    key = "menu_coins"
    label = dict(MENU_BUTTONS)[key]

    # absent -> ✅ (default "on")
    text = asyncio.run(admin_mod.render_menu_text())
    line = [ln for ln in text.splitlines() if label in ln][0]
    assert line.startswith("✅")

    for raw, expect_on in [("on", True), ("off", False), ("junk", False)]:
        asyncio.run(db.set_setting(key, raw))
        text = asyncio.run(admin_mod.render_menu_text())
        line = [ln for ln in text.splitlines() if label in ln][0]
        assert line.startswith("✅" if expect_on else "❌"), f"raw={raw!r}"

    # explicit "" behaves like absent -> ✅ (enum branch: raw if raw else default)
    asyncio.run(db.set_setting(key, ""))
    text = asyncio.run(admin_mod.render_menu_text())
    line = [ln for ln in text.splitlines() if label in ln][0]
    assert line.startswith("✅")


def test_build_menu_keyboard_toggle_marks_match_render_menu_text(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("menu_coins", "off"))
    text = asyncio.run(admin_mod.render_menu_text())
    kb = asyncio.run(admin_mod.build_menu_keyboard())
    texts = _kb_texts(kb)
    label = dict(MENU_BUTTONS)["menu_coins"]
    text_line = [ln for ln in text.splitlines() if label in ln][0]
    kb_line = [t for t in texts if label in t][0]
    assert text_line.split(" ", 1)[0] == kb_line.split(" ", 1)[0]


def test_order_and_labels_and_toggle_callbacks_unchanged(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_menu_keyboard())
    toggle_rows = [row for row in kb.inline_keyboard if row[0].callback_data.startswith("menu_toggle:")]
    assert [row[0].callback_data.split(":", 1)[1] for row in toggle_rows] == [k for k, _ in MENU_BUTTONS]
    for (key, label), row in zip(MENU_BUTTONS, toggle_rows):
        assert label in row[0].text


def test_module_off_keyboard_has_no_per_city_row(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_mod.build_menu_keyboard())
    assert "menu_city" not in _kb_callbacks(kb)
    assert "🏙 Кнопки по городу" not in _kb_texts(kb)


def test_module_on_keyboard_has_per_city_row(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    kb = asyncio.run(admin_mod.build_menu_keyboard())
    assert "menu_city" in _kb_callbacks(kb)
    assert "🏙 Кнопки по городу" in _kb_texts(kb)
    # Still ends with the pre-existing "← Назад к настройкам" tail row.
    assert kb.inline_keyboard[-1][0].callback_data == "menu_back"


def test_menu_city_list_gate_module_off(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("menu_city")
    asyncio.run(admin_mod.menu_city_list(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers[-1][1] is True  # show_alert


def test_menu_city_list_shows_labels_marks_and_no_codes(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting(cities.per_city_key("menu_coins", "spb"), "off"))
    asyncio.run(db.set_setting("city_enabled__tyumen", "off"))

    cb = FakeCallback("menu_city")
    asyncio.run(admin_mod.menu_city_list(cb))
    texts = _kb_texts(cb.message.markup)

    spb_row = [t for t in texts if "Санкт-Петербург" in t][0]
    assert "✅ своё" in spb_row
    msk_row = [t for t in texts if "Москва" in t][0]
    assert "— как везде" in msk_row
    tyumen_row = [t for t in texts if "Тюмень" in t][0]
    assert "❌" in tyumen_row
    # No city codes leak into any label (CLAUDE.md "бот для людей").
    assert not any("spb" in t or "msk" in t or "tyumen" in t for t in texts)


def test_menu_city_list_bound_manager_sees_only_own_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg;moderate_receipts;settings"))

    cb = FakeCallback("menu_city", user_id=MANAGER_ID)
    asyncio.run(admin_mod.menu_city_list(cb))
    callbacks = [c for c in _kb_callbacks(cb.message.markup) if c and c.startswith("menu_city_pick:")]
    assert callbacks == ["menu_city_pick:spb"]


def test_admin_caps_cover_menu_city_prefixes():
    assert all(
        required_capability(callback_data=c) == "settings"
        for c in [
            "menu_city",
            "menu_city_pick:spb",
            "menu_city_toggle:spb:menu_coins",
            "menu_city_clear:spb",
            "menu_city_clear_go:spb",
        ]
    )

