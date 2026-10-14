"""Вкладки Google-таблицы «Записи на сессии» (по сессиям) и «Кто куда записан» (по людям).

Обе вкладки общие для всех городов, город — колонкой «Город»: у города по умолчанию (Москва)
своих вкладок нет, а он главный потребитель. Пересобираются целиком из БД (`sync_named_worksheet`
очищает лист и пишет заново), главный лист регистраций не трогаем.

Автосинхрон: слой БД после записи/отмены/правки программы зовёт `request_enroll_sheet_resync()`,
пачка событий даёт одну пересборку через ~30 секунд. Регистрирует пересборку только процесс бота
(`enable_autosync()` на импорте `handlers/forum/admin_enroll_list.py`): веб-процесс Mini App тоже
пишет записи (сканер), но таймер у него ничего не делает — запись сканером попадёт в лист при
следующем событии в боте или по кнопке.
"""
from __future__ import annotations

import logging

from database import session_enroll_db as edb
from database.db import _sheet_safe
from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed
from services.infra.debounce import DebouncedJob
from shared import enroll_stats

logger = logging.getLogger(__name__)

SESSIONS_TAB_KEY = "enroll_sessions_tab"
PEOPLE_TAB_KEY = "enroll_people_tab"

_job = DebouncedJob(
    "enroll_sheets",
    "⚠️ Автосинк вкладок записи на сессии: Google Sheets вернул ошибку (см. лог сервера).",
)


async def _tab_name(key: str) -> str:
    return (await get_setting_typed(key) or "").strip() or SETTINGS_SCHEMA[key]["default"]


async def enroll_tab_names() -> tuple[str, str]:
    """(вкладка по сессиям, вкладка по людям) — из реестра, пустое значение = дефолт."""
    return await _tab_name(SESSIONS_TAB_KEY), await _tab_name(PEOPLE_TAB_KEY)


def _safe_rows(rows: list[list]) -> list[list]:
    return [[_sheet_safe(cell) for cell in row] for row in rows]


async def rebuild_enroll_sheets() -> tuple[int, int]:
    """Перезаписывает обе вкладки. Возвращает (строк по сессиям, строк по людям); -1 на месте
    вкладки, которая не записалась (таблица не подключена или ошибка API). Сбой одной вкладки
    не мешает второй."""
    from domain.cities import city_labels_map
    from services.sheets.sheets import sync_named_worksheet  # sheets тянет gspread — лениво

    sessions_tab, people_tab = await enroll_tab_names()
    labels = await city_labels_map()
    try:
        session_rows = _safe_rows(
            enroll_stats.sessions_sheet_rows(await edb.enroll_overview_rows(None), labels)
        )
        n_sessions = await sync_named_worksheet(sessions_tab, enroll_stats.SESSION_HEADERS, session_rows)
    except Exception as e:
        logger.error("rebuild_enroll_sheets: вкладка «%s» не записалась: %s", sessions_tab, e)
        n_sessions = -1
    try:
        people_rows = _safe_rows(
            enroll_stats.people_sheet_rows(await edb.enroll_people_rows(None), labels)
        )
        n_people = await sync_named_worksheet(people_tab, enroll_stats.PEOPLE_HEADERS, people_rows)
    except Exception as e:
        logger.error("rebuild_enroll_sheets: вкладка «%s» не записалась: %s", people_tab, e)
        n_people = -1
    return n_sessions, n_people


def enable_autosync() -> None:
    """Включает отложенную пересборку в ЭТОМ процессе (зовёт только процесс бота)."""
    _job.set_job(rebuild_enroll_sheets)


def request_enroll_sheet_resync() -> None:
    """Поставить отложенную пересборку (последний вызов побеждает). Синхронная."""
    _job.request()
