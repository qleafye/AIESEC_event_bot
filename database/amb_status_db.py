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
    # Колонку ответа анкеты и дату подачи init_db добавляет не всегда (их заводит реестр
    # вопросов), поэтому на старой схеме без них шаг пропускается, а не роняет старт бота.
    has_candidate = await _db._column_exists(db, "users", "is_ambassador_candidate")
    reg_stamp = ("registration_date"
                 if await _db._column_exists(db, "users", "registration_date") else "NULL")
    steps = [
        (STATUS_ACTIVE, "is_ambassador = 1", "ambassador_since"),
        (STATUS_LEFT, "COALESCE(is_ambassador, 0) = 0 AND ambassador_left_at IS NOT NULL",
         "ambassador_left_at"),
    ]
    if has_candidate:
        steps.append((STATUS_CANDIDATE, "is_ambassador_candidate = 1", reg_stamp))
    changed = {}
    for status, where, stamp in steps:
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


# ── Место в лимите и пакет ───────────────────────────────────────────────────────────────────

_SLOTS_TAKEN_SQL = "SELECT COUNT(*) FROM users WHERE ambassador_slot_at IS NOT NULL"


async def slots_taken() -> int:
    """Сколько мест занято. Сезон в счёт не входит: «🔄 Новый сезон» обнуляет места, а
    вышедший с выданным пакетом место держит (подарок уже у него)."""
    async with _db._connect() as conn:
        async with conn.execute(_SLOTS_TAKEN_SQL) as cursor:
            return int((await cursor.fetchone())[0])


