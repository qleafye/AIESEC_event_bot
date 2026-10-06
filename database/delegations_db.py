"""Слой БД делегаций вузов: оценка ответа формы (ЦА / не ЦА / проверить), решения менеджера,
привязка ответа к делегату в Telegram, сводки по вузам.

Схема (`delegation_answers`, `users.delegation*`) создаётся в `database.db.init_db` — схемой
владеет только бот. Источник правды по самому ответу — `external_form_answers` фазы внешних
форм; здесь лежит только оценка и то, что нужно сводкам без разбора payload.

Соединение берётся как `_db._connect()` через атрибут модуля (тесты подменяют `config.DB_PATH`).
Все значения — параметрами запроса; имена колонок из входа не собираются. В лог не пишется
ничего, кроме id: вуз, курс и ник из чужой формы — ПД делегата.

Время — строки "%Y-%m-%d %H:%M:%S" по Москве.
"""
from __future__ import annotations

import aiosqlite

from database import db as _db
from database.ext_forms_db import _with_payload
from services.timeutil import msk_now

_FMT = "%Y-%m-%d %H:%M:%S"

TA_STATUSES = ("ok", "no", "check")

# Пометки автоматического «проверить»: автоматика сама отправила ответ менеджеру, и очередная
# переоценка не должна вернуть его в «ЦА» (иначе каждый sweep гонял бы строку листа туда-сюда).
NOTE_REJECTED_IN_BOT = "rejected_in_bot"
NOTE_AMBIGUOUS_NICK = "ambiguous_nick"

# Точка «Вход» в `checkins` — тот же литерал, что `services.checkin.ENTRY_POINT`. Сам модуль
# checkin тянет aiogram и бота, слой БД его не импортирует; равенство держит тест
# `tests/test_delegations_db.py::test_entry_point_literal_matches_checkin`.
ENTRY_POINT = "entry"

_ARRIVED_SQL = (
    "EXISTS (SELECT 1 FROM checkins c WHERE c.telegram_id = d.linked_telegram_id "
    "AND c.point = ?)"
)


def _now() -> str:
    return msk_now().strftime(_FMT)


async def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def _fetchone(sql: str, params: tuple = ()) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None


async def _exec(sql: str, params: tuple = ()) -> int:
    async with _db._connect() as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.rowcount


def _check_status(ta_status: str) -> None:
    if ta_status not in TA_STATUSES:
        raise ValueError(f"неизвестный ta_status: {ta_status!r}")


def _linked_sql(linked: bool | None) -> str:
    if linked is None:
        return ""
    return " AND d.linked_telegram_id IS NOT NULL" if linked else " AND d.linked_telegram_id IS NULL"


# ---------- оценка ответа ----------

