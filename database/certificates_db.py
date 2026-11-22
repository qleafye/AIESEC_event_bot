"""Сертификаты участникам после форума: шаблоны, списки по @никам, имена, выдачи.

- `certificate_templates` — PDF-шаблон каждого вида (участник/компетенции/волонтёр/амбассадор) и варианта
  дат (`all` — «оба дня», `YYYY-MM-DD` — версия на один день). Хранится `file_id` Telegram:
  менеджер загрузил файл в бот, сам файл с подписью и печатью в репозиторий не попадает.
- `certificate_people` — волонтёры и амбассадоры: @ник и имя из списка менеджера
  (участник определяется отметкой входа). Имя подтверждённого сертификата участника
  живёт отдельно, в `certificate_names`.
- `certificate_names` — имя, которое человек подтвердил или исправил перед выдачей.
- `certificate_sends` — что и кому уже выдано (повторный запрос отдаёт файл снова, счётчик
  в админке — по этой таблице).
- `certificate_announces` — кому ушло сообщение «сертификат готов»: рассылка идёт джобой
  раз в несколько минут, PK не даёт написать человеку дважды.
- `certificate_config` — одна строка: когда открыть выдачу.

Отдельный модуль, как `chat_coins_db`: `database/db.py` и так больше 12 тысяч строк."""
from __future__ import annotations

import json
import sqlite3

import aiosqlite
from datetime import datetime

from database import db as _db
from services.infra.timeutil import msk_now

KIND_PARTICIPANT = "participant"
KIND_VOLUNTEER = "volunteer"
KIND_AMBASSADOR = "ambassador"
KIND_COMPETENCY = "competency"  # шкалы по тесту компетенций, только вариант «все дни»
KINDS = (KIND_PARTICIPANT, KIND_COMPETENCY, KIND_VOLUNTEER, KIND_AMBASSADOR)
KIND_SHORT = {
    KIND_PARTICIPANT: "Участник",
    KIND_COMPETENCY: "Компетенции",
    KIND_VOLUNTEER: "Волонтёр",
    KIND_AMBASSADOR: "Амбассадор",
}
VARIANT_ALL = "all"


