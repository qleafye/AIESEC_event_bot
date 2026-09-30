"""Слой БД статуса амбассадора: схема, разовая миграция флагов, единственный писатель статуса
с зеркалом `is_ambassador`, место в лимите («пакет»), выборки для экранов и архив сезона.

Отдельный модуль, а не ещё один блок в `database/db.py`: тот уже больше 12 тысяч строк.

Модель. У делегата один статус в `users.ambassador_status`: NULL (= «none»), `candidate`,
`active`, `left`, `declined`. `is_ambassador` остаётся зеркалом «status = active» — его читают
десятки мест (волны, баллы, ступени, дашборд), и они не меняются. Писать обе колонки можно
только здесь (`set_status` и массовые операции ниже) — это держит сторож в
`tests/test_amb_status_34.py`. `is_ambassador_candidate` — ответ анкеты, не статус: его
перезаписывает `add_user` при каждой подаче, поэтому здесь его не пишет никто.

Место в лимите — хранимое `ambassador_slot_at`, а не вычисляемое «active и заявка одобрена»:
иначе одобрение заявки амбассадора «без пакета» молча делает его 18-м при лимите 17. Выдаётся
в `BEGIN IMMEDIATE` — бот и веб-процесс Mini App пишут одну SQLite. Амбассадор с выданным
пакетом (`ambassador_pack_at`) при выходе место не освобождает: подарок уже у него.

Соединение — `_db._connect()` через атрибут модуля (тесты подменяют `config.DB_PATH`), тот же
приём, что `database/amb_tiers_db.py`.
"""
from __future__ import annotations

import logging

import aiosqlite

from database import db as _db

logger = logging.getLogger(__name__)

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_LEFT = "left"
STATUS_DECLINED = "declined"
STATUS_NONE = "none"  # в БД — NULL

_STATUSES = (STATUS_CANDIDATE, STATUS_ACTIVE, STATUS_LEFT, STATUS_DECLINED)

_AMB_STATUS_MIGRATION_USER_VERSION = 3

_LIST_FILTERS = ("candidates", "team", "no_pack", "declined")

# Явный список колонок экранов и выгрузки — без SELECT *.
_LIST_COLUMNS = (
    "telegram_id, full_name, username, event_city, status, season, ambassador_status, "
    "ambassador_since, ambassador_slot_at, ambassador_pack_at, ambassador_reserve_at"
)
_EXPORT_COLUMNS = (
    _LIST_COLUMNS + ", ambassador_status_at, ambassador_left_at, ambassador_declined_notified_at"
)


def _norm(status: str | None) -> str | None:
    """'none'/None -> None (NULL в БД); неизвестное значение — ValueError."""
    if status is None or status == STATUS_NONE:
        return None
    if status not in _STATUSES:
        raise ValueError(f"неизвестный статус амбассадора: {status!r}")
    return status


# ── Схема и миграция ─────────────────────────────────────────────────────────────────────────

