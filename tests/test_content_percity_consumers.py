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

Phase 09.2 Plan 04 (CITY-04, CONTEXT B) — the registration-path texts + registration_mode,
appended below (Plan 03's file per the plan's own file-scope, `tests/test_reg_percity_texts.py`
was folded into this consumers file instead of a new file — orchestrator instruction, keeps
"one consumers test file" as the single source for decision-B coverage):

Task 1 (below, "Plan 04 Task 1"): `handlers/registration.py::cmd_start` resolves
    `start_text`/`start_text_registered` by the delegate's own city; `finalize_registration`
    resolves `reg_complete_text` the same way.
Task 2 (below, "Plan 04 Task 2"): `handlers/registration.py::_approve_text_for` grows an
    optional `city_code` param -- `approve_text__party` stays global, only the base
    `approve_text` composes with a city override; `send_completion_and_bonus` resolves the
    delegate's city once.
Task 3 (below, "Plan 04 Task 3"): `handlers/registration.py::_resolve_track` grows an
    optional `city_code` param for `registration_mode`; `_should_show_city_fork` is
    unchanged and its independence from `registration_mode` is locked by a regression test
    (RESEARCH Pitfall 2).
"""
import asyncio
import inspect

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from cities import per_city_key
from handlers import user_actions as ua_mod
from handlers import registration as reg_mod
from handlers import reg_schema as reg_schema_mod
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


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, user_id):
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.answers = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append((text, reply_markup, parse_mode))


class FakeCallback:
    def __init__(self, user_id):
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id)
        self.answered = False

    async def answer(self, *a, **kw):
        self.answered = True


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


# ── Task 2: contacts / info screens resolve by delegate city ───────────────────────────

def test_screens_module_off_byte_identical(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    asyncio.run(db.set_setting("contact_person", "@global_manager"))
    asyncio.run(db.set_setting("event_date", "31 октября"))
    asyncio.run(db.set_setting("event_place_name", "Глобальная площадка"))
    # event_city_enabled left OFF.

    message = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.show_contacts(message))
    text = message.answers[0][0]
    assert "@global_manager" in text

    message2 = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.show_info_menu(message2))
    text2 = message2.answers[0][0]
    assert "Глобальная площадка" in text2


def test_show_contacts_city_override(tmp_path):
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    _add_delegate(MSK_DELEGATE_ID, event_city="msk")
    asyncio.run(db.set_setting("contact_person", "@global_manager"))
    _set_override("contact_person", "spb", "@spb_manager")

    spb_msg = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.show_contacts(spb_msg))
    assert "@spb_manager" in spb_msg.answers[0][0]

    msk_msg = FakeMessage(MSK_DELEGATE_ID)
    asyncio.run(ua_mod.show_contacts(msk_msg))
    assert "@global_manager" in msk_msg.answers[0][0]
    assert "@spb_manager" not in msk_msg.answers[0][0]


def test_show_info_menu_and_info_place_city_override(tmp_path):
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    asyncio.run(db.set_setting("event_date", "31 октября"))
    asyncio.run(db.set_setting("event_place_name", "Глобальная площадка"))
    _set_override("event_place_name", "spb", "Питерская площадка")

    msg = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.show_info_menu(msg))
    assert "Питерская площадка" in msg.answers[0][0]

    cb = FakeCallback(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.info_place(cb))
    assert "Питерская площадка" in cb.message.answers[0][0]


def test_info_date_resolves_same_city_as_info_menu(tmp_path):
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    asyncio.run(db.set_setting("event_date", "31 октября"))
    _set_override("event_date", "spb", "3 октября")

    cb = FakeCallback(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.info_date(cb))
    assert "3 октября" in cb.message.answers[0][0]


def test_venue_photo_and_program_speakers_stay_global(tmp_path):
    """venue_photo_file_id is out of the per-city mechanism (RESEARCH: photo captions are not
    registry keys); program/speakers must not even reference the resolver."""
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    # No override anywhere for venue_photo_file_id -- resolver is never expected to touch it.
    src_program = inspect.getsource(ua_mod.show_program)
    src_speakers = inspect.getsource(ua_mod.show_speakers)
    assert "get_setting_for_city" not in src_program
    assert "get_setting_for_city" not in src_speakers


def test_each_screen_resolves_city_exactly_once():
    """Structural: show_contacts calls the shared _delegate_city helper exactly once."""
    src = inspect.getsource(ua_mod.show_contacts)
    assert src.count("_delegate_city") == 1


def test_consumer_screens_use_resolver():
    for fn in (ua_mod.show_contacts, ua_mod.show_info_menu, ua_mod.info_date, ua_mod.info_place):
        assert "get_setting_for_city" in inspect.getsource(fn)


def test_city_resolve_failure_falls_back_to_global_for_contacts(tmp_path, monkeypatch):
    """Fault is injected INSIDE the resolve (get_user raises), not by bypassing
    _delegate_city's own try/except -- exercises the real fail-soft path show_contacts
    relies on."""
    _db_ready(tmp_path)
    _enable_cities()
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    asyncio.run(db.set_setting("contact_person", "@global_manager"))
    _set_override("contact_person", "spb", "@spb_manager")

    def _boom(_code):
        raise RuntimeError("boom")

    # normalize_city runs INSIDE _delegate_city, after get_user (which ensure_registered
    # also needs unpatched) -- faulting here isolates the resolve step without breaking the
    # registration gate.
    monkeypatch.setattr(ua_mod, "normalize_city", _boom)
    msg = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.show_contacts(msg))
    assert "@global_manager" in msg.answers[0][0]
    assert "@spb_manager" not in msg.answers[0][0]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Phase 09.2 Plan 04 (CITY-04) — registration.py: welcome/complete/approve texts + form mode
# ═══════════════════════════════════════════════════════════════════════════════════════════

REG_MSK_ID = 920910
REG_SPB_ID = 920911
REG_NEWCOMER_ID = 920912
REG_NEWCOMER2_ID = 920913
REG_RETURNING_NO_CITY_ID = 920914


class _RegUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "testuser"
        self.full_name = "Test User"


class _RegChat:
    def __init__(self, cid):
        self.id = cid


class _RegMessage:
    """Copied from tests/test_uat1708_hotfixes.py::_KBCapturingMessage — captures every
    (text, reply_markup) sent via .answer()/.answer_photo()."""

    def __init__(self, text, uid):
        self.text = text
        self.from_user = _RegUser(uid)
        self.chat = _RegChat(uid)
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((caption, reply_markup))


class RegFakeCommand:
    def __init__(self, args=None):
        self.args = args


def _new_reg_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _reg_register_delegate(uid, event_city=None, status="approved"):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-17 00:00:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(uid, status))


# ── Plan 04 Task 1: welcome/registered/reg_complete texts resolve by delegate city ─────────

def test_registered_delegate_start_text_registered_city_override(tmp_path):
    _db_ready(tmp_path, name="test_reg04_registered_override.db")
    _enable_cities()
    _reg_register_delegate(REG_SPB_ID, event_city="spb")
    _reg_register_delegate(MSK_DELEGATE_ID, event_city="msk")
    asyncio.run(db.set_setting("start_text_registered", "Общий текст для вернувшихся"))
    _set_override("start_text_registered", "spb", "СПб текст для вернувшихся")

    spb_msg = _RegMessage("/start", REG_SPB_ID)
    asyncio.run(reg_mod.cmd_start(spb_msg, _new_reg_state(REG_SPB_ID), bot=object(), command=None))
    assert spb_msg.sent[0][0] == "СПб текст для вернувшихся"

    msk_msg = _RegMessage("/start", MSK_DELEGATE_ID)
    asyncio.run(reg_mod.cmd_start(msk_msg, _new_reg_state(MSK_DELEGATE_ID), bot=object(), command=None))
    assert msk_msg.sent[0][0] == "Общий текст для вернувшихся"


def test_registered_delegate_without_event_city_defaults_without_crash(tmp_path):
    _db_ready(tmp_path, name="test_reg04_registered_no_city.db")
    _enable_cities()
    _reg_register_delegate(REG_RETURNING_NO_CITY_ID, event_city=None)
    asyncio.run(db.set_setting("start_text_registered", "Общий текст для вернувшихся"))

    msg = _RegMessage("/start", REG_RETURNING_NO_CITY_ID)
    asyncio.run(reg_mod.cmd_start(msg, _new_reg_state(REG_RETURNING_NO_CITY_ID), bot=object(), command=None))
    assert msg.sent  # did not raise
    assert msg.sent[0][0] == "Общий текст для вернувшихся"


def test_newcomer_unknown_city_gets_global_start_text(tmp_path):
    """CONTEXT A: before the city fork screen is even reached, an unresolved city must NOT
    invent a per-city text -- the global start_text is the correct answer."""
    _db_ready(tmp_path, name="test_reg04_newcomer_unknown_city.db")
    _enable_cities()
    asyncio.run(db.set_setting("start_text", "Общий текст новичку"))
    _set_override("start_text", "spb", "СПб текст новичку")

    msg = _RegMessage("/start", REG_NEWCOMER_ID)
    asyncio.run(reg_mod.cmd_start(msg, _new_reg_state(REG_NEWCOMER_ID), bot=object(), command=None))
    assert msg.sent[0][0] == "Общий текст новичку"


def test_newcomer_deep_link_city_gets_city_start_text(tmp_path):
    _db_ready(tmp_path, name="test_reg04_newcomer_deep_link.db")
    _enable_cities()
    asyncio.run(db.set_setting("start_text", "Общий текст новичку"))
    _set_override("start_text", "spb", "СПб текст новичку")

    msg = _RegMessage("/start", REG_NEWCOMER_ID)
    asyncio.run(reg_mod.cmd_start(msg, _new_reg_state(REG_NEWCOMER_ID), bot=object(), command=RegFakeCommand("city_spb")))
    assert msg.sent[0][0] == "СПб текст новичку"


def test_newcomer_recovered_city_gets_city_start_text(tmp_path):
    """Second /start (bare, no deep-link) after a first /start already recorded the city
    into reg_started -- the recovered city must drive the welcome text, same as a fresh
    deep-link (RESEARCH: recovered_city and dl_event_city are equally authoritative)."""
    _db_ready(tmp_path, name="test_reg04_newcomer_recovered.db")
    _enable_cities()
    asyncio.run(db.set_setting("start_text", "Общий текст новичку"))
    _set_override("start_text", "spb", "СПб текст новичку")
    asyncio.run(db.mark_reg_started(REG_NEWCOMER2_ID, "someuser", None, "spb"))

    msg = _RegMessage("/start", REG_NEWCOMER2_ID)
    asyncio.run(reg_mod.cmd_start(msg, _new_reg_state(REG_NEWCOMER2_ID), bot=object(), command=None))
    assert msg.sent[0][0] == "СПб текст новичку"


def test_percity_welcome_texts_module_off_parity(tmp_path):
    """Module OFF: overrides written for both keys must be invisible -- registered AND
    newcomer branches both fall back to the plain global reads, byte-identical to today."""
    _db_ready(tmp_path, name="test_reg04_offparity.db")
    # event_city_enabled left at its default OFF.
    _reg_register_delegate(REG_SPB_ID, event_city="spb")
    asyncio.run(db.set_setting("start_text_registered", "Общий текст для вернувшихся"))
    _set_override("start_text_registered", "spb", "СПб текст для вернувшихся")
    asyncio.run(db.set_setting("start_text", "Общий текст новичку"))
    _set_override("start_text", "spb", "СПб текст новичку")

    reg_msg = _RegMessage("/start", REG_SPB_ID)
    asyncio.run(reg_mod.cmd_start(reg_msg, _new_reg_state(REG_SPB_ID), bot=object(), command=None))
    assert reg_msg.sent[0][0] == "Общий текст для вернувшихся"

    new_msg = _RegMessage("/start", REG_NEWCOMER_ID)
    asyncio.run(reg_mod.cmd_start(new_msg, _new_reg_state(REG_NEWCOMER_ID), bot=object(), command=RegFakeCommand("city_spb")))
    assert new_msg.sent[0][0] == "Общий текст новичку"


def test_start_texts_fall_back_to_default_constants_when_unset(tmp_path):
    _db_ready(tmp_path, name="test_reg04_defaults.db")
    _enable_cities()
    msg = _RegMessage("/start", REG_NEWCOMER_ID)
    asyncio.run(reg_mod.cmd_start(msg, _new_reg_state(REG_NEWCOMER_ID), bot=object(), command=None))
    assert msg.sent[0][0] == reg_mod.DEFAULT_START_TEXT


class _RegFSMFakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _RegFSMFakeChat:
    def __init__(self, cid):
        self.id = cid


class _RegFinalizeMessage:
    def __init__(self, uid):
        self.from_user = _RegFSMFakeUser(uid, "test")
        self.chat = _RegFSMFakeChat(uid)
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class _RegFinalizeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _finalize_with_city(uid, event_city, monkeypatch, reg_complete_override=None):
    async def _fake_row(data):
        return []

    async def _fake_append(row):
        return None

    monkeypatch.setattr(reg_mod, "_sheet_dispatch", lambda *_a, **_kw: (_fake_row, _fake_append))
    monkeypatch.setattr(reg_mod, "_spawn", lambda coro: coro.close())

    bot = _RegFinalizeBot()
    state = _new_reg_state(uid)
    data = {"full_name": "Тест Тестов", "username": "@test", "participant_type": "full"}
    if event_city is not None:
        data["event_city"] = event_city
    asyncio.run(state.update_data(**data))
    message = _RegFinalizeMessage(uid)
    asyncio.run(reg_mod.finalize_registration(message, state, bot))
    return message


def test_reg_complete_text_resolves_by_delegate_city(tmp_path, monkeypatch):
    _db_ready(tmp_path, name="test_reg04_complete_override.db")
    _enable_cities()
    asyncio.run(db.set_setting("start_text", "irrelevant"))
    asyncio.run(db.set_setting("reg_complete_text", "Общий текст «заявка принята»"))
    _set_override("reg_complete_text", "spb", "СПб текст «заявка принята»")

    message = _finalize_with_city(920920, "spb", monkeypatch)
    assert message.answers[0][0] == "СПб текст «заявка принята»"


def test_reg_complete_text_no_override_falls_back_to_global(tmp_path, monkeypatch):
    _db_ready(tmp_path, name="test_reg04_complete_no_override.db")
    _enable_cities()
    asyncio.run(db.set_setting("reg_complete_text", "Общий текст «заявка принята»"))
    # No override written for msk.
    message = _finalize_with_city(920921, "msk", monkeypatch)
    assert message.answers[0][0] == "Общий текст «заявка принята»"


def test_reg_complete_text_module_off_parity(tmp_path, monkeypatch):
    _db_ready(tmp_path, name="test_reg04_complete_offparity.db")
    # event_city_enabled left OFF.
    asyncio.run(db.set_setting("reg_complete_text", "Общий текст «заявка принята»"))
    _set_override("reg_complete_text", "spb", "СПб текст «заявка принята»")
    message = _finalize_with_city(920922, "spb", monkeypatch)
    assert message.answers[0][0] == "Общий текст «заявка принята»"


def test_registration_texts_use_resolver():
    assert "get_setting_for_city" in inspect.getsource(reg_mod.cmd_start)
    assert 'get_setting_for_city("reg_complete_text"' in inspect.getsource(reg_mod.finalize_registration)


# ── Plan 04 Task 2: approve text — party stays global, base composes with city ─────────────

def test_approve_text_for_default_city_code_none_behaves_as_today(tmp_path):
    _db_ready(tmp_path, name="test_reg04_approve_default.db")
    asyncio.run(db.set_setting("approve_text", "Глобальный текст одобрения"))
    text = asyncio.run(reg_mod._approve_text_for("full"))
    assert text == "Глобальный текст одобрения"


def test_approve_text_party_track_ignores_city_override(tmp_path):
    _db_ready(tmp_path, name="test_reg04_approve_party.db")
    _enable_cities()
    asyncio.run(db.set_setting("approve_text__party", "Партийный текст"))
    asyncio.run(db.set_setting("approve_text", "Глобальный текст одобрения"))
    _set_override("approve_text", "spb", "СПб текст одобрения")

    text = asyncio.run(reg_mod._approve_text_for("party_overnight", "spb"))
    assert text == "Партийный текст"


def test_approve_text_non_party_city_override_applies(tmp_path):
    _db_ready(tmp_path, name="test_reg04_approve_nonparty_spb.db")
    _enable_cities()
    asyncio.run(db.set_setting("approve_text", "Глобальный текст одобрения"))
    _set_override("approve_text", "spb", "СПб текст одобрения")

    assert asyncio.run(reg_mod._approve_text_for("full", "spb")) == "СПб текст одобрения"
    assert asyncio.run(reg_mod._approve_text_for("full", "msk")) == "Глобальный текст одобрения"


def test_approve_text_party_track_no_party_override_still_ignores_city():
    """Even with the party override absent (falls through to the global approve_text per
    D-15), a party track must NOT pick up a city override -- CONTEXT B/RESEARCH Open Q2:
    the city axis composes ONLY with the base approve_text, not the party-track branch."""
    text = asyncio.run(reg_mod._approve_text_for("party_overnight", "spb"))
    assert text  # non-empty, did not raise


def test_send_completion_and_bonus_module_off_no_get_user_call(tmp_path, monkeypatch):
    _db_ready(tmp_path, name="test_reg04_completion_offparity.db")
    # event_city_enabled left OFF.
    asyncio.run(db.set_setting("approve_text", "Готово!"))
    calls = []
    orig_get_user = reg_schema_mod.get_user

    async def _counting_get_user(uid):
        calls.append(uid)
        return await orig_get_user(uid)

    # 13-02 (REFAC-02): patch where send_completion_and_bonus actually resolves get_user
    # (handlers/reg_schema.py), same reasoning as the city-resolve-failure test below.
    monkeypatch.setattr(reg_schema_mod, "get_user", _counting_get_user)
    bot = _RegFinalizeBot()
    asyncio.run(reg_mod.send_completion_and_bonus(bot, 920930, with_menu=False, participant_type="full"))
    assert calls == []
    assert bot.sent and bot.sent[0][1] == "Готово!"


def test_send_completion_and_bonus_resolves_city_and_applies_override(tmp_path, monkeypatch):
    _db_ready(tmp_path, name="test_reg04_completion_city.db")
    _enable_cities()
    _reg_register_delegate(920931, event_city="spb")
    asyncio.run(db.set_setting("approve_text", "Глобальный текст одобрения"))
    _set_override("approve_text", "spb", "СПб текст одобрения")

    bot = _RegFinalizeBot()
    asyncio.run(reg_mod.send_completion_and_bonus(bot, 920931, with_menu=False, participant_type="full"))
    assert bot.sent and bot.sent[0][1] == "СПб текст одобрения"


def test_send_completion_and_bonus_city_resolve_failure_falls_back(tmp_path, monkeypatch):
    _db_ready(tmp_path, name="test_reg04_completion_fail.db")
    _enable_cities()
    _reg_register_delegate(920932, event_city="spb")
    asyncio.run(db.set_setting("approve_text", "Глобальный текст одобрения"))
    _set_override("approve_text", "spb", "СПб текст одобрения")

    async def _boom(_uid):
        raise RuntimeError("boom")

    # 13-02 (REFAC-02): send_completion_and_bonus now lives in handlers/reg_schema.py, so its
    # internal `await get_user(...)` call resolves via reg_schema's own module globals, not
    # registration.py's -- patch the function where it is actually defined.
    monkeypatch.setattr(reg_schema_mod, "get_user", _boom)
    bot = _RegFinalizeBot()
    asyncio.run(reg_mod.send_completion_and_bonus(bot, 920932, with_menu=False, participant_type="full"))
    # T-05-04-04: approved user must always receive a message, even on a city-resolve failure.
    assert bot.sent and bot.sent[0][1] == "Глобальный текст одобрения"


def test_approve_text_for_signature_and_callers_unchanged():
    params = inspect.signature(reg_mod._approve_text_for).parameters
    assert "city_code" in params and params["city_code"].default is None
    src = inspect.getsource(reg_mod._approve_text_for)
    assert "approve_text__party" in src
    assert 'get_setting_for_city("approve_text"' in src
    assert list(inspect.signature(reg_mod.send_completion_and_bonus).parameters) == [
        "bot", "telegram_id", "with_menu", "participant_type",
    ]


# ── Plan 04 Task 3: registration_mode per city + city-fork regression guard ────────────────

def test_resolve_track_no_city_code_behaves_as_today(tmp_path):
    _db_ready(tmp_path, name="test_reg04_resolve_track_default.db")
    asyncio.run(db.set_setting("registration_mode", "short"))
    assert asyncio.run(reg_mod._resolve_track(None)) == "short"
    assert asyncio.run(reg_mod._resolve_track("full")) == "short"
    assert asyncio.run(reg_mod._resolve_track("party_overnight")) == "party_overnight"


def test_resolve_track_city_override_short_for_one_city_full_globally(tmp_path):
    _db_ready(tmp_path, name="test_reg04_resolve_track_spb_short.db")
    _enable_cities()
    asyncio.run(db.set_setting("registration_mode", "full"))
    _set_override("registration_mode", "spb", "short")

    assert asyncio.run(reg_mod._resolve_track(None, "spb")) == "short"
    assert asyncio.run(reg_mod._resolve_track(None, "msk")) == "full"


def test_resolve_track_city_override_full_for_one_city_short_globally(tmp_path):
    _db_ready(tmp_path, name="test_reg04_resolve_track_spb_full.db")
    _enable_cities()
    asyncio.run(db.set_setting("registration_mode", "short"))
    _set_override("registration_mode", "spb", "full")

    assert asyncio.run(reg_mod._resolve_track(None, "spb")) == "full"
    assert asyncio.run(reg_mod._resolve_track(None, "msk")) == "short"


def test_resolve_track_unknown_city_falls_back_to_global(tmp_path):
    _db_ready(tmp_path, name="test_reg04_resolve_track_none_city.db")
    _enable_cities()
    asyncio.run(db.set_setting("registration_mode", "short"))
    _set_override("registration_mode", "spb", "full")
    assert asyncio.run(reg_mod._resolve_track(None, None)) == "short"


def test_resolve_track_module_off_ignores_city_override(tmp_path):
    _db_ready(tmp_path, name="test_reg04_resolve_track_offparity.db")
    # event_city_enabled left OFF.
    asyncio.run(db.set_setting("registration_mode", "full"))
    _set_override("registration_mode", "spb", "short")
    assert asyncio.run(reg_mod._resolve_track(None, "spb")) == "full"


def test_party_track_still_authoritative_over_any_city_registration_mode(tmp_path):
    _db_ready(tmp_path, name="test_reg04_resolve_track_party.db")
    _enable_cities()
    asyncio.run(db.set_setting("registration_mode", "full"))
    _set_override("registration_mode", "spb", "short")
    assert asyncio.run(reg_mod._resolve_track("party_overnight", "spb")) == "party_overnight"


def test_start_registration_flow_computes_city_before_track():
    src = inspect.getsource(reg_mod._start_registration_flow)
    assert src.index("saved_city =") < src.index("saved_track =")


def test_city_fork_shown_for_short_mode(tmp_path):
    """RESEARCH Pitfall 2 / P2 note regression guard: the city pre-flow screen must still
    appear even when registration_mode == "short" -- the gate has (and must keep having) no
    dependency on registration_mode at all."""
    _db_ready(tmp_path, name="test_reg04_city_fork_short.db")
    _enable_cities()  # default CITIES config carries 3 enabled cities (>= 2 required)
    asyncio.run(db.set_setting("registration_mode", "short"))
    assert asyncio.run(reg_mod._should_show_city_fork(None, False)) is True


def test_should_show_city_fork_has_no_registration_mode_dependency():
    src = inspect.getsource(reg_mod._should_show_city_fork)
    assert "registration_mode" not in src
