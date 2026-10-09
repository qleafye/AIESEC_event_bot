"""Какая Google-таблица у события — ОДИН резолвер на весь проект.

Раньше ID таблицы жил только в `.env` (`GOOGLE_SHEET_ID`): новое событие = разработчик правит
файл на сервере и перезапускает бота. Теперь суперадмин вставляет ССЫЛКУ на таблицу в боте
(«📊 Данные → 🔗 Какая таблица», handlers/admin_sheet_target.py), бот сам вытаскивает ID,
проверяет доступ сервисного аккаунта и сохраняет в `bot_settings.google_sheet_id`.

Порядок: значение из БД, иначе `config.GOOGLE_SHEET_ID` (`.env`) — обратная совместимость:
стек, где в боте ничего не задавали, живёт как раньше.

Резолвер СИНХРОННЫЙ (plain sqlite3, только чтение): его зовут и с цикла событий (гейты
«таблица подключена?»), и из `asyncio.to_thread` (`services/sheets._get_sheet`), где у
aiosqlite нет цикла. Короткий кэш в памяти процесса — гейты стоят на каждой записи в лист;
сохранение из бота сбрасывает его сразу (`invalidate`), второй процесс (Mini App) увидит
смену не позже `_TTL_S`.

Модуль без aiogram — тянуть его можно отовсюду (скрипты, Mini App)."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time

from config import config

logger = logging.getLogger(__name__)

SETTING_KEY = "google_sheet_id"

_TTL_S = 5.0
_DB_BUSY_TIMEOUT_S = 5.0

# ID таблицы Google: латиница, цифры, «-» и «_», на практике 44 символа. Нижняя граница 20 —
# чтобы случайное слово («таблица», «привет») не сошло за ID.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_URL_RE = re.compile(r"/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,})")

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "value": None, "at": 0.0}


def parse_sheet_ref(text: str | None) -> str | None:
    """ID таблицы из того, что прислал человек: полная ссылка из адресной строки
    (`https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0`) или голый ID. Не похоже ни на
    то, ни на другое -> None."""
    raw = (text or "").strip().strip("<>").strip('"').strip("'").strip()
    if not raw:
        return None
    m = _URL_RE.search(raw)
    if m:
        return m.group(1)
    if _ID_RE.match(raw):
        return raw
    return None


def _read_db_value() -> str | None:
    """Сохранённый в боте ID — только чтение (`mode=ro`: не создаём файл БД, если его нет)."""
    try:
        conn = sqlite3.connect(
            f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=_DB_BUSY_TIMEOUT_S,
        )
        try:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (SETTING_KEY,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:  # нет файла/таблицы — значит в боте не задано
        logger.debug(f"sheet_target: чтение {SETTING_KEY} не удалось: {e}")
        return None
    return ((row[0] if row else None) or "").strip() or None


def env_sheet_id() -> str:
    """Значение из `.env` (как раньше) — без кавычек и пробелов по краям."""
    return (config.GOOGLE_SHEET_ID or "").strip().strip('"').strip("'").strip()


def bot_sheet_id() -> str | None:
    """Только то, что задано в боте (без отката на `.env`), — для экрана настройки."""
    return _read_db_value()


def sheet_id() -> str:
    """ID таблицы события: из бота, иначе из `.env`; пустая строка — таблица не подключена."""
    env = env_sheet_id()
    key = (config.DB_PATH, env)
    now = time.monotonic()
    with _cache_lock:
        if _cache["key"] == key and now - _cache["at"] < _TTL_S:
            return _cache["value"]
    value = _read_db_value() or env
    with _cache_lock:
        _cache.update(key=key, value=value, at=now)
    return value


def invalidate() -> None:
    with _cache_lock:
        _cache.update(key=None, value=None, at=0.0)


def sheets_enabled() -> bool:
    """Запись в таблицу вообще возможна: есть и таблица, и ключ сервисного аккаунта."""
    return bool(sheet_id() and config.GOOGLE_CREDENTIALS_FILE)


def sheet_url(spreadsheet_id: str | None = None) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id or sheet_id()}/edit"


def service_account_email() -> str | None:
    """Адрес сервисного аккаунта из файла ключа — его человек добавляет в доступ таблицы."""
    path = config.GOOGLE_CREDENTIALS_FILE
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("client_email") or "").strip() or None
    except Exception as e:
        logger.warning(f"sheet_target: не прочитать client_email из ключа: {e}")
        return None


# Итог проверки доступа: "ok" | "no_access" | "no_key" | "error".
def check_access_sync(spreadsheet_id: str) -> tuple[str, str | None]:
    """Открыть таблицу сервисным аккаунтом. Возвращает (итог, название таблицы | текст сбоя).
    Блокирующий сетевой вызов — с цикла событий только через `asyncio.to_thread`."""
    import gspread

    path = config.GOOGLE_CREDENTIALS_FILE
    if not path:
        return "no_key", None
    try:
        gc = gspread.service_account(filename=path)
    except FileNotFoundError:
        return "no_key", None
    except Exception as e:
        return "error", str(e)
    try:
        sh = gc.open_by_key(spreadsheet_id)
        return "ok", sh.title
    except gspread.exceptions.SpreadsheetNotFound:
        return "no_access", None
    except gspread.exceptions.APIError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (403, 404):
            return "no_access", None
        return "error", str(e)
    except Exception as e:
        return "error", str(e)


def reset_client_caches() -> None:
    """Таблица сменилась — бросить всё, что держит открытую старую (кэш главного листа)."""
    invalidate()
    try:
        from services import sheets
        sheets._reset_sheet_cache()
    except Exception as e:  # noqa: BLE001 — сброс кэша не должен ронять сохранение
        logger.warning(f"sheet_target: сброс кэша листа не удался: {e}")
