"""Quick 260817-4pj regression tests.

Covers the fix that replaced the admin re-registration reply-keyboard + the old
admin-rereg FSM state (removed from the Registration states group) with an inline button
(`callback_data="admin_rereg"`) so a second ReplyKeyboardMarkup no longer overwrites the
just-sent main menu, and the admin's first tap on any menu button is no longer swallowed
by a stray FSM state.

Style matches tests/test_registration_phase5.py (direct handler calls, real FSMContext over
MemoryStorage, config.DB_PATH pointed at tmp_path) — pytest-asyncio is unavailable in this env.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers.states import Registration

ADMIN_ID = 900001
OTHER_ID = 900002


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    """Records (text, reply_markup) pairs from answer/answer_photo — needed to distinguish
    the main-menu ReplyKeyboardMarkup from a possible second one, and from the new inline
    admin_rereg button."""

    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup)]

    async def answer(self, text=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None

    async def answer_photo(self, *a, reply_markup=None, **k):
        self.sent.append(("<photo>", reply_markup))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.username)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _KBCapturingMessage(0)
        self.answers = []  # list[(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


def _register_approved_admin(uid):
    async def go_add():
        await db.add_user({
            "telegram_id": uid, "full_name": "Admin Tester",
            "username": "admintester", "registration_date": "2026-08-17 10:00:00",
        })
        await db.set_user_status(uid, "approved")
    return go_add()


# ── Test 1: /start main menu no longer clobbered, inline button instead ─────────

def test_admin_start_shows_main_menu_and_inline_rereg_button_only(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])

    async def go():
        await db.init_db()
        await _register_approved_admin(ADMIN_ID)

        msg = _KBCapturingMessage(ADMIN_ID, "admintester")
        state = _new_state(ADMIN_ID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        reply_kb_msgs = [m for m in msg.sent if isinstance(m[1], ReplyKeyboardMarkup)]
        inline_kb_msgs = [m for m in msg.sent if isinstance(m[1], InlineKeyboardMarkup)]

        # Exactly one ReplyKeyboardMarkup (the main menu) — no second reply-keyboard clobbers it.
        assert len(reply_kb_msgs) == 1

        # Exactly one InlineKeyboardMarkup message, single button, admin_rereg callback_data.
        assert len(inline_kb_msgs) == 1
        buttons = inline_kb_msgs[0][1].inline_keyboard
        assert len(buttons) == 1 and len(buttons[0]) == 1
        assert buttons[0][0].callback_data == "admin_rereg"

        # No leftover FSM state after /start.
        assert await state.get_state() is None

    asyncio.run(go())


# ── Test 2: non-admin tap on admin_rereg is rejected, no side effects ───────────

def test_admin_rereg_callback_rejects_non_admin(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])  # OTHER_ID is NOT an admin

    calls = []

    async def fake_start_registration_flow(*a, **k):
        calls.append((a, k))

    monkeypatch.setattr(reg, "_start_registration_flow", fake_start_registration_flow)

    async def go():
        await db.init_db()
        state = _new_state(OTHER_ID)
        callback = _FakeCallback("admin_rereg", OTHER_ID, "notadmin")

        await reg.admin_rereg(callback, state)

        assert len(callback.answers) == 1
        text, show_alert = callback.answers[0]
        assert "Недостаточно прав" in text
        assert show_alert is True

        assert calls == []  # _start_registration_flow never invoked
        assert await state.get_state() is None

    asyncio.run(go())


# ── Test 3: admin tap on admin_rereg starts the registration flow as the admin ──

def test_admin_rereg_callback_starts_flow_as_tapping_admin(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])

    recorded = []
    real_mark_reg_started = reg.mark_reg_started

    async def spying_mark_reg_started(telegram_id, username, *a, **k):
        recorded.append(telegram_id)
        return await real_mark_reg_started(telegram_id, username, *a, **k)

    monkeypatch.setattr(reg, "mark_reg_started", spying_mark_reg_started)

    async def go():
        await db.init_db()
        await _register_approved_admin(ADMIN_ID)

        state = _new_state(ADMIN_ID)
        callback = _FakeCallback("admin_rereg", ADMIN_ID, "admintester")

        await reg.admin_rereg(callback, state)

        # Authorized -> callback.answer() with no alert (just closes the "loading" spinner).
        assert len(callback.answers) == 1
        assert callback.answers[0][1] is False

        # mark_reg_started recorded the TAPPING admin's id, not the bot's (id 0).
        assert recorded == [ADMIN_ID]

        # Flow moved into the first questionnaire step (empty consents on a clean DB).
        assert await state.get_state() == Registration.full_name.state

    asyncio.run(go())