def variant_label(variant: str) -> str:
    """Подпись варианта на кнопке: «все дни» или день.месяц. Не код ключа."""
    if variant == VARIANT_ALL:
        return "все дни"
    try:
        return datetime.strptime(variant, "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        return variant


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_templates ("
        "kind TEXT NOT NULL, variant TEXT NOT NULL, file_id TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, updated_by INTEGER, markup TEXT, "
        "PRIMARY KEY (kind, variant))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_people ("
        "kind TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, added_at TEXT NOT NULL, "
        "list_name TEXT, name_ok INTEGER NOT NULL DEFAULT 0, send_blocked INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (kind, username))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_names ("
        "telegram_id INTEGER PRIMARY KEY, name TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_sends ("
        "telegram_id INTEGER NOT NULL, kind TEXT NOT NULL, sent_at TEXT NOT NULL, "
        "PRIMARY KEY (telegram_id, kind))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_announces ("
        "telegram_id INTEGER PRIMARY KEY, sent_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_config ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), open_at TEXT)"
    )
    # Старые базы уже создали certificate_people без этих колонок. CREATE IF NOT EXISTS их не добавит.
    await _db._ensure_column(db, "certificate_people", "list_name", "TEXT")
    await _db._ensure_column(db, "certificate_people", "name_ok", "INTEGER NOT NULL DEFAULT 0")
    await _db._ensure_column(db, "certificate_people", "send_blocked", "INTEGER NOT NULL DEFAULT 0")
    await _db._ensure_column(db, "certificate_templates", "markup", "TEXT")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_kinds ("
        "id INTEGER PRIMARY KEY, title TEXT NOT NULL, audience TEXT NOT NULL, "
        "role_key TEXT, by_day INTEGER NOT NULL DEFAULT 0, fields TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


# ── свои виды (четыре старых кода живут в KINDS, не в этой таблице) ────────────────────

AUDIENCES = ("checkin", "test", "nicks", "role")
KIND_FIELDS = ("name", "date", "city", "scales")


def kind_code(kind_id: int) -> str:
    """Код шаблона и выдачи: `k` и номер строки. Человеку его не показывают."""
    return f"k{int(kind_id)}"


def kind_id_from_code(code: str) -> int | None:
    if not isinstance(code, str) or len(code) < 2 or code[0] != "k" or not code[1:].isdigit():
        return None
    if code[1] == "0":
        return None
    return int(code[1:])


def _fields_list(fields: list[str] | None) -> list[str]:
    picked: list[str] = []
    for item in fields or ("name",):
        if item in KIND_FIELDS and item not in picked:
            picked.append(item)
    if "name" not in picked:
        picked.insert(0, "name")
    return picked


def _kind_row(row) -> dict:
    try:
        fields = json.loads(row[5] or "[]")
    except (TypeError, json.JSONDecodeError):
        fields = ["name"]
    if not isinstance(fields, list):
        fields = ["name"]
    return {
        "id": int(row[0]),
        "code": kind_code(row[0]),
        "title": row[1],
        "audience": row[2],
        "role_key": row[3],
        "by_day": int(row[4] or 0),
        "fields": _fields_list([item for item in fields if isinstance(item, str)]),
        "created_at": row[6],
    }


async def _kind_rows(sql: str, params: tuple = ()) -> list | None:
    """`None` — таблицы ещё нет (старая база до миграции). Пустой список — видов нет."""
    try:
        async with _db._connect() as db:
            async with db.execute(sql, params) as cur:
                return await cur.fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return None
        raise


async def list_kinds() -> list[dict]:
    rows = await _kind_rows(
        "SELECT id, title, audience, role_key, by_day, fields, created_at "
        "FROM certificate_kinds ORDER BY id",
    )
    if not rows:
        return []
    return [_kind_row(row) for row in rows]


async def get_kind(code: str) -> dict | None:
    kind_id = kind_id_from_code(code)
    if kind_id is None:
        return None
    rows = await _kind_rows(
        "SELECT id, title, audience, role_key, by_day, fields, created_at "
        "FROM certificate_kinds WHERE id = ?",
        (kind_id,),
    )
    if not rows:
        return None
    return _kind_row(rows[0])


async def insert_kind(
    title: str, audience: str, *, role_key: str | None = None,
    by_day: bool = False, fields: list[str] | None = None,
) -> dict | None:
    """Вставить вид. Роль — только ключ из `ROLES`, иначе строка не пишется."""
    if audience not in AUDIENCES:
        return None
    stored_role = None
    if audience == "role":
        from services.access.role_registry import ROLES
        if role_key not in ROLES:
            return None
        stored_role = role_key
    async with _db._connect() as db:
        cur = await db.execute(
            "INSERT INTO certificate_kinds (title, audience, role_key, by_day, fields, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                title, audience, stored_role, 1 if by_day else 0,
                json.dumps(_fields_list(fields), ensure_ascii=False), _stamp(),
            ),
        )
        await db.commit()
        new_id = cur.lastrowid
    return await get_kind(kind_code(new_id))


async def delete_kind(code: str) -> bool:
    """Удалить свой вид, его шаблоны и список ников. Четыре старых кода не принимает."""
    if code in KINDS or kind_id_from_code(code) is None:
        return False
    kind_id = kind_id_from_code(code)
    async with _db._connect() as db:
        cur = await db.execute("DELETE FROM certificate_kinds WHERE id = ?", (kind_id,))
        if not cur.rowcount:
            await db.commit()
            return False
        await db.execute("DELETE FROM certificate_templates WHERE kind = ?", (code,))
        await db.execute("DELETE FROM certificate_people WHERE kind = ?", (code,))
        await db.commit()
    return True


async def has_active_role(telegram_id: int, role_key: str) -> bool:
    """Один SELECT: тот же срок, что `get_staff_roles` — пустой `expires_at` или не раньше сегодня."""
    today = msk_now().date().isoformat()
    async with _db._connect() as db:
        async with db.execute(
            "SELECT 1 FROM staff WHERE telegram_id = ? AND role = ? "
            "AND (expires_at IS NULL OR expires_at >= ?) LIMIT 1",
            (telegram_id, role_key, today),
        ) as cur:
            row = await cur.fetchone()
    return row is not None


async def active_staff_ids(role_key: str) -> list[int]:
    """Сотрудники с этой ролью на сегодня. Предикат срока тот же, что у `has_active_role`."""
    today = msk_now().date().isoformat()
    async with _db._connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM staff WHERE role = ? "
            "AND (expires_at IS NULL OR expires_at >= ?)",
            (role_key, today),
        ) as cur:
            rows = await cur.fetchall()
    return [int(row[0]) for row in rows]


