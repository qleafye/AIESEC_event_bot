"""Тап по кнопке главного меню, пока бот ждёт от делегата ответ (вопрос организаторам, сдача
задания, комментарий к сессии…).

`keyboards.menu_dynamic.MenuButton` в состояниях делегата не матчится — поэтому «🪙 Мои баллы»,
нажатая в форме вопроса, уходила организаторам КАК ТЕКСТ ВОПРОСА. Здесь outer-middleware
`user_actions.router` (через него идут все эти состояния): точное совпадение с подписью кнопки
меню (текущей, стандартной или прежней — та же `menu_key_for_text`, без машинного перевода)
снимает состояние, говорит короткую строку («Вопрос не отправлен.») и пускает апдейт дальше
уже без состояния — нажатие ловит обычный хендлер раздела.

Анкета (`Registration`, `_CompositeChat`, `_LookupChat`, `OnsiteReg`) сюда не входит: там меню
не показывается, а ответ может совпасть с подписью.
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware

from handlers.i18n import reg_i18n
from handlers.user_actions import router
from keyboards.menu_dynamic import menu_key_for_text

logger = logging.getLogger(__name__)

# Группа состояний -> что сказать делегату при выходе. Переводы — services/i18n/i18n_form_manual.py.
ESCAPE_NOTICES: dict[str, str] = {
    "Question": "Вопрос не отправлен.",
    "GameSubmit": "Подтверждение задания не отправлено.",
    "SessionFeedbackComment": "Комментарий не отправлен.",
    "ForumNoshowPollOther": "Ответ не отправлен.",
    "SosReport": "🆘 SOS уже у организаторов, дописывание закрыто.",
}


class MenuTapEscape(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        raw_state = data.get("raw_state")
        notice = ESCAPE_NOTICES.get(raw_state.split(":", 1)[0]) if raw_state else None
        text = getattr(event, "text", None)
        if notice and text and getattr(getattr(event, "chat", None), "type", None) == "private":
            try:
                is_menu = await menu_key_for_text(text, event) is not None
            except Exception as e:
                logger.warning("menu_tap_escape: подпись не распознана: %s", e)
                is_menu = False
            state = data.get("state")
            if is_menu and state is not None:
                await state.clear()
                data["raw_state"] = None
                await reg_i18n.say(event, notice)
        return await handler(event, data)


router.message.outer_middleware(MenuTapEscape())
