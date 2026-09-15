"""16.09: «все уведомления делегатам подходят под правило тихого часа» (владелец).

До этого дня очередь `delayed_notifications` умела ровно один произвольный вид — голый текст.
Поэтому ночью молча уходили мимо тишины: подтверждение/отказ по чеку (вместе с главным меню и
бонусом за регистрацию), ответ организаторов на «Задать вопрос» (в т.ч. не-текстовый) и
нативные опросы. Здесь — круговой тест каждого нового вида (положили на «вебной» стороне
простыми словарями, разобрали на стороне бота фейковым ботом) и проверки самих точек вызова.

pytest-asyncio в окружении нет — async через `asyncio.run()`, `config.DB_PATH` в `tmp_path`
(конвенция сьюта, см. `tests/test_quiet_hours_260904.py`).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from config import config
from database import db
import services.quiet_hours as qh

DELEGATE = 960916
NOW = datetime(2026, 9, 16, 23, 30)
DUE = datetime(2026, 9, 17, 9, 0)


def _ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


class _RecordingBot:
    """Фейковый бот стороны разбора: пишет вызовы, ничего не сериализует."""

    def __init__(self):
        self.messages = []
        self.photos = []
        self.documents = []
        self.copies = []
        self.polls = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.messages.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                              "reply_markup": reply_markup})

    async def send_photo(self, chat_id, file_id, caption=None, parse_mode=None):
        self.photos.append({"chat_id": chat_id, "file_id": file_id, "caption": caption,
                            "parse_mode": parse_mode})

    async def send_document(self, chat_id, file_id, caption=None, parse_mode=None):
        self.documents.append({"chat_id": chat_id, "file_id": file_id, "caption": caption,
                               "parse_mode": parse_mode})

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None):
        self.copies.append({"chat_id": chat_id, "from_chat_id": from_chat_id,
                            "message_id": message_id, "caption": caption})

    async def send_poll(self, chat_id, question, options, is_anonymous, allows_multiple_answers):
        self.polls.append({"chat_id": chat_id, "question": question, "options": options,
                           "is_anonymous": is_anonymous,
                           "allows_multiple_answers": allows_multiple_answers})

        class _Msg:
            message_id = 4242

            class poll:
                id = "tg-poll-1"

        return _Msg()


def _install_bot():
    from services import scheduler as sched
    bot = _RecordingBot()
    sched._bot = bot
    return bot


async def _quiet_all_day():
    await db.set_setting("quiet_hours_enabled", "on")
    await db.set_setting("quiet_hours_start", "00:00")
    await db.set_setting("quiet_hours_end", "23:59")


def _never():
    async def _sender():
        raise AssertionError("в тихие часы отправлять нельзя — только в очередь")
    return _sender


# ── serialize_markup: чистая функция, aiogram не нужен ───────────────────────────────────

def test_serialize_markup_passes_plain_web_dicts_through():
    raw = {"inline_keyboard": [[{"text": "Открыть", "callback_data": "go"}]]}
    assert qh.serialize_markup(raw) == {"type": "inline", "data": raw}
    reply = {"keyboard": [[{"text": "Меню"}]], "resize_keyboard": True}
    assert qh.serialize_markup(reply) == {"type": "reply", "data": reply}


def test_serialize_markup_none_and_garbage_are_none():
    assert qh.serialize_markup(None) is None
    assert qh.serialize_markup({"что-то": "чужое"}) is None
    assert qh.serialize_markup(object()) is None


def test_serialize_markup_uses_aiogram_model_dump_without_importing_it():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Да", callback_data="y")]])
    payload = qh.serialize_markup(kb)
    assert payload["type"] == "inline"
    assert payload["data"]["inline_keyboard"][0][0]["text"] == "Да"


# ── круговой тест каждого вида ────────────────────────────────────────────────────────────

def test_text_kind_round_trip_keeps_reply_markup(tmp_path):
    """Веб кладёт клавиатуру обычным словарём — бот утром собирает объект aiogram."""
    _ready(tmp_path, "qh_kinds_text.db")
    bot = _install_bot()

    async def scenario():
        await _quiet_all_day()
        sent_now = await qh.send_or_queue_text(
            NOW, DELEGATE, "✅ Оплата подтверждена!", sender=_never(),
            reply_markup={"keyboard": [[{"text": "🏠 Меню"}]], "resize_keyboard": True},
        )
        assert sent_now is False
        assert await qh.queued_count() == 1

        assert await qh.flush_due(DUE) == 1
        assert len(bot.messages) == 1
        markup = bot.messages[0]["reply_markup"]
        from aiogram.types import ReplyKeyboardMarkup
        assert isinstance(markup, ReplyKeyboardMarkup)
        assert markup.keyboard[0][0].text == "🏠 Меню"

    asyncio.run(scenario())


def test_text_kind_without_markup_stays_byte_identical(tmp_path):
    """Форма до 16.09: в payload нет ключа reply_markup вовсе, бот шлёт None."""
    _ready(tmp_path, "qh_kinds_text_plain.db")
    bot = _install_bot()

    async def scenario():
        await _quiet_all_day()
        await qh.send_or_queue_text(NOW, DELEGATE, "просто текст", sender=_never())
        rows = await db.list_due_delayed_notifications(DUE.strftime("%Y-%m-%d %H:%M:%S"))
        assert "reply_markup" not in rows[0]["payload"]
        await qh.flush_due(DUE)
        assert bot.messages[0]["reply_markup"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("method, bucket", [("send_photo", "photos"), ("send_document", "documents")])
def test_media_kind_round_trip(tmp_path, method, bucket):
    _ready(tmp_path, f"qh_kinds_{method}.db")
    bot = _install_bot()

    async def scenario():
        await _quiet_all_day()
        sent_now = await qh.send_or_queue_media(
            NOW, DELEGATE, sender=_never(), method=method, file_id="FILE-1",
            caption="🎁 Бонус за регистрацию!",
        )
        assert sent_now is False
        assert await qh.flush_due(DUE) == 1
        calls = getattr(bot, bucket)
        assert calls == [{"chat_id": DELEGATE, "file_id": "FILE-1",
                          "caption": "🎁 Бонус за регистрацию!", "parse_mode": "HTML"}]

    asyncio.run(scenario())


def test_media_kind_unknown_method_never_executed(tmp_path):
    """Закрытый набор методов — как закрытый диспетчер по kind: строка из БД не превращается
    в произвольный вызов атрибута бота."""
    _ready(tmp_path, "qh_kinds_media_bad.db")
    bot = _install_bot()

    async def scenario():
        await qh.enqueue(DELEGATE, qh.KIND_MEDIA, {"method": "send_invoice", "file_id": "x"},
                         NOW, NOW)
        assert await qh.flush_due(DUE) == 1
        assert bot.messages == [] and bot.photos == [] and bot.documents == []
        assert await qh.queued_count() == 0

    asyncio.run(scenario())


def test_copy_kind_round_trip(tmp_path):
    _ready(tmp_path, "qh_kinds_copy.db")
    bot = _install_bot()

    async def scenario():
        await _quiet_all_day()
        sent_now = await qh.send_or_queue_copy(
            NOW, DELEGATE, sender=_never(), from_chat_id=777, message_id=12,
        )
        assert sent_now is False
        assert await qh.flush_due(DUE) == 1
        assert bot.copies == [{"chat_id": DELEGATE, "from_chat_id": 777,
                               "message_id": 12, "caption": None}]

    asyncio.run(scenario())


def test_poll_kind_round_trip_writes_checkpoint(tmp_path):
    """Отложенный опрос обязан дописать `poll_messages` — иначе ответ делегата некуда
    замапить, а `stop_poll` некуда послать."""
    _ready(tmp_path, "qh_kinds_poll.db")
    bot = _install_bot()

    async def scenario():
        await _quiet_all_day()
        poll_id = await db.create_poll(
            "Как вам форум?", ["Огонь", "Нормально"], is_anonymous=False,
            allows_multiple=False, created_by=1, city=None, audience=[],
            scheduled_at="2026-09-16 23:30:00",
        )
        sent_now = await qh.send_or_queue_poll(
            NOW, DELEGATE, sender=_never(), question="Как вам форум?",
            options=["Огонь", "Нормально"], is_anonymous=False, allows_multiple_answers=False,
            intro_text="Пара вопросов", poll_id=poll_id,
        )
        assert sent_now is False
        assert await qh.flush_due(DUE) == 1

        assert bot.messages[0]["text"] == "Пара вопросов"  # вступление перед опросом
        assert bot.polls == [{"chat_id": DELEGATE, "question": "Как вам форум?",
                              "options": ["Огонь", "Нормально"], "is_anonymous": False,
                              "allows_multiple_answers": False}]
        rows = await db.list_poll_messages(poll_id)
        assert [r["chat_id"] for r in rows] == [DELEGATE]
        assert rows[0]["message_id"] == 4242

    asyncio.run(scenario())


def test_every_send_or_queue_sends_immediately_when_toggle_off(tmp_path):
    """Инвариант 3 докстринга модуля для всей новой семьи: тумблер выключен -> шлём сразу,
    ни одной строки в очереди."""
    _ready(tmp_path, "qh_kinds_off.db")
    calls = []

    async def _sender():
        calls.append(1)

    async def scenario():
        assert await qh.send_or_queue_text(NOW, DELEGATE, "t", sender=_sender) is True
        assert await qh.send_or_queue_media(
            NOW, DELEGATE, sender=_sender, method="send_photo", file_id="F") is True
        assert await qh.send_or_queue_copy(
            NOW, DELEGATE, sender=_sender, from_chat_id=1, message_id=2) is True
        assert await qh.send_or_queue_poll(
            NOW, DELEGATE, sender=_sender, question="q", options=["a", "b"],
            is_anonymous=True, allows_multiple_answers=False) is True
        assert len(calls) == 4
        assert await qh.queued_count() == 0

    asyncio.run(scenario())


def test_send_or_queue_text_due_returns_delivery_moment(tmp_path):
    """Вариант для интерфейсов, которые показывают человеку «доставим утром в 09:00»."""
    _ready(tmp_path, "qh_kinds_due.db")

    async def scenario():
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        due = await qh.send_or_queue_text_due(NOW, DELEGATE, "t", sender=_never())
        assert due == datetime(2026, 9, 17, 9, 0)
        assert await qh.send_or_queue_text_due(
            datetime(2026, 9, 16, 12, 0), DELEGATE, "t",
            sender=lambda: asyncio.sleep(0),
        ) is None

    asyncio.run(scenario())


def test_flush_marks_row_sent_and_never_resends(tmp_path):
    _ready(tmp_path, "qh_kinds_once.db")
    bot = _install_bot()

    async def scenario():
        await _quiet_all_day()
        await qh.send_or_queue_copy(NOW, DELEGATE, sender=_never(), from_chat_id=1, message_id=2)
        await qh.send_or_queue_media(
            NOW, DELEGATE, sender=_never(), method="send_photo", file_id="F")
        assert await qh.flush_due(DUE) == 2
        assert await qh.flush_due(DUE + timedelta(days=1)) == 0
        assert len(bot.copies) == 1 and len(bot.photos) == 1

    asyncio.run(scenario())
