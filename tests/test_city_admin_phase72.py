"""Phase 07.2 Plan 02 (per-city admin panels, CITY-02) tests: the per-admin city switcher
(`admin_keyboard_for`, `admin_city_switch`, `admin_city_pick`) and both moderation queues
(«Заявки» / «Чеки») scoped by the selected city, including the safe mass-approve
confirmation text.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_admin_phase71.py / tests/test_city_scope_phase72.py.
"""
import asyncio
import inspect

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
import cities


ADMIN_ID = 920101
NON_ADMIN_ID = 920102


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_admin72.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    """Stand-in for the aiogram Message the callback/target carries — captures both
    edit_text (callback re-render) and answer (new-message target) calls."""

    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.answers_sent = []
        self.answer_markups = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.bot = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _seed_city(telegram_id, event_city, status="pending", payment_status=None, full_name=None):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": full_name or f"User {telegram_id}",
        "registration_date": f"2026-01-01 09:{telegram_id:02d}:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))
    if payment_status is not None:
        asyncio.run(db.update_payment_status(telegram_id, payment_status))


# ── Task 1: city switcher — admin_keyboard_for() + admin_city_switch / admin_city_pick ──

def test_admin_keyboard_for_module_off_equals_build_admin_keyboard(tmp_path):
    _admin_ready(tmp_path)
    scoped = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    plain = admin_mod.build_admin_keyboard()
    assert len(scoped.inline_keyboard) == len(plain.inline_keyboard)
    for row_a, row_b in zip(scoped.inline_keyboard, plain.inline_keyboard):
        assert [b.text for b in row_a] == [b.text for b in row_b]
        assert [b.callback_data for b in row_a] == [b.callback_data for b in row_b]


def test_admin_keyboard_for_module_on_has_city_header_row(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    kb = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    plain = admin_mod.build_admin_keyboard()
    assert len(kb.inline_keyboard) == len(plain.inline_keyboard) + 1
    header_row = kb.inline_keyboard[0]
    assert len(header_row) == 1
    assert header_row[0].callback_data == "admin_city_switch"
    assert "🏙 Город:" in header_row[0].text
    # remaining rows unchanged
    assert [ [b.callback_data for b in row] for row in kb.inline_keyboard[1:] ] == \
           [ [b.callback_data for b in row] for row in plain.inline_keyboard ]


def test_admin_city_switch_screen_lists_all_cities_and_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("admin_city_switch", user_id=NON_ADMIN_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    assert cb.answers[-1] == ("Недостаточно прав", True)
    assert cb.message.edit_calls == 0

    cb2 = FakeCallback("admin_city_switch")
    asyncio.run(admin_mod.admin_city_switch(cb2))
    assert cb2.message.edit_calls == 1
    flat = _flat_callback_data(cb2.message.markup)
    for code in cities.city_codes():
        assert f"admin_city_pick:{code}" in flat
    assert "admin_menu" in flat


def test_admin_city_pick_valid_code_sets_and_rerenders(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("admin_city_pick:spb")
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert asyncio.run(db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}")) == "spb"
    assert asyncio.run(cities.admin_selected_city(ADMIN_ID)) == "spb"
    assert cb.message.edit_calls == 1
    assert "Панель администратора" in cb.message.text
    header_row = cb.message.markup.inline_keyboard[0]
    assert header_row[0].callback_data == "admin_city_switch"


def test_admin_city_pick_unknown_code_rejected_no_write(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("admin_city_pick:__evil__")
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert cb.answers[-1] == ("Неизвестный город", True)
    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}")) is None


def test_admin_city_pick_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("admin_city_pick:spb", user_id=NON_ADMIN_ID)
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert cb.answers[-1] == ("Недостаточно прав", True)
    assert asyncio.run(db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{NON_ADMIN_ID}")) is None


# ── Task 2: applications queue city-scoped + safe mass-approve ──────────────────────────

def _seed_pending_three_cities(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, None)
    _seed_city(2, "msk")
    _seed_city(3, "spb")
    _seed_city(4, "spb")
    _seed_city(5, "tyumen")


def test_show_current_card_city_scoped_spb(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "1/2" in target.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in target.text


def test_show_current_card_city_scoped_msk_includes_null(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "1/2" in target.text  # NULL + msk


def test_render_application_card_header_byte_identical_without_city_label():
    user = {"telegram_id": 1, "full_name": "X"}
    text = admin_mod._render_application_card(user, 1, 1)
    assert text.startswith("📋 <b>Заявка 1/1</b>")
    assert "🏙" not in text.split("\n")[0]


def test_render_application_card_header_has_city_when_label_given():
    user = {"telegram_id": 1, "full_name": "X"}
    text = admin_mod._render_application_card(user, 1, 1, city_label_text="СПб")
    first_line = text.split("\n")[0]
    assert first_line == "📋 <b>Заявка 1/1</b> · 🏙 СПб"


def test_show_current_card_module_off_header_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert target.text.startswith("📋 <b>Заявка 1/1</b>\n")
    assert "🏙" not in target.text.split("\n")[0]


def test_show_current_card_empty_queue_names_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, "msk", status="approved")  # no pending rows at all
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "Заявок нет" in target.answers_sent[-1]
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in target.answers_sent[-1]


def test_show_current_card_empty_queue_module_off_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert target.answers_sent[-1] == "✅ Заявок нет."


def test_appr_all_confirm_names_city_and_count(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert "2" in cb.message.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text


def test_appr_all_confirm_module_off_equals_todays_literal(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert cb.message.text == "Одобрить все 1 заявок?"


def test_appr_all_yes_scoped_does_not_touch_other_cities(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all_yes")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(cb, state))

    async def check():
        for tid in (1, 2, 5):
            user = await db.get_user(tid)
            assert user["status"] == "pending"
        for tid in (3, 4):
            user = await db.get_user(tid)
            assert user["status"] == "approved"

    asyncio.run(check())
