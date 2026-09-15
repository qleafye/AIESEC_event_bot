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


# ── B1: чек — подтверждение, бонус, отказ ─────────────────────────────────────────────────

ADMIN = 960001


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeBotChat:
    """Бот, доступный хендлеру через callback.bot / message.bot."""

    def __init__(self):
        self.sent = []
        self.documents = []
        self.photos = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    async def send_document(self, chat_id, file_id, caption=None, parse_mode=None):
        self.documents.append((chat_id, file_id, caption))

    async def send_photo(self, chat_id, file_id, caption=None, parse_mode=None):
        self.photos.append((chat_id, file_id, caption))


class _FakeMessage:
    def __init__(self, text=None, bot=None):
        self.text = text
        self.bot = bot or _FakeBotChat()
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        pass

    async def delete(self):
        pass


class _FakeCallback:
    def __init__(self, data, bot):
        self.data = data
        self.bot = bot
        self.from_user = _FakeUser(ADMIN)
        self.message = _FakeMessage(bot=bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


class _FakeState:
    def __init__(self):
        self._data = {}

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, state):
        pass


def _receipt_ready(tmp_path, name, monkeypatch):
    from handlers import admin_moderation
    import services.scheduler as sched

    _ready(tmp_path, name)
    config.ADMIN_IDS = [ADMIN]
    monkeypatch.setattr(sched, "cancel_payment_reminders", lambda user_id: None)

    async def _noop_card(target, state):
        pass

    monkeypatch.setattr(admin_moderation, "_show_current_receipt_card", _noop_card)
    return admin_moderation


async def _seed_payer():
    await db.add_user({
        "telegram_id": DELEGATE, "full_name": "Делегат", "registration_date": "2026-09-16",
        "participant_type": "full", "payment_status": "receipt_sent",
    })


def test_rcpt_confirm_in_quiet_hours_queues_text_menu_and_bonus(tmp_path, monkeypatch):
    """Подтверждение чека ночью: делегату не уходит НИЧЕГО, в очереди две строки — текст с
    главным меню и бонус-файл, — а менеджер видит приписку «увидит утром»."""
    admin_moderation = _receipt_ready(tmp_path, "qh_rcpt_confirm.db", monkeypatch)
    bot = _FakeBotChat()

    async def scenario():
        await _seed_payer()
        await _quiet_all_day()
        await db.set_setting("reg_bonus_enabled", "on")
        await db.set_setting("reg_bonus_doc_file_id", "BONUS-DOC")
        await db.set_setting("quiet_hours_manager_notice_text", "Делегат увидит в {time}.")

        callback = _FakeCallback(f"rcpt_confirm:{DELEGATE}", bot)
        await admin_moderation.rcpt_confirm(callback, _FakeState())

        assert bot.sent == [] and bot.documents == [] and bot.photos == []
        rows = await db.list_due_delayed_notifications("2030-01-01 00:00:00")
        kinds = [r["kind"] for r in rows]
        assert kinds == [qh.KIND_TEXT, qh.KIND_TEXT, qh.KIND_MEDIA], kinds
        # первая строка — «оплата подтверждена» вместе с главным меню (иначе утром приехал
        # бы текст без клавиатуры), третья — бонус-файл по file_id.
        assert rows[0]["payload"]["reply_markup"]["type"] == "reply"
        assert rows[2]["payload"]["file_id"] == "BONUS-DOC"
        assert any("увидит" in (a or "") for a in callback.answers), callback.answers
        assert (await db.get_user(DELEGATE))["payment_status"] == "paid"

    asyncio.run(scenario())


def test_rcpt_confirm_outside_quiet_hours_sends_immediately(tmp_path, monkeypatch):
    admin_moderation = _receipt_ready(tmp_path, "qh_rcpt_confirm_off.db", monkeypatch)
    bot = _FakeBotChat()

    async def scenario():
        await _seed_payer()
        await db.set_setting("reg_bonus_enabled", "on")
        await db.set_setting("reg_bonus_doc_file_id", "BONUS-DOC")

        callback = _FakeCallback(f"rcpt_confirm:{DELEGATE}", bot)
        await admin_moderation.rcpt_confirm(callback, _FakeState())

        assert [c[0] for c in bot.sent] == [DELEGATE, DELEGATE]  # «оплата подтверждена» + approve_text
        assert bot.sent[0][2] is not None  # главное меню приехало сразу
        assert bot.documents == [(DELEGATE, "BONUS-DOC", "🎁 Бонус за регистрацию!")]
        assert await qh.queued_count() == 0
        assert callback.answers == ["Оплата подтверждена"]

    asyncio.run(scenario())