async def upsert_eval(
    form_id: int, answer_id: str, *, ta_status: str, university: str | None,
    course_raw: str | None, course_canonical: str | None, username_needle: str | None,
    answered_at: str | None,
) -> int:
    """Идемпотентная запись оценки по (form_id, answer_id); возвращает id строки.

    Переоценка обновляет данные ответа (вуз, курс, ник, время), но статус меняет только пока
    менеджер не решал сам: при `decided_by IS NOT NULL` прежний ta_status сохраняется — иначе
    очередная сверка формы молча перетёрла бы ручное «ЦА / не ЦА»."""
    _check_status(ta_status)
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO delegation_answers (form_id, answer_id, ta_status, university, "
            "course_raw, course_canonical, username_needle, answered_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(form_id, answer_id) DO UPDATE SET "
            "university = excluded.university, course_raw = excluded.course_raw, "
            "course_canonical = excluded.course_canonical, "
            "username_needle = excluded.username_needle, answered_at = excluded.answered_at, "
            "ta_status = CASE WHEN delegation_answers.decided_by IS NOT NULL "
            "THEN delegation_answers.ta_status "
            "WHEN delegation_answers.ta_status = 'check' AND excluded.ta_status = 'ok' "
            "AND delegation_answers.note IN ('rejected_in_bot', 'ambiguous_nick') "
            "THEN 'check' ELSE excluded.ta_status END",
            (form_id, str(answer_id), ta_status, university, course_raw, course_canonical,
             username_needle, answered_at, _now()),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM delegation_answers WHERE form_id = ? AND answer_id = ?",
            (form_id, str(answer_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0])


async def get_by_answer(form_id: int, answer_id: str) -> dict | None:
    return await _fetchone(
        "SELECT * FROM delegation_answers WHERE form_id = ? AND answer_id = ?",
        (form_id, str(answer_id)),
    )


async def get_by_id(row_id: int) -> dict | None:
    return await _fetchone("SELECT * FROM delegation_answers WHERE id = ?", (row_id,))


async def get_by_telegram_id(telegram_id: int) -> dict | None:
    return await _fetchone(
        "SELECT * FROM delegation_answers WHERE linked_telegram_id = ? ORDER BY id DESC LIMIT 1",
        (telegram_id,),
    )


async def set_decision(
    row_id: int, ta_status: str, decided_by: int | None, note: str | None = None,
) -> None:
    """Решение менеджера («ЦА / не ЦА / проверить»): фиксирует статус, автора и время.
    После него `upsert_eval` статус больше не меняет."""
    _check_status(ta_status)
    await _exec(
        "UPDATE delegation_answers SET ta_status = ?, decided_by = ?, decided_at = ?, note = ? "
        "WHERE id = ?",
        (ta_status, decided_by, _now(), note, row_id),
    )


async def link(row_id: int, telegram_id: int, how: str) -> bool:
    """Привязка ответа к делегату. True — привязан (или уже был привязан к этому же tid);
    False — строка уже привязана к другому человеку, ничего не изменено."""
    n = await _exec(
        "UPDATE delegation_answers SET linked_telegram_id = ?, link_how = ? "
        "WHERE id = ? AND (linked_telegram_id IS NULL OR linked_telegram_id = ?)",
        (telegram_id, how, row_id, telegram_id),
    )
    return n > 0


async def unlink(row_id: int, telegram_id: int) -> None:
    """Снять привязку, которую только что поставил этот же tid (откат неудавшегося одобрения)."""
    await _exec(
        "UPDATE delegation_answers SET linked_telegram_id = NULL, link_how = NULL "
        "WHERE id = ? AND linked_telegram_id = ?",
        (row_id, telegram_id),
    )


async def people_by_username(needle: str | None) -> list[int]:
    """telegram_id всех, у кого в боте (анкета или только /start) такой ник; без учёта регистра."""
    key = _db.username_needle(needle)
    if key is None:
        return []
    rows = await _fetchall(
        "SELECT telegram_id FROM users WHERE ltrim(username, '@') = ? COLLATE NOCASE "
        "UNION SELECT telegram_id FROM reg_started WHERE ltrim(username, '@') = ? COLLATE NOCASE",
        (key, key),
    )
    return [int(r["telegram_id"]) for r in rows]


async def find_pending_by_username(needle: str | None) -> list[dict]:
    """ЦА-ответы без привязки с таким ником (без учёта регистра и ведущего «@»).
    Пустой ник — пустой список без запроса (см. `database.db.username_needle`)."""
    key = _db.username_needle(needle)
    if key is None:
        return []
    return await _fetchall(
        "SELECT * FROM delegation_answers WHERE ta_status = 'ok' AND linked_telegram_id IS NULL "
        "AND username_needle = ? COLLATE NOCASE ORDER BY id",
        (key,),
    )


# ---------- списки и сводки ----------

async def list_by_status(
    form_id: int, ta_status: str, *, linked: bool | None, offset: int, limit: int,
) -> list[dict]:
    """Оценки формы с нужным статусом + сам ответ формы: `payload` (разобранный список) и
    `answer_row_id`. Порядок — по вузу, затем по id."""
    rows = await _fetchall(
        "SELECT d.*, a.payload AS payload, a.id AS answer_row_id "
        "FROM delegation_answers d "
        "JOIN external_form_answers a ON a.form_id = d.form_id AND a.answer_id = d.answer_id "
        "WHERE d.form_id = ? AND d.ta_status = ?" + _linked_sql(linked) + " "
        "ORDER BY d.university, d.id LIMIT ? OFFSET ?",
        (form_id, ta_status, int(limit), int(offset)),
    )
    return [_with_payload(r) for r in rows]


async def count_by_status(form_id: int, ta_status: str, *, linked: bool | None) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM delegation_answers d WHERE d.form_id = ? AND d.ta_status = ?"
        + _linked_sql(linked),
        (form_id, ta_status),
    )
    return int(row["n"]) if row else 0


