"""Проверка ключа сообщества ВК и отправка сообщения. Сам вызов библиотеки синхронный."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets

import vk_api

logger = logging.getLogger(__name__)

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


def lacks_messages_right(error: str | None) -> bool:
    return bool(error) and error.startswith("Токен не имеет права")


async def forget_community_token_without_messages_right(
    error: str | None, admin_id: int | None,
) -> None:
    """Нет галочки «Сообщения сообщества» — уже сохранённый ключ больше не рабочий.
    Сеть и прочая ошибка VK сохранённый ключ не трогают."""
    if not lacks_messages_right(error):
        return
    from services.settings.audit import delete_setting_by_admin

    await delete_setting_by_admin(admin_id, "vk_community_token")


def vk_session(token: str) -> vk_api.VkApi:
    """Сессия в памяти. Ключ сообщества не пишется в vk_config на диск."""
    from jconfig.memory import MemoryConfig

    return vk_api.VkApi(token=token, config=MemoryConfig)


def keyboard_for_send(keyboard_json: str | None) -> str | None:
    """Пустая клавиатура в API не уходит: ВК всё равно считает её клавиатурой."""
    if keyboard_json is None:
        return None
    text = str(keyboard_json).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(data, dict):
        return text
    buttons = data.get("buttons")
    if isinstance(buttons, list) and not any(buttons):
        return None
    return text


def _send_vk_message_sync(
    token: str, vk_id: int, text: str, keyboard_json: str | None,
) -> None:
    api = vk_session(token).get_api()
    params: dict = {
        "user_id": int(vk_id),
        "message": text,
        "random_id": secrets.randbelow(2**31 - 1) + 1,
    }
    if keyboard_json is not None:
        params["keyboard"] = keyboard_json
    api.messages.send(**params)


async def send_vk_message(vk_id: int, text: str, keyboard_json: str | None = None) -> bool:
    """Письмо делегату во ВК. Ключ читается сейчас: смена ключа не оставляет старую сессию.
    True — сообщение ушло. Сбой VK не поднимается наверх. В журнал не попадают ни ключ, ни текст.
    Ошибка 912 (у сообщества выключены возможности ботов) повторяет то же письмо без клавиатуры."""
    from domain.settings.schema import get_setting_typed

    token = str(await get_setting_typed("vk_community_token") or "").strip()
    if not token:
        logger.error("VK send skipped for %s: community key is not set", vk_id)
        return False
    keyboard_json = keyboard_for_send(keyboard_json)
    try:
        await asyncio.to_thread(_send_vk_message_sync, token, vk_id, text, keyboard_json)
    except vk_api.exceptions.ApiError as exc:
        if getattr(exc, "code", None) == 912 and keyboard_json is not None:
            logger.error("VK send failed to %s: [912]", vk_id)
            try:
                await asyncio.to_thread(_send_vk_message_sync, token, vk_id, text, None)
            except vk_api.exceptions.ApiError as retry_exc:
                logger.error("VK send failed to %s: [%s]", vk_id, retry_exc.code)
                return False
            except Exception as retry_exc:
                logger.error(
                    "VK send unexpected error to %s: %s", vk_id, type(retry_exc).__name__,
                )
                return False
            return True
        logger.error("VK send failed to %s: [%s]", vk_id, exc.code)
        return False
    except Exception as exc:
        logger.error("VK send unexpected error to %s: %s", vk_id, type(exc).__name__)
        return False
    return True
