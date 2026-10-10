"""Доставка ответа менеджера на «❓ Задать вопрос» с записью результата — общий шов для
ответа reply'ем в чате (`handlers/admin.py::admin_reply_to_question`) и для журнала вопросов
(`handlers/comms/admin_questions.py::aq_answer_step`). Вынесено из `handlers/admin.py` (потолок
размера модуля); сама отправка и тексты ошибок по-прежнему там (`_deliver_question_reply`,
`_reply_with_delivery_error`) — импортируются лениво, `handlers/admin.py` импортирует этот
модуль на уровне модуля.

10.10, правила попытки отправки (`_attempt_question_delivery`):

1. Перед отправкой — атомарная отметка «ответ уходит» с ТОКЕНОМ попытки
   (`begin_question_delivery`). Без неё тот же менеджер, отправивший ответ второй раз, пока
   первый ещё летел, проходил проверку «захват мой, delivered_at пуст», и делегат получал две
   копии. Все дальнейшие шаги — только по своему токену.
2. Сразу после ПЕРВОЙ успешной отправки (у не-текстового ответа — заголовка) ставится
   `dispatched_at`: с ней попытку не снимает ни ошибка, ни перехват по давности — иначе повтор
   продублировал бы уже дошедшее.
3. Ответ записывается (`services.questions.record_answer`, с повтором) СРАЗУ после отправки или
   постановки в очередь, до подтверждения менеджеру и рассылки «кто ответил». Не записалось и
   после повторов — ERROR в лог, вопрос остаётся «отправляется», повтора не будет.
4. Ошибка ДО первой отправки снимает отметку — повтор того же менеджера снова возможен
   (T-08-33 часть C); захват при этом не отпускается (принятое ограничение T-08-33).
"""
from __future__ import annotations

import logging

from aiogram import Bot, types
from aiogram.exceptions import TelegramForbiddenError

from database.db import (
    begin_question_delivery,
    get_question,
    mark_question_dispatched,
    release_question_delivery,
)

logger = logging.getLogger(__name__)


async def _attempt_question_delivery(message: types.Message, bot: Bot, user_id: int, admin_name: str, qid: int):
    """Deliver + record, shared by the first-claim path and the C-variant same-person retry
    path -- both need identical delivery/error-handling behaviour."""
    from handlers.admin import _deliver_question_reply, _reply_with_delivery_error
    from services.questions import record_answer

    token = await begin_question_delivery(qid, message.from_user.id)
    if token is None:
        row = await get_question(qid)
        if row and row.get("delivered_at"):
            await message.reply("✅ Ответ на этот вопрос уже отправлен — повторять не нужно.")
        else:
            await message.reply(
                "⏳ Ответ уже отправляется — дождитесь подтверждения, повторять не нужно."
            )
        return
    state = {"part_sent": False, "dispatched": False}

    async def _part_sent():
        state["part_sent"] = True
        await mark_question_dispatched(qid, token)

    async def _dispatched():
        # D: delivered_at is stamped together with answer_text once the answer has been sent
        # or queued -- see set_question_answer's own docstring for why it can't be derived
        # from answer_text alone.
        state["part_sent"] = state["dispatched"] = True
        try:
            await mark_question_dispatched(qid, token)
        except Exception as e:
            logger.error(f"Question {qid}: не удалось поставить отметку «дошло»: {e}")
        await record_answer(qid, message.html_text or message.text or "")

    try:
        await _deliver_question_reply(
            message, bot, user_id, admin_name, on_dispatched=_dispatched, on_part_sent=_part_sent,
            question_ref={"question_id": qid, "question_token": token},
        )
    except Exception as e:
        if state["dispatched"]:
            # Ответ уже у делегата или в очереди — упал хвост (подтверждение менеджеру,
            # рассылка «кто ответил»). «Не удалось» не пишем: это было бы неправдой.
            logger.error(f"Question {qid}: answer dispatched to user {user_id}, follow-up step failed: {e}")
            return
        if state["part_sent"]:
            # Заголовок не-текстового ответа дошёл, копия — нет. Отметку не снимаем: повтор
            # прислал бы делегату второй заголовок. После уведомления менеджеру вопрос
            # помечается отвеченным — «отправляется навсегда» в списках путало бы менеджеров;
            # дальше менеджер пишет делегату напрямую.
            logger.error(f"Question {qid}: header delivered to user {user_id}, copy failed: {e}")
            reason = (
                "делегат заблокировал бота" if isinstance(e, TelegramForbiddenError)
                else "ошибка Telegram"
            )
            try:
                await message.reply(
                    "⚠️ Делегат получил только заголовок «Ответ от организаторов», а само сообщение "
                    f"не дошло ({reason}). Повтор из бота закрыт, чтобы заголовок не пришёл дважды — "
                    "напишите делегату напрямую."
                )
            finally:
                logger.warning(
                    f"Question {qid}: помечен отвеченным, хотя делегат получил только заголовок "
                    "(копия не дошла) — менеджеру предложено написать напрямую"
                )
                await record_answer(qid, message.html_text or message.text or "")
            return
        # T-08-33 (accepted risk): the claim is NOT released here -- releasing it would let a
        # retry double-send to the delegate. Снимается только своя отметка «уходит».
        await release_question_delivery(qid, token)
        logger.error(f"Failed to send reply to user {user_id}: {e}")
        await _reply_with_delivery_error(message, e)
