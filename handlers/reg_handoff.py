"""Quick 260904-3vm (решение владельца 04.09: «эстафета» вместо двустороннего синхрона фазы 21).

Гвард на входе в хендлеры состояния Registration + возврат владения черновиком в чат.

Импортируется в САМОМ НИЗУ `handlers/registration.py`, СТРОКОЙ НИЖЕ `from handlers import
reg_resume` — тот же шов, что у `reg_resume`: его хендлер (`reg_handoff:to_bot`) попадает в
самый хвост `registration.router`, снапшот только дописывается
(`tests/test_refac_snapshot_260816.py`).

`RegHandoffGuard` — OUTER middleware (решение принимается ДО фильтров, иначе `process_course`
и подобные уже проглотили бы текст как ответ на вопрос). Регистрируется на ОБОИХ обсерверах
общего `router`: `message` и `callback_query` — у делегата в чате остаются живые inline-
клавиатуры прошлых вопросов (`regmulti:*`, `regmulti_done:*`, `consent_accept:*`, экран
«Оставить/Изменить»), тап по которым идёт мимо message-обсервера прямо в `_advance` и записал
бы ответ в чужой черновик (D8 — «подхватывает то, что ввелось позже»)."""
from __future__ import annotations

import logging

from aiogram import Bot, BaseMiddleware, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject

from database.db import get_reg_draft, get_user, set_reg_draft_surface
from settings_schema import get_setting_typed
from services.reg_handoff import SURFACE_APP, SURFACE_BOT, draft_holder
from reg_engine import has_submitted_anketa
from handlers.registration import router

logger = logging.getLogger(__name__)

# Проверяется ПЕРВЫМ, до всякого чтения БД — иначе гвард запер бы делегата, отобрав у него же
# кнопку возврата/отмену.
_EXEMPT_CALLBACKS = {"reg_handoff:to_bot", "reg_cancel_yes", "reg_cancel_no"}


def _is_exempt_callback(data: str | None) -> bool:
    if not data:
        return False
    if data in _EXEMPT_CALLBACKS:
        return True
    return data.startswith("reg_resume:")


async def _held_by_app_keyboard() -> InlineKeyboardMarkup:
    label = await get_setting_typed("reg_handoff_to_bot_label")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data="reg_handoff:to_bot")],
    ])


async def handoff_plate(message: types.Message) -> None:
    """Плита «анкета открыта в приложении» + кнопка возврата — общая точка для гварда
    (Registration-состояние) и для `handlers/user_actions.py`'s фолбэка без состояния."""
    text = await get_setting_typed("reg_handoff_held_by_app_text")
    await message.answer(text, reply_markup=await _held_by_app_keyboard())


class RegHandoffGuard(BaseMiddleware):
    """Единственная точка входа, решающая — принять апдейт как ответ на вопрос анкеты, или
    отбить его (владение у приложения / анкета уже отправлена). Целиком в try/except:
    T-3vm-03 — любой сбой чтения БД пропускает апдейт дальше, делегат никогда не запирается
    перед анкетой."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        state: FSMContext = data["state"]
        raw = await state.get_state()
        if not raw or not raw.startswith("Registration:"):
            return await handler(event, data)

        # Duck-typed, а не isinstance(event, types.CallbackQuery) — так же легко проходят
        # тестовые двойники хендлеров (тот же приём, что везде в этом проекте, см.
        # tests/test_reg_resume_draft.py::_FakeCallback), не только настоящие aiogram-объекты.
        is_callback = hasattr(event, "data")
        if is_callback and _is_exempt_callback(event.data):
            return await handler(event, data)

        try:
            uid = event.from_user.id
            draft = await get_reg_draft(uid)
            user_row = await get_user(uid)
            season = await get_setting_typed("event_season") or None
            holder = draft_holder(draft)
            submitted = has_submitted_anketa(user_row, season)
        except Exception as e:
            logger.error("RegHandoffGuard: fail-soft, пропускаю апдейт (%s)", e)
            return await handler(event, data)

        if submitted and (draft is None or draft.get("submitting_at")):
            await state.clear()
            if is_callback:
                text = await get_setting_typed("reg_already_submitted_text")
                await event.answer(text, show_alert=True)
                return None
            msg_text = getattr(event, "text", None) or ""
            if msg_text.startswith("/"):
                # Команды остаются рабочими, состояние уже снято выше.
                return await handler(event, data)
            text = await get_setting_typed("reg_already_submitted_text")
            await event.answer(text)
            return None

        if holder == SURFACE_APP:
            text = await get_setting_typed("reg_handoff_held_by_app_text")
            if is_callback:
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text, reply_markup=await _held_by_app_keyboard())
            return None

        return await handler(event, data)


router.message.outer_middleware(RegHandoffGuard())
router.callback_query.outer_middleware(RegHandoffGuard())


@router.callback_query(F.data == "reg_handoff:to_bot")
async def reg_handoff_to_bot(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    """T-3vm-05: черновик читается ТОЛЬКО по `callback.from_user.id` — в callback_data нет
    параметров (та же посадка, что `reg_resume:*`)."""
    from handlers.reg_resume import resume_from_draft  # локально: reg_resume импортируется
    # ПОСЛЕ этого модуля (см. докстринг вверху) — импорт наверху дал бы циклическую загрузку.

    uid = callback.from_user.id
    draft = await get_reg_draft(uid)
    if not draft:
        await callback.answer("Черновик не найден — начни заново с /start.", show_alert=True)
        return

    await set_reg_draft_surface(uid, SURFACE_BOT)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()

    text = await get_setting_typed("reg_handoff_resumed_text")
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    try:
        await tap_message.answer(text)
    except Exception as e:
        logger.warning("reg_handoff_resumed_text send failed for %s: %s", uid, e)

    await resume_from_draft(tap_message, state, bot, draft)


__all__ = ["RegHandoffGuard", "handoff_plate"]
