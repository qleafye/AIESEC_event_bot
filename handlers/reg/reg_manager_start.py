"""Ночь 09.10: /start менеджера не запускает анкету делегата.

Раньше любой без строки в `users` получал приветствие делегата и первый вопрос; брошенная
анкета писала `reg_started`, и менеджер (админ или человек с ролью в админке) попадал во
вкладку «Незавершённые», в рассылку «Не завершили регистрацию» и под напоминание. Теперь
чистый /start человека с ролью показывает подсказку про /admin и кнопку «Всё-таки заполнить
анкету»; `reg_started` пишется только после тапа.

Подсказка показывается ТОЛЬКО на «голом» /start: без payload (реф-ссылки, метки, города,
делегации, приглашения волонтёров, `continue`/`edit` — у всех них своя логика выше или ниже),
без строки в `users`, без черновика анкеты, без записи в `reg_started` и без состояния FSM.
Текст адресован менеджеру, не делегату, — не переводится (как «Вы админ — можете пройти
регистрацию заново» рядом).
"""
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_reg_draft, get_reg_started_by_id, get_user
from handlers.access.admin_caps import resolve_capabilities
from handlers.registration import _city_fork_then_continue, router

logger = logging.getLogger(__name__)

MANAGER_HINT_TEXT = "Вы в команде бота: админка открывается командой /admin."
MANAGER_FILL_BUTTON_TEXT = "📝 Всё-таки заполнить анкету"
MANAGER_FILL_CALLBACK = "mgr_fill_form"


async def offer_manager_hint(
    message: types.Message, state: FSMContext, user: dict | None, args: str | None
) -> bool:
    """True -> подсказка показана, /start закончен (анкета не стартует, reg_started не пишется)."""
    if args or user:
        return False
    user_id = message.from_user.id
    try:
        if not await resolve_capabilities(user_id):
            return False
        if await state.get_state() is not None:
            return False
        if await get_reg_draft(user_id) or await get_reg_started_by_id(user_id):
            return False
    except Exception as e:
        # Сбой проверки не должен стоить входа: ведём себя как раньше.
        logger.warning(f"manager /start check skipped for {user_id}: {e}")
        return False
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=MANAGER_FILL_BUTTON_TEXT, callback_data=MANAGER_FILL_CALLBACK)
    ]])
    await message.answer(MANAGER_HINT_TEXT, reply_markup=kb)
    return True


@router.callback_query(F.data == MANAGER_FILL_CALLBACK)
async def manager_fill_form(callback: types.CallbackQuery, state: FSMContext):
    """Тап «Всё-таки заполнить анкету»: обычный путь анкеты (город -> форк -> старт), как у
    admin_rereg. Права перепроверяются по тапнувшему — callback_data угадываемый."""
    if not await resolve_capabilities(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _city_fork_then_continue(tap_message, state, None, None, None, None, None)
