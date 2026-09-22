#!/usr/bin/env python3
"""Миграция: очистка payment_option и payment_due у всех делегатов.

Баг 22.09: Mini App показывал блок оплаты когда payment_enabled=off, потому что
payment_option/payment_due были записаны при одобрении (handlers/payment.py вызывался
при payment_enabled=on, потом настройку выключили).

Фикс в коде (f504357) защищает новые одобрения. Эта миграция чистит существующие записи.
"""
import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from config import config


async def main():
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Считаем сколько строк затронем
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE payment_option IS NOT NULL OR payment_due IS NOT NULL"
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0

        print(f"Найдено {count} записей с payment_option/payment_due")

        if count == 0:
            print("Нечего чистить, выходим.")
            return

        # Очищаем
        await db.execute(
            "UPDATE users SET payment_option = NULL, payment_due = NULL "
            "WHERE payment_option IS NOT NULL OR payment_due IS NOT NULL"
        )
        await db.commit()

        print(f"✅ Очищено {count} записей")

        # Проверка
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE payment_option IS NOT NULL OR payment_due IS NOT NULL"
        ) as cursor:
            row = await cursor.fetchone()
            remain = row[0] if row else 0

        if remain > 0:
            print(f"⚠️ Осталось {remain} записей — что-то пошло не так")
        else:
            print("✅ Проверка: все записи очищены")


if __name__ == "__main__":
    asyncio.run(main())
