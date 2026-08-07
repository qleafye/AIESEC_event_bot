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
        self.markups = []

    async def answer(self, text=None, *a, **k):
        self.texts.append(text)
        self.markups.append(k.get("reply_markup"))
        return None

    async def answer_photo(self, *a, **k):
        self.texts.append("<photo>")
        self.markups.append(k.get("reply_markup"))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, self.from_user.username)
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        # Stands in for the bot's own city/fork message; city_pick/party_pick swap in
        # callback.from_user via model_copy() before continuing the chain.
        self.message = _FakeMessage(0)

    async def answer(self, text=None, show_alert=False):
        return None


class FakeCommand:
    def __init__(self, args=None):
        self.args = args


def _callback_datas(markup):
    """Flatten an InlineKeyboardMarkup's callback_data values, or [] if markup is None or a
    different keyboard type (e.g. the admin re-register ReplyKeyboardMarkup)."""
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return []
    return [btn.callback_data for row in rows for btn in row]


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


# ── Task 2: order of the two pre-flow forks in cmd_start + city_pick ────────────────────────

def test_module_off_bare_start_no_city_screen_regression(tmp_path):
    """event_city_enabled default off -> ordinary /start behaves exactly as before this
    plan: no city_pick keyboard anywhere, flow starts normally."""
    _use_tmp_db(tmp_path)
    uid = 810001

    async def go():
        await db.init_db()
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    all_codes = [c for m in msg.markups for c in _callback_datas(m)]
    assert not any(c.startswith("city_pick:") for c in all_codes)
    assert "Отлично, начинаем регистрацию." in msg.texts


def test_module_on_bare_start_shows_three_city_buttons_flow_not_started(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 810002

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    all_codes = [c for m in msg.markups for c in _callback_datas(m)]
    assert all_codes == ["city_pick:msk", "city_pick:spb", "city_pick:tyumen"]
    assert "Отлично, начинаем регистрацию." not in msg.texts


def test_city_deeplink_skips_screen_starts_flow_with_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    uid = 810003
    captured = {}
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        captured["kwargs"] = k
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=FakeCommand("city_spb"))
        return msg

    msg = asyncio.run(go())
    all_codes = [c for m in msg.markups for c in _callback_datas(m)]
    assert not any(c.startswith("city_pick:") for c in all_codes)
    assert captured["kwargs"].get("event_city") == "spb"


def test_collision_party_link_then_city_pick_track_authoritative(tmp_path):
    """Party deep-link + enabled cities -> city screen first; after city_pick the party fork
    is NOT shown (track already authoritative), flow starts with both dimensions set."""
    _use_tmp_db(tmp_path)
    uid = 810004

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("party_enabled", "on")
        await db.set_setting("party_fork_question", "on")
        state = _new_state(uid)

        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand("party_over"))
        codes_after_start = [c for m in msg.markups for c in _callback_datas(m)]

        cb = _FakeCallback("city_pick:spb", uid, "u")
        await reg.city_pick(cb, state)
        data = await state.get_data()
        return codes_after_start, cb.message.markups if hasattr(cb.message, "markups") else None, data

    codes_after_start, _unused, data = asyncio.run(go())
    assert codes_after_start[0].startswith("city_pick:")  # city screen shown first
    assert data.get("participant_type") == "party_overnight"
    assert data.get("event_city") == "spb"


def test_collision_city_link_then_manual_party_pick_city_preserved(tmp_path):
    """City deep-link + party fork on -> city screen skipped, party fork shown; after
    party_pick the city chosen via deep-link survives into the flow."""
    _use_tmp_db(tmp_path)
    uid = 810005

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("party_enabled", "on")
        await db.set_setting("party_fork_question", "on")
        state = _new_state(uid)

        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand("city_spb"))
        city_codes = [c for m in msg.markups for c in _callback_datas(m) if c.startswith("city_pick:")]

        await reg.party_pick(_FakeCallback("party_pick:party_over", uid, "u"), state)
        data = await state.get_data()
        return city_codes, data

    city_codes, data = asyncio.run(go())
    assert city_codes == []  # city screen never shown
    assert data.get("event_city") == "spb"  # not lost across the party fork
    assert data.get("participant_type") == "party_overnight"


def test_collision_bare_start_city_then_party_then_full(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 810006

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("party_enabled", "on")
        await db.set_setting("party_fork_question", "on")
        await db.set_setting("registration_mode", "full")
        state = _new_state(uid)

        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=object(), command=None)  # bare -> city screen

        cb = _FakeCallback("city_pick:spb", uid, "u")
        await reg.city_pick(cb, state)  # -> party fork (track unknown)

        await reg.party_pick(_FakeCallback("party_pick:full", uid, "u"), state)
        data = await state.get_data()
        return data

    data = asyncio.run(go())
    assert data.get("event_city") == "spb"
    assert not reg._is_party_track(data.get("participant_type"))


def test_attribution_survives_city_pick_referrer(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 810007
    referrer = uid + 1

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        state = _new_state(uid)
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand(str(referrer)))
        await reg.city_pick(_FakeCallback("city_pick:spb", uid, "u"), state)
        data = await state.get_data()
        return data

    data = asyncio.run(go())
    assert data.get("referrer_id") == referrer


def test_attribution_survives_city_pick_source_tag(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 810008

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        state = _new_state(uid)
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand("src_vk"))
        await reg.city_pick(_FakeCallback("city_pick:spb", uid, "u"), state)
        data = await state.get_data()
        return data

    data = asyncio.run(go())
    assert data.get("source") == "vk"
    assert data.get("_source_from_tag") is True


def test_underreg_through_release_city_screen_shown_track_recovered(tmp_path):
    """A registration started BEFORE this plan's release: reg_started has event_city IS NULL
    and participant_type="full". A bare /start after release shows the city screen (city not
    recovered), but the track IS recovered from reg_started and not lost; after city_pick the
    flow starts with participant_type="full" and event_city="spb"."""
    _use_tmp_db(tmp_path)
    uid = 810009

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        # SHORT-01: registration_mode's registry default is "short"; pin explicitly to "full"
        # so this test isolates the pre-release track-recovery behavior, not the promo override.
        await db.set_setting("registration_mode", "full")
        await db.mark_reg_started(uid, "u", "full")  # pre-release row, no event_city arg
        state = _new_state(uid)
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=object(), command=None)
        city_codes = [c for m in msg.markups for c in _callback_datas(m) if c.startswith("city_pick:")]

        cb = _FakeCallback("city_pick:spb", uid, "u")
        await reg.city_pick(cb, state)
        data = await state.get_data()
        return city_codes, data

    city_codes, data = asyncio.run(go())
    assert city_codes  # city screen WAS shown (no city on record)
    assert data.get("participant_type") == "full"
    assert data.get("event_city") == "spb"


def test_city_pick_unknown_code_rejected(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    uid = 810010
    called = []
    monkeypatch.setattr(reg, "_start_registration_flow", lambda *a, **k: called.append(1))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        state = _new_state(uid)
        await reg.city_pick(_FakeCallback("city_pick:atlantis", uid, "u"), state)

    asyncio.run(go())
    assert called == []


def test_city_pick_disabled_city_rejected(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    uid = 810011
    called = []
    monkeypatch.setattr(reg, "_start_registration_flow", lambda *a, **k: called.append(1))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_enabled__tyumen", "off")
        state = _new_state(uid)
        await reg.city_pick(_FakeCallback("city_pick:tyumen", uid, "u"), state)

    asyncio.run(go())
    assert called == []
