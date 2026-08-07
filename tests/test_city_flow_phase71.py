"""Phase 07.1 Plan 03 (city-selection pre-flow screen, CITY-01) tests.

pytest-asyncio is unavailable in this env — each async test drives the DB/registration
helpers via asyncio.run() and points config.DB_PATH at a tmp_path file, same convention as
tests/test_cities_phase71.py / tests/test_city_sheets_phase71.py.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum_city_flow.db")


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.texts = []

    async def answer(self, text=None, *a, **k):
        self.texts.append(text)
        return None

    async def answer_photo(self, *a, **k):
        self.texts.append("<photo>")
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, self.from_user.username)
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


# ── Task 1: _should_show_city_fork gate ──────────────────────────────────────────────────

def test_should_show_city_fork_false_when_city_already_known(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return await reg._should_show_city_fork("spb", False)

    assert asyncio.run(go()) is False


def test_should_show_city_fork_false_when_already_registered(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return await reg._should_show_city_fork(None, True)

    assert asyncio.run(go()) is False


def test_should_show_city_fork_false_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await reg._should_show_city_fork(None, False)

    assert asyncio.run(go()) is False


def test_should_show_city_fork_true_when_on_and_three_cities_enabled(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return await reg._should_show_city_fork(None, False)

    assert asyncio.run(go()) is True


def test_should_show_city_fork_false_after_disabling_down_to_one_city(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_enabled__spb", "off")
        await db.set_setting("city_enabled__tyumen", "off")
        return await reg._should_show_city_fork(None, False)

    assert asyncio.run(go()) is False


# ── Task 1: _city_fork_kb keyboard ────────────────────────────────────────────────────────

def test_city_fork_kb_lists_all_enabled_cities(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return await reg._city_fork_kb()

    kb = asyncio.run(go())
    codes = [row[0].callback_data for row in kb.inline_keyboard]
    assert codes == ["city_pick:msk", "city_pick:spb", "city_pick:tyumen"]


def test_city_fork_kb_excludes_disabled_city(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_enabled__tyumen", "off")
        return await reg._city_fork_kb()

    kb = asyncio.run(go())
    codes = [row[0].callback_data for row in kb.inline_keyboard]
    assert codes == ["city_pick:msk", "city_pick:spb"]


def test_city_fork_kb_button_text_uses_city_label_override(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        kb_before = await reg._city_fork_kb()
        await db.set_setting("city_label__spb", "СПб, 4 окт")
        kb_after = await reg._city_fork_kb()
        return kb_before, kb_after

    kb_before, kb_after = asyncio.run(go())
    spb_before = next(row[0].text for row in kb_before.inline_keyboard if row[0].callback_data == "city_pick:spb")
    spb_after = next(row[0].text for row in kb_after.inline_keyboard if row[0].callback_data == "city_pick:spb")
    assert spb_before == "Санкт-Петербург, 3 октября"
    assert spb_after == "СПб, 4 окт"


# ── Task 1: _start_registration_flow carries event_city through state.clear() ──────────────

def test_start_registration_flow_with_city_survives_clear_and_reg_started(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 800001

    async def go():
        await db.init_db()
        state = _new_state(uid)
        await reg._start_registration_flow(_FakeMessage(uid, "u"), state, event_city="spb")
        data = await state.get_data()
        city_row = await db.get_reg_started_city(uid)
        return data.get("event_city"), city_row

    fsm_city, reg_started_city = asyncio.run(go())
    assert fsm_city == "spb"
    assert reg_started_city == "spb"


def test_start_registration_flow_without_city_leaves_no_default(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 800002

    async def go():
        await db.init_db()
        state = _new_state(uid)
        await reg._start_registration_flow(_FakeMessage(uid, "u"), state)
        data = await state.get_data()
        city_row = await db.get_reg_started_city(uid)
        tab = await reg.city_row_tab(None, None)
        return data.get("event_city"), city_row, tab

    fsm_city, reg_started_city, tab = asyncio.run(go())
    assert fsm_city is None
    assert reg_started_city is None
    assert tab is None  # no city means legacy Moscow appender, resolved on read only
