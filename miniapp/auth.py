"""Phase 19 (D-09, T-19-01/T-19-02/T-19-08): проверка подписи `initData` Telegram Mini App.

Алгоритм — `core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app`:
пары `initData` URL-декодируются (`parse_qsl`), исключается ТОЛЬКО `hash` (Ed25519-поле
`signature` остаётся в check-string как обычное поле), пары сортируются по ключу и
склеиваются `k=v` через `\\n`; секрет — `HMAC_SHA256(key="WebAppData", msg=bot_token)`;
сравнение — `hmac.compare_digest`.

Чем это НЕ `dashboard.auth.verify_login_payload` (Login Widget): у виджета секрет
`sha256(bot_token)`, значения приходят уже декодированными query-параметрами, `id` лежит
на верхнем уровне; здесь секрет — HMAC с ключом `WebAppData`, `user` — JSON-строка, а
check-string строится по декодированным значениям (RESEARCH Pitfall 6: кириллица/эмодзи в
`first_name` percent-encoded в исходной строке).

ПД: сырой `init_data` не логируется ни целиком, ни частично — внутри `user` имя, username
и язык человека. На кривом вводе функция возвращает `None`, не исключение.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from miniapp.config import INIT_DATA_MAX_AGE

# Допустимый забег часов вперёд — как `_FUTURE_SKEW_SECONDS` в dashboard/auth.py.
_FUTURE_SKEW_SECONDS = 60


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = INIT_DATA_MAX_AGE,
    now: float | None = None,
) -> dict | None:
    """Словарь полей `initData` (поле `user` уже разобрано в dict) при валидной свежей
    подписи, иначе `None`. Отсутствие `user`/`user.id` — тоже `None`: так выглядит «простой»
    web view, открытый reply-кнопкой (RESEARCH Pitfall 1) — аутентифицировать некого.
    `now` — только для тестов."""
    if not isinstance(init_data, str) or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None

    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, ValueError, TypeError):
        return None
    current = time.time() if now is None else now
    age = current - auth_date
    if age < -_FUTURE_SKEW_SECONDS or age > max_age_seconds:
        return None

    try:
        user = json.loads(pairs["user"])
        if not isinstance(user, dict):
            return None
        user["id"] = int(user["id"])
    except (KeyError, ValueError, TypeError):
        return None
    pairs["user"] = user
    return pairs
