"""Шим закрытой ссылки чата города: реализация в services.chat.city_chat_api.

Те же имена. Сервисы импортируют сервисный модуль, не этот файл.
"""
from services.chat.city_chat_api import (
    _InviteClient,
    client_from_bot,
    notify_if_bot_ready,
    notify_link_failure,
)

__all__ = [
    "_InviteClient",
    "client_from_bot",
    "notify_if_bot_ready",
    "notify_link_failure",
]
