"""Идея №23 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): делегатская сторона
опроса неявившихся «почему не пришёл» — пять кнопок ответа, «Другое» просит дописать словами
(FSM, отмена следующим /start — `cmd_start` чистит FSM, тот же приём, что `SosReport.collecting`/
`SessionFeedbackComment`). Домен (аудитория, рассылка, идемпотентность, сводка) — целиком в
`services/forum_noshow_poll.py`, здесь только тонкий шов (та же форма, что `handlers/sos.py`
поверх `services/sos.py`).

Импортирован ХВОСТОМ `handlers/user_actions.py` (сразу после `session_feedback`, перед
фолбэк-хендлером `reg_handoff_idle_fallback`) — тот же приём, что `program`/`sos`/
`session_feedback` выше.

Повторный тап другой кнопки меняет ответ (правило плана) — `services.forum_noshow_poll.
record_answer` делает обычный UPDATE, второй тап просто перезаписывает `reason`/`comment`."""
from __future__ import annotations

import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext

from handlers import reg_i18n
from handlers.states import ForumNoshowPollOther
from handlers.user_actions import router
from services import forum_noshow_poll as fnsp

logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("fnsp:"))
async def fnsp_answer(callback: types.CallbackQuery, state: FSMContext):
    reason = callback.data.split(":", 1)[1]
    if reason not in fnsp.OPTION_KEYS:
        await callback.answer()
        return
    lang, tr_map = await reg_i18n.ctx_for(callback)

    if reason == "other":
        await state.set_state(ForumNoshowPollOther.waiting)
        await callback.answer()
        from core.settings_schema import get_setting_typed

        prompt = await get_setting_typed("forum_noshow_poll_other_prompt_text") or fnsp.DEFAULT_OTHER_PROMPT
        try:
            await callback.message.answer(reg_i18n.tr_text(prompt, lang, tr_map))
        except Exception as e:
            logger.info(f"fnsp_answer: prompt send failed for {callback.from_user.id}: {e}")
        return

    ok = await fnsp.record_answer(callback.from_user.id, reason, None)
    if not ok:
        # Строки нет (сообщение переслано другому человеку/сезон сменился) — тихо, без
        # падения, тот же fail-soft приём, что у остального чек-ина.
        await callback.answer()
        return

    from core.settings_schema import get_setting_typed

    thanks = await get_setting_typed("forum_noshow_poll_thanks_text") or fnsp.DEFAULT_THANKS
    await callback.answer(reg_i18n.tr_text(thanks, lang, tr_map), show_alert=True)


@router.message(ForumNoshowPollOther.waiting)
async def fnsp_other_step(message: types.Message, state: FSMContext):
    await state.clear()
    text = (message.text or message.caption or "").strip()
    if not text:
        return
    ok = await fnsp.record_answer(message.from_user.id, "other", text)
    if not ok:
        return  # заявка устарела/сезон сменился — тихо, без ошибки на пустом месте

    lang, tr_map = await reg_i18n.ctx_for(message)
    from core.settings_schema import get_setting_typed

    thanks = await get_setting_typed("forum_noshow_poll_thanks_text") or fnsp.DEFAULT_THANKS
    await message.answer(reg_i18n.tr_text(thanks, lang, tr_map))
