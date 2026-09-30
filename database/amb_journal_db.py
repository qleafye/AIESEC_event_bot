"""Слой БД журнала зачётов приглашённых: расширенная `referral_credits`.

Одна строка на приглашённого навсегда (PK `invitee_id`): кто пригласил, когда одобрен, сезон,
волна, баллы (бывают 0), источник, отзыв, исключение, обратная строка монет. Колонки только
добавляются (`_ensure_column`), ничего не удаляется — старые строки читаются как прежде.

Отдельный модуль, а не ещё один блок в `database/db.py`: тот уже больше 12 тысяч строк.
Соединение — `_db._connect()` через атрибут модуля (тесты подменяют `config.DB_PATH`).
"""
from __future__ import annotations

import aiosqlite

from database import db as _db
from services.timeutil import msk_now

_JOURNAL_COLUMNS = (
    ("season", "TEXT"),
    ("referrer_was_ambassador", "INTEGER"),
    ("revoked_at", "TEXT"),
    ("revoked_reason", "TEXT"),
    ("excluded_at", "TEXT"),
    ("excluded_by", "INTEGER"),
    ("exclude_reason", "TEXT"),
    ("manual_by", "INTEGER"),
    ("manual_note", "TEXT"),
    ("reversal_coin_id", "INTEGER"),
)


