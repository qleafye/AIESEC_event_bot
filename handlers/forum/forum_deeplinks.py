"""Deep-link'и форумных модулей: `/start sessions` (запись на сессии), `/start quiz` (тест компетенций),
`/start survey_<id>` (конкретный опрос).

Закрытый словарь литералов -> «модуль:функция»: произвольная строка из `/start` ничего не
импортирует и не исполняет. Опрос — единственное исключение с числом: префикс `survey_` разбирается
здесь же и зовёт одну заранее известную функцию, второй диспетчер старта не заводится.
Функция получает (message, state) и возвращает True, если обработала старт; False — обычный /start
идёт дальше (например, делегат ещё не регистрировался). Ссылка опроса возвращает True и при ошибке,
чтобы человека не выкинуло в главное меню.
"""
import importlib
import logging

logger = logging.getLogger(__name__)

DEEPLINKS: dict[str, str] = {
    "sessions": "handlers.forum.session_enroll:open_from_deeplink",
    "quiz": "handlers.forum.quiz:open_from_deeplink",
}

_SURVEY_OPEN = "handlers.comms.survey_delegate:open_from_deeplink"


async def try_forum_deeplink(message, state, args) -> bool:
    key = (args or "").strip()
    target = DEEPLINKS.get(key)
    poll_id = None
    if target is None:
        from services.comms.surveys import parse_survey_start

        poll_id = parse_survey_start(key)
        if poll_id is None:
            return False
        target = _SURVEY_OPEN
    module_name, func_name = target.split(":")
    try:
        func = getattr(importlib.import_module(module_name), func_name)
        if poll_id is None:
            return bool(await func(message, state))
        return bool(await func(message, state, poll_id))
    except Exception:
        logger.exception("forum deeplink %s не отработал", target)
        return False
