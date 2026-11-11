"""Три выключателя уведомлений и срок «до завтра».

Старая дата рассылок остаётся как была: отправители до подключения общего фильтра
читают её и не начинают глушить сессии и игру. Флаги живут отдельно от срока.
"""
import asyncio
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import db
from handlers.comms import delegate_notices as notices
from tests._dbtpl import fast_init_db

DELEGATE_ID = 900971
OTHER_ID = 900972


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "notice_mutes.db")
    fast_init_db()


async def _add(uid):
    await db.add_user({
        "telegram_id": uid, "full_name": f"Delegate {uid}",
        "registration_date": "2026-09-24 00:00:00",
    })


def test_flags_persist_and_do_not_clear_daily_deadline(tmp_path):
    async def go():
        _ready(tmp_path)
        await _add(DELEGATE_ID)
        now = datetime(2026, 9, 24, 15, 0)
        until = db.tomorrow_moscow_midnight(now)
        assert until == "2026-09-25 00:00:00"
        assert await db.set_muted_until(DELEGATE_ID, until) is True
        assert await db.set_notice_flag(DELEGATE_ID, "game", True) is True

        user = await db.get_user(DELEGATE_ID)
        assert db.notice_flags(user) == {
            "broadcasts": False, "game": True, "reminders": False,
        }
        assert user["muted_until"] == until

        assert await db.set_notice_flag(DELEGATE_ID, "game", False) is True
        user = await db.get_user(DELEGATE_ID)
        assert db.notice_flags(user)["game"] is False
        assert user["muted_until"] == until

        assert await db.set_muted_until(DELEGATE_ID, None) is True
        await db.set_notice_flag(DELEGATE_ID, "reminders", True)
        user = await db.get_user(DELEGATE_ID)
        assert user["muted_until"] is None
        assert db.notice_flags(user)["reminders"] is True

    asyncio.run(go())


def test_daily_mute_blocks_all_three_kinds_without_a_fourth(tmp_path):
    async def go():
        _ready(tmp_path)
        await _add(DELEGATE_ID)
        await _add(OTHER_ID)
        now = datetime(2026, 9, 24, 15, 0)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")
        await db.set_muted_until(DELEGATE_ID, "2026-09-25 00:00:00")
        await db.set_notice_flag(OTHER_ID, "broadcasts", True)

        assert await db.get_muted_today_ids("2026-09-24") == {DELEGATE_ID}
        assert await db.get_muted_today_ids("2026-09-25") == set()

        for kind in db.NOTICE_KINDS:
            blocked = await db.notice_blocked_ids(kind, now)
            assert DELEGATE_ID in blocked
            if kind == "broadcasts":
                assert OTHER_ID in blocked
            else:
                assert OTHER_ID not in blocked

        later = datetime(2026, 9, 25, 0, 0)
        assert await db.notice_blocked_ids("game", later) == set()
        assert OTHER_ID in await db.notice_blocked_ids("broadcasts", later)

    asyncio.run(go())


def test_missing_delegate_is_not_saved(tmp_path):
    async def go():
        _ready(tmp_path)
        assert await db.set_notice_flag(DELEGATE_ID, "broadcasts", True) is False
        assert await db.set_muted_until(DELEGATE_ID, "2026-09-25 00:00:00") is False
        assert await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24") is False

    asyncio.run(go())


class _User:
    def __init__(self, uid):
        self.id = uid


class _Message:
    def __init__(self, markup):
        self.reply_markup = markup
        self.text = "Рассылка"
        self.answers = []

    async def edit_text(self, *args, **kwargs):
        raise AssertionError("текст рассылки стирать нельзя")

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self, uid, message):
        self.from_user = _User(uid)
        self.message = message
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_mute_today_writes_deadline_and_keeps_old_promise(tmp_path, monkeypatch):
    async def go():
        _ready(tmp_path)
        await _add(DELEGATE_ID)
        monkeypatch.setattr(
            "handlers.user_actions.msk_now", lambda: datetime(2026, 9, 24, 15, 0),
        )
        from handlers import user_actions as ua
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔕 Не присылать сегодня", callback_data="bc_mute_today"),
        ]])
        msg = _Message(markup)
        cb = _Callback(DELEGATE_ID, msg)
        await ua.mute_broadcasts_today(cb)

        user = await db.get_user(DELEGATE_ID)
        assert user["mute_broadcasts_until"] == "2026-09-24"
        assert user["muted_until"] == "2026-09-25 00:00:00"
        assert db.notice_flags(user) == {
            "broadcasts": False, "game": False, "reminders": False,
        }
        text, show_alert = cb.answers[0]
        assert "только важное" in text
        assert "игр" not in text
        assert "напоминан" not in text
        assert show_alert is True
        assert msg.reply_markup.inline_keyboard[0][0].callback_data == "bc_unmute_today"
        assert msg.answers
        labels = [btn.text for row in msg.answers[0][1].inline_keyboard for btn in row]
        assert "mute_broadcasts" not in " ".join(labels)
        assert any("рассылки" in label for label in labels)
        assert any("игр" in label for label in labels)
        assert any("напомина" in label for label in labels)

    asyncio.run(go())


def test_unmute_clears_deadline_but_not_flags(tmp_path):
    async def go():
        _ready(tmp_path)
        await _add(DELEGATE_ID)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")
        await db.set_muted_until(DELEGATE_ID, "2026-09-25 00:00:00")
        await db.set_notice_flag(DELEGATE_ID, "game", True)
        from handlers import user_actions as ua
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔔 Присылать всё", callback_data="bc_unmute_today"),
        ]])
        msg = _Message(markup)
        cb = _Callback(DELEGATE_ID, msg)
        await ua.unmute_broadcasts_today(cb)

        user = await db.get_user(DELEGATE_ID)
        assert user["mute_broadcasts_until"] is None
        assert user["muted_until"] is None
        assert db.notice_flags(user)["game"] is True
        text, show_alert = cb.answers[0]
        assert "все рассылки" in text
        assert show_alert is True

    asyncio.run(go())


def test_toggle_does_not_clear_daily_deadline(tmp_path):
    async def go():
        _ready(tmp_path)
        await _add(DELEGATE_ID)
        await db.set_muted_until(DELEGATE_ID, "2026-09-25 00:00:00")
        msg = _Message(InlineKeyboardMarkup(inline_keyboard=[]))
        cb = _Callback(DELEGATE_ID, msg)
        await notices.toggle_notice_game(cb)

        user = await db.get_user(DELEGATE_ID)
        assert db.notice_flags(user)["game"] is True
        assert user["muted_until"] == "2026-09-25 00:00:00"
        text, show_alert = cb.answers[0]
        assert "Игра и баллы выключены" in text
        assert "mute_game" not in text
        assert show_alert is True
        labels = [btn.text for row in msg.reply_markup.inline_keyboard for btn in row]
        assert any("Снова присылать игру" in label for label in labels)

        await notices.toggle_notice_game(cb)
        user = await db.get_user(DELEGATE_ID)
        assert db.notice_flags(user)["game"] is False
        assert user["muted_until"] == "2026-09-25 00:00:00"

    asyncio.run(go())


def test_unknown_and_missing_delegate_say_what_to_press(tmp_path):
    async def go():
        _ready(tmp_path)
        msg = _Message(None)
        cb = _Callback(DELEGATE_ID, msg)
        await notices._toggle(cb, "nope")
        assert "Нажмите одну из трёх" in cb.answers[0][0]

        await notices.toggle_notice_broadcasts(cb)
        assert "Нажмите кнопку ещё раз" in cb.answers[1][0]

    asyncio.run(go())