# ── выдача открывается ─────────────────────────────────────────────────────────────────

async def get_open_at() -> str | None:
    """«YYYY-MM-DD HH:MM» по Москве или `None` — выдача не назначена."""
    async with _db._connect() as db:
        async with db.execute("SELECT open_at FROM certificate_config WHERE id = 1") as cur:
            row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def set_open_at(value: str | None) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO certificate_config (id, open_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET open_at = excluded.open_at",
            (value,),
        )
        await db.commit()


# ── шаблоны ────────────────────────────────────────────────────────────────────────────

async def set_template(kind: str, variant: str, file_id: str, admin_id: int) -> bool:
    """Сохранить file_id. True — файл сменился и рамки прошлого файла сняты."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT file_id, markup FROM certificate_templates WHERE kind = ? AND variant = ?",
            (kind, variant),
        ) as cur:
            prev = await cur.fetchone()
        cleared = bool(prev and prev[0] != file_id and prev[1])
        await db.execute(
            "INSERT INTO certificate_templates (kind, variant, file_id, updated_at, updated_by, markup) "
            "VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT(kind, variant) DO UPDATE SET "
            "file_id = excluded.file_id, updated_at = excluded.updated_at, updated_by = excluded.updated_by, "
            "markup = CASE WHEN certificate_templates.file_id = excluded.file_id "
            "THEN certificate_templates.markup ELSE NULL END",
            (kind, variant, file_id, _stamp(), admin_id),
        )
        await db.commit()
    return cleared


async def delete_template(kind: str, variant: str) -> None:
    async with _db._connect() as db:
        await db.execute("DELETE FROM certificate_templates WHERE kind = ? AND variant = ?", (kind, variant))
        await db.commit()


async def list_templates() -> dict[tuple[str, str], str]:
    """`{(вид, вариант): file_id}`."""
    async with _db._connect() as db:
        async with db.execute("SELECT kind, variant, file_id FROM certificate_templates") as cur:
            rows = await cur.fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


async def list_template_rows() -> list[dict]:
    """Шаблоны с долями рамок. Доли — JSON рядом с file_id, не внутри PDF."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT kind, variant, file_id, markup FROM certificate_templates ORDER BY kind, variant",
        ) as cur:
            rows = await cur.fetchall()
    return [{"kind": r[0], "variant": r[1], "file_id": r[2], "markup": r[3]} for r in rows]


async def markup_for_file(file_id: str) -> str | None:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT markup FROM certificate_templates WHERE file_id = ? AND markup IS NOT NULL LIMIT 1",
            (file_id,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def set_markup(kind: str, variant: str, markup: str | None) -> bool:
    """Записать доли к уже загруженному файлу. False — такой строки шаблона нет."""
    async with _db._connect() as db:
        cur = await db.execute(
            "UPDATE certificate_templates SET markup = ? WHERE kind = ? AND variant = ?",
            (markup, kind, variant),
        )
        await db.commit()
    return bool(cur.rowcount)


# ── волонтёры и амбассадоры ────────────────────────────────────────────────────────────

def normalize_username(raw: str) -> str:
    raw = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
    return raw.lstrip("@").strip().strip(",;").lower()


async def add_people(kind: str, usernames: list[str]) -> int:
    """Добавить ники, уже записанные пропускаются. Возвращает, сколько добавлено."""
    added = 0
    async with _db._connect() as db:
        for name in usernames:
            cur = await db.execute(
                "INSERT OR IGNORE INTO certificate_people (kind, username, added_at) VALUES (?, ?, ?)",
                (kind, name, _stamp()),
            )
            added += cur.rowcount or 0
        await db.commit()
    return added


async def remove_person(kind: str, username: str) -> None:
    async with _db._connect() as db:
        await db.execute("DELETE FROM certificate_people WHERE kind = ? AND username = ?", (kind, username))
        await db.commit()


async def clear_people(kind: str) -> None:
    async with _db._connect() as db:
        await db.execute("DELETE FROM certificate_people WHERE kind = ?", (kind,))
        await db.commit()


def _person_row(row) -> dict:
    return {
        "username": row[0],
        "list_name": row[1],
        "name_ok": int(row[2] or 0),
        "send_blocked": int(row[3] or 0),
    }


async def list_people(kind: str) -> list[str]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT username FROM certificate_people WHERE kind = ? ORDER BY added_at, username", (kind,),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def list_people_rows(kind: str) -> list[dict]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT username, list_name, name_ok, send_blocked FROM certificate_people "
            "WHERE kind = ? ORDER BY added_at, username",
            (kind,),
        ) as cur:
            rows = await cur.fetchall()
    return [_person_row(r) for r in rows]


