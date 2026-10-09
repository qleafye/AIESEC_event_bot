"""10.10: ответ менеджера на «❓ Задать вопрос» — три дыры доставки.

1. Тот же менеджер дважды отправил ответ, пока первый ещё уходит: оба проходили проверку
   «захват мой, delivered_at пуст» и делегат получал две копии. Теперь перед отправкой
   ставится атомарная отметка `delivering_at`; второй повтор слышит «ответ уже отправляется».
2. Тихие часы + исключение ПОСЛЕ постановки в очередь (например, `message.reply` менеджеру):
   `set_question_answer` не вызывался, вопрос оставался «в работе» при уже стоящем в очереди
   тексте, повтор ставил второй. Теперь отметка ставится сразу после постановки/отправки.
3. Утренняя отправка из очереди упала: вопрос числился «отвечен», делегат ответа не получил.
   Теперь вопрос возвращается «в работу» (текст ответа сохраняется), автор ответа получает
   сообщение «Не удалось доставить ответ на вопрос #N — …».

pytest-asyncio в окружении нет — async через `asyncio.run()`, `config.DB_PATH` в `tmp_path`.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from config import config
from database import db
from handlers import admin as admin_mod
import services.quiet_hours as qh
from tests._dbtpl import fast_init_db

ADMIN_ID = 910101
DELEGATE_ID = 910102


def _ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _after_quiet_window():
    from services.timeutil import msk_now
    return msk_now() + timedelta(days=2)


async def _quiet_all_day():
    await db.set_setting("quiet_hours_enabled", "on")
    await db.set_setting("quiet_hours_start", "00:00")
    await db.set_setting("quiet_hours_end", "23:59")


class _User:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = None


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _Replied:
    def __init__(self, text):
        self.text = text


def _notification_text(qid):
    return (
        f"❓ Новый вопрос от ID: {DELEGATE_ID}:\n"
        f"🆔 {DELEGATE_ID}\n"
        f"🧾 Вопрос #{qid}\n\n"
        f"Когда дедлайн?\n\n"
        f"↩️ Ответьте reply'ем на это сообщение, чтобы отправить ответ."
    )


class _AdminMessage:
    """Ответ менеджера reply'ем на уведомление о вопросе. `reply_fail_times` — сколько первых
    `reply` бросят исключение (имитация сбоя уже ПОСЛЕ отправки/постановки в очередь)."""

    def __init__(self, text, qid, reply_fail_times=0):
        self.text = text
        self.html_text = text
        self.from_user = _User(ADMIN_ID)
        self.chat = _Chat(ADMIN_ID)
        self.message_id = 77
        self.reply_to_message = _Replied(_notification_text(qid))
        self.replies = []
        self._reply_fail_times = reply_fail_times

    async def reply(self, text, **kw):
        if self._reply_fail_times > 0:
            self._reply_fail_times -= 1
            raise RuntimeError("сеть моргнула на ответе менеджеру")
        self.replies.append(text)

    async def answer(self, text, **kw):
        self.replies.append(text)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class _GatedBot(_Bot):
    """Первая отправка делегату «висит», пока тест не откроет ворота — окно, в которое
    второй повтор того же менеджера раньше проскакивал."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self._first = True

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id == DELEGATE_ID and self._first:
            self._first = False
            self.started.set()
            await self.gate.wait()
        self.sent.append((chat_id, text))


async def _no_fanout(*a, **kw):
    pass


def _delegate_msgs(bot):
    return [t for cid, t in bot.sent if cid == DELEGATE_ID]


# ── 1: повтор того же менеджера, пока первая отправка ещё идёт ───────────────────────────