async def try_claim_slot(tid: int, *, limit: int, season: str, at: str) -> bool:
    """Выдать место в лимите: только active-амбассадору с ОДОБРЕННОЙ заявкой текущего сезона и
    только если места есть (`limit <= 0` — без ограничения). Одна транзакция `BEGIN IMMEDIATE`:
    второй процесс ждёт коммита первого и считает уже с его местом — при лимите 17 и 16 занятых
    две одновременные выдачи дают ровно одно место. Уже с местом — False, дата не меняется."""
    async with _db._connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if int(limit) > 0:
                async with conn.execute(_SLOTS_TAKEN_SQL) as cursor:
                    taken = int((await cursor.fetchone())[0])
                if taken >= int(limit):
                    await conn.rollback()
                    return False
            cursor = await conn.execute(
                "UPDATE users SET ambassador_slot_at = ? WHERE telegram_id = ? "
                "AND ambassador_slot_at IS NULL AND ambassador_status = 'active' "
                "AND status = 'approved' AND COALESCE(season, '') = ?",
                (at, int(tid), (season or "").strip()),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return cursor.rowcount == 1


async def set_pack(tid: int, given: bool, *, at: str) -> bool:
    """Отметка «пакет выдан». Снятие отметки у того, кто уже не в команде, освобождает его
    место: держать его было нечем, кроме пакета. True — делегат найден."""
    if given:
        sql = ("UPDATE users SET ambassador_pack_at = COALESCE(ambassador_pack_at, ?) "
               "WHERE telegram_id = ?")
        params: tuple = (at, int(tid))
    else:
        sql = ("UPDATE users SET ambassador_pack_at = NULL, ambassador_slot_at = CASE "
               "WHEN COALESCE(ambassador_status, '') = 'active' THEN ambassador_slot_at "
               "ELSE NULL END WHERE telegram_id = ?")
        params = (int(tid),)
    async with _db._connect() as conn:
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor.rowcount == 1


async def set_reserve(tid: int, *, at: str) -> bool:
    """«Не сейчас»: кандидат остаётся в запасе, в списке уходит вниз. Только у candidate."""
    async with _db._connect() as conn:
        cursor = await conn.execute(
            "UPDATE users SET ambassador_reserve_at = ? "
            "WHERE telegram_id = ? AND ambassador_status = 'candidate'",
            (at, int(tid)),
        )
        await conn.commit()
        return cursor.rowcount == 1


async def release_slot(tid: int) -> bool:
    """Снять место (например, собственную заявку амбассадора больше не одобряют). Статус не
    меняется; выданный пакет не отбирается — тогда False и место остаётся."""
    async with _db._connect() as conn:
        cursor = await conn.execute(
            "UPDATE users SET ambassador_slot_at = NULL WHERE telegram_id = ? "
            "AND ambassador_slot_at IS NOT NULL AND ambassador_pack_at IS NULL",
            (int(tid),),
        )
        await conn.commit()
        return cursor.rowcount == 1


async def unapproved_slot_holders(season: str) -> list[int]:
    """Кто держит место без пакета, хотя заявка уже не одобрена или не этого сезона — для
    сверки (место только с одобренной заявкой текущего сезона). Только чтение."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT telegram_id FROM users WHERE ambassador_slot_at IS NOT NULL "
            "AND ambassador_pack_at IS NULL AND (COALESCE(status, '') != 'approved' "
            "OR COALESCE(season, '') != ?) ORDER BY telegram_id",
            ((season or "").strip(),),
        ) as cursor:
            return [int(r[0]) for r in await cursor.fetchall()]


# ── Списки для экранов ───────────────────────────────────────────────────────────────────────

# Условие и порядок каждого списка. Кандидаты: сначала без отметки «в запасе», внутри — кто
# раньше попросился.
_FILTERS: dict[str, tuple[str, str]] = {
    "candidates": ("ambassador_status = 'candidate'",
                   "(ambassador_reserve_at IS NOT NULL), ambassador_status_at, telegram_id"),
    "team": ("ambassador_status = 'active'",
             "ambassador_since, ambassador_status_at, telegram_id"),
    "no_pack": ("ambassador_status = 'active' AND ambassador_slot_at IS NULL",
                "ambassador_since, ambassador_status_at, telegram_id"),
    "declined": ("ambassador_status = 'declined'", "ambassador_status_at, telegram_id"),
}


def _filter_where(flt: str, city_scope) -> tuple[str, str, list]:
    if flt not in _FILTERS:
        raise ValueError(f"неизвестный список амбассадоров: {flt!r}")
    where, order = _FILTERS[flt]
    frag, params = _db._city_clause(city_scope, "event_city")
    if frag:
        where = f"{where} AND {frag}"
    return where, order, params


async def list_page(flt: str, *, offset: int, limit: int, city_scope=None) -> list[dict]:
    """Страница списка `candidates` / `team` / `no_pack` / `declined`."""
    where, order, params = _filter_where(flt, city_scope)
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM users WHERE {where} ORDER BY {order} "
            "LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def count_by_filter(flt: str, *, city_scope=None) -> int:
    where, _order, params = _filter_where(flt, city_scope)
    async with _db._connect() as conn:
        async with conn.execute(f"SELECT COUNT(*) FROM users WHERE {where}", params) as cursor:
            return int((await cursor.fetchone())[0])


# ── Массовый отказ ───────────────────────────────────────────────────────────────────────────

async def decline_remaining(*, at: str, by: int | None) -> list[int]:
    """«Вежливо отказать всем оставшимся»: все кандидаты -> declined одной транзакцией.
    Возвращает, кому отказали (для рассылки); повторный вызов — пустой список."""
    async with _db._connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            async with conn.execute(
                "SELECT telegram_id FROM users WHERE ambassador_status = 'candidate' "
                "ORDER BY telegram_id"
            ) as cursor:
                ids = [int(r[0]) for r in await cursor.fetchall()]
            if ids:
                await conn.execute(
                    "UPDATE users SET ambassador_status = 'declined', ambassador_status_at = ?, "
                    "ambassador_status_by = ? WHERE ambassador_status = 'candidate' "
                    f"AND telegram_id IN ({', '.join('?' for _ in ids)})",
                    [at, by, *ids],
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return ids


async def claim_decline_notice(tid: int, *, at: str) -> bool:
    """Отметка «письмо об отказе отправлено» ДО отправки: True — один раз, дальше False
    (повтор рассылки или две вкладки не шлют человеку второе письмо)."""
    async with _db._connect() as conn:
        cursor = await conn.execute(
            "UPDATE users SET ambassador_declined_notified_at = ? WHERE telegram_id = ? "
            "AND ambassador_declined_notified_at IS NULL AND ambassador_status = 'declined'",
            (at, int(tid)),
        )
        await conn.commit()
        return cursor.rowcount == 1


async def declined_pending_notice() -> list[int]:
    """Отказанные, кому письмо об отказе ещё не ушло (нет отметки): рассылка обходит их всех,
    а не только отказанных этим нажатием, — повтор после рестарта досылает хвост."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT telegram_id FROM users WHERE ambassador_status = 'declined' "
            "AND ambassador_declined_notified_at IS NULL ORDER BY telegram_id"
        ) as cursor:
            return [int(r[0]) for r in await cursor.fetchall()]


