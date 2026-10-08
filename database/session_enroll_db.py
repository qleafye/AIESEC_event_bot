"""Слой БД записи на сессии программы: треки, компетенции, записи делегатов, подтверждение
расписания.

Отдельный модуль, а не ещё один блок в `database/db.py` (тот уже больше 12 тысяч строк).

Модель. Сессия программы (`program_sessions`) может входить в трек (`track_id`). Записываться
можно только на сессии с треком; пленарки (без трека) общие для всех и в проверке пересечения
не участвуют. Запись — строка `session_enrollments` с PK (telegram_id, session_id). Треки и
компетенции — справочники города с порядком; пустыми они создаются на чистой БД, названия
заводит менеджер.

Соединение — `_db._connect()` через атрибут модуля (тесты подменяют `config.DB_PATH`), тот же
приём, что `database/amb_status_db.py`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiosqlite

from database import db as _db
from services.timeutil import msk_now

logger = logging.getLogger(__name__)


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


# ── Схема ────────────────────────────────────────────────────────────────────────────────────

async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Таблицы и колонки записи на сессии. Зовётся из `database.db.init_db` на его соединении
    и в его транзакции — коммит делает init_db. Старые сессии получают enroll_closed = 0,
    track_id и enroll_limit = NULL."""
    await _db._ensure_column(db, "program_sessions", "track_id", "INTEGER")
    await _db._ensure_column(db, "program_sessions", "enroll_closed", "INTEGER NOT NULL DEFAULT 0")
    await _db._ensure_column(db, "program_sessions", "enroll_limit", "INTEGER")
    for table in ("program_tracks", "competencies"):
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, city TEXT NOT NULL, name TEXT NOT NULL, "
            "position INTEGER NOT NULL DEFAULT 0, created_at TEXT)"
        )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS program_session_competencies ("
        "session_id INTEGER NOT NULL, competency_id INTEGER NOT NULL, "
        "PRIMARY KEY (session_id, competency_id))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS session_enrollments ("
        "telegram_id INTEGER NOT NULL, session_id INTEGER NOT NULL, created_at TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT 'self', by_staff_id INTEGER, "
        "PRIMARY KEY (telegram_id, session_id))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS session_schedule_confirms ("
        "telegram_id INTEGER NOT NULL, city TEXT NOT NULL, confirmed_at TEXT NOT NULL, "
        "PRIMARY KEY (telegram_id, city))"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_enroll_session ON session_enrollments(session_id)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_program_tracks_city ON program_tracks(city)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_competencies_city ON competencies(city)")


# ── Справочники: общие операции ──────────────────────────────────────────────────────────────
# `table` — всегда литерал этого файла (program_tracks / competencies), не пользовательский ввод.

async def _list(table: str, city: str) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT id, city, name, position FROM {table} WHERE city = ? "
            "ORDER BY position, id", (city,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def _get(table: str, row_id: int) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT id, city, name, position FROM {table} WHERE id = ?", (int(row_id),),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def _create(table: str, city: str, name: str) -> int:
    async with _db._connect() as db:
        async with db.execute(
            f"SELECT COALESCE(MAX(position), 0) + 1 FROM {table} WHERE city = ?", (city,),
        ) as cursor:
            position = int((await cursor.fetchone())[0])
        cursor = await db.execute(
            f"INSERT INTO {table} (city, name, position, created_at) VALUES (?, ?, ?, ?)",
            (city, name.strip(), position, _stamp()),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def _rename(table: str, row_id: int, name: str) -> bool:
    async with _db._connect() as db:
        cursor = await db.execute(
            f"UPDATE {table} SET name = ? WHERE id = ?", (name.strip(), int(row_id)),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def _move(table: str, row_id: int, delta: int) -> bool:
    """Сдвиг на соседа выше (delta < 0) или ниже (delta > 0) внутри города. False — на краю."""
    row = await _get(table, row_id)
    if not row:
        return False
    items = await _list(table, row["city"])
    ids = [i["id"] for i in items]
    idx = ids.index(int(row_id))
    new = idx + (1 if delta > 0 else -1)
    if new < 0 or new >= len(ids):
        return False
    ids[idx], ids[new] = ids[new], ids[idx]
    async with _db._connect() as db:
        # Позиции переписываем целиком: исходные могут повторяться или иметь дыры.
        for pos, rid in enumerate(ids, start=1):
            await db.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, rid))
        await db.commit()
    return True


# ── Треки ────────────────────────────────────────────────────────────────────────────────────

async def list_tracks(city: str) -> list[dict]:
    return await _list("program_tracks", city)


async def get_track(track_id: int) -> dict | None:
    return await _get("program_tracks", track_id)


async def create_track(city: str, name: str) -> int:
    return await _create("program_tracks", city, name)


async def rename_track(track_id: int, name: str) -> bool:
    return await _rename("program_tracks", track_id, name)


async def move_track(track_id: int, delta: int) -> bool:
    return await _move("program_tracks", track_id, delta)


async def count_sessions_for_track(track_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM program_sessions WHERE track_id = ?", (int(track_id),),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def count_enrollments_for_track(track_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM session_enrollments WHERE session_id IN "
            "(SELECT id FROM program_sessions WHERE track_id = ?)", (int(track_id),),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def delete_track(track_id: int) -> int:
    """Удалить трек: записи на его сессии удаляются, у сессий трек снимается (они становятся
    общими). Одна транзакция. Возвращает число удалённых записей."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "DELETE FROM session_enrollments WHERE session_id IN "
            "(SELECT id FROM program_sessions WHERE track_id = ?)", (int(track_id),),
        )
        removed = int(cursor.rowcount or 0)
        await db.execute(
            "UPDATE program_sessions SET track_id = NULL WHERE track_id = ?", (int(track_id),),
        )
        await db.execute("DELETE FROM program_tracks WHERE id = ?", (int(track_id),))
        await db.commit()
    return removed


# ── Компетенции ──────────────────────────────────────────────────────────────────────────────

async def list_competencies(city: str) -> list[dict]:
    return await _list("competencies", city)


async def get_competency(cid: int) -> dict | None:
    return await _get("competencies", cid)


async def create_competency(city: str, name: str) -> int:
    return await _create("competencies", city, name)


async def rename_competency(cid: int, name: str) -> bool:
    return await _rename("competencies", cid, name)


async def move_competency(cid: int, delta: int) -> bool:
    return await _move("competencies", cid, delta)


async def delete_competency(cid: int) -> None:
    """Удаляет компетенцию и снимает её со всех сессий."""
    async with _db._connect() as db:
        await db.execute(
            "DELETE FROM program_session_competencies WHERE competency_id = ?", (int(cid),),
        )
        await db.execute("DELETE FROM competencies WHERE id = ?", (int(cid),))
        await db.commit()


async def get_session_competency_ids(session_id: int) -> list[int]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT competency_id FROM program_session_competencies WHERE session_id = ? "
            "ORDER BY competency_id", (int(session_id),),
        ) as cursor:
            return [int(r[0]) for r in await cursor.fetchall()]


async def toggle_session_competency(session_id: int, competency_id: int) -> bool:
    """Включить/выключить компетенцию у сессии. Возвращает новое состояние (True — включена)."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "DELETE FROM program_session_competencies WHERE session_id = ? AND competency_id = ?",
            (int(session_id), int(competency_id)),
        )
        if cursor.rowcount:
            await db.commit()
            return False
        await db.execute(
            "INSERT INTO program_session_competencies (session_id, competency_id) VALUES (?, ?)",
            (int(session_id), int(competency_id)),
        )
        await db.commit()
        return True


async def competency_names_for_sessions(session_ids: list[int]) -> dict[int, list[str]]:
    """{session_id: [названия компетенций по порядку справочника]}; для каждой запрошенной
    сессии ключ есть всегда (пустой список, если компетенций нет)."""
    out: dict[int, list[str]] = {int(s): [] for s in session_ids}
    if not out:
        return out
    marks = ",".join("?" for _ in out)
    async with _db._connect() as db:
        async with db.execute(
            "SELECT psc.session_id, c.name FROM program_session_competencies psc "
            "JOIN competencies c ON c.id = psc.competency_id "
            f"WHERE psc.session_id IN ({marks}) ORDER BY c.position, c.id", list(out),
        ) as cursor:
            for sid, name in await cursor.fetchall():
                out[int(sid)].append(name)
    return out


async def list_trackable_sessions(city: str, day: str | None = None) -> list[dict]:
    """Сессии города с треком (на них можно записаться) + `track_name`."""
    sql = (
        "SELECT s.*, t.name AS track_name FROM program_sessions s "
        "JOIN program_tracks t ON t.id = s.track_id "
        "WHERE s.city = ? AND s.track_id IS NOT NULL"
    )
    params: list = [city]
    if day is not None:
        sql += " AND s.day = ?"
        params.append(day)
    sql += " ORDER BY s.day, s.start_time, s.id"
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


# ── Запись ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class EnrollResult:
    """status: ok | already | conflict | full | not_enrollable | no_session.
    conflicts — id сессий-конфликтов (status conflict); replaced — id снятых при замене."""
    status: str
    conflicts: list[int] = field(default_factory=list)
    replaced: list[int] = field(default_factory=list)


async def enroll_tx(
    telegram_id: int, session_id: int, *, allow_replace: bool = False,
    limit_check: bool = True, source: str = "self", by_staff_id: int | None = None,
) -> EnrollResult:
    """Записать делегата на сессию одной транзакцией `BEGIN IMMEDIATE` (бот и веб-процесс
    Mini App пишут одну SQLite — проверка и вставка не должны разъезжаться).

    Пересечение ПОПАРНОЕ: конфликтуют только записи того же города и дня с треком, у которых
    `start < other.end AND other.start < end` с целевой сессией. Это НЕ `parallel_group` /
    `slot_other_ids` чек-ина: те строят транзитивный слот (A пересекает B, B пересекает C ->
    все трое «параллельны»), а для записи A и C, не пересекающиеся между собой, совместимы.

    Сессия без трека — `not_enrollable` (пленарка общая). `enroll_closed` здесь не проверяется:
    это правило сервиса, а сканер на входе его игнорирует (`limit_check=False` — тоже путь
    сканера: волонтёр не отказывает человеку у двери)."""
    tid, sid = int(telegram_id), int(session_id)
    conflicts: list[int] = []
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            async with conn.execute(
                "SELECT id, city, day, start_time, end_time, track_id, enroll_limit "
                "FROM program_sessions WHERE id = ?", (sid,),
            ) as cur:
                target = await cur.fetchone()
            if target is None:
                await conn.rollback()
                return EnrollResult("no_session")
            if target["track_id"] is None:
                await conn.rollback()
                return EnrollResult("not_enrollable")
            async with conn.execute(
                "SELECT 1 FROM session_enrollments WHERE telegram_id = ? AND session_id = ?",
                (tid, sid),
            ) as cur:
                if await cur.fetchone():
                    await conn.rollback()
                    return EnrollResult("already")
            async with conn.execute(
                "SELECT s.id FROM session_enrollments e JOIN program_sessions s "
                "ON s.id = e.session_id WHERE e.telegram_id = ? AND s.city = ? AND s.day = ? "
                "AND s.track_id IS NOT NULL AND s.id != ? "
                "AND s.start_time < ? AND ? < s.end_time ORDER BY s.start_time, s.id",
                (tid, target["city"], target["day"], sid,
                 target["end_time"], target["start_time"]),
            ) as cur:
                conflicts = [int(r[0]) for r in await cur.fetchall()]
            if conflicts and not allow_replace:
                await conn.rollback()
                return EnrollResult("conflict", conflicts=conflicts)
            if limit_check and target["enroll_limit"] is not None:
                async with conn.execute(
                    "SELECT COUNT(*) FROM session_enrollments WHERE session_id = ?", (sid,),
                ) as cur:
                    taken = int((await cur.fetchone())[0])
                if taken >= int(target["enroll_limit"]):
                    await conn.rollback()
                    return EnrollResult("full")
            for cid in conflicts:
                await conn.execute(
                    "DELETE FROM session_enrollments WHERE telegram_id = ? AND session_id = ?",
                    (tid, cid),
                )
            await conn.execute(
                "INSERT INTO session_enrollments (telegram_id, session_id, created_at, source, "
                "by_staff_id) VALUES (?, ?, ?, ?, ?)",
                (tid, sid, _stamp(), source, by_staff_id),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return EnrollResult("ok", replaced=conflicts)


async def unenroll(telegram_id: int, session_id: int) -> bool:
    async with _db._connect() as db:
        cursor = await db.execute(
            "DELETE FROM session_enrollments WHERE telegram_id = ? AND session_id = ?",
            (int(telegram_id), int(session_id)),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def user_enrollment_ids(telegram_id: int) -> set[int]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT session_id FROM session_enrollments WHERE telegram_id = ?",
            (int(telegram_id),),
        ) as cursor:
            return {int(r[0]) for r in await cursor.fetchall()}


async def list_user_enrollments(telegram_id: int, city: str | None = None) -> list[dict]:
    sql = (
        "SELECT s.*, t.name AS track_name, e.created_at AS enrolled_at, e.source AS enroll_source "
        "FROM session_enrollments e JOIN program_sessions s ON s.id = e.session_id "
        "LEFT JOIN program_tracks t ON t.id = s.track_id WHERE e.telegram_id = ?"
    )
    params: list = [int(telegram_id)]
    if city is not None:
        sql += " AND s.city = ?"
        params.append(city)
    sql += " ORDER BY s.day, s.start_time, s.id"
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def count_enrollments(session_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM session_enrollments WHERE session_id = ?", (int(session_id),),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def enrollment_counts_for_city(city: str) -> dict[int, int]:
    """{session_id: число записанных} по сессиям города (сессии без записей — нет в словаре)."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT e.session_id, COUNT(*) FROM session_enrollments e JOIN program_sessions s "
            "ON s.id = e.session_id WHERE s.city = ? GROUP BY e.session_id", (city,),
        ) as cursor:
            return {int(r[0]): int(r[1]) for r in await cursor.fetchall()}


async def count_enrolled_users(city: str) -> int:
    """Сколько разных делегатов записано хотя бы на одну сессию города."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT e.telegram_id) FROM session_enrollments e "
            "JOIN program_sessions s ON s.id = e.session_id WHERE s.city = ?", (city,),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def list_enrolled_users(session_id: int) -> list[dict]:
    """Записанные на сессию: telegram_id, ФИО, username, телефон, почта, вуз (колонки users,
    что в выгрузке `cmd_export`) + created_at, source, by_staff_id."""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT e.telegram_id, u.full_name, u.username, u.phone, u.email, u.university, "
            "u.event_city, u.status, e.created_at, e.source, e.by_staff_id "
            "FROM session_enrollments e LEFT JOIN users u ON u.telegram_id = e.telegram_id "
            "WHERE e.session_id = ? ORDER BY e.created_at, e.telegram_id", (int(session_id),),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


# ── Подтверждение расписания ─────────────────────────────────────────────────────────────────

async def confirm_schedule(telegram_id: int, city: str) -> str:
    """Делегат подтвердил свой выбор; повтор обновляет метку. Возвращает метку времени."""
    stamp = _stamp()
    async with _db._connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO session_schedule_confirms (telegram_id, city, confirmed_at) "
            "VALUES (?, ?, ?)", (int(telegram_id), city, stamp),
        )
        await db.commit()
    return stamp


async def get_schedule_confirmed_at(telegram_id: int, city: str) -> str | None:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT confirmed_at FROM session_schedule_confirms WHERE telegram_id = ? AND city = ?",
            (int(telegram_id), city),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def count_schedule_confirms(city: str) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM session_schedule_confirms WHERE city = ?", (city,),
        ) as cursor:
            return int((await cursor.fetchone())[0])


# ── Чистка делегата ──────────────────────────────────────────────────────────────────────────

async def count_enrollments_for_user(telegram_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM session_enrollments WHERE telegram_id = ?", (int(telegram_id),),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def delete_enrollments_for_user(telegram_id: int) -> int:
    """Удаляет записи делегата и подтверждения расписания (переезд в другой город).
    Возвращает число удалённых записей."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "DELETE FROM session_enrollments WHERE telegram_id = ?", (int(telegram_id),),
        )
        removed = int(cursor.rowcount or 0)
        await db.execute(
            "DELETE FROM session_schedule_confirms WHERE telegram_id = ?", (int(telegram_id),),
        )
        await db.commit()
    return removed
