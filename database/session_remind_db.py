"""БД напоминаний о сессиях: журнал отправок (идемпотентность) и выборка получателей.

Журнал `session_remind_sends` — PK (telegram_id, kind, ref): kind 'remind' + ref=id сессии,
kind 'morning' + ref='город:день'. Повторный тик джобы (рестарт, перепланирование) не шлёт
второго сообщения: вставка `INSERT OR IGNORE` возвращает False. Запись в журнал делается ДО
отправки (резервирование), при окончательном сбое доставки строка остаётся — повторять
недоставляемому нечего, а двойная отправка хуже потери.

Соединение — `_db._connect()` через атрибут модуля (тесты подменяют `config.DB_PATH`)."""
from __future__ import annotations

import aiosqlite

from database import db as _db

KIND_REMIND = "remind"
KIND_MORNING = "morning"


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS session_remind_sends ("
        "telegram_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, sent_at TEXT NOT NULL, "
        "PRIMARY KEY (telegram_id, kind, ref))"
    )


async def reserve(telegram_id: int, kind: str, ref: str, stamp: str) -> bool:
    """True — запись создана (можно слать); False — уже была (слать нельзя)."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO session_remind_sends (telegram_id, kind, ref, sent_at) VALUES (?, ?, ?, ?)",
            (int(telegram_id), kind, str(ref), stamp),
        )
        await db.commit()
        return cursor.rowcount > 0


async def release(telegram_id: int, kind: str, ref: str) -> None:
    """Снять резерв, если отправка не состоялась по нашей причине (не по вине получателя)."""
    async with _db._connect() as db:
        await db.execute(
            "DELETE FROM session_remind_sends WHERE telegram_id = ? AND kind = ? AND ref = ?",
            (int(telegram_id), kind, str(ref)),
        )
        await db.commit()


async def was_sent(telegram_id: int, kind: str, ref: str) -> bool:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT 1 FROM session_remind_sends WHERE telegram_id = ? AND kind = ? AND ref = ?",
            (int(telegram_id), kind, str(ref)),
        ) as cursor:
            return await cursor.fetchone() is not None


async def list_enrolled_approved(session_id: int) -> list[dict]:
    """Записанные на сессию одобренные делегаты (строки users). Город проверяет вызывающий."""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT u.* FROM session_enrollments e JOIN users u ON u.telegram_id = e.telegram_id "
            "WHERE e.session_id = ? AND u.status = 'approved' ORDER BY e.created_at, u.telegram_id",
            (int(session_id),),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def list_cities_with_sessions() -> list[str]:
    async with _db._connect() as db:
        async with db.execute("SELECT DISTINCT city FROM program_sessions ORDER BY city") as cursor:
            return [r[0] for r in await cursor.fetchall()]
