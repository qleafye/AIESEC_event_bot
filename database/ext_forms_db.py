"""Слой БД внешних форм (Яндекс/Google): подключения, формы, ответы, колонки, очередь.

Схема создаётся в `database.db.init_db` — схемой владеет только бот, здесь миграций нет.
Соединение берётся как `_db._connect()` через атрибут модуля, а не прямым импортом функции:
тесты подменяют `config.DB_PATH`, и прямой импорт закрепил бы старую ссылку.

Секреты (ключи приложения Яндекса, токены менеджера) лежат в своих таблицах, а не в общих
настройках бота: запись настройки логирует значение. Модуль не пишет в лог ни значений анкет,
ни токенов.

Время — строки "%Y-%m-%d %H:%M:%S" по Москве; их передаёт вызывающий.
"""
from __future__ import annotations

import json

import aiosqlite

from database import db as _db
from services.timeutil import msk_now

_FMT = "%Y-%m-%d %H:%M:%S"
_ERR_MAX = 300


def _now() -> str:
    return msk_now().strftime(_FMT)


def _err(text: str | None) -> str | None:
    if text is None:
        return None
    return str(text)[:_ERR_MAX]


def _with_payload(row: dict) -> dict:
    d = dict(row)
    raw = d.get("payload")
    if isinstance(raw, str):
        try:
            d["payload"] = json.loads(raw)
        except ValueError:
            d["payload"] = []
    return d


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


# ---------- секреты приложения ----------

async def get_app_secret(name: str) -> str | None:
    row = await _fetchone("SELECT value FROM external_form_secrets WHERE name = ?", (name,))
    return row["value"] if row else None


async def set_app_secret(name: str, value: str, by: int | None) -> None:
    await _exec(
        "INSERT INTO external_form_secrets (name, value, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (name, value, _now(), by),
    )


# ---------- подключения ----------

async def create_connection(
    *, platform, org_id, org_header, access_token, refresh_token, expires_at, created_by,
) -> int:
    async with _db._connect() as db:
        cursor = await db.execute(
            "INSERT INTO external_form_connections (platform, org_id, org_header, access_token, "
            "refresh_token, expires_at, status, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?)",
            (platform, org_id, org_header, access_token, refresh_token, expires_at, _now(), created_by),
        )
        await db.commit()
        return cursor.lastrowid


async def get_connection(conn_id: int) -> dict | None:
    return await _fetchone("SELECT * FROM external_form_connections WHERE id = ?", (conn_id,))


async def get_yandex_connection() -> dict | None:
    return await _fetchone(
        "SELECT * FROM external_form_connections WHERE platform = 'yandex' ORDER BY id DESC LIMIT 1"
    )


async def upsert_yandex_connection(
    *, org_id, org_header, access_token, refresh_token, expires_at, by,
) -> int:
    """Одно подключение Яндекса на бота: повторный вход обновляет ту же строку, и формы
    продолжают ссылаться на прежний id."""
    existing = await get_yandex_connection()
    if existing is None:
        return await create_connection(
            platform="yandex", org_id=org_id, org_header=org_header, access_token=access_token,
            refresh_token=refresh_token, expires_at=expires_at, created_by=by,
        )
    await _exec(
        "UPDATE external_form_connections SET org_id = ?, org_header = ?, access_token = ?, "
        "refresh_token = ?, expires_at = ?, status = 'ok', alerted_at = NULL WHERE id = ?",
        (org_id, org_header, access_token, refresh_token, expires_at, existing["id"]),
    )
    return existing["id"]


async def update_connection_tokens(conn_id: int, access_token, refresh_token, expires_at) -> None:
    await _exec(
        "UPDATE external_form_connections SET access_token = ?, refresh_token = ?, "
        "expires_at = ?, status = 'ok', alerted_at = NULL WHERE id = ?",
        (access_token, refresh_token, expires_at, conn_id),
    )


async def set_connection_status(conn_id: int, status: str, *, alerted_at: str | None = None) -> None:
    await _exec(
        "UPDATE external_form_connections SET status = ?, alerted_at = ? WHERE id = ?",
        (status, alerted_at, conn_id),
    )


async def list_connections_to_alert() -> list[dict]:
    return await _fetchall(
        "SELECT * FROM external_form_connections WHERE status = 'needs_reauth' AND alerted_at IS NULL"
    )


