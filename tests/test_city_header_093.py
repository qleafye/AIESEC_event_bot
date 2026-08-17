"""Phase 09.3 Plan 03 (CITY-08) tests: header «Все города» mode + ANY_CAPABILITY widen (Task
1), «Одобрить все» in ALL_CITIES mode (Task 2), task-wizard city prefill (Task 3).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_manager_city_091.py / tests/test_roles_phase8.py / tests/test_city_admin_phase72.py.
"""
import asyncio

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton

from config import config
from database import db
import cities
from handlers import admin as admin_mod
from handlers.admin_caps import ADMIN_CAPS, ANY_CAPABILITY, required_capability, role_caps_key


ADMIN_ID = 930301
MANAGER_ID = 930302
STAFF_ID = 930303


def _admin_ready(tmp_path, *, db_name="test_city_header_093.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.set_setting("event_city_enabled", "on"))


# ── FakeCallback/FakeMessage idiom, matching tests/test_manager_city_091.py verbatim ────────

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
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
        # appr_all_yes's roundtrip-mismatch branch re-renders the card via _show_current_card
        # (target.answer, not edit_text) -- same dual-purpose FakeMessage shape as
        # tests/test_city_admin_phase72.py.
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)


class FakeCallback:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage()
        self.bot = None  # appr_all_yes reads callback.bot for the background welcome drain
        self.answered_texts = []
        self.answered_alerts = []

    async def answer(self, text=None, show_alert=False):
        self.answered_texts.append(text)
        self.answered_alerts.append(show_alert)


def _first_row_button(cb) -> InlineKeyboardButton:
    return cb.message.markup.inline_keyboard[0][0]


# ── Screen: «🌍 Все города» first row (behavior bullets 1-4) ────────────────────────────────

