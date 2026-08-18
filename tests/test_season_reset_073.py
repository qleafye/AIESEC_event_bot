"""Phase 07.3 Plan 02 (RET-01) — «🔄 Новый сезон» wizard.

Covers the superadmin gate (checked 3x: button-render, entry, execute), the numbers-in-text
confirm screen, the typed-passphrase gate, and the actual season switch. Style matches
tests/test_admin_rereg_260817.py / tests/test_city_admin_phase71.py — pytest-asyncio is
unavailable in this env, every async call goes through asyncio.run(), config.DB_PATH points
at a tmp_path file, no conftest.py (project convention).

NOTE on the plan's `user_id=` wording: build_settings_group_keyboard already carries a live
`admin_id: int | None = None` keyword parameter (added by 09.3 for per-city header context,
reused here for the superadmin gate) — tests below call it with `admin_id=`, per the
executor's "adapt to CURRENT signatures" instruction, not the PLAN.md sketch's `user_id=`.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import ADMIN_CAPS
from handlers.states import SeasonReset

ADMIN_ID = 900301
OTHER_ID = 900302  # holds "settings" capability but is NOT in config.ADMIN_IDS


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_reset_073.db")


def _db_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    """Records (text, reply_markup) pairs from .answer() — needed to inspect prompts/keyboards
    sent mid-wizard, same shape as tests/test_admin_rereg_260817.py's own double."""

    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup)]

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _KBCapturingMessage(0)
        self.answers = []  # list[(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class _FakeMessage:
    def __init__(self, user_id, text):
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.sent = []  # list[(text, reply_markup)]

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


def _flat_callback_data(kb: InlineKeyboardMarkup):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── Task 1: вход в визард — состояния, capability-ключи, кнопка ─────────────────────────────

def test_season_reset_caps_registered():
    assert ADMIN_CAPS["admin_season_reset"] == "settings"
    assert ADMIN_CAPS["season_reset_go"] == "settings"
    assert ADMIN_CAPS["state:SeasonReset:*"] == "settings"


def test_season_reset_button_hidden_for_non_superadmin(tmp_path):
    _db_ready(tmp_path)

    kb = asyncio.run(admin_mod.build_settings_group_keyboard("event", admin_id=OTHER_ID))
    assert "admin_season_reset" not in _flat_callback_data(kb)

    kb_admin = asyncio.run(admin_mod.build_settings_group_keyboard("event", admin_id=ADMIN_ID))
    codes = _flat_callback_data(kb_admin)
    assert codes.count("admin_season_reset") == 1


def test_season_reset_button_absent_without_admin_id(tmp_path):
    _db_ready(tmp_path)
    # Backward compatibility with old call sites that never pass admin_id at all.
    kb = asyncio.run(admin_mod.build_settings_group_keyboard("event"))
    assert "admin_season_reset" not in _flat_callback_data(kb)


def test_season_reset_start_denies_non_superadmin(tmp_path):
    _db_ready(tmp_path)

    async def go():
        state = _new_state(OTHER_ID)
        callback = _FakeCallback("admin_season_reset", OTHER_ID)
        await admin_mod.season_reset_start(callback, state)

        assert len(callback.answers) == 1
        text, show_alert = callback.answers[0]
        assert show_alert is True
        assert "суперадмин" in text.lower()
        assert await state.get_state() is None
        assert callback.message.sent == []

    asyncio.run(go())


def test_season_reset_start_sets_naming_state(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.set_setting("event_season", "YL'25")
        state = _new_state(ADMIN_ID)
        callback = _FakeCallback("admin_season_reset", ADMIN_ID)
        await admin_mod.season_reset_start(callback, state)

        assert await state.get_state() == SeasonReset.naming.state
        data = await state.get_data()
        assert data.get("season_old") == "YL'25"
        assert len(callback.message.sent) == 1
        text, markup = callback.message.sent[0]
        assert "Новый сезон" in text
        assert markup is not None  # get_cancel_kb()
        assert len(callback.answers) == 1 and callback.answers[0][1] is False

    asyncio.run(go())


def test_season_reset_cancel(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.set_setting("event_season", "YL'25")
        state = _new_state(ADMIN_ID)
        await state.set_state(SeasonReset.passphrase)
        await state.update_data(season_old="YL'25", season_new="YL'26", season_phrase="YL'25")

        message = _FakeMessage(ADMIN_ID, "Отмена")
        await admin_mod.cancel_season_reset(message, state)

        assert await state.get_state() is None
        assert len(message.sent) == 1
        text, markup = message.sent[0]
        assert "Отменено" in text
        assert isinstance(markup, ReplyKeyboardRemove)
        assert await db.get_setting("event_season") == "YL'25"

    asyncio.run(go())