# ---------- формы ----------

async def create_form(
    *, platform, external_id, title, connection_id=None, gsheet_gid=None, secret=None,
    key_username_q=None, key_phone_q=None, mirror_tab=None, created_by=None,
) -> int:
    async with _db._connect() as db:
        cursor = await db.execute(
            "INSERT INTO external_forms (platform, connection_id, external_id, gsheet_gid, title, "
            "secret, key_username_q, key_phone_q, mirror_tab, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, connection_id, external_id, gsheet_gid, title, secret,
             key_username_q, key_phone_q, mirror_tab, _now(), created_by),
        )
        await db.commit()
        return cursor.lastrowid


async def get_form(form_id: int) -> dict | None:
    return await _fetchone("SELECT * FROM external_forms WHERE id = ?", (form_id,))


async def get_form_by_secret(secret: str) -> dict | None:
    if not secret:
        return None
    return await _fetchone("SELECT * FROM external_forms WHERE secret = ?", (secret,))


async def get_form_by_external(platform: str, external_id: str, gsheet_gid=None) -> dict | None:
    if gsheet_gid is None:
        return await _fetchone(
            "SELECT * FROM external_forms WHERE platform = ? AND external_id = ? "
            "AND gsheet_gid IS NULL ORDER BY id LIMIT 1",
            (platform, external_id),
        )
    return await _fetchone(
        "SELECT * FROM external_forms WHERE platform = ? AND external_id = ? "
        "AND gsheet_gid = ? ORDER BY id LIMIT 1",
        (platform, external_id, gsheet_gid),
    )


async def list_forms() -> list[dict]:
    return await _fetchall(
        "SELECT f.id, f.platform, f.connection_id, f.external_id, f.gsheet_gid, f.title, "
        "f.secret, f.key_username_q, f.key_phone_q, f.mirror_tab, f.mirror_error, f.status, "
        "f.notify, f.notified_at, f.last_sync_at, f.sync_error, f.created_at, f.created_by, "
        "(SELECT COUNT(*) FROM external_form_answers a WHERE a.form_id = f.id) AS total, "
        "(SELECT COUNT(*) FROM external_form_answers a WHERE a.form_id = f.id "
        "   AND a.matched_telegram_id IS NULL) AS unmatched, "
        "(SELECT MAX(COALESCE(a.answered_at, a.received_at)) FROM external_form_answers a "
        "   WHERE a.form_id = f.id) AS last_answer_at "
        "FROM external_forms f ORDER BY f.id"
    )


async def list_active_forms(platform: str) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM external_forms WHERE platform = ? AND status = 'active' ORDER BY id",
        (platform,),
    )


async def set_form_status(form_id: int, status: str) -> None:
    await _exec("UPDATE external_forms SET status = ? WHERE id = ?", (status, form_id))


async def set_form_notify(form_id: int, on: bool) -> None:
    await _exec("UPDATE external_forms SET notify = ? WHERE id = ?", (1 if on else 0, form_id))


async def set_form_keys(form_id: int, username_q, phone_q) -> None:
    await _exec(
        "UPDATE external_forms SET key_username_q = ?, key_phone_q = ? WHERE id = ?",
        (username_q, phone_q, form_id),
    )


async def set_form_mirror(form_id: int, tab: str | None, error: str | None) -> None:
    await _exec(
        "UPDATE external_forms SET mirror_tab = ?, mirror_error = ? WHERE id = ?",
        (tab, _err(error), form_id),
    )


async def set_form_secret(form_id: int, secret: str) -> None:
    await _exec("UPDATE external_forms SET secret = ? WHERE id = ?", (secret, form_id))


async def set_form_sync(form_id: int, *, last_sync_at, sync_error) -> None:
    await _exec(
        "UPDATE external_forms SET last_sync_at = ?, sync_error = ? WHERE id = ?",
        (last_sync_at, _err(sync_error), form_id),
    )


async def set_form_notified(form_id: int, ts: str) -> None:
    await _exec("UPDATE external_forms SET notified_at = ? WHERE id = ?", (ts, form_id))


# ---------- очередь дочитывания ----------

