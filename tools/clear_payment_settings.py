#!/usr/bin/env python3
"""Очистка настроек с упоминанием оплаты для города СПб.

Баг 22.09: reg_status_tile_approved_text (spb) = 'Одобрена · оплати до {дата}'
была сохранена вручную администратором когда оплата была включена. После
выключения payment_enabled=off текст остался в БД → делегаты видят
"Одобрена · оплати до " (пустая дата).

Фикс: очистить городские настройки с упоминанием оплаты → вернутся к дефолту
или будут перезаписаны администратором без упоминания оплаты.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from config import config


PAYMENT_SETTINGS = [
    "reg_status_tile_approved_text__spb",
    "reg_status_payment_due_label_text__spb",
    "reg_status_payment_reminder_note_text__spb",
]


async def main():
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Проверяем что есть в БД
        for key in PAYMENT_SETTINGS:
            async with db.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    print(f"Найдено: {key} = {row[0]!r}")

        # Удаляем
        deleted = 0
        for key in PAYMENT_SETTINGS:
            result = await db.execute(
                "DELETE FROM bot_settings WHERE key = ?", (key,)
            )
            if result.rowcount > 0:
                deleted += result.rowcount
                print(f"✅ Удалено: {key}")

        await db.commit()

        if deleted == 0:
            print("Нечего удалять, настройки отсутствуют.")
        else:
            print(f"\n✅ Удалено {deleted} настроек с упоминанием оплаты")
            print("Теперь tile_text вернётся к хардкоду 'Одобрена' (hub.py:322)")


if __name__ == "__main__":
    asyncio.run(main())
