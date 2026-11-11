"""Три выключателя уведомлений и срок «до завтра».

Флаги живут отдельно от срока. Старая дата на сегодня глушит все три типа
через общий фильтр.
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


def test_mute_today_writes_deadline_and_names_three_types(tmp_path, monkeypatch):
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
        assert "рассылок" in text and "игры" in text and "напоминаний" in text
        assert "Важное" in text
        assert "заявке" in text and "личное письмо" in text and "оплата" in text and "QR" in text
        assert "mute_" not in text and "muted_until" not in text
        assert len(text) <= 200
        assert show_alert is True
        assert msg.reply_markup.inline_keyboard[0][0].callback_data == "bc_unmute_today"
        assert msg.answers
        assert "приходят всегда" in msg.answers[0][0]
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
        assert "остаются" in text
        assert "все рассылки" not in text
        assert "mute_" not in text
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


def test_game_letter_skips_when_switch_is_on(tmp_path, monkeypatch):
    async def go():
        _ready(tmp_path)
        await _add(DELEGATE_ID)
        await _add(OTHER_ID)
        await db.set_notice_flag(DELEGATE_ID, "game", True)
        sent = []

        async def sender():
            sent.append("x")

        async def deliver(now, user_id, text, *, sender, parse_mode="HTML", reply_markup=None, question_ref=None):
            await sender()
            return True

        monkeypatch.setattr("services.comms.quiet_hours.send_or_queue_text", deliver)
        from services.game.coins_notify import send_game_text

        now = datetime(2026, 9, 24, 15, 0)
        assert await send_game_text(DELEGATE_ID, "баллы", sender, now=now) is True
        assert sent == []
        assert await send_game_text(OTHER_ID, "баллы", sender, now=now) is True
        assert sent == ["x"]

    asyncio.run(go())


def test_survey_nudge_skips_reminder_switch(tmp_path, monkeypatch):
    async def go():
        _ready(tmp_path)
        await _add(DELEGATE_ID)
        await _add(OTHER_ID)
        await db.set_notice_flag(DELEGATE_ID, "reminders", True)
        from services.comms import survey_nudge

        sent = []

        async def invite(bot, poll, tid, now):
            sent.append(tid)

        async def finished(_pid):
            return set()

        monkeypatch.setattr(survey_nudge, "finished_survey_user_ids", finished)
        monkeypatch.setattr("services.comms.polls._send_invite", invite)
        n_sent, n_failed = await survey_nudge.send_nudge(None, {"id": 1}, [DELEGATE_ID, OTHER_ID])
        assert sent == [OTHER_ID]
        assert (n_sent, n_failed) == (1, 0)

    asyncio.run(go())


def test_session_remind_skips_blocked_before_journal(monkeypatch):
    from services.forum import session_remind as sr

    seen = {"was": [], "sent": [], "kind": None}

    async def get_session(_sid):
        return {"id": 7, "city": "msk", "title": "Сессия", "start_time": "12:00", "hall_name": ""}

    async def enabled(_city):
        return True

    async def minutes(_city):
        return 10

    async def offset(_city):
        return 0

    def start_msk(_session, _offset):
        return datetime(2026, 9, 25, 12, 0)

    def run_at(_session, _minutes, _offset):
        return datetime(2026, 9, 25, 11, 50)

    async def names(_ids):
        return {7: []}

    async def recipients(_session):
        return [{"telegram_id": 42}, {"telegram_id": 43}]

    async def blocked(kind, now):
        seen["kind"] = kind
        return {42}

    async def was_sent(tid, kind, ref):
        seen["was"].append(tid)
        return False

    async def send(tid, text, kind, ref, now):
        seen["sent"].append(tid)
        return True

    async def lang(_tid, _maps):
        return "ru", {}

    async def text(*_a, **_k):
        return "скоро"

    monkeypatch.setattr("database.db.get_program_session", get_session)
    monkeypatch.setattr("services.scheduler._now_moscow_naive", lambda: datetime(2026, 9, 25, 11, 50))
    monkeypatch.setattr("database.db.notice_blocked_ids", blocked)
    monkeypatch.setattr("database.session_enroll_db.competency_names_for_sessions", names)
    monkeypatch.setattr(sr, "remind_enabled", enabled)
    monkeypatch.setattr(sr, "city_offset_hours", offset)
    monkeypatch.setattr(sr, "session_start_msk", start_msk)
    monkeypatch.setattr(sr, "remind_minutes", minutes)
    monkeypatch.setattr(sr, "remind_run_at", run_at)
    monkeypatch.setattr(sr, "remind_recipients", recipients)
    monkeypatch.setattr(sr.rdb, "was_sent", was_sent)
    monkeypatch.setattr(sr, "_send", send)
    monkeypatch.setattr(sr, "_lang_for", lang)
    monkeypatch.setattr(sr, "build_remind_text", text)

    result = asyncio.run(sr.deliver_session_reminders(7))
    assert seen["kind"] == "reminders"
    assert seen["was"] == [43]
    assert seen["sent"] == [43]
    assert result["sent"] == 1
    assert result["skipped"] >= 1
