"""Доставка ответа менеджера на «❓ Задать вопрос» с записью результата — общий шов для
ответа reply'ем в чате (`handlers/admin.py::admin_reply_to_question`) и для журнала вопросов
(`handlers/admin_questions.py::aq_answer_step`). Вынесено из `handlers/admin.py` (потолок
размера модуля); сама отправка и тексты ошибок по-прежнему там (`_deliver_question_reply`,
`_reply_with_delivery_error`) — импортируются лениво, `handlers/admin.py` импортирует этот
модуль на уровне модуля.

10.10, три правила, которые держит `_attempt_question_delivery`:

1. Перед отправкой — атомарная отметка «ответ уходит» (`begin_question_delivery`). Без неё тот
   же менеджер, отправивший ответ второй раз, пока первый ещё летел, проходил проверку «захват
   мой, delivered_at пуст», и делегат получал две копии.
2. `set_question_answer` ставится СРАЗУ после отправки/постановки в очередь тихих часов, до
   подтверждения менеджеру и рассылки «кто ответил»: сбой на этих шагах не оставляет вопрос
   «в работе» при уже ушедшем ответе (повтор поставил бы вторую копию).
3. Ошибка ДО отправки снимает отметку «уходит» — повтор того же менеджера снова возможен
   (T-08-33 часть C); захват при этом не отпускается (принятое ограничение T-08-33).
"""
from __future__ import annotations

import logging

from aiogram import Bot, types

from database.db import (
    begin_question_delivery,
    get_question,
    release_question_delivery,
    set_question_answer,
)

logger = logging.getLogger(__name__)


async def _attempt_question_delivery(message: types.Message, bot: Bot, user_id: int, admin_name: str, qid: int):
    """Deliver + record, shared by the first-claim path and the C-variant same-person retry
    path -- both need identical delivery/error-handling behaviour."""
    from handlers.admin import _deliver_question_reply, _reply_with_delivery_error

    if not await begin_question_delivery(qid, message.from_user.id):
        row = await get_question(qid)
        if row and row.get("delivered_at"):
            await message.reply("✅ Ответ на этот вопрос уже отправлен — повторять не нужно.")
        else:
            await message.reply(
                "⏳ Ответ уже отправляется — дождитесь подтверждения, повторять не нужно."
            )
        return
    dispatched = False

    async def _record_answer():
        # D: delivered_at is stamped together with answer_text once the answer has been sent
        # or queued -- see set_question_answer's own docstring for why it can't be derived
        # from answer_text alone.
        nonlocal dispatched
        dispatched = True
        await set_question_answer(qid, message.html_text or message.text or "")

    try:
        await _deliver_question_reply(
            message, bot, user_id, admin_name, on_dispatched=_record_answer, question_id=qid,
        )
    except Exception as e:
        if dispatched:
            # Ответ уже у делегата или в очереди — упал хвост (подтверждение менеджеру,
            # рассылка «кто ответил»). Отметку не снимаем и «не удалось» не пишем: это было бы
            # неправдой, а повтор задвоил бы ответ.
            logger.error(f"Question {qid}: answer dispatched to user {user_id}, follow-up step failed: {e}")
            return
        # T-08-33 (accepted risk): the claim is NOT released here -- releasing it would let a
        # retry double-send to the delegate. Снимается только отметка «уходит».
        await release_question_delivery(qid)
        logger.error(f"Failed to send reply to user {user_id}: {e}")
        await _reply_with_delivery_error(message, e)
