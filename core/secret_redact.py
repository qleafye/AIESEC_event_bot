"""Санитайзер секретов для текста, который видит человек или лог.

Инцидент 25.09: алерт «🔀 Прокси переключился … Причина: TelegramNetworkError: …
https://api.telegram.org/bot<ТОКЕН>/sendPhoto» ушёл в личку всем админам — `str(exception)`
у aiogram/aiohttp несёт URL Bot API, а в пути URL лежит полный токен бота. Та же строка
писалась в logs/bot.log и в `docker logs`.

Один модуль на три процесса (бот, Mini App, дашборд) — поэтому корневой и только stdlib:
образ дашборда не тянет aiogram/pydantic (dashboard/Dockerfile копирует его отдельно).

- `redact_secrets(text)` — прогнать ЛЮБОЙ текст исключения перед отправкой человеку.
- `register_secret(value)` — процесс сообщает свой токен из конфига, чтобы его маскировало
  и там, где он встретился без префикса `bot`.
- `install_log_redaction()` — фильтр на хендлеры root (+ uvicorn и lastResort): сообщение,
  аргументы, трейсбек и stack_info проходят через `redact_secrets` до форматирования.
"""
from __future__ import annotations

import logging
import re

REDACTED = "[скрыт]"

# Путь Bot API: .../bot123456:AAH-abc_DEF/sendPhoto
_BOT_URL_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
# Тот же токен без префикса «bot» (напр. в тексте ошибки конфига). Хвост у настоящего токена —
# 35 символов; 30+ отсекает случайные «12:30»-подобные совпадения.
_BARE_TOKEN_RE = re.compile(r"(?<![\w])\d{5,}:[A-Za-z0-9_-]{30,}")
# Пароль в URL прокси/сервиса: socks5://user:pass@host -> socks5://user:***@host
_URL_PASSWORD_RE = re.compile(r"(://[^\s/:@]+):[^\s/@]+@")

_registered: set[str] = set()


def register_secret(value) -> None:
    """Запомнить конкретное значение (токен из конфига) для маскировки. Пустое/короткое —
    игнорируется: маскировать «1234» значило бы портить обычный текст."""
    if value is None:
        return
    get = getattr(value, "get_secret_value", None)
    s = get() if callable(get) else str(value)
    s = s.strip()
    if len(s) >= 16:
        _registered.add(s)


def redact_secrets(text) -> str:
    """Вернуть `text` без токенов бота и паролей в URL. Не-строку приводит через str()."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    for secret in _registered:
        if secret in s:
            s = s.replace(secret, REDACTED)
    s = _BOT_URL_TOKEN_RE.sub("bot" + REDACTED, s)
    s = _BARE_TOKEN_RE.sub(REDACTED, s)
    s = _URL_PASSWORD_RE.sub(r"\1:***@", s)
    return s


class RedactSecretsFilter(logging.Filter):
    """Переписывает запись до форматирования: если в тексте нашёлся секрет, msg =
    отформатированный и очищенный текст, args = (); exc_text/stack_info — очищенный трейсбек (Formatter берёт готовый exc_text
    и не форматирует exc_info повторно). Никогда не отбрасывает запись."""

    _fmt = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            clean = redact_secrets(message)
            if clean != message:  # чистую запись не трогаем: args остаются для caplog и т.п.
                record.msg = clean
                record.args = ()
        except Exception:  # noqa: BLE001 — кривой %-формат не должен ронять логирование
            record.msg = redact_secrets(record.msg)
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = self._fmt.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_secrets(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_secrets(record.stack_info)
        except Exception:  # noqa: BLE001
            pass
        return True


_EXTRA_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _attach(target) -> None:
    if not any(isinstance(f, RedactSecretsFilter) for f in target.filters):
        target.addFilter(RedactSecretsFilter())


def install_log_redaction() -> None:
    """Повесить фильтр на root-логгер и на КАЖДЫЙ хендлер root, uvicorn-логгеров и
    lastResort. Именно на хендлеры: фильтр логгера не видит записи дочерних логгеров,
    которые доходят до root через propagate. Идемпотентна; звать после настройки хендлеров."""
    root = logging.getLogger()
    _attach(root)
    for handler in root.handlers:
        _attach(handler)
    for name in _EXTRA_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            _attach(handler)
    if logging.lastResort is not None:
        _attach(logging.lastResort)


__all__ = [
    "REDACTED",
    "RedactSecretsFilter",
    "install_log_redaction",
    "redact_secrets",
    "register_secret",
]