async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Колонки журнала на `referral_credits` и индекс «пригласивший + сезон». Идемпотентно."""
    for name, definition in _JOURNAL_COLUMNS:
        await _db._ensure_column(db, "referral_credits", name, definition)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_referral_credits_referrer_season "
        "ON referral_credits(referrer_id, season)"
    )


async def record_approval(
    invitee_id: int, referrer_id: int, *, coins: int, wave_id: int | None, season: str | None,
    referrer_was_ambassador: bool, excluded: bool, reason: str, source: str = "approval",
    changed_by: int | None = None, manual_by: int | None = None, manual_note: str | None = None,
) -> str:
    """Строка журнала и (если coins > 0) строка монет — одной транзакцией.

    Возвращает "new" (строка создана), "reapproved" (строка была отозвана — отметка снята,
    монеты второй раз не начисляются) или "exists" (повтор, ничего не изменилось)."""
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    coins = 0 if excluded else max(int(coins), 0)
    async with _db._connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO referral_credits "
            "(invitee_id, referrer_id, coins, wave_id, credited_at, source, season, "
            "referrer_was_ambassador, excluded_at, excluded_by, exclude_reason, "
            "manual_by, manual_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(invitee_id), int(referrer_id), coins, wave_id, stamp, source, season,
                1 if referrer_was_ambassador else 0,
                stamp if excluded else None, None, reason if excluded else None,
                manual_by, manual_note,
            ),
        )
        if cursor.rowcount == 1:
            if coins > 0:
                await db.execute(
                    "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source, "
                    "task_id) VALUES (?, ?, ?, ?, ?, 'referral', NULL)",
                    (int(referrer_id), coins, reason, changed_by, stamp),
                )
            await db.commit()
            return "new"
        cursor = await db.execute(
            "UPDATE referral_credits SET revoked_at = NULL, revoked_reason = NULL "
            "WHERE invitee_id = ? AND revoked_at IS NOT NULL",
            (int(invitee_id),),
        )
        outcome = "reapproved" if cursor.rowcount == 1 else "exists"
        await db.commit()
        return outcome


async def get_row(invitee_id: int) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM referral_credits WHERE invitee_id = ?", (int(invitee_id),)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_revoked(invitee_ids=None, *, at: str) -> int:
    """Отзыв зачёта: строки журнала тех, чья заявка сейчас не одобрена. Баллы и ступени не
    трогает — только отметка `revoked_at`/`revoked_reason`. Возвращает число помеченных."""
    sql = (
        "UPDATE referral_credits SET revoked_at = ?, revoked_reason = 'status' "
        "WHERE revoked_at IS NULL AND invitee_id IN "
        "(SELECT telegram_id FROM users WHERE status != 'approved')"
    )
    params: list = [at]
    if invitee_ids is not None:
        ids = [int(i) for i in invitee_ids]
        if not ids:
            return 0
        sql += f" AND invitee_id IN ({','.join('?' * len(ids))})"
        params += ids
    async with _db._connect() as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.rowcount or 0


async def clear_revoked(invitee_ids=None) -> int:
    """Снимает отметку отзыва у снова одобренных. Монет второй раз не начисляет."""
    sql = (
        "UPDATE referral_credits SET revoked_at = NULL, revoked_reason = NULL "
        "WHERE revoked_at IS NOT NULL AND invitee_id IN "
        "(SELECT telegram_id FROM users WHERE status = 'approved')"
    )
    params: list = []
    if invitee_ids is not None:
        ids = [int(i) for i in invitee_ids]
        if not ids:
            return 0
        sql += f" AND invitee_id IN ({','.join('?' * len(ids))})"
        params += ids
    async with _db._connect() as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.rowcount or 0


def _season_frag(season: str | None) -> tuple[str, list]:
    if season:
        return "AND (u.season IS NULL OR u.season = ?)", [season]
    return "", []


async def missing_recent(since: str, season: str | None) -> list[int]:
    """Одобренные с `since` приглашённые без строки журнала (пропущенная врезка/сбой)."""
    frag, params = _season_frag(season)
    async with _db._connect() as db:
        async with db.execute(
            "SELECT u.telegram_id FROM users u "
            "LEFT JOIN referral_credits rc ON rc.invitee_id = u.telegram_id "
            "WHERE rc.invitee_id IS NULL AND u.status = 'approved' "
            "AND u.referrer_id IS NOT NULL AND u.referrer_id != 0 "
            "AND u.referrer_id != u.telegram_id AND u.approved_at >= ? " + frag
            + " ORDER BY u.approved_at",
            [since, *params],
        ) as cursor:
            return [int(r[0]) for r in await cursor.fetchall()]


async def backfill_candidates(season: str | None) -> list[dict]:
    """Одобренные приглашённые (любого срока) без строки журнала: для бэкафилла."""
    frag, params = _season_frag(season)
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT u.telegram_id AS invitee_id, u.referrer_id AS referrer_id, "
            "u.season AS season, r.full_name AS referrer_name, COALESCE(r.is_ambassador, 0) AS was_ambassador "
            "FROM users u "
            "LEFT JOIN referral_credits rc ON rc.invitee_id = u.telegram_id "
            "LEFT JOIN users r ON r.telegram_id = u.referrer_id "
            "WHERE rc.invitee_id IS NULL AND u.status = 'approved' "
            "AND u.referrer_id IS NOT NULL AND u.referrer_id != 0 "
            "AND u.referrer_id != u.telegram_id " + frag + " ORDER BY u.telegram_id",
            params,
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def has_journal_schema() -> bool:
    async with _db._connect() as db:
        async with db.execute("PRAGMA table_info(referral_credits)") as cursor:
            cols = {r[1] for r in await cursor.fetchall()}
    return {"season", "referrer_was_ambassador", "revoked_at"} <= cols


async def insert_backfill_rows(rows: list[dict], *, at: str) -> int:
    """Одна транзакция, INSERT OR IGNORE, coins 0, source 'backfill'. Монет, ступеней и
    сообщений не создаёт. Возвращает число реально вставленных строк."""
    inserted = 0
    async with _db._connect() as db:
        for row in rows:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO referral_credits "
                "(invitee_id, referrer_id, coins, wave_id, credited_at, source, season, "
                "referrer_was_ambassador) VALUES (?, ?, 0, NULL, ?, 'backfill', ?, ?)",
                (int(row["invitee_id"]), int(row["referrer_id"]), at, row.get("season"),
                 1 if row.get("was_ambassador") else 0),
            )
            inserted += cursor.rowcount or 0
        await db.commit()
    return inserted
