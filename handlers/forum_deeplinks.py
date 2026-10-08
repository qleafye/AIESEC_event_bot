"""Deep-link'и форумных модулей: `/start sessions` (запись на сессии), позже `/start quiz`.

Закрытый словарь литералов -> «модуль:функция»: произвольная строка из `/start` ничего не
импортирует и не исполняет. Функция получает (message, state) и возвращает True, если
обработала старт; False — обычный /start идёт дальше (например, делегат ещё не регистрировался).
"""
import importlib
import logging

logger = logging.getLogger(__name__)

DEEPLINKS: dict[str, str] = {
    "sessions": "handlers.session_enroll:open_from_deeplink",
}


async def try_forum_deeplink(message, state, args) -> bool:
    target = DEEPLINKS.get((args or "").strip())
    if target is None:
        return False
    module_name, func_name = target.split(":")
    try:
        func = getattr(importlib.import_module(module_name), func_name)
        return bool(await func(message, state))
    except Exception:
        logger.exception("forum deeplink %s не отработал", target)
        return False
