"""Какая Google-таблица у события — ОДИН резолвер на весь проект.

Раньше ID таблицы жил только в `.env` (`GOOGLE_SHEET_ID`): новое событие = разработчик правит
файл на сервере и перезапускает бота. Теперь суперадмин вставляет ССЫЛКУ на таблицу в боте
(«📊 Данные → 🔗 Какая таблица», handlers/sheets/admin_sheet_target.py), бот сам вытаскивает ID,
проверяет доступ сервисного аккаунта и сохраняет в `bot_settings.google_sheet_id`.

Порядок: значение из БД, иначе `config.GOOGLE_SHEET_ID` (`.env`) — обратная совместимость:
стек, где в боте ничего не задавали, живёт как раньше.

Резолвер СИНХРОННЫЙ (plain sqlite3, только чтение): его зовут и с цикла событий (гейты
«таблица подключена?»), и из `asyncio.to_thread` (`services/sheets._get_sheet`), где у
aiosqlite нет цикла. Короткий кэш в памяти процесса — гейты стоят на каждой записи в лист;
сохранение из бота кладёт новое значение сразу (`remember`), второй процесс (Mini App) увидит
смену не позже `_TTL_S`.

Сбой чтения базы не равен «не задано»: отдаём последнее удачное значение, а если его ещё не
было — пустое (запись пропускается), но не таблицу из `.env`.

Модуль без aiogram — тянуть его можно отовсюду (скрипты, Mini App)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
_refreshing = False


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


class _ReadError(Exception):
    """Базу прочитать не удалось (занята, сбой диска) — это НЕ «в боте не задано»."""


def _read_db_value() -> str | None:
    """Сохранённый в боте ID или None, если его там нет (нет файла базы, нет таблицы настроек,
    нет строки). Только чтение (`mode=ro`: файл базы не создаём). Настоящий сбой чтения —
    `_ReadError`: путать его с «не задано» нельзя, иначе запись молча уедет в таблицу из .env."""
    if not os.path.exists(config.DB_PATH):
        return None
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
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return None
        raise _ReadError(str(e)) from e
    except Exception as e:
        raise _ReadError(str(e)) from e
    return ((row[0] if row else None) or "").strip() or None


def env_sheet_id() -> str:
    """Значение из `.env` (как раньше) — без кавычек и пробелов по краям."""
    return (config.GOOGLE_SHEET_ID or "").strip().strip('"').strip("'").strip()


def bot_sheet_id() -> str | None:
    """Только то, что задано в боте (без отката на `.env`), — для экрана настройки. Блокирующее
    чтение: с цикла событий — через `asyncio.to_thread`. Сбой чтения — None и WARNING."""
    try:
        return _read_db_value()
    except _ReadError as e:
        logger.warning(f"sheet_target: не прочитать {SETTING_KEY} из базы: {e}")
        return None


def _store(key, value: str) -> None:
    with _cache_lock:
        _cache.update(key=key, value=value, at=time.monotonic())


def _resolve_sync(key, env: str) -> str:
    try:
        db_value = _read_db_value()
    except _ReadError as e:
        with _cache_lock:
            last = _cache["value"] if _cache["key"] == key else None
            if last is not None:
                _cache["at"] = time.monotonic()  # следующая попытка — через TTL, базу не долбим
        logger.warning(
            f"sheet_target: не прочитать {SETTING_KEY} из базы ({e}) — "
            + ("оставляю прежнюю таблицу" if last is not None
               else "таблица неизвестна, запись в лист пропускается до следующей попытки")
        )
        # Удачного значения ещё не было: неизвестно, задана ли таблица в боте — лучше не писать
        # никуда (строку догонит «🔄 Синхронизация»), чем писать в чужую таблицу из .env.
        return last if last is not None else ""
    value = db_value or env
    _store(key, value)
    return value


def _refresh_in_background(key, env: str) -> None:
    global _refreshing
    with _cache_lock:
        if _refreshing:
            return
        _refreshing = True

    def job():
        global _refreshing
        try:
            _resolve_sync(key, env)
        finally:
            with _cache_lock:
                _refreshing = False

    threading.Thread(target=job, name="sheet-target-refresh", daemon=True).start()


def sheet_id() -> str:
    """ID таблицы события: из бота, иначе из `.env`; пустая строка — таблица не подключена.

    Значение живёт в памяти процесса. Свежее (моложе `_TTL_S`) отдаётся сразу. Устаревшее на
    цикле событий тоже отдаётся сразу, а перечитывается в фоновом потоке — цикл на базе не
    блокируется. В рабочем потоке (gspread через `to_thread`) и при самом первом обращении
    читается синхронно. Сохранение из бота кладёт новое значение сразу (`remember`)."""
    env = env_sheet_id()
    key = (config.DB_PATH, env)
    with _cache_lock:
        cached = _cache["value"] if _cache["key"] == key else None
        fresh = cached is not None and time.monotonic() - _cache["at"] < _TTL_S
    if fresh:
        return cached
    if cached is not None and _on_event_loop():
        _refresh_in_background(key, env)
        return cached
    return _resolve_sync(key, env)


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def remember(bot_value: str | None) -> None:
    """Таблицу только что сохранили/сбросили в боте — положить итог в память сразу."""
    env = env_sheet_id()
    _store((config.DB_PATH, env), (bot_value or "").strip() or env)


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
    """Таблица сменилась — бросить открытый главный лист сразу. Кэши вкладок сами сверяют
    ID таблицы (services/sheets/sheets.py), второй процесс (Mini App) переключится по TTL."""
    try:
        from services.sheets import sheets
        sheets._reset_sheet_cache()
    except Exception as e:  # noqa: BLE001 — сброс кэша не должен ронять сохранение
        logger.warning(f"sheet_target: сброс кэша листа не удался: {e}")
