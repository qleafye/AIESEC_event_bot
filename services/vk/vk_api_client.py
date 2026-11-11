"""Проверка ключа сообщества ВК. Сам вызов библиотеки синхронный."""
from __future__ import annotations

import asyncio

import vk_api

_MESSAGES_SCOPE = 4096

_NO_MESSAGES = (
    "Токен не имеет права на сообщения сообщества.\n\n"
    "В настройках сообщества ВК:\n"
    "Управление → Работа с API → Ключи доступа → Создать ключ → "
    "поставьте галочку «Сообщения сообщества» и пришлите новый ключ."
)


def _validate_vk_token_sync(token: str) -> tuple[bool, str | None]:
    try:
        vk = vk_api.VkApi(token=token).get_api()
        perms = vk.groups.getTokenPermissions()
        if not (perms.get("mask", 0) & _MESSAGES_SCOPE):
            return False, _NO_MESSAGES
        return True, None
    except vk_api.exceptions.ApiError as exc:
        return False, f"Ошибка VK API: {exc}"
    except Exception as exc:
        return False, f"Не удалось проверить токен: {exc}"


async def validate_vk_token(token: str) -> tuple[bool, str | None]:
    """Право «Сообщения сообщества» (бит 4096). Ключ в ответ не попадает."""
    return await asyncio.to_thread(_validate_vk_token_sync, token)
