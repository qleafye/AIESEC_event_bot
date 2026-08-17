"""Phase 14 Plan 04 (GAME-09) — manual coins «for humans».

Covers Task 1 (coins.source, mandatory reason on /coins, delegate notification, moderate_game
repoint), Task 2 (button wizard entry: person resolution, sign, amount) and Task 3 (reason,
confirm screen, ledger write, notification, resync) of 14-04-PLAN.md.

Handlers driven directly via Fake message/callback doubles -- same convention as
tests/test_gamification_review_phase9.py (FakeUser/FakeBot/FakeMessage/FakeCallback,
asyncio.run(), config.DB_PATH -> tmp_path). pytest-asyncio is unavailable in this env.
"""
import asyncio
import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability
from handlers.states import GameReview
from settings_schema import SETTINGS_SCHEMA


ADMIN_ID = 931401
GAME_MANAGER_ID = 931402
DELEGATE_ID = 931403


def _db_ready(tmp_path, name="test_coins_manual_260818.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


class FakeMessage:
    """Stand-in for both callback.message and a dispatched message event. `forward_origin`/
    `forward_from` default to None (plain text input) -- individual tests set them to exercise
    _resolve_staff_input's forward-message branch."""

    def __init__(self, text=None, user_id=ADMIN_ID, bot=None, forward_origin=None, forward_from=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.forward_origin = forward_origin
        self.forward_from = forward_from
        self.deleted = False
        self.answers_sent = []
        self.answer_markups = []
        self.answer_parse_modes = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_parse_modes.append(parse_mode)
        self.text = text

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, bot=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.message = FakeMessage(user_id=user_id, bot=self.bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeForwardedSender:
    def __init__(self, uid):
        self.id = uid


class FakeForwardOrigin:
    """Minimal MessageOriginUser double -- _resolve_staff_input reads only `.sender_user`."""

    def __init__(self, uid):
        self.sender_user = FakeForwardedSender(uid)


def _coin_rows():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM coins ORDER BY id").fetchall()]
    conn.close()
    return rows


def _seed_task(coins=10, created_by=ADMIN_ID):
    return asyncio.run(db.create_task("Тестовое задание", "Light", coins, "text",
                                       "2099-01-01 00:00:00", created_by))


def _seed_submission(task_id, user_id=DELEGATE_ID, content="готово"):
    return asyncio.run(db.create_submission(task_id, user_id, "text", content, "2026-08-14 10:00:00"))


def _seed_delegate(user_id=DELEGATE_ID, username="@delegate1", full_name="Дельгат Тестов"):
    """`username` stored WITH the leading @ — get_user_by_username's own normalization only
    ADDS a missing @, it never strips one, so the seeded value must match what a real Telegram
    username row looks like (database.db.get_user_by_username, `users.username` column)."""
    asyncio.run(db.add_user({
        "telegram_id": user_id, "username": username, "full_name": full_name,
        "registration_date": "2026-08-01",
    }))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: coins.source, mandatory reason on /coins, delegate notification, moderate_game repoint
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_coins_table_has_source_column(tmp_path):
    _db_ready(tmp_path)
    conn = sqlite3.connect(config.DB_PATH)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(coins)").fetchall()]
    conn.close()
    assert "source" in cols


def test_add_coins_without_source_stays_null_regression(tmp_path):
    """Every pre-existing call site that never passes source= keeps writing NULL."""
    _db_ready(tmp_path)
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="старый вызов", changed_by=ADMIN_ID))
    rows = _coin_rows()
    assert len(rows) == 1
    assert rows[0]["source"] is None


def test_add_coins_writes_source_when_given(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="за помощь", changed_by=ADMIN_ID, source="manual"))
    rows = _coin_rows()
    assert rows[-1]["source"] == "manual"


def test_grev_approve_default_amount_writes_source_task(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(coins=30)
    sub_id = _seed_submission(task_id)
    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_mod.grev_approve(callback, state))
    rows = _coin_rows()
    assert rows[-1]["source"] == "task"


def test_grev_approve_custom_amount_writes_source_task(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(coins=5)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    start_cb = FakeCallback(f"grev_approve_custom:{sub_id}")
    asyncio.run(admin_mod.grev_approve_custom_start(start_cb, state))
    assert asyncio.run(state.get_state()) == GameReview.approve_amount
    amount_msg = FakeMessage(text="45")
    asyncio.run(admin_mod.grev_approve_amount_step(amount_msg, state))
    rows = _coin_rows()
    assert rows[-1]["source"] == "task"


def test_cmd_coins_without_reason_shows_hint_and_writes_nothing(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert "причин" in msg.answers_sent[-1].lower()
    assert _coin_rows() == []


def test_cmd_coins_with_whitespace_only_reason_shows_hint_and_writes_nothing(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5    ")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert "причин" in msg.answers_sent[-1].lower()
    assert _coin_rows() == []


def test_cmd_coins_with_reason_writes_manual_row(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5 за активность")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    rows = _coin_rows()
    assert len(rows) == 1
    assert rows[0]["source"] == "manual"
    assert rows[0]["reason"] == "за активность"
    assert rows[0]["changed_by"] == ADMIN_ID
    assert rows[0]["delta"] == 5


def test_cmd_coins_notifies_delegate_with_amount_reason_balance(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5 за активность")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert msg.bot.sent, "no notification sent to delegate"
    chat_id, text = msg.bot.sent[-1]
    assert chat_id == DELEGATE_ID
    assert "+5" in text
    assert "за активность" in text
    assert "5" in text  # new balance


def test_cmd_coins_blocked_bot_keeps_ledger_row_and_flags_manager(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    failing_bot = FakeBot(fail=True)
    msg = FakeMessage(text="/coins @delegate1 +5 за активность", bot=failing_bot)
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert len(_coin_rows()) == 1  # ledger write NOT rolled back by a failed notification
    assert "не получил уведомление" in msg.answers_sent[-1]


def test_notify_manual_coins_escapes_html_in_reason(tmp_path):
    _db_ready(tmp_path)
    bot = FakeBot()
    ok = asyncio.run(admin_mod._notify_manual_coins(bot, DELEGATE_ID, 5, "<b>hack</b>", 10))
    assert ok is True
    _, text = bot.sent[-1]
    assert "<b>hack</b>" not in text
    assert "&lt;b&gt;hack&lt;/b&gt;" in text


def test_coins_capability_is_moderate_game(tmp_path):
    _db_ready(tmp_path)
    assert required_capability(command="coins") == "moderate_game"


def test_coins_manual_notify_text_registry_key():
    entry = SETTINGS_SCHEMA["coins_manual_notify_text"]
    assert entry["type"] == "text"
    assert entry["group"] == "game"
    for placeholder in ("{delta}", "{reason}", "{balance}"):
        assert placeholder in entry["default"]
    assert "coins_manual_notify_text" in admin_mod._GAME_FIELD_ORDER