async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Колонки статуса, индекс, архив сезонов и разовая миграция флагов. Зовётся из
    `database.db.init_db` на его соединении и в его транзакции — коммит делает init_db."""
    for column, definition in (
        ("ambassador_status", "TEXT"),
        ("ambassador_status_at", "TEXT"),
        ("ambassador_status_by", "INTEGER"),
        ("ambassador_slot_at", "TEXT"),
        ("ambassador_pack_at", "TEXT"),
        ("ambassador_reserve_at", "TEXT"),
        ("ambassador_declined_notified_at", "TEXT"),
    ):
        await _db._ensure_column(db, "users", column, definition)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_amb_status ON users(ambassador_status)"
    )
    # История статусов прошлых сезонов («🔄 Новый сезон» обнуляет статусы в users). Личный
    # след делегата — стоит в database.db.USER_PURGE_TABLES.
    await db.execute(
        "CREATE TABLE IF NOT EXISTS ambassador_season_archive ("
        "telegram_id INTEGER NOT NULL, season TEXT NOT NULL, status TEXT, since TEXT, "
        "slot_at TEXT, pack_at TEXT, archived_at TEXT NOT NULL, "
        "PRIMARY KEY (telegram_id, season))"
    )
    await _migrate_ambassador_status(db)


async def _migrate_ambassador_status(db: aiosqlite.Connection) -> None:
    """Одноразово по `PRAGMA user_version` (та же форма, что миграции 1 и 2 в db.py): флаги
    фазы 32 переводятся в статус. Порядок active → left → candidate, и каждый шаг трогает только
    строки без статуса: амбассадор, который когда-то ответил «да» в анкете, остаётся active, а
    вышедший — left. Дата статуса берётся из той же истории (вступление/выход/подача анкеты),
    чтобы экраны сортировали мигрированных осмысленно. `is_ambassador` и
    `is_ambassador_candidate` не меняются. На чистой БД не пишет ни одной строки."""
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    if (row[0] if row else 0) >= _AMB_STATUS_MIGRATION_USER_VERSION:
        return
    changed = {}
    for status, where, stamp in (
        (STATUS_ACTIVE, "is_ambassador = 1", "ambassador_since"),
        (STATUS_LEFT, "COALESCE(is_ambassador, 0) = 0 AND ambassador_left_at IS NOT NULL",
         "ambassador_left_at"),
        (STATUS_CANDIDATE, "is_ambassador_candidate = 1", "registration_date"),
    ):
        cursor = await db.execute(
            f"UPDATE users SET ambassador_status = ?, ambassador_status_at = {stamp} "
            f"WHERE {where} AND ambassador_status IS NULL",
            (status,),
        )
        changed[status] = cursor.rowcount
    await db.execute(f"PRAGMA user_version = {_AMB_STATUS_MIGRATION_USER_VERSION}")
    logger.info("_migrate_ambassador_status: статус проставлен %s", changed)


# ── Единственный писатель статуса ────────────────────────────────────────────────────────────

async def set_status(tid: int, new: str | None, *, at: str, by: int | None = None,
                     expect: tuple[str, ...] | None = None) -> bool:
    """Единственный UPDATE статуса и зеркала `is_ambassador`. `new=None` (или 'none') — сброс.

    - в active из не-active: `ambassador_since = at`, `ambassador_left_at` и отметка «в запасе»
      сняты; повторный active дату вступления не переставляет (CR-08);
    - в любой не-active: `is_ambassador = 0`, место освобождается, только если пакет не выдан
      (CASE в SQL — решение в той же строке, что и запись);
    - в left: `ambassador_left_at = at`, `ambassador_since` не трогается (исторический факт).

    `expect` — допустимые текущие статусы ('none' = NULL); не совпало — ничего не пишет и
    возвращает False. Возврат: ровно одна строка изменена."""
    new = _norm(new)
    active = 1 if new == STATUS_ACTIVE else 0
    sql = (
        "UPDATE users SET ambassador_status = ?, ambassador_status_at = ?, "
        "ambassador_status_by = ?, is_ambassador = ?, "
        "ambassador_since = CASE WHEN ? = 1 AND COALESCE(ambassador_status, '') != 'active' "
        "THEN ? ELSE ambassador_since END, "
        "ambassador_left_at = CASE WHEN ? = 'left' THEN ? WHEN ? = 1 THEN NULL "
        "ELSE ambassador_left_at END, "
        "ambassador_reserve_at = CASE WHEN ? = 1 THEN NULL ELSE ambassador_reserve_at END, "
        "ambassador_slot_at = CASE WHEN ? = 0 AND ambassador_pack_at IS NULL THEN NULL "
        "ELSE ambassador_slot_at END "
        "WHERE telegram_id = ?"
    )
    params: list = [new, at, by, active, active, at, new or "", at, active, active, active,
                    int(tid)]
    if expect:
        wanted = [STATUS_NONE if e is None else e for e in expect]
        sql += (" AND COALESCE(ambassador_status, 'none') IN ("
                + ", ".join("?" for _ in wanted) + ")")
        params += wanted
    async with _db._connect() as conn:
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor.rowcount == 1


async def get_status(tid: int) -> dict | None:
    """Статус делегата (`'none'` вместо NULL) и даты; None — такого делегата нет."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT ambassador_status, ambassador_since, ambassador_slot_at, ambassador_pack_at, "
            "ambassador_reserve_at, ambassador_left_at, ambassador_declined_notified_at, "
            "ambassador_status_at FROM users WHERE telegram_id = ?",
            (int(tid),),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "status": row["ambassador_status"] or STATUS_NONE,
        "since": row["ambassador_since"],
        "slot_at": row["ambassador_slot_at"],
        "pack_at": row["ambassador_pack_at"],
        "reserve_at": row["ambassador_reserve_at"],
        "left_at": row["ambassador_left_at"],
        "declined_notified_at": row["ambassador_declined_notified_at"],
        "status_at": row["ambassador_status_at"],
    }
