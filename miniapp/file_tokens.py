"""Quick 260910-w3j (IMG-01..06): короткоживущий токен доступа к файлам Mini App.

Тег `<img src="...">` физически не может отправить заголовок `X-Telegram-Init-Data`, а куки
во встроенном браузере Телеграма нет — `miniapp/deps.py::principal` отвечал 401 ещё ДО
проверки прав, поэтому картинки внутри приложения показывать было нечем (логотип, аватары,
стикеры, обложки, PDF согласий).

Токен несёт ТОЛЬКО `telegram_id` — не отдельные права и не конкретный `file_id`. Маршрут
`GET /app/api/file/{id}` (`miniapp/routers/files.py::file_principal`) по токену восстанавливает
принципала и заново читает права/город из БД на КАЖДЫЙ запрос — `can_read_file` не меняется ни
строкой. Это принципиально отличается от подписи конкретного `file_id`: снятое право закрывает
файл на следующем же запросе, а не по истечении TTL подписи (D-09/T-19-05 — заморозки прав
здесь и не может быть).

Формат: `{telegram_id}.{exp}.{hexdigest}`, подпись — HMAC-SHA256 по строке `{telegram_id}.{exp}`
ключом `hmac.new(b"MiniAppFileAccess", bot_token.encode(), sha256).digest()`. Домен ключа
(`MiniAppFileAccess`) намеренно ОТЛИЧАЕТСЯ от `b"WebAppData"` из `miniapp/auth.py` — общий
домен свёл бы подписи двух разных механизмов в одно пространство. Сравнение —
`hmac.compare_digest`, как у `verify_init_data`. На любом кривом вводе — `None`, не исключение.
"""
from __future__ import annotations

import hashlib
import hmac
import time

# Сутки: вебвью Телеграма живёт долго, а токен даёт строго одно право — чтение файлов с
# полной проверкой прав на каждый запрос (снятое право не переживает даже эти сутки).
FILE_TOKEN_TTL = 86400

_KEY_DOMAIN = b"MiniAppFileAccess"


def _secret(bot_token: str) -> bytes:
    return hmac.new(_KEY_DOMAIN, bot_token.encode(), hashlib.sha256).digest()


def mint_file_token(bot_token: str, telegram_id: int, now: float | None = None) -> str:
    current = time.time() if now is None else now
    exp = int(current) + FILE_TOKEN_TTL
    payload = f"{telegram_id}.{exp}"
    digest = hmac.new(_secret(bot_token), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def verify_file_token(token: str, bot_token: str, now: float | None = None) -> int | None:
    """Исходный `telegram_id` при валидной свежей подписи, иначе `None`."""
    if not isinstance(token, str) or not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    raw_id, raw_exp, digest = parts
    payload = f"{raw_id}.{raw_exp}"
    expected = hmac.new(_secret(bot_token), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    try:
        telegram_id = int(raw_id)
        exp = int(raw_exp)
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    if current > exp:
        return None
    return telegram_id


def file_url(file_id: str, token: str | None = None) -> str:
    """`/app/api/file/{file_id}` — с `?t=` при заданном токене, без него для публичных
    ассетов (сервер обязан уметь собрать ссылку и туда, где никакого токена не выписывается)."""
    base = f"/app/api/file/{file_id}"
    return f"{base}?t={token}" if token else base


__all__ = ["FILE_TOKEN_TTL", "file_url", "mint_file_token", "verify_file_token"]