# ── Новый сезон и выгрузки ───────────────────────────────────────────────────────────────────

_HAS_AMB_STATE = (
    "(ambassador_status IS NOT NULL OR ambassador_slot_at IS NOT NULL "
    "OR ambassador_pack_at IS NOT NULL OR COALESCE(is_ambassador, 0) = 1)"
)


async def archive_and_reset_season(old_season: str, *, at: str) -> int:
    """«🔄 Новый сезон»: статусы уходят в `ambassador_season_archive` (сезон — `old_season`,
    пустой — 'legacy', как у архива заданий), у всех обнуляются статус, место, пакет, запас и
    отметка отказа, `is_ambassador = 0`. `ambassador_since`/`ambassador_left_at` остаются —
    это история человека. Одна транзакция; повтор с тем же сезоном перезаписывает архив.
    Возвращает, скольким сброшено."""
    stamp = (old_season or "").strip() or "legacy"
    async with _db._connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "INSERT OR REPLACE INTO ambassador_season_archive "
                "(telegram_id, season, status, since, slot_at, pack_at, archived_at) "
                "SELECT telegram_id, ?, ambassador_status, ambassador_since, "
                "ambassador_slot_at, ambassador_pack_at, ? FROM users "
                "WHERE ambassador_status IS NOT NULL",
                (stamp, at),
            )
            cursor = await conn.execute(
                "UPDATE users SET ambassador_status = NULL, is_ambassador = 0, "
                "ambassador_status_at = ?, ambassador_slot_at = NULL, ambassador_pack_at = NULL, "
                f"ambassador_reserve_at = NULL, ambassador_declined_notified_at = NULL "
                f"WHERE {_HAS_AMB_STATE}",
                (at,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return cursor.rowcount


async def count_with_status() -> int:
    """Сколько делегатов с непустым статусом — «сбросится у N человек» на экране сезона."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM users WHERE ambassador_status IS NOT NULL"
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def export_rows(*, city_scope=None) -> list[dict]:
    """Все с непустым статусом — для выгрузки в таблицу."""
    where = "ambassador_status IS NOT NULL"
    frag, params = _db._city_clause(city_scope, "event_city")
    if frag:
        where = f"{where} AND {frag}"
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            f"SELECT {_EXPORT_COLUMNS} FROM users WHERE {where} "
            "ORDER BY ambassador_status, ambassador_status_at, telegram_id",
            params,
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def export_archive_rows() -> list[dict]:
    """Архив прошлых сезонов с именем и ником из users (если человек ещё есть)."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT a.season, a.telegram_id, u.full_name, u.username, u.event_city, "
            "a.status, a.since, a.slot_at, a.pack_at, a.archived_at FROM ambassador_season_archive a "
            "LEFT JOIN users u ON u.telegram_id = a.telegram_id "
            "ORDER BY a.archived_at, a.season, a.telegram_id"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]