def test_rcpt_reject_in_quiet_hours_queues_and_tells_manager(tmp_path, monkeypatch):
    admin_moderation = _receipt_ready(tmp_path, "qh_rcpt_reject.db", monkeypatch)
    bot = _FakeBotChat()

    async def scenario():
        await _seed_payer()
        await _quiet_all_day()
        await db.set_setting("quiet_hours_manager_notice_text", "Делегат увидит в {time}.")

        state = _FakeState()
        await state.update_data(rcpt_reject_uid=DELEGATE)
        message = _FakeMessage(text="чек нечитаемый", bot=bot)
        await admin_moderation.rcpt_reject_reason(message, state)

        assert bot.sent == []
        rows = await db.list_due_delayed_notifications("2030-01-01 00:00:00")
        assert len(rows) == 1 and rows[0]["kind"] == qh.KIND_TEXT
        assert "Чек отклонён" in rows[0]["payload"]["text"]
        assert any("увидит" in a for a in message.answers), message.answers

    asyncio.run(scenario())


def test_rcpt_reject_outside_quiet_hours_sends_immediately(tmp_path, monkeypatch):
    admin_moderation = _receipt_ready(tmp_path, "qh_rcpt_reject_off.db", monkeypatch)
    bot = _FakeBotChat()

    async def scenario():
        await _seed_payer()
        state = _FakeState()
        await state.update_data(rcpt_reject_uid=DELEGATE)
        message = _FakeMessage(text="чек нечитаемый", bot=bot)
        await admin_moderation.rcpt_reject_reason(message, state)

        assert len(bot.sent) == 1 and "Чек отклонён" in bot.sent[0][1]
        assert await qh.queued_count() == 0
        assert message.answers == ["Готово."]

    asyncio.run(scenario())


# ── B2: ответ организаторов на «Задать вопрос» ────────────────────────────────────────────

class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _AdminReplyMessage(_FakeMessage):
    """Сообщение менеджера-ответа: текстовое (`text`/`html_text`) либо любое другое
    (`send_copy` — ровно то, что делает `_deliver_question_reply` для голосового/фото)."""

    def __init__(self, text=None, bot=None):
        super().__init__(text=text, bot=bot)
        self.html_text = text
        self.chat = _FakeChat(ADMIN)
        self.message_id = 55
        self.from_user = _FakeUser(ADMIN)
        self.replies = []
        self.copies = []

    async def reply(self, text, **kw):
        self.replies.append(text)

    async def send_copy(self, chat_id):
        self.copies.append(chat_id)


async def _no_fanout(*a, **kw):
    """«Кто ответил» остальным держателям moderate_reg — не предмет этого теста."""


