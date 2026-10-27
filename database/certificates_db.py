"""Сертификаты участникам после форума: шаблоны, списки по @никам, имена, выдачи.

- `certificate_templates` — PDF-шаблон каждого вида (участник/волонтёр/амбассадор) и варианта
  дат (`all` — «оба дня», `YYYY-MM-DD` — версия на один день). Хранится `file_id` Telegram:
  менеджер загрузил файл в бот, сам файл с подписью и печатью в репозиторий не попадает.
- `certificate_people` — волонтёры и амбассадоры @никами (участник определяется отметкой входа).
- `certificate_names` — имя, которое человек подтвердил или исправил перед выдачей.
- `certificate_sends` — что и кому уже выдано (повторный запрос отдаёт файл снова, счётчик
  в админке — по этой таблице).
- `certificate_announces` — кому ушло сообщение «сертификат готов»: рассылка идёт джобой
  раз в несколько минут, PK не даёт написать человеку дважды.
- `certificate_config` — одна строка: когда открыть выдачу.

Отдельный модуль, как `chat_coins_db`: `database/db.py` и так больше 12 тысяч строк."""
from __future__ import annotations

import aiosqlite

from database import db as _db
from services.infra.timeutil import msk_now

KIND_PARTICIPANT = "participant"
KIND_VOLUNTEER = "volunteer"
KIND_AMBASSADOR = "ambassador"
KINDS = (KIND_PARTICIPANT, KIND_VOLUNTEER, KIND_AMBASSADOR)
VARIANT_ALL = "all"


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_templates ("
        "kind TEXT NOT NULL, variant TEXT NOT NULL, file_id TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, updated_by INTEGER, PRIMARY KEY (kind, variant))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS certificate_people ("
        "kind TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, added_at TEXT NOT NULL, "
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


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


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

async def set_template(kind: str, variant: str, file_id: str, admin_id: int) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO certificate_templates (kind, variant, file_id, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind, variant) DO UPDATE SET "
            "file_id = excluded.file_id, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (kind, variant, file_id, _stamp(), admin_id),
        )
        await db.commit()


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


async def list_people(kind: str) -> list[str]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT username FROM certificate_people WHERE kind = ? ORDER BY added_at, username", (kind,),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def kinds_for_username(username: str | None) -> set[str]:
    if not username:
        return set()
    async with _db._connect() as db:
        async with db.execute(
            "SELECT kind FROM certificate_people WHERE username = ?", (normalize_username(username),),
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def ids_for_people() -> dict[str, int]:
    """@ник из списков -> telegram_id, если бот этого человека знает: по `users.username` или
    по нику, который бот видел в чате (`chat_usernames`). Нужен рассылке «сертификат готов»."""
    out: dict[str, int] = {}
    async with _db._connect() as db:
        async with db.execute("SELECT DISTINCT username FROM certificate_people") as cur:
            names = [r[0] for r in await cur.fetchall()]
        for name in names:
            async with db.execute(
                "SELECT telegram_id FROM users WHERE ltrim(username, '@') = ? COLLATE NOCASE "
                "UNION SELECT telegram_id FROM chat_usernames WHERE ltrim(username, '@') = ? COLLATE NOCASE "
                "LIMIT 1",
                (name, name),
            ) as cur:
                row = await cur.fetchone()
            if row:
                out[name] = int(row[0])
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


async def claim_announce(telegram_id: int) -> bool:
    """`True` — этому человеку «сертификат готов» ещё не писали, место занято за нами."""
    async with _db._connect() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO certificate_announces (telegram_id, sent_at) VALUES (?, ?)",
            (telegram_id, _stamp()),
        )
        await db.commit()
        return bool(cur.rowcount)


async def announce_count() -> int:
    async with _db._connect() as db:
        async with db.execute("SELECT COUNT(*) FROM certificate_announces") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0