async def get_person(kind: str, username: str) -> dict | None:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT username, list_name, name_ok, send_blocked FROM certificate_people "
            "WHERE kind = ? AND username = ?",
            (kind, username),
        ) as cur:
            row = await cur.fetchone()
    return _person_row(row) if row else None


async def upsert_person(kind: str, username: str, list_name: str | None, *, reset_ok: bool) -> bool:
    """Новая строка — `True`. Имя списка пишется, только если его прислали.
    `reset_ok` снимает «Да, это он», когда фамилия в списке сменилась."""
    async with _db._connect() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO certificate_people "
            "(kind, username, added_at, list_name, name_ok, send_blocked) VALUES (?, ?, ?, ?, 0, 0)",
            (kind, username, _stamp(), list_name),
        )
        added = bool(cur.rowcount)
        if not added and list_name is not None:
            if reset_ok:
                await db.execute(
                    "UPDATE certificate_people SET list_name = ?, name_ok = 0 "
                    "WHERE kind = ? AND username = ?",
                    (list_name, kind, username),
                )
            else:
                await db.execute(
                    "UPDATE certificate_people SET list_name = ? WHERE kind = ? AND username = ?",
                    (list_name, kind, username),
                )
        await db.commit()
    return added


async def confirm_list_name(kind: str, username: str) -> None:
    async with _db._connect() as db:
        await db.execute(
            "UPDATE certificate_people SET name_ok = 1 WHERE kind = ? AND username = ?",
            (kind, username),
        )
        await db.commit()


async def mark_send_blocked(kind: str, username: str) -> None:
    async with _db._connect() as db:
        await db.execute(
            "UPDATE certificate_people SET send_blocked = 1 WHERE kind = ? AND username = ?",
            (kind, username),
        )
        await db.commit()