def test_question_reply_text_in_quiet_hours_queues(tmp_path, monkeypatch):
    from handlers import admin as admin_mod

    _ready(tmp_path, "qh_qreply_text.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)
    bot = _FakeBotChat()

    async def scenario():
        await _quiet_all_day()
        await db.set_setting("quiet_hours_manager_notice_text", "Делегат увидит в {time}.")
        message = _AdminReplyMessage(text="Ответ такой", bot=bot)
        await admin_mod._deliver_question_reply(message, bot, DELEGATE, "Менеджер")

        assert bot.sent == [] and message.copies == []
        rows = await db.list_due_delayed_notifications("2030-01-01 00:00:00")
        assert [r["kind"] for r in rows] == [qh.KIND_TEXT]
        assert "Ответ такой" in rows[0]["payload"]["text"]
        assert "увидит" in message.replies[0], message.replies

    asyncio.run(scenario())


def test_question_reply_non_text_in_quiet_hours_queues_copy(tmp_path, monkeypatch):
    """Голосовое/фото менеджера уходит утром копией того же сообщения, а не теряется."""
    from handlers import admin as admin_mod

    _ready(tmp_path, "qh_qreply_copy.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)
    bot = _FakeBotChat()

    async def scenario():
        await _quiet_all_day()
        message = _AdminReplyMessage(text=None, bot=bot)
        await admin_mod._deliver_question_reply(message, bot, DELEGATE, "Менеджер")

        assert bot.sent == [] and message.copies == []
        rows = await db.list_due_delayed_notifications("2030-01-01 00:00:00")
        assert [r["kind"] for r in rows] == [qh.KIND_TEXT, qh.KIND_COPY]
        assert rows[1]["payload"] == {"from_chat_id": ADMIN, "message_id": 55, "caption": None}

        flush_bot = _install_bot()
        assert await qh.flush_due(DUE) == 2
        assert flush_bot.copies == [{"chat_id": DELEGATE, "from_chat_id": ADMIN,
                                     "message_id": 55, "caption": None}]

    asyncio.run(scenario())


def test_question_reply_outside_quiet_hours_sends_immediately(tmp_path, monkeypatch):
    from handlers import admin as admin_mod

    _ready(tmp_path, "qh_qreply_off.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)
    bot = _FakeBotChat()

    async def scenario():
        message = _AdminReplyMessage(text=None, bot=bot)
        await admin_mod._deliver_question_reply(message, bot, DELEGATE, "Менеджер")
        assert len(bot.sent) == 1 and message.copies == [DELEGATE]
        assert await qh.queued_count() == 0
        assert message.replies == ["✅ Ответ отправлен пользователю."]

    asyncio.run(scenario())


# ── B3: опросы ────────────────────────────────────────────────────────────────────────────

class _PollBot:
    def __init__(self):
        self.polls = []
        self.messages = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.messages.append((chat_id, text))

    async def send_poll(self, chat_id, question, options, is_anonymous=False,
                        allows_multiple_answers=False, is_closed=False, **kw):
        from types import SimpleNamespace
        self.polls.append(chat_id)
        return SimpleNamespace(message_id=1000 + chat_id, poll=SimpleNamespace(id=f"tg{chat_id}"))


async def _seed_two_delegates():
    for tid in (DELEGATE, DELEGATE + 1):
        await db.add_user({"telegram_id": tid, "full_name": f"Д{tid}",
                           "registration_date": "2026-09-16"})


def test_deliver_poll_in_quiet_hours_queues_per_recipient(tmp_path):
    """Каждому делегату уходит СВОЙ send_poll — значит и окно тишины считается по нему."""
    from services import polls as polls_svc

    _ready(tmp_path, "qh_poll_deliver.db")
    bot = _PollBot()

    async def scenario():
        await _seed_two_delegates()
        await _quiet_all_day()
        await db.set_setting("poll_intro_text", "Пара вопросов 👇")
        poll_id = await db.create_poll(
            "Как форум?", ["Огонь", "Норм"], is_anonymous=False, allows_multiple=False,
            created_by=ADMIN, city=None, audience=[], scheduled_at="2026-09-16 23:30:00",
        )
        stats = await polls_svc.deliver_poll(bot, poll_id)
        assert stats["sent"] == 0 and stats["queued"] == 2
        assert bot.polls == [] and bot.messages == []
        assert await qh.queued_count() == 2

        # Утром: опрос уходит ВМЕСТЕ со вступлением, чекпоинт poll_messages записан.
        flush_bot = _install_bot()
        assert await qh.flush_due(DUE) == 2
        assert len(flush_bot.polls) == 2
        assert [m["text"] for m in flush_bot.messages] == ["Пара вопросов 👇"] * 2
        assert {r["chat_id"] for r in await db.list_poll_messages(poll_id)} == {
            DELEGATE, DELEGATE + 1}

    asyncio.run(scenario())


def test_deliver_poll_outside_quiet_hours_unchanged(tmp_path):
    from services import polls as polls_svc

    _ready(tmp_path, "qh_poll_deliver_off.db")
    bot = _PollBot()

    async def scenario():
        await _seed_two_delegates()
        poll_id = await db.create_poll(
            "Как форум?", ["Огонь", "Норм"], is_anonymous=False, allows_multiple=False,
            created_by=ADMIN, city=None, audience=[], scheduled_at="2026-09-16 12:00:00",
        )
        stats = await polls_svc.deliver_poll(bot, poll_id)
        assert stats["sent"] == 2 and stats["queued"] == 0
        assert sorted(bot.polls) == [DELEGATE, DELEGATE + 1]
        assert await qh.queued_count() == 0

    asyncio.run(scenario())


def test_poll_wizard_confirm_screen_warns_about_quiet_hours(tmp_path):
    from handlers import admin_poll_wizard as wiz

    _ready(tmp_path, "qh_poll_wizard.db")

    async def scenario():
        assert await wiz._quiet_hours_warning() == ""  # тумблер выключен — экран прежний
        await _quiet_all_day()
        warning = await wiz._quiet_hours_warning()
        assert "🌙" in warning and "тихие часы" in warning
        assert "утром" in warning, warning

    asyncio.run(scenario())


# ── B4: ручные монеты из Mini App ─────────────────────────────────────────────────────────

def test_manual_coins_text_is_one_function_for_both_paths():
    """Формулировка живёт в одном месте: `handlers/admin.py` реэкспортирует ту же функцию,
    что зовёт разборщик outbox'а."""
    from handlers import admin as admin_mod
    from services import coins_notify, miniapp_outbox

    assert admin_mod._notify_manual_coins is coins_notify.notify_manual_coins
    assert miniapp_outbox.notify_manual_coins is coins_notify.notify_manual_coins


def _drain_coins_row(monkeypatch, payload):
    """Ставит строку `coins_manual` в outbox и разбирает её ботом-запоминалкой."""
    from services import miniapp_outbox

    monkeypatch.setattr(miniapp_outbox, "request_resync", lambda *a, **kw: None)
    bot = _FakeBotChat()

    async def scenario():
        await db.enqueue_miniapp_outbox("coins_manual", payload, "2026-09-16 23:30:00")
        await miniapp_outbox.drain(bot)

    return bot, scenario


def test_manual_coins_from_miniapp_notifies_delegate(tmp_path, monkeypatch):
    """Регрессия 16.09: до правки ручные монеты из приложения делегату не приходили вовсе —
    ветка умела только пересборку вкладок."""
    _ready(tmp_path, "qh_coins_outbox.db")
    bot, scenario = _drain_coins_row(
        monkeypatch, {"user_id": DELEGATE, "delta": 7, "reason": "помог(ла) команде", "balance": 42},
    )
    asyncio.run(scenario())

    assert len(bot.sent) == 1, bot.sent
    chat_id, text, _markup = bot.sent[0]
    assert chat_id == DELEGATE
    assert "+7" in text and "помог" in text and "42" in text


def test_manual_coins_from_miniapp_respects_quiet_hours(tmp_path, monkeypatch):
    _ready(tmp_path, "qh_coins_outbox_quiet.db")
    asyncio.run(_quiet_all_day())
    bot, scenario = _drain_coins_row(
        monkeypatch, {"user_id": DELEGATE, "delta": -3, "reason": "опоздание", "balance": 5},
    )
    asyncio.run(scenario())

    assert bot.sent == []
    rows = asyncio.run(db.list_due_delayed_notifications("2030-01-01 00:00:00"))
    assert [r["kind"] for r in rows] == [qh.KIND_TEXT]
    assert "-3" in rows[0]["payload"]["text"]


def test_manual_coins_row_of_old_shape_reads_balance_from_db(tmp_path, monkeypatch):
    """Строки, поставленные ДО 16.09, не несут reason/balance — ретраить их не нужно,
    баланс дочитывается из журнала монет."""
    _ready(tmp_path, "qh_coins_outbox_legacy.db")

    async def seed():
        await db.add_user({"telegram_id": DELEGATE, "full_name": "Д",
                           "registration_date": "2026-09-16"})
        await db.add_coins(DELEGATE, 11, reason="старое", changed_by=ADMIN, source="manual")

    asyncio.run(seed())
    bot, scenario = _drain_coins_row(monkeypatch, {"user_id": DELEGATE, "delta": 11})
    asyncio.run(scenario())

    assert len(bot.sent) == 1
    assert "11" in bot.sent[0][1]