async def summary_by_university(form_id: int) -> list[dict]:
    """Строки {university, total, ta, in_bot, arrived}: всего ответов, из них ЦА, привязано
    к боту, отмечено на входе форума. Сортировка — по числу ЦА, затем по вузу."""
    return await _fetchall(
        "SELECT d.university AS university, COUNT(*) AS total, "
        "SUM(CASE WHEN d.ta_status = 'ok' THEN 1 ELSE 0 END) AS ta, "
        "COUNT(DISTINCT d.linked_telegram_id) AS in_bot, "
        "COUNT(DISTINCT CASE WHEN d.linked_telegram_id IS NOT NULL AND " + _ARRIVED_SQL + " "
        "THEN d.linked_telegram_id END) AS arrived "
        "FROM delegation_answers d WHERE d.form_id = ? "
        "GROUP BY d.university ORDER BY ta DESC, d.university",
        (ENTRY_POINT, form_id),
    )


async def list_unevaluated(form_id: int, limit: int = 500) -> list[dict]:
    """Ответы формы, у которых ещё нет оценки: {answer_id, answer_row_id} — для sweep'а,
    страхующего хук приёма ответа."""
    return await _fetchall(
        "SELECT a.answer_id AS answer_id, a.id AS answer_row_id FROM external_form_answers a "
        "WHERE a.form_id = ? AND NOT EXISTS (SELECT 1 FROM delegation_answers d "
        "WHERE d.form_id = a.form_id AND d.answer_id = a.answer_id) "
        "ORDER BY a.id LIMIT ?",
        (form_id, int(limit)),
    )


async def mirror_status_for(form_id: int, answer_ids: list[str]) -> dict[str, dict]:
    """answer_id -> {ta_status, linked, arrived, decided_by} для колонки «В боте» зеркала.
    Ответы без оценки в словарь не попадают."""
    ids = [str(a) for a in answer_ids]
    if not ids:
        return {}
    out: dict[str, dict] = {}
    chunk = 400  # предел числа параметров SQLite — с запасом
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        marks = ",".join("?" * len(part))
        rows = await _fetchall(
            "SELECT d.answer_id AS answer_id, d.ta_status AS ta_status, d.decided_by AS decided_by, "
            "d.linked_telegram_id AS linked_telegram_id, d.sheet_greyed AS sheet_greyed, "
            "CASE WHEN d.linked_telegram_id IS NOT NULL AND " + _ARRIVED_SQL + " "
            "THEN 1 ELSE 0 END AS arrived "
            f"FROM delegation_answers d WHERE d.form_id = ? AND d.answer_id IN ({marks})",
            (ENTRY_POINT, form_id, *part),
        )
        for r in rows:
            out[r["answer_id"]] = {
                "ta_status": r["ta_status"],
                "linked": r["linked_telegram_id"] is not None,
                "arrived": bool(r["arrived"]),
                "decided_by": r["decided_by"],
                "greyed": bool(r["sheet_greyed"]),
            }
    return out


async def set_greyed(form_id: int, flags: dict[str, int]) -> None:
    """Запомнить, какие строки листа бот покрасил серым сам: {answer_id: 1 | 0}."""
    for answer_id, flag in flags.items():
        await _exec(
            "UPDATE delegation_answers SET sheet_greyed = ? WHERE form_id = ? AND answer_id = ?",
            (1 if flag else 0, form_id, str(answer_id)),
        )
