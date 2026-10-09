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


def test_stale_delivering_mark_does_not_lock_question_forever(tmp_path):
    """Процесс упал посреди отправки — отметка осталась. Через несколько минут повтор снова
    разрешён, иначе вопрос заперт навсегда."""
    _ready(tmp_path, "qguard_stale.db")

    async def scenario():
        qid = await db.create_question(DELEGATE_ID, "Когда дедлайн?")
        assert await db.claim_question(qid, ADMIN_ID, "Админ")
        assert await db.begin_question_delivery(qid, ADMIN_ID) is True
        assert await db.begin_question_delivery(qid, ADMIN_ID) is False
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE delegate_questions SET delivering_at = '2020-01-01T00:00:00' WHERE id = ?",
                (qid,),
            )
            await conn.commit()
        assert await db.begin_question_delivery(qid, ADMIN_ID) is True
        # Чужой менеджер отметку не ставит никогда — захват не его.
        await db.release_question_delivery(qid)
        assert await db.begin_question_delivery(qid, ADMIN_ID + 1) is False

    asyncio.run(scenario())