def test_same_manager_double_send_while_first_in_flight_delivers_once(tmp_path, monkeypatch):
    _ready(tmp_path, "qguard_double.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        bot = _GatedBot()
        first = _AdminMessage("Дедлайн 1 сентября", qid)
        second = _AdminMessage("Дедлайн 1 сентября", qid)

        task = asyncio.create_task(admin_mod.admin_reply_to_question(first, bot))
        await bot.started.wait()
        await admin_mod.admin_reply_to_question(second, bot)
        bot.gate.set()
        await task

        assert len(_delegate_msgs(bot)) == 1, bot.sent
        assert any("уже отправляется" in t for t in second.replies), second.replies
        assert "✅ Ответ отправлен пользователю." in first.replies
        row = await db.get_question(qid)
        assert row["delivered_at"] is not None

    asyncio.run(scenario())


def test_failed_send_releases_mark_so_own_retry_still_works(tmp_path, monkeypatch):
    """Отметка «отправляется» не должна запирать вопрос навсегда: временная ошибка до
    отправки снимает её, и повтор того же менеджера проходит (T-08-33 часть C не ломается)."""
    _ready(tmp_path, "qguard_release.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    class _FailOnce(_Bot):
        def __init__(self):
            super().__init__()
            self.fails = 1

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            if chat_id == DELEGATE_ID and self.fails:
                self.fails -= 1
                raise TimeoutError("network blip")
            self.sent.append((chat_id, text))

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        bot = _FailOnce()
        m1 = _AdminMessage("Ответ", qid)
        await admin_mod.admin_reply_to_question(m1, bot)
        assert any("попробовать ещё раз" in t for t in m1.replies), m1.replies
        m2 = _AdminMessage("Ответ (повтор)", qid)
        await admin_mod.admin_reply_to_question(m2, bot)
        assert "✅ Ответ отправлен пользователю." in m2.replies, m2.replies
        assert len(_delegate_msgs(bot)) == 1

    asyncio.run(scenario())


async def _age_mark(qid):
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE delegate_questions SET delivering_at = '2020-01-01T00:00:00' WHERE id = ?",
            (qid,),
        )
        await conn.commit()


def test_stale_delivering_mark_does_not_lock_question_forever(tmp_path):
    """Процесс упал посреди отправки — отметка осталась. Через несколько минут повтор снова
    разрешён, иначе вопрос заперт навсегда."""
    _ready(tmp_path, "qguard_stale.db")

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        assert await db.claim_question(qid, ADMIN_ID, "Админ")
        assert await db.begin_question_delivery(qid, ADMIN_ID) is not None
        assert await db.begin_question_delivery(qid, ADMIN_ID) is None
        await _age_mark(qid)
        assert await db.begin_question_delivery(qid, ADMIN_ID) is not None
        # Чужой менеджер отметку не ставит никогда — захват не его.
        await _age_mark(qid)
        assert await db.begin_question_delivery(qid, ADMIN_ID + 1) is None

    asyncio.run(scenario())


def test_slow_first_holder_cannot_release_mark_taken_over_by_retry(tmp_path):
    """Ревью 10.10: первая попытка зависла, отметку перехватили по давности; потом первая
    попытка всё-таки вернулась с ошибкой — снять она может только СВОЮ отметку, не чужую."""
    _ready(tmp_path, "qguard_token.db")

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        assert await db.claim_question(qid, ADMIN_ID, "Админ")
        old = await db.begin_question_delivery(qid, ADMIN_ID)
        await _age_mark(qid)
        new = await db.begin_question_delivery(qid, ADMIN_ID)
        assert new and new != old
        assert await db.release_question_delivery(qid, old) is False
        assert await db.begin_question_delivery(qid, ADMIN_ID) is None, "свежая отметка снята чужим токеном"
        assert (await db.get_question(qid))["delivery_token"] == new

    asyncio.run(scenario())


def test_stale_takeover_forbidden_once_something_reached_delegate(tmp_path):
    """Ревью 10.10: если до делегата уже что-то дошло (dispatched_at), перехват по давности
    запрещён — повтор продублировал бы дошедшее; снять отметку тоже нельзя."""
    _ready(tmp_path, "qguard_dispatched.db")

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        assert await db.claim_question(qid, ADMIN_ID, "Админ")
        token = await db.begin_question_delivery(qid, ADMIN_ID)
        await db.mark_question_dispatched(qid, token)
        await _age_mark(qid)
        assert await db.begin_question_delivery(qid, ADMIN_ID) is None
        assert await db.release_question_delivery(qid, token) is False
        assert await db.begin_question_delivery(qid, ADMIN_ID) is None

    asyncio.run(scenario())


def test_non_text_answer_header_sent_copy_failed_blocks_retry(tmp_path, monkeypatch):
    """Ревью 10.10: не-текстовый ответ вне тихих часов — заголовок дошёл, копия упала.
    Отметку не снимаем (повтор прислал бы второй заголовок), менеджеру — честно, что дошло;
    после уведомления вопрос помечается отвеченным (не висит «отправляется» в списках)."""
    _ready(tmp_path, "qguard_header.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    class _VoiceFail(_AdminMessage):
        async def send_copy(self, chat_id):
            raise TimeoutError("network blip")

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        bot = _Bot()
        m1 = _VoiceFail(None, qid)
        m1.text = m1.html_text = None
        await admin_mod.admin_reply_to_question(m1, bot)
        assert len(_delegate_msgs(bot)) == 1  # заголовок
        assert any("только заголовок" in t for t in m1.replies), m1.replies
        assert not any("попробовать ещё раз" in t for t in m1.replies)
        row = await db.get_question(qid)
        assert row["dispatched_at"] is not None and row["delivered_at"] is not None
        from services.questions import question_status
        assert question_status(row) == "answered"

        m2 = _VoiceFail(None, qid)
        m2.text = m2.html_text = None
        await admin_mod.admin_reply_to_question(m2, bot)
        assert len(_delegate_msgs(bot)) == 1, "второй заголовок делегату"
        assert any("уже" in t for t in m2.replies), m2.replies

    asyncio.run(scenario())


def test_record_failure_after_send_is_retried_then_answered(tmp_path, monkeypatch):
    """Ревью 10.10: ответ дошёл, запись в БД упала один раз — повтор записи, вопрос отвечен,
    менеджеру — успех, а не «не удалось»."""
    import services.questions as qs

    _ready(tmp_path, "qguard_record_retry.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)
    monkeypatch.setattr(qs, "RECORD_ANSWER_PAUSE", 0)
    real = db.set_question_answer
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return await real(*a, **kw)

    monkeypatch.setattr(db, "set_question_answer", flaky)

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        bot = _Bot()
        m = _AdminMessage("Ответ", qid)
        await admin_mod.admin_reply_to_question(m, bot)
        assert "✅ Ответ отправлен пользователю." in m.replies, m.replies
        assert (await db.get_question(qid))["delivered_at"] is not None
        assert len(_delegate_msgs(bot)) == 1

    asyncio.run(scenario())


def test_record_failure_persistent_keeps_sending_and_never_resends(tmp_path, monkeypatch, caplog):
    """Запись не удалась и после повторов: ERROR в лог, вопрос остаётся «отправляется»,
    повтор менеджера второй копии делегату не шлёт."""
    import logging
    import services.questions as qs

    _ready(tmp_path, "qguard_record_fail.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)
    monkeypatch.setattr(qs, "RECORD_ANSWER_PAUSE", 0)

    async def broken(*a, **kw):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(db, "set_question_answer", broken)

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        bot = _Bot()
        m1 = _AdminMessage("Ответ", qid)
        with caplog.at_level(logging.ERROR):
            await admin_mod.admin_reply_to_question(m1, bot)
        assert any("записать его не удалось" in r.getMessage() for r in caplog.records)
        assert not any("Не удалось" in t or "❌" in t for t in m1.replies), m1.replies
        row = await db.get_question(qid)
        assert row["delivered_at"] is None and row["dispatched_at"] is not None
        await _age_mark(qid)
        m2 = _AdminMessage("Ответ", qid)
        await admin_mod.admin_reply_to_question(m2, bot)
        assert len(_delegate_msgs(bot)) == 1
        assert any("уже отправляется" in t for t in m2.replies), m2.replies

    asyncio.run(scenario())


def test_send_timeout_releases_mark_and_says_retry(tmp_path, monkeypatch):
    """Ревью 10.10: явный потолок отправки — зависшая отправка обрывается и не держит отметку."""
    _ready(tmp_path, "qguard_timeout.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)
    monkeypatch.setattr(db, "QUESTION_SEND_TIMEOUT", 0.05)

    class _Hang(_Bot):
        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            if chat_id == DELEGATE_ID:
                await asyncio.sleep(5)
            self.sent.append((chat_id, text))

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        m = _AdminMessage("Ответ", qid)
        await admin_mod.admin_reply_to_question(m, _Hang())
        assert any("попробовать ещё раз" in t for t in m.replies), m.replies
        assert (await db.get_question(qid))["delivery_token"] is None

    asyncio.run(scenario())


# ── 2: тихие часы + сбой после постановки в очередь ──────────────────────────────────────

def test_quiet_hours_failure_after_enqueue_marks_question_and_retry_does_not_queue_twice(
    tmp_path, monkeypatch,
):
    _ready(tmp_path, "qguard_queue.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    async def scenario():
        await _quiet_all_day()
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        bot = _Bot()

        m1 = _AdminMessage("Ответ ночью", qid, reply_fail_times=1)
        await admin_mod.admin_reply_to_question(m1, bot)

        assert await qh.queued_count() == 1
        row = await db.get_question(qid)
        assert row["delivered_at"] is not None, "ответ уже в очереди — вопрос не «в работе»"
        assert row["answer_text"] == "Ответ ночью"

        m2 = _AdminMessage("Ответ ночью", qid)
        await admin_mod.admin_reply_to_question(m2, bot)
        assert await qh.queued_count() == 1, "повтор поставил вторую копию в очередь"
        assert bot.sent == []

    asyncio.run(scenario())


# ── 3: утренняя отправка из очереди упала ────────────────────────────────────────────────

class _FlushBot:
    def __init__(self, exc):
        self.exc = exc
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id == DELEGATE_ID:
            raise self.exc
        self.sent.append((chat_id, text))

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None):
        if chat_id == DELEGATE_ID:
            raise self.exc


async def _queue_answer(qid, text="Ответ ночью"):
    await _quiet_all_day()
    m = _AdminMessage(text, qid)
    await admin_mod.admin_reply_to_question(m, _Bot())
    assert await qh.queued_count() >= 1


def _install(bot):
    from services import scheduler as sched
    sched._bot = bot


def test_flush_failure_blocked_reopens_question_and_tells_author(tmp_path, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError

    _ready(tmp_path, "qguard_flush_block.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        await _queue_answer(qid)

        bot = _FlushBot(TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user"))
        _install(bot)
        await qh.flush_due(_after_quiet_window())

        row = await db.get_question(qid)
        assert row["delivered_at"] is None
        assert row["answered_by"] == ADMIN_ID
        assert row["answer_text"] == "Ответ ночью"

        to_author = [t for cid, t in bot.sent if cid == ADMIN_ID]
        assert len(to_author) == 1, bot.sent
        assert f"Не удалось доставить ответ на вопрос #{qid}" in to_author[0]
        assert "заблокировал бота" in to_author[0]

    asyncio.run(scenario())


def test_flush_failure_transient_says_unavailable_and_allows_retry(tmp_path, monkeypatch):
    _ready(tmp_path, "qguard_flush_net.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        await _queue_answer(qid)

        bot = _FlushBot(TimeoutError("network blip"))
        _install(bot)
        await qh.flush_due(_after_quiet_window())

        row = await db.get_question(qid)
        assert row["delivered_at"] is None
        to_author = [t for cid, t in bot.sent if cid == ADMIN_ID]
        assert len(to_author) == 1, bot.sent
        assert f"Не удалось доставить ответ на вопрос #{qid}" in to_author[0]
        assert "недоступен" in to_author[0]
        assert "заблокировал" not in to_author[0]

        # Вопрос снова «в работе» — тот же менеджер может отправить ответ ещё раз.
        await db.set_setting("quiet_hours_enabled", "off")
        m = _AdminMessage("Ответ ещё раз", qid)
        ok_bot = _Bot()
        await admin_mod.admin_reply_to_question(m, ok_bot)
        assert len(_delegate_msgs(ok_bot)) == 1
        assert (await db.get_question(qid))["delivered_at"] is not None

    asyncio.run(scenario())


def test_flush_failure_of_non_text_answer_notifies_author_once(tmp_path, monkeypatch):
    """Не-текстовый ответ — две строки очереди (шапка + копия). Обе упали — автору одно
    сообщение, не два."""
    _ready(tmp_path, "qguard_flush_copy.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    class _VoiceMessage(_AdminMessage):
        async def send_copy(self, chat_id):
            raise AssertionError("в тихие часы — только очередь")

    async def scenario():
        await _quiet_all_day()
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        m = _VoiceMessage(None, qid)
        m.text = None
        m.html_text = None
        await admin_mod.admin_reply_to_question(m, _Bot())
        assert await qh.queued_count() == 2

        bot = _FlushBot(TimeoutError("network blip"))
        _install(bot)
        await qh.flush_due(_after_quiet_window())

        assert (await db.get_question(qid))["delivered_at"] is None
        assert len([1 for cid, _t in bot.sent if cid == ADMIN_ID]) == 1, bot.sent

    asyncio.run(scenario())


def test_flush_success_keeps_question_answered(tmp_path, monkeypatch):
    _ready(tmp_path, "qguard_flush_ok.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        await _queue_answer(qid)
        bot = _Bot()
        _install(bot)
        await qh.flush_due(_after_quiet_window())
        assert len(_delegate_msgs(bot)) == 1
        assert [t for cid, t in bot.sent if cid == ADMIN_ID] == []
        assert (await db.get_question(qid))["delivered_at"] is not None

    asyncio.run(scenario())


def test_flush_failure_of_old_row_after_new_answer_touches_nothing(tmp_path, monkeypatch):
    """Ревью 10.10: старая строка очереди (прежняя попытка) упала уже после того, как менеджер
    ответил заново — вопрос остаётся отвеченным, автору ничего не пишем."""
    _ready(tmp_path, "qguard_flush_stale.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        await _queue_answer(qid, "Старый ответ")
        old_token = (await db.get_question(qid))["delivery_token"]
        assert await db.reopen_question_delivery(qid, old_token)

        await db.set_setting("quiet_hours_enabled", "off")
        ok_bot = _Bot()
        await admin_mod.admin_reply_to_question(_AdminMessage("Новый ответ", qid), ok_bot)
        assert len(_delegate_msgs(ok_bot)) == 1
        delivered = (await db.get_question(qid))["delivered_at"]
        assert delivered is not None

        bot = _FlushBot(TimeoutError("network blip"))
        _install(bot)
        await qh.flush_due(_after_quiet_window())
        row = await db.get_question(qid)
        assert row["delivered_at"] == delivered
        assert [t for cid, t in bot.sent if cid == ADMIN_ID] == []

    asyncio.run(scenario())


def _queued_voice(qid):
    class _Voice(_AdminMessage):
        async def send_copy(self, chat_id):
            raise AssertionError("в тихие часы — только очередь")

    m = _Voice(None, qid)
    m.text = m.html_text = None
    return m


def test_flush_copy_source_deleted_says_resend_as_text(tmp_path, monkeypatch):
    """Ревью 10.10: менеджер удалил исходное голосовое — копия утром не находит сообщение."""
    from aiogram.exceptions import TelegramBadRequest

    _ready(tmp_path, "qguard_flush_gone.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    class _CopyGoneBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.sent.append((chat_id, text))

        async def copy_message(self, chat_id, from_chat_id, message_id, caption=None):
            raise TelegramBadRequest(method=None, message="Bad Request: message to copy not found")

    async def scenario():
        await _quiet_all_day()
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        await admin_mod.admin_reply_to_question(_queued_voice(qid), _Bot())
        bot = _CopyGoneBot()
        _install(bot)
        await qh.flush_due(_after_quiet_window())

        to_author = [t for cid, t in bot.sent if cid == ADMIN_ID]
        assert len(to_author) == 1, bot.sent
        assert "исходное сообщение удалено" in to_author[0]
        assert "заново текстом" in to_author[0]
        # Заголовок ушёл, копия — нет: так и сказано.
        assert "только заголовок" in to_author[0]
        assert (await db.get_question(qid))["delivered_at"] is None

    asyncio.run(scenario())


def test_flush_header_sent_copy_failed_says_so(tmp_path, monkeypatch):
    _ready(tmp_path, "qguard_flush_half.db")
    monkeypatch.setattr(admin_mod, "_notify_other_moderate_reg_holders", _no_fanout)

    class _CopyFailBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.sent.append((chat_id, text))

        async def copy_message(self, chat_id, from_chat_id, message_id, caption=None):
            raise TimeoutError("network blip")

    async def scenario():
        await _quiet_all_day()
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        await admin_mod.admin_reply_to_question(_queued_voice(qid), _Bot())
        bot = _CopyFailBot()
        _install(bot)
        await qh.flush_due(_after_quiet_window())

        to_author = [t for cid, t in bot.sent if cid == ADMIN_ID]
        assert len(to_author) == 1, bot.sent
        assert "только заголовок" in to_author[0]
        assert "удалено" not in to_author[0]

    asyncio.run(scenario())