async def enqueue_pending(form_id: int, answer_id: str, delivery_id: str | None, now: str) -> bool:
    n = await _exec(
        "INSERT OR IGNORE INTO external_form_pending "
        "(form_id, answer_id, delivery_id, received_at, next_try_at) "
        "SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS "
        "(SELECT 1 FROM external_form_deleted WHERE form_id = ? AND answer_id = ?)",
        (form_id, str(answer_id), delivery_id, now, now, form_id, str(answer_id)),
    )
    return n > 0


async def list_due_pending(now: str, limit: int) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM external_form_pending WHERE next_try_at <= ? ORDER BY id LIMIT ?",
        (now, int(limit)),
    )


async def drop_pending(row_id: int) -> None:
    await _exec("DELETE FROM external_form_pending WHERE id = ?", (row_id,))


async def fail_pending(row_id: int, err: str, next_try_at: str) -> None:
    await _exec(
        "UPDATE external_form_pending SET attempts = attempts + 1, last_error = ?, "
        "next_try_at = ? WHERE id = ?",
        (_err(err), next_try_at, row_id),
    )


async def count_pending(form_id: int) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM external_form_pending WHERE form_id = ?", (form_id,)
    )
    return row["n"] if row else 0


# ---------- ответы ----------

async def insert_answer(
    *, form_id, answer_id, answered_at, received_at, payload: list[dict], raw: str | None,
    matched_telegram_id: int | None, match_how: str | None,
) -> bool:
    """False = такой ответ уже есть или был удалён (надгробие), ничего не изменено."""
    n = await _exec(
        "INSERT OR IGNORE INTO external_form_answers (form_id, answer_id, answered_at, "
        "received_at, payload, raw, matched_telegram_id, match_how) "
        "SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
        "(SELECT 1 FROM external_form_deleted WHERE form_id = ? AND answer_id = ?)",
        (form_id, str(answer_id), answered_at, received_at,
         json.dumps(payload, ensure_ascii=False), raw, matched_telegram_id, match_how,
         form_id, str(answer_id)),
    )
    if n > 0:
        await _exec(
            "UPDATE external_forms SET last_answer_at = ? WHERE id = ?",
            (answered_at or received_at, form_id),
        )
    return n > 0


async def known_answer_ids(form_id: int) -> set[str]:
    """Id, которые заново тянуть не нужно: сохранённые и удалённые (надгробия)."""
    rows = await _fetchall(
        "SELECT answer_id FROM external_form_answers WHERE form_id = ? "
        "UNION SELECT answer_id FROM external_form_deleted WHERE form_id = ?",
        (form_id, form_id),
    )
    return {r["answer_id"] for r in rows}


async def upsert_columns(form_id: int, items: list[tuple[str, str]]) -> list[dict]:
    """Новые вопросы получают следующие позиции справа; существующие qkey не трогаются."""
    added: list[dict] = []
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT qkey, position FROM external_form_columns WHERE form_id = ?", (form_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        known = {r["qkey"] for r in rows}
        pos = max((r["position"] for r in rows), default=0)
        for qkey, label in items:
            if qkey in known:
                continue
            pos += 1
            known.add(qkey)
            await db.execute(
                "INSERT INTO external_form_columns (form_id, qkey, label, position) "
                "VALUES (?, ?, ?, ?)", (form_id, qkey, label, pos),
            )
            added.append({"qkey": qkey, "label": label, "position": pos})
        await db.commit()
    return added


async def list_columns(form_id: int) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM external_form_columns WHERE form_id = ? ORDER BY position", (form_id,)
    )


async def mark_headers_written(form_id: int, qkeys: list[str]) -> None:
    async with _db._connect() as db:
        for qkey in qkeys:
            await db.execute(
                "UPDATE external_form_columns SET header_written = 1 WHERE form_id = ? AND qkey = ?",
                (form_id, qkey),
            )
        await db.commit()


async def list_unmatched_answers(limit: int) -> list[dict]:
    rows = await _fetchall(
        "SELECT a.* FROM external_form_answers a "
        "JOIN external_forms f ON f.id = a.form_id "
        "WHERE a.matched_telegram_id IS NULL "
        "AND (f.key_username_q IS NOT NULL OR f.key_phone_q IS NOT NULL) "
        "ORDER BY a.id LIMIT ?",
        (int(limit),),
    )
    return [_with_payload(r) for r in rows]


