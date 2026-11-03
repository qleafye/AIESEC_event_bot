"""Слоты и брони интервью. Схема создаётся в database.db.init_db.

Соединение — через атрибут `_db._connect()`: тесты подменяют `config.DB_PATH`.
"""
from __future__ import annotations

from database import db as _db


def _row(row) -> dict:
    return {
        "id": int(row[0]),
        "event_city": row[1],
        "starts_at": row[2],
        "cap": int(row[3]),
        "created_by": row[4],
        "created_at": row[5],
    }


async def create_slot(event_city: str | None, starts_at: str, cap: int,
                      created_by: int | None, created_at: str) -> int:
    async with _db._connect() as db:
        cur = await db.execute(
            "INSERT INTO interview_slots (event_city, starts_at, cap, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_city, starts_at, int(cap), created_by, created_at),
        )
        await db.commit()
        return int(cur.lastrowid)


async def list_slots(event_city: str | None) -> list[dict]:
    sql = (
        "SELECT id, event_city, starts_at, cap, created_by, created_at "
        "FROM interview_slots WHERE event_city IS NULL ORDER BY starts_at, id"
        if event_city is None else
        "SELECT id, event_city, starts_at, cap, created_by, created_at "
        "FROM interview_slots WHERE event_city = ? ORDER BY starts_at, id"
    )
    args = () if event_city is None else (event_city,)
    async with _db._connect() as db:
        async with db.execute(sql, args) as cur:
            return [_row(r) for r in await cur.fetchall()]


async def get_slot(slot_id: int) -> dict | None:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT id, event_city, starts_at, cap, created_by, created_at "
            "FROM interview_slots WHERE id = ?",
            (int(slot_id),),
        ) as cur:
            row = await cur.fetchone()
    return None if row is None else _row(row)


async def slot_taken(slot_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM interview_bookings WHERE slot_id = ?", (int(slot_id),),
        ) as cur:
            return int((await cur.fetchone())[0])


async def get_booking(user_id: int) -> dict | None:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT user_id, slot_id, reschedule_count, booked_at "
            "FROM interview_bookings WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "user_id": int(row[0]),
        "slot_id": int(row[1]),
        "reschedule_count": int(row[2]),
        "booked_at": row[3],
    }


async def _changes(db) -> int:
    async with db.execute("SELECT changes()") as cur:
        return int((await cur.fetchone())[0])


async def book(user_id: int, slot_id: int, booked_at: str) -> str:
    """ok | exists | full | missing_slot. Кап проверяется в той же транзакции, что и вставка."""
    uid, sid = int(user_id), int(slot_id)
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "INSERT INTO interview_bookings (user_id, slot_id, reschedule_count, booked_at) "
                "SELECT ?, ?, 0, ? "
                "WHERE EXISTS (SELECT 1 FROM interview_slots WHERE id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM interview_bookings WHERE user_id = ?) "
                "AND (SELECT COUNT(*) FROM interview_bookings WHERE slot_id = ?) "
                "    < (SELECT cap FROM interview_slots WHERE id = ?)",
                (uid, sid, booked_at, sid, uid, sid, sid),
            )
            if await _changes(db) == 1:
                await db.commit()
                return "ok"
            async with db.execute("SELECT 1 FROM interview_slots WHERE id = ?", (sid,)) as look:
                slot = await look.fetchone()
            async with db.execute(
                "SELECT 1 FROM interview_bookings WHERE user_id = ?", (uid,),
            ) as look:
                existing = await look.fetchone()
            await db.rollback()
        except Exception:
            await db.rollback()
            raise
    if slot is None:
        return "missing_slot"
    if existing is not None:
        return "exists"
    return "full"


async def reschedule(user_id: int, slot_id: int, booked_at: str) -> str:
    """ok | missing | same | spent | full | missing_slot.

    Один UPDATE: второе место и второй перенос не проходят, даже если два вызова
    пришли внахлёст.
    """
    uid, sid = int(user_id), int(slot_id)
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "UPDATE interview_bookings SET slot_id = ?, reschedule_count = 1, booked_at = ? "
                "WHERE user_id = ? AND reschedule_count = 0 AND slot_id != ? "
                "AND EXISTS (SELECT 1 FROM interview_slots WHERE id = ?) "
                "AND (SELECT COUNT(*) FROM interview_bookings WHERE slot_id = ?) "
                "    < (SELECT cap FROM interview_slots WHERE id = ?)",
                (sid, booked_at, uid, sid, sid, sid, sid),
            )
            if await _changes(db) == 1:
                await db.commit()
                return "ok"
            async with db.execute(
                "SELECT slot_id, reschedule_count FROM interview_bookings WHERE user_id = ?",
                (uid,),
            ) as look:
                booking = await look.fetchone()
            async with db.execute("SELECT 1 FROM interview_slots WHERE id = ?", (sid,)) as look:
                slot = await look.fetchone()
            await db.rollback()
        except Exception:
            await db.rollback()
            raise
    if booking is None:
        return "missing"
    if slot is None:
        return "missing_slot"
    if int(booking[0]) == sid:
        return "same"
    if int(booking[1]) != 0:
        return "spent"
    return "full"


async def delete_slot(slot_id: int) -> list[int]:
    """Снимает слот и его брони. Напоминания этих людей отменяет."""
    sid = int(slot_id)
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT user_id FROM interview_bookings WHERE slot_id = ?", (sid,),
            ) as cur:
                users = [int(r[0]) for r in await cur.fetchall()]
            await db.execute("DELETE FROM interview_bookings WHERE slot_id = ?", (sid,))
            await db.execute("DELETE FROM interview_slots WHERE id = ?", (sid,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    try:
        from services.scheduler import cancel_interview_reminder
    except Exception:
        return users
    for uid in users:
        try:
            cancel_interview_reminder(uid)
        except Exception:
            pass
    return users
