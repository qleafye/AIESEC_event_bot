"""Вкладка Google-таблицы «Магазин»: снимок выдач, не дописывание.

Пачка выдач даёт одну перезапись через 5 секунд после последней. Вкладка одна на событие.
Пустое имя настройки — это «Магазин», а не «вкладку не вести».
"""
from __future__ import annotations

import logging

from services.infra.debounce import DebouncedJob

logger = logging.getLogger(__name__)

HEADERS = [
    "Когда", "Имя", "Username", "Telegram ID", "Товар", "Баллов",
    "Выдал (ID)", "Отменена", "Статус", "Код",
]
TAB_KEY = "shop_sheet_tab"

_job = DebouncedJob(
    "shop_sheet",
    "⚠️ Вкладка магазина не обновилась. Выдачи в боте на месте.",
    delay=5,
)
_enabled = False


async def tab_name() -> str:
    from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed

    stored = (await get_setting_typed(TAB_KEY) or "").strip()
    return stored or SETTINGS_SCHEMA[TAB_KEY]["default"]


def _row(sale: dict) -> list:
    from database.db import _sheet_safe

    status = "бронь" if sale.get("status") == "reserved" else "выдано"
    cells = [
        sale.get("sold_at") or "",
        sale.get("full_name") or "",
        sale.get("username") or "",
        sale.get("telegram_id") if sale.get("telegram_id") is not None else "",
        sale.get("title") or "",
        sale.get("price") if sale.get("price") is not None else "",
        sale.get("issued_by") or "",
        sale.get("undone_at") or "",
        status,
        sale.get("pickup_code") or "",
    ]
    return [_sheet_safe(cell) for cell in cells]


async def shop_sheet_rows() -> list[list]:
    from database import shop_db

    return [_row(sale) for sale in await shop_db.list_sales(100_000, 0)]


async def rebuild_shop_sheet() -> int:
    from services.sheets.sheets import sync_named_worksheet

    title = await tab_name()
    try:
        return await sync_named_worksheet(title, HEADERS, await shop_sheet_rows())
    except Exception as e:
        logger.error("rebuild_shop_sheet: вкладка «%s» не записалась: %s", title, e)
        return -1


def enable_shop_sheet_sync() -> None:
    """Включает отложенную пересборку в этом процессе. Повторный вызов безвреден."""
    global _enabled
    _enabled = True
    _job.set_job(rebuild_shop_sheet)


def request_shop_sheet_sync() -> None:
    """Поставить пересборку. Сразу выходит, если процесс её не включил или таблица не подключена."""
    if not _enabled:
        return
    from services.sheets.sheet_target import sheets_enabled

    if not sheets_enabled():
        return
    _job.request()