async def set_answer_match(answer_row_id: int, telegram_id: int, how: str) -> bool:
    n = await _exec(
        "UPDATE external_form_answers SET matched_telegram_id = ?, match_how = ?, "
        "sheet_state = CASE WHEN sheet_state = 'synced' THEN 'update' ELSE sheet_state END "
        "WHERE id = ? AND matched_telegram_id IS NULL",
        (telegram_id, how, answer_row_id),
    )
    return n > 0


async def list_sheet_due(now: str, limit: int) -> list[dict]:
    rows = await _fetchall(
        "SELECT a.*, f.mirror_tab AS mirror_tab, f.title AS title "
        "FROM external_form_answers a JOIN external_forms f ON f.id = a.form_id "
        "WHERE a.sheet_state IN ('append', 'update') "
        "AND (a.sheet_next_try_at IS NULL OR a.sheet_next_try_at <= ?) "
        "AND f.mirror_tab IS NOT NULL AND f.mirror_error IS NULL "
        "ORDER BY a.id LIMIT ?",
        (now, int(limit)),
    )
    return [_with_payload(r) for r in rows]


async def mark_sheet_state(ids: list[int], state: str) -> None:
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    await _exec(
        f"UPDATE external_form_answers SET sheet_state = ?, sheet_attempts = 0, "
        f"sheet_next_try_at = NULL WHERE id IN ({marks})",
        (state, *ids),
    )


async def mark_sheet_synced(items: list[tuple[int, str, int | None]]) -> None:
    """Отмечает записанные в лист строки как synced, но только те, что за время записи
    не изменились: (id, ожидаемое sheet_state, привязанный делегат на момент чтения).
    Иначе привязка, пришедшая во время записи, потеряла бы своё обновление."""
    if not items:
        return
    async with _db._connect() as db:
        for row_id, expect, tid in items:
            await db.execute(
                "UPDATE external_form_answers SET sheet_state = 'synced', sheet_attempts = 0, "
                "sheet_next_try_at = NULL WHERE id = ? AND sheet_state = ? "
                "AND matched_telegram_id IS ?",
                (row_id, expect, tid),
            )
        await db.commit()


async def fail_sheet(ids: list[int], next_try_at: str) -> None:
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    await _exec(
        f"UPDATE external_form_answers SET sheet_attempts = sheet_attempts + 1, "
        f"sheet_next_try_at = ? WHERE id IN ({marks})",
        (next_try_at, *ids),
    )


async def count_answers(form_id: int) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM external_form_answers WHERE form_id = ?", (form_id,)
    )
    return row["n"] if row else 0


async def delete_form_answers(form_id: int) -> int:
    """Удаляет анкеты и очередь формы. Колонки остаются: их позиции совпадают с уже
    записанной шапкой вкладки."""
    async with _db._connect() as db:
        # Источник помнит анкеты: без надгробий ближайшая сверка вернула бы стёртое.
        await db.execute(
            "INSERT OR IGNORE INTO external_form_deleted (form_id, answer_id, deleted_at) "
            "SELECT form_id, answer_id, ? FROM external_form_answers WHERE form_id = ? "
            "UNION SELECT form_id, answer_id, ? FROM external_form_pending WHERE form_id = ?",
            (_now(), form_id, _now(), form_id),
        )
        cursor = await db.execute("DELETE FROM external_form_answers WHERE form_id = ?", (form_id,))
        n = cursor.rowcount
        await db.execute("DELETE FROM external_form_pending WHERE form_id = ?", (form_id,))
        await db.commit()
        return n


async def answers_for_user(telegram_id: int) -> list[dict]:
    rows = await _fetchall(
        "SELECT a.*, f.title AS title FROM external_form_answers a "
        "JOIN external_forms f ON f.id = a.form_id "
        "WHERE a.matched_telegram_id = ? "
        "ORDER BY COALESCE(a.answered_at, a.received_at) DESC, a.id DESC",
        (telegram_id,),
    )
    return [_with_payload(r) for r in rows]


async def count_new_since(form_id: int, ts: str | None) -> int:
    if ts is None:
        return await count_answers(form_id)
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM external_form_answers WHERE form_id = ? AND received_at > ?",
        (form_id, ts),
    )
    return row["n"] if row else 0
