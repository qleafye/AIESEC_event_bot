"""Код ответа Google и человеческое объяснение при сбое сверки листа делегаций.

Зачем: раньше любой сбой чтения листа показывался как «таблица сейчас недоступна» и в логе
оставался только тип исключения — нельзя было отличить нет доступа (403) от лимита
запросов (429) или сбоя на стороне Google. Сетевых вызовов модуль не делает.
"""
from __future__ import annotations

import html

import gspread

from services.sheets.sheet_target import service_account_email


def google_status(exc) -> int | None:
    """HTTP-код ответа Google для `gspread.exceptions.APIError`; для прочих — None."""
    if not isinstance(exc, gspread.exceptions.APIError):
        return None
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def explain(exc, tab: str) -> str | None:
    """Текст менеджеру по коду ответа; None — ошибка не от API Google (остаётся общий текст)."""
    if not isinstance(exc, gspread.exceptions.APIError):
        return None
    code = google_status(exc)
    name = html.escape(str(tab))
    if code == 403:
        email = service_account_email()
        who = html.escape(email) if email else "сервисному аккаунту бота"
        return (f"Нет доступа к таблице: бот не может прочитать лист «{name}». Откройте "
                f"Google-таблицу → «Настройки доступа» и дайте {who} роль «Редактор», затем "
                "нажмите «🔁 Сверить ещё раз».")
    if code == 404:
        return "Таблица не найдена — проверьте ссылку на таблицу в «📊 Данные»."
    if code == 429:
        return ("Google временно ограничил число запросов к таблице. Подождите минуту и "
                "нажмите «🔁 Сверить ещё раз».")
    return (f"Google не ответил на чтение листа «{name}» (код {code}). Попробуйте "
            "«🔁 Сверить ещё раз» через несколько минут; если повторяется — проверьте таблицу "
            "в «📊 Данные».")
