"""Кнопка главного меню «✏️ Изменить анкету» (владелец 09.10).

Делегат на событии без приложения раньше узнавал о правке анкеты только от менеджера, который
присылал ссылку `?start=edit`. Кнопка ведёт ровно в тот же вход: `cmd_start` с аргументом
`edit` — там же стоит гейт `reg_edit_policy.edit_gate`. Если правило правки запретило её
после того, как клавиатура была выдана, делегат получит текст отказа и обновлённое меню уже
без кнопки. Хендлер регистрируется на роутер `user_actions` (хвостовой импорт там).
"""
from types import SimpleNamespace

from aiogram import Bot, types
from aiogram.fsm.context import FSMContext

from handlers.user_actions import router
from keyboards.menu_dynamic import MenuButton


@router.message(MenuButton("menu_edit_anketa"))
async def menu_edit_anketa(message: types.Message, state: FSMContext, bot: Bot):
    from handlers.registration import cmd_start
    await state.clear()
    await cmd_start(message, state, bot, command=SimpleNamespace(args="edit"))