async def sent_kinds(telegram_id: int) -> set[str]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT kind FROM certificate_sends WHERE telegram_id = ?", (telegram_id,),
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def kinds_for_username(username: str | None, telegram_id: int | None = None) -> set[str]:
    """Роли из списков по нику. С `telegram_id` — ещё и по нику из анкеты (`users.username`) и
    из чата (`chat_usernames`): рассылка «готов» (`ids_for_people`) находит человека и по ним,
    так что сменивший ник волонтёр получил бы «готов», а по кнопке — «не нашёл тебя»."""
    names = {normalize_username(username)} if username else set()
    async with _db._connect() as db:
        if telegram_id is not None:
            async with db.execute(
                "SELECT username FROM users WHERE telegram_id = ? "
                "UNION SELECT username FROM chat_usernames WHERE telegram_id = ? "
                "UNION SELECT username FROM reg_started WHERE telegram_id = ?",
                (telegram_id, telegram_id, telegram_id),
            ) as cur:
                names |= {normalize_username(r[0]) for r in await cur.fetchall() if r[0]}
        names.discard("")
        if not names:
            return set()
        marks = ", ".join("?" * len(names))
        async with db.execute(
            f"SELECT kind FROM certificate_people WHERE username IN ({marks})", tuple(names),
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def resolve_nick(username: str) -> dict | None:
    """Кто стоит за @ником. Сначала анкета (`users`, там есть ФИО), потом ник из чата,
    потом человек, который только нажал /start (`reg_started`). Нет нигде — `None`, id не выдумываем."""
    nick = normalize_username(username)
    if not nick:
        return None
    async with _db._connect() as db:
        async with db.execute(
            "SELECT telegram_id, full_name FROM users "
            "WHERE ltrim(username, '@') = ? COLLATE NOCASE LIMIT 1",
            (nick,),
        ) as cur:
            user = await cur.fetchone()
        if user:
            async with db.execute(
                "SELECT first_name FROM chat_usernames WHERE telegram_id = ?", (int(user[0]),),
            ) as cur:
                chat = await cur.fetchone()
            return {
                "telegram_id": int(user[0]),
                "full_name": user[1],
                "profile_name": chat[0] if chat and chat[0] else None,
            }
        async with db.execute(
            "SELECT telegram_id, first_name FROM chat_usernames "
            "WHERE ltrim(username, '@') = ? COLLATE NOCASE LIMIT 1",
            (nick,),
        ) as cur:
            chat = await cur.fetchone()
        if chat:
            return {
                "telegram_id": int(chat[0]),
                "full_name": None,
                "profile_name": chat[1],
            }
        async with db.execute(
            "SELECT telegram_id FROM reg_started "
            "WHERE ltrim(username, '@') = ? COLLATE NOCASE LIMIT 1",
            (nick,),
        ) as cur:
            started = await cur.fetchone()
    if not started:
        return None
    return {"telegram_id": int(started[0]), "full_name": None, "profile_name": None}


async def ids_for_people() -> dict[str, int]:
    """@ник из списков -> telegram_id, если бот этого человека знает. Нужен рассылке."""
    out: dict[str, int] = {}
    async with _db._connect() as db:
        async with db.execute("SELECT DISTINCT username FROM certificate_people") as cur:
            names = [r[0] for r in await cur.fetchall()]
    for name in names:
        found = await resolve_nick(name)
        if found:
            out[name] = found["telegram_id"]
    return out


# ── участники: отметка на входе ────────────────────────────────────────────────────────

async def entry_days(telegram_id: int, entry_point: str) -> list[str]:
    """Дни (`YYYY-MM-DD`), в которые человека отметили на входе."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT DISTINCT day FROM checkins WHERE telegram_id = ? AND point = ? ORDER BY day",
            (telegram_id, entry_point),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def checked_in_ids(entry_point: str) -> list[int]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT DISTINCT telegram_id FROM checkins WHERE point = ?", (entry_point,),
        ) as cur:
            rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


# ── имя, выдачи, рассылка ──────────────────────────────────────────────────────────────

async def get_name(telegram_id: int) -> str | None:
    async with _db._connect() as db:
        async with db.execute("SELECT name FROM certificate_names WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def set_name(telegram_id: int, name: str) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO certificate_names (telegram_id, name, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
            (telegram_id, name, _stamp()),
        )
        await db.commit()


async def record_send(telegram_id: int, kind: str) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO certificate_sends (telegram_id, kind, sent_at) VALUES (?, ?, ?)",
            (telegram_id, kind, _stamp()),
        )
        await db.commit()


async def send_counts() -> dict[str, int]:
    async with _db._connect() as db:
        async with db.execute("SELECT kind, COUNT(*) FROM certificate_sends GROUP BY kind") as cur:
            rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}


async def announced_ids() -> set[int]:
    """Кому уже ушло «сертификат готов» — один SELECT на проход джобы, не claim на каждого."""
    async with _db._connect() as db:
        async with db.execute("SELECT telegram_id FROM certificate_announces") as cur:
            rows = await cur.fetchall()
    return {int(row[0]) for row in rows}


async def claim_announce(telegram_id: int) -> bool:
    """`True` — этому человеку «сертификат готов» ещё не писали, место занято за нами."""
    async with _db._connect() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO certificate_announces (telegram_id, sent_at) VALUES (?, ?)",
            (telegram_id, _stamp()),
        )
        await db.commit()
        return bool(cur.rowcount)


async def release_announce(telegram_id: int) -> None:
    """Отправка «готов» не дошла по сбою сети/Telegram — освободить место, чтобы следующий
    проход джобы написал человеку снова."""
    async with _db._connect() as db:
        await db.execute("DELETE FROM certificate_announces WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def announce_count() -> int:
    async with _db._connect() as db:
        async with db.execute("SELECT COUNT(*) FROM certificate_announces") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0