def test_admin_city_switch_all_cities_row_first_for_superadmin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("admin_city_switch", ADMIN_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    btn = _first_row_button(cb)
    assert btn.text == cities.ALL_CITIES_LABEL
    assert btn.callback_data == f"admin_city_pick:{cities.ALL_CITIES}"


def test_admin_city_switch_all_cities_row_checked_when_current_is_all(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback("admin_city_switch", ADMIN_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    rows = cb.message.markup.inline_keyboard
    assert rows[0][0].text == f"✅ {cities.ALL_CITIES_LABEL}"
    # ни одна городская кнопка не отмечена (последняя строка -- «◀️ Назад», её тоже пропускаем)
    for row in rows[1:-1]:
        assert not row[0].text.startswith("✅ ")


def test_admin_city_switch_all_cities_row_for_unbound_manager(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    cb = FakeCallback("admin_city_switch", MANAGER_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    btn = _first_row_button(cb)
    assert btn.text == cities.ALL_CITIES_LABEL


def test_admin_city_switch_bound_manager_never_sees_all_cities_option(tmp_path):
    """09.1's lock (unchanged) means the picker is never built at all for a bound manager --
    the «Все города» row can't appear in any form. Extends the 3 existing 091 lock tests with
    the explicit «all cities» claim."""
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))
    cb = FakeCallback("admin_city_switch", MANAGER_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    assert cb.message.edit_calls == 0
    assert all(cities.ALL_CITIES_LABEL not in (t or "") for t in cb.answered_texts)


def test_admin_city_switch_text_matches_context_a_no_old_phrases(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("admin_city_switch", ADMIN_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    text = cb.message.text
    assert "Город админки" in text
    assert "НЕ фильтруется" not in text
    assert "Фильтруется" not in text
    assert "🌍 Все города" in text


# ── admin_city_pick:* ────────────────────────────────────────────────────────────────────

def test_admin_city_pick_all_cities_for_superadmin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback(f"admin_city_pick:{cities.ALL_CITIES}", ADMIN_ID)
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert cb.answered_texts[-1] == f"Город: {cities.ALL_CITIES_LABEL}"
    assert cb.message.edit_calls == 1
    assert asyncio.run(cities.admin_selected_city(ADMIN_ID)) == cities.ALL_CITIES


def test_admin_city_pick_all_cities_rejected_for_bound_manager_forged_callback(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))
    cb = FakeCallback(f"admin_city_pick:{cities.ALL_CITIES}", MANAGER_ID)
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert "Неизвестный город" in (cb.answered_texts[-1] or "")
    assert asyncio.run(cities.admin_selected_city(MANAGER_ID)) == "spb"


# ── Task 2 (T-093-10/T-093-11): «Одобрить все» in ALL_CITIES mode ──────────────────────────

def _seed_pending(tid, event_city, status="pending"):
    asyncio.run(db.add_user({
        "telegram_id": tid,
        "full_name": f"User {tid}",
        "registration_date": f"2026-01-01 09:{tid:02d}:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(tid, status))


def _seed_three_pending_two_cities(tmp_path):
    _admin_ready(tmp_path)
    _seed_pending(1, "spb")
    _seed_pending(2, "spb")
    _seed_pending(3, "msk")


def test_appr_all_confirm_all_cities_names_scope_and_count(tmp_path):
    _seed_three_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback("appr_all", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert "по всем городам" in cb.message.text
    assert "3" in cb.message.text
    flat = [b.callback_data for row in cb.message.markup.inline_keyboard for b in row]
    assert f"appr_all_yes:{cities.ALL_CITIES}" in flat


def test_appr_all_confirm_real_city_unaffected_by_the_new_branch(tmp_path):
    """Регресс: третья ветка (ALL_CITIES) не задевает вторую -- реальный город остаётся
    байт-в-байт тем же текстом, что и до этого плана."""
    _seed_three_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert "по всем городам" not in cb.message.text
    assert "Заявки других городов не будут затронуты." in cb.message.text


def test_appr_all_yes_all_cities_approves_both_cities(tmp_path):
    _seed_three_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback(f"appr_all_yes:{cities.ALL_CITIES}", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(cb, state))

    async def check():
        for tid in (1, 2, 3):
            user = await db.get_user(tid)
            assert user["status"] == "approved", tid

    asyncio.run(check())


def test_appr_all_yes_context_mismatch_confirmed_all_but_header_now_city(tmp_path):
    _seed_three_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback("appr_all", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))  # шапка переключена уже после диалога
    stale = FakeCallback(f"appr_all_yes:{cities.ALL_CITIES}", ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(stale, state))
    assert stale.answered_alerts and stale.answered_alerts[-1] is True
    assert "изменил" in (stale.answered_texts[-1] or "")

    async def check():
        for tid in (1, 2, 3):
            user = await db.get_user(tid)
            assert user["status"] == "pending", tid

    asyncio.run(check())


def test_appr_all_yes_context_mismatch_confirmed_city_but_header_now_all(tmp_path):
    _seed_three_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))  # симметричный случай
    stale = FakeCallback("appr_all_yes:spb", ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(stale, state))
    assert stale.answered_alerts and stale.answered_alerts[-1] is True
    assert "изменил" in (stale.answered_texts[-1] or "")

    async def check():
        for tid in (1, 2, 3):
            user = await db.get_user(tid)
            assert user["status"] == "pending", tid

    asyncio.run(check())


def test_appr_all_yes_forged_unknown_code_still_rejected_when_header_is_all_cities(tmp_path):
    """Behavior bullet: a forged `appr_all_yes:<нет_такого_кода>` is still rejected when the
    header is ALL_CITIES. It hits the SAME roundtrip-mismatch branch a forged code always hits
    (confirmed="__evil__" != current="*", exactly parallel to
    tests/test_city_admin_phase72.py::test_appr_all_yes_refuses_forged_city_code) -- the
    membership guard's own ALL_CITIES exception (Pitfall 1) is a distinct, narrower claim,
    covered directly by the two context-mismatch tests above (confirmed==current=="*" with a
    real `city_codes()` value on the other side)."""
    _seed_three_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = FakeCallback("appr_all_yes:__evil__", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(cb, state))
    assert cb.answered_alerts and cb.answered_alerts[-1] is True

    async def check():
        for tid in (1, 2, 3):
            user = await db.get_user(tid)
            assert user["status"] == "pending", tid

    asyncio.run(check())


# ── admin_keyboard_for header label (behavior bullet: button caption) ───────────────────────

def test_admin_keyboard_for_header_label_all_cities_mode(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    kb = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    header = kb.inline_keyboard[0][0]
    assert header.text == cities.ALL_CITIES_LABEL
    assert header.callback_data == "admin_city_switch"


def test_admin_keyboard_for_header_label_real_city_unchanged(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    kb = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    header = kb.inline_keyboard[0][0]
    label = asyncio.run(cities.city_label("spb"))
    assert header.text == f"🏙 Город: {label}"


def test_admin_keyboard_for_no_header_when_module_off(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_header_093_off.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    # event_city_enabled намеренно не включён -- дефолт "off"
    kb = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    assert all(row[0].callback_data != "admin_city_switch" for row in kb.inline_keyboard)


# ── ADMIN_CAPS widen (Pitfall 2 / T-093-08) ─────────────────────────────────────────────────

def test_admin_caps_city_switcher_widened_to_any_capability():
    assert ADMIN_CAPS["admin_city_switch"] == ANY_CAPABILITY
    assert ADMIN_CAPS["admin_city_pick:*"] == ANY_CAPABILITY
    assert required_capability(callback_data="admin_city_switch") == ANY_CAPABILITY
    assert required_capability(callback_data="admin_city_pick:spb") == ANY_CAPABILITY


# ── Rights matrix through the REAL CapabilityMiddleware (idiom: tests/test_roles_phase8.py) ─

class _RolesFakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = None


class _RolesFakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.answers = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class _RolesFakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _RolesFakeUser(user_id)
        self.message = _RolesFakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _RolesFakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _fresh_state(user_id):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _dispatch(data, user_id):
    """Drives a real CallbackQuery through `admin_mod.router.propagate_event`, same shape as
    tests/test_roles_phase8.py::dispatch_callback -- duplicated locally (same convention this
    codebase's test suite already follows for its Fake* harnesses per file)."""
    bot = _RolesFakeBot()
    state = _fresh_state(user_id)
    event = _RolesFakeCallback(data, user_id)
    kwargs = dict(
        event_from_user=_RolesFakeUser(user_id),
        bot=bot,
        raw_state=None,
        state=state,
        event_update=None,
    )
    result = asyncio.run(admin_mod.router.propagate_event("callback_query", event, **kwargs))
    return result, event


def test_settings_only_manager_reaches_admin_city_switch(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(STAFF_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "settings"))

    result, event = _dispatch("admin_city_switch", STAFF_ID)

    assert result is not UNHANDLED
    assert ("Недостаточно прав", True) not in event.answers


def test_settings_only_manager_reaches_admin_city_pick(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(STAFF_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "settings"))

    result, event = _dispatch("admin_city_pick:spb", STAFF_ID)

    assert result is not UNHANDLED
    assert ("Недостаточно прав", True) not in event.answers
    assert asyncio.run(cities.admin_selected_city(STAFF_ID)) == "spb"


def test_manager_with_zero_capability_denied_admin_city_switch(tmp_path):
    """T-08-14/D-04: zero effective capabilities is treated like the "stranger" branch of
    `_deny` (silent, UNHANDLED, no toast -- the admin surface's shape is never revealed) --
    NOT the "known staff, wrong capability" alert branch, which requires a non-empty
    `user_caps`. Deny-by-default (T-093-08's mitigation) holds either way: the handler body
    never runs."""
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(STAFF_ID, "game_manager", ADMIN_ID))
    # "не настоящее право" -- фильтруется resolve_capabilities'ом (T-08-02), сотрудник
    # реально держит роль, но эффективных прав у него ноль.
    asyncio.run(db.set_setting(role_caps_key("game_manager"), "not_a_real_capability"))

    result, event = _dispatch("admin_city_switch", STAFF_ID)

    assert result is UNHANDLED
    assert event.answers == []


def test_manager_with_zero_capability_denied_admin_city_pick(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(STAFF_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_setting(role_caps_key("game_manager"), "not_a_real_capability"))

    result, event = _dispatch("admin_city_pick:spb", STAFF_ID)

    assert result is UNHANDLED
    assert event.answers == []
    # deny happened BEFORE the handler body -- no write to bot_settings for this admin.
    assert asyncio.run(db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{STAFF_ID}")) is None
