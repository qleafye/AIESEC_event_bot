"""Короткий стабильный токен ключа настройки для callback_data.

Telegram не принимает кнопку, если `callback_data` длиннее 64 байт. Порядковый
номер ключа в реестре для этого не годится: он сдвигается, когда ключ добавляют.
Токен — точка и 10 hex-символов sha256 самого ключа. Точка в именах ключей
(`a-z`, цифры, `_`) не встречается, поэтому токен не путается с сырым ключом.
Один и тот же ключ даёт один и тот же токен, пока ключ не переименовали.
Неизвестный токен не разворачивается: вызывающий отказывает и ничего не удаляет.
"""
from __future__ import annotations

import hashlib

from domain.settings.schema import SETTINGS_SCHEMA

# 64 байта — потолок callback_data в Bot API.
CB_LIMIT = 64
# 10 hex = 40 бит. На реестре из ~1300 ключей коллизий нет; сторож ловит новую.
TOKEN_HEX = 10


def setting_token(key: str) -> str:
    """Токен ключа. Не индекс в словаре — от порядка реестра не зависит."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:TOKEN_HEX]
    return f".{digest}"


def _token_index() -> dict[str, str]:
    """Токен → ключ. Коллизию в карту не кладём: неизвестный токен — отказ, не чужой ключ."""
    owners: dict[str, list[str]] = {}
    for key in SETTINGS_SCHEMA:
        owners.setdefault(setting_token(key), []).append(key)
    return {token: keys[0] for token, keys in owners.items() if len(keys) == 1}


_TOKEN_TO_KEY = _token_index()


def resolve_setting_token(token: str) -> str | None:
    """Ключ по токену. None — такого токена нет (или коллизия, токен из карты вынут)."""
    if not token.startswith(".") or len(token) != TOKEN_HEX + 1:
        return None
    if any(ch not in "0123456789abcdef" for ch in token[1:]):
        return None
    return _TOKEN_TO_KEY.get(token)


def reset_payload_key(payload: str) -> str | None:
    """Ключ для сброса города. None — неизвестная кнопка, удалять нельзя.

    Токен (начинается с точки) не считается сырым ключом, даже если в реестре
    нет совпадения. Сырой ключ реестра по-прежнему принимается.
    """
    if payload.startswith("."):
        return resolve_setting_token(payload)
    if payload in SETTINGS_SCHEMA:
        return payload
    return None


def edit_callback_key(payload: str) -> str | None:
    """Ключ для `settings_edit`. Сырой ключ — как есть, токен — в ключ, неизвестный токен — None."""
    if payload.startswith("."):
        return resolve_setting_token(payload)
    return payload


def city_reset_callback(action: str, key: str, code: str | None = None) -> str:
    """Единая строка кнопки сброса города — и для экрана настройки, и для даты форума.

    `ask` — `settings_reset_city:{token}`.
    `go` — `settings_reset_city_go:{token}:{code}`.
    `back` — `settings_edit:{key}`, если сырой ключ влезает в 64 байта, иначе
    `settings_edit:{token}`.
    """
    token = setting_token(key)
    if action == "ask":
        return f"settings_reset_city:{token}"
    if action == "go":
        return f"settings_reset_city_go:{token}:{code}"
    if action == "back":
        raw = f"settings_edit:{key}"
        if len(raw.encode("utf-8")) <= CB_LIMIT:
            return raw
        return f"settings_edit:{token}"
    raise ValueError(f"unknown city reset action: {action}")
