import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

import aiosqlite
import reg_options
from config import config
from services.timeutil import msk_now, process_clock_is_utc

logger = logging.getLogger(__name__)

# WR-08: SQLite can't bind identifiers (table/column names), so migrations interpolate them
# into the SQL string. Every current caller passes a hardcoded literal, so there's no
# injection path today — this guard makes any FUTURE dynamic/user-derived identifier fail
# loudly instead of silently opening an injection primitive.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _assert_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


async def _column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    _assert_identifier(table_name)
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return any(row[1] == column_name for row in rows)


async def _ensure_column(db: aiosqlite.Connection, table_name: str, column_name: str, definition: str):
    _assert_identifier(table_name)
    _assert_identifier(column_name)
    if not await _column_exists(db, table_name, column_name):
        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

_SOS_CATEGORY_NOT_NULL = re.compile(r"\bcategory\s+TEXT\s+NOT\s+NULL\b", re.IGNORECASE)


async def _relax_sos_reports_category(db: aiosqlite.Connection) -> None:
    """D-31 снял NOT NULL с `sos_reports.category` только в `CREATE TABLE IF NOT EXISTS` — на
    базах, где таблица уже была (стенд, SkillUp, REC26), `create_sos_report` без категории
    падал на NOT NULL, и SOS молча не работал. SQLite не умеет снять ограничение ALTER'ом —
    таблица пересоздаётся по своему же DDL из sqlite_master (с колонками, добавленными
    `_ensure_column`), без NOT NULL у category, в одной транзакции. Счётчик AUTOINCREMENT
    сохраняется, индексы init_db создаёт заново сразу после. Идемпотентно: у пересозданной
    таблицы category уже NULL-able, повторный вызов ничего не делает. Внешних ключей и
    триггеров на sos_reports нет."""
    async with db.execute("PRAGMA table_info(sos_reports)") as cursor:
        cols = await cursor.fetchall()
    if not any(c[1] == "category" and c[3] for c in cols):
        return
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sos_reports'"
    ) as cursor:
        (ddl,) = await cursor.fetchone()
    new_ddl = _SOS_CATEGORY_NOT_NULL.sub("category TEXT", ddl, count=1)
    new_ddl = re.sub(r"\bsos_reports\b", "sos_reports_new", new_ddl, count=1)
    await db.commit()  # закрыть неявную транзакцию init_db — пересоздание идёт своей
    await db.execute("BEGIN")
    try:
        async with db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'sos_reports'"
        ) as cursor:
            seq_row = await cursor.fetchone()
        await db.execute(new_ddl)
        await db.execute("INSERT INTO sos_reports_new SELECT * FROM sos_reports")
        await db.execute("DROP TABLE sos_reports")
        await db.execute("ALTER TABLE sos_reports_new RENAME TO sos_reports")
        if seq_row is not None:
            cursor = await db.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'sos_reports'",
                (seq_row[0],),
            )
            if not cursor.rowcount:  # таблица была пуста — строки счётчика ещё нет
                await db.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('sos_reports', ?)",
                    (seq_row[0],),
                )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info("init_db: sos_reports пересоздана — category больше не NOT NULL")


# Вход каждый день (двухдневный форум в Москве): отметка уникальна по (делегат, точка, ДЕНЬ).
# `day` — «YYYY-MM-DD» по Москве, всегда `scanned_at[:10]` (пишут record_checkin/
# record_session_checkin/undo_venue_checkin). Сессии этим не меняются: сессия сама идёт в один
# день, её строки по-прежнему одна на (делегат, сессия).
_CHECKINS_DDL = '''
    CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        point TEXT NOT NULL,
        scanned_at TEXT NOT NULL,
        source TEXT NOT NULL,
        approx_time INTEGER NOT NULL DEFAULT 0,
        by_staff_id INTEGER,
        created_at TEXT NOT NULL,
        day TEXT NOT NULL,
        UNIQUE(telegram_id, point, day)
    )
'''


async def _migrate_checkins_per_day(db: aiosqlite.Connection) -> None:
    """Старая `checkins` с UNIQUE(telegram_id, point) -> новая с колонкой `day` и
    UNIQUE(telegram_id, point, day). Констрейнт ALTER'ом не снять — таблица пересоздаётся тем же
    приёмом, что `_relax_sos_reports_category`: одна транзакция, `day` старых строк =
    date(scanned_at), счётчик AUTOINCREMENT сохраняется, индексы init_db создаёт заново сразу
    после. Идемпотентно: колонка `day` уже есть — выходим."""
    if await _column_exists(db, "checkins", "day"):
        return
    async with db.execute("PRAGMA table_info(checkins)") as cursor:
        old_cols = [row[1] for row in await cursor.fetchall()]
    new_ddl = _CHECKINS_DDL.replace("IF NOT EXISTS checkins", "checkins_new", 1)
    await db.commit()  # закрыть неявную транзакцию init_db — пересоздание идёт своей
    await db.execute("BEGIN")
    try:
        async with db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'checkins'"
        ) as cursor:
            seq_row = await cursor.fetchone()
        await db.execute(new_ddl)
        async with db.execute("PRAGMA table_info(checkins_new)") as cursor:
            new_cols = {row[1] for row in await cursor.fetchall()}
        common = [c for c in old_cols if c in new_cols]
        col_list = ", ".join(common)
        await db.execute(
            f"INSERT INTO checkins_new ({col_list}, day) "
            f"SELECT {col_list}, substr(scanned_at, 1, 10) FROM checkins"
        )
        await db.execute("DROP TABLE checkins")
        await db.execute("ALTER TABLE checkins_new RENAME TO checkins")
        if seq_row is not None:
            cursor = await db.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'checkins'",
                (seq_row[0],),
            )
            if not cursor.rowcount:  # таблица была пуста — строки счётчика ещё нет
                await db.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('checkins', ?)",
                    (seq_row[0],),
                )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info("init_db: checkins пересоздана — отметка уникальна по (делегат, точка, день)")


_USER_CONSENTS_DDL = '''
    CREATE TABLE IF NOT EXISTS user_consents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        consent_key TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        consent_version TEXT,
        UNIQUE(user_id, consent_key, consent_version)
    )
'''

# Every connection goes through here so the busy handler is set in ONE place. Without it a
# writer holding the lock makes a concurrent reader/writer fail immediately with
# "database is locked"; with it SQLite retries for up to this long. aiosqlite hands
# `timeout` to sqlite3.connect(), which installs it via sqlite3_busy_timeout — the value is
# visible as PRAGMA busy_timeout on the connection. The file itself runs in WAL mode (set
# once, persistently, in init_db) so readers never block the single writer.
DB_BUSY_TIMEOUT_MS = 5000


def _connect() -> aiosqlite.Connection:
    """Open the bot DB with the standard busy timeout. Use as `async with _connect() as db:`."""
    return aiosqlite.connect(config.DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000)


async def _enable_wal(db: aiosqlite.Connection) -> str:
    """Switch the DB file to WAL journaling (persistent across connections/restarts).

    WAL lets readers proceed while one writer commits — the long-polling bot, the reminder
    loop, the scheduler and the Sheets worker thread all touch the same file. Returns the
    resulting journal mode ('wal' for a file DB; ':memory:' / tmp DBs report 'memory').
    """
    async with db.execute("PRAGMA journal_mode=WAL") as cursor:
        row = await cursor.fetchone()
    mode = (row[0] if row else "").lower()
    if mode != "wal":
        logger.warning("SQLite journal_mode is %r (expected 'wal') for %s", mode, config.DB_PATH)
    return mode


# name -> (table, columns). Kept as a module-level table so tests can assert the exact set
# exists after init_db() and so "what is indexed and why" is readable in one place. The DDL
# is derived (CREATE INDEX IF NOT EXISTS name ON table(cols)); columns are verified to exist
# first so a pre-migration DB shape never makes startup fail on an index.
_HOT_PATH_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    # get_pending_users / get_pending_count / approve_all_pending: moderation queue —
    # WHERE status='pending' [AND event_city ...] ORDER BY registration_date
    "idx_users_status_city_regdate": ("users", ("status", "event_city", "registration_date")),
    # get_receipt_pending_users / _count + scheduler overdue sweep: WHERE payment_status = ...
    "idx_users_payment_status": ("users", ("payment_status",)),
    # get_referrals: WHERE referrer_id = ?
    "idx_users_referrer": ("users", ("referrer_id",)),
    # get_non_subscriber_ids (broadcast exclusion): WHERE subscribed = 0
    "idx_users_subscribed": ("users", ("subscribed",)),
    # reconcile on restart: WHERE status='pending' ORDER BY scheduled_at
    "idx_scheduled_broadcasts_status_at": ("scheduled_broadcasts", ("status", "scheduled_at")),
    # incomplete-registration listings: ORDER BY started_at
    "idx_reg_started_started_at": ("reg_started", ("started_at",)),
    # dropout-nudge scan: WHERE started_at < ? AND nudged_at IS NULL
    "idx_reg_started_nudge": ("reg_started", ("nudged_at", "started_at")),
    # game submission queue: WHERE s.status='pending' ORDER BY s.submitted_at, s.id
    "idx_game_submissions_status_at": ("game_submissions", ("status", "submitted_at")),
    # run_revoke / _broadcast_card: WHERE broadcast_id = ? (list_broadcast_messages)
    "idx_broadcast_deliveries_bid": ("broadcast_deliveries", ("broadcast_id",)),
}


async def _ensure_hot_path_indexes(db: aiosqlite.Connection) -> list[str]:
    """CREATE INDEX IF NOT EXISTS for every entry of _HOT_PATH_INDEXES whose columns exist.
    Returns the names created/confirmed. Idempotent, additive, safe on the live DB — SQLite
    builds a missing index in place on first start after upgrade (sub-second at 1-2k rows)."""
    created: list[str] = []
    for name, (table, cols) in _HOT_PATH_INDEXES.items():
        _assert_identifier(name)
        _assert_identifier(table)
        for c in cols:
            _assert_identifier(c)
        present = True
        for c in cols:
            if not await _column_exists(db, table, c):
                present = False
                break
        if not present:
            logger.debug("skip index %s: %s(%s) not fully present", name, table, ", ".join(cols))
            continue
        await db.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table}({', '.join(cols)})"
        )
        created.append(name)
    return created


# Квик 260912-mcj: семья «сейчас» бота (все точки записи в этом файле переведены на
# `services.timeutil.msk_now()` — см. коммит, добавивший этот модуль-уровневый импорт).
# strftime-колонки ("%Y-%m-%d %H:%M:%S") — одним UPDATE через SQLite `datetime(col, '+3 hours')`.
# НЕ входят: `datetime.utcnow()`-семья (staff.added_at, delegate_questions.*,
# reg_answer_history.changed_at, translations.updated_at), уже-московские поля ввода менеджера
# (delayed_notifications.*, scheduled_broadcasts.scheduled_at, polls.scheduled_at,
# game_tasks.deadline_at, users.payment_due) и служебные `translation_queue.created_at`/
# avatar-кэш `users` (не критичны — TTL/очередь по attempts, не по человеку читаемому времени).
_MSK_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "registration_date"),
    ("users", "edited_at"),
    ("users", "approved_at"),
    ("coins", "timestamp"),
    ("reg_started", "started_at"),
    ("reg_started", "nudged_at"),
    ("reg_drafts", "updated_at"),
    ("reg_drafts", "submitting_at"),
    ("reg_drafts", "created_at"),
    ("reg_events", "ts"),
    ("application_decisions", "decided_at"),
    ("application_decisions", "effects_due_at"),
    ("application_decisions", "effects_sent_at"),
    ("application_decisions", "undone_at"),
    ("scheduled_broadcasts", "created_at"),
    ("scheduled_broadcasts", "sending_since"),
    ("scheduled_broadcast_deliveries", "sent_at"),
    ("broadcasts", "started_at"),
    ("broadcasts", "finished_at"),
    ("broadcast_deliveries", "sent_at"),
    ("game_tasks", "created_at"),
    ("game_tasks", "archived_at"),
    ("game_submissions", "submitted_at"),
    ("game_submissions", "reviewed_at"),
    ("game_submit_digest_queue", "created_at"),
    ("game_submit_digest_queue", "sent_at"),
    ("reg_submit_digest_queue", "created_at"),
    ("reg_submit_digest_queue", "sent_at"),
    ("polls", "created_at"),
    ("polls", "sending_since"),
    ("polls", "closed_at"),
    ("poll_answers", "answered_at"),
    ("miniapp_outbox", "created_at"),
    ("miniapp_outbox", "processed_at"),
    ("cities", "created_at"),
)

# isoformat-колонки (`.isoformat()`, не strftime) — SQLite `datetime(col,'+3 hours')` вернул бы
# `'YYYY-MM-DD HH:MM:SS'` и потерял бы `T`-разделитель и микросекунды, которые
# `datetime.fromisoformat` на показе ожидает обратно — эти две колонки сдвигаются построчно
# в python, не одним UPDATE.
_MSK_MIGRATION_ISO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "paid_at"),
    ("user_consents", "accepted_at"),
)

_MSK_MIGRATION_USER_VERSION = 1


async def _migrate_local_timestamps_to_msk(db: aiosqlite.Connection) -> None:
    """Одноразовая гейтированная миграция семьи «сейчас» бота (квик 260912-mcj) — до этого
    квика вся семья писалась часами процесса (UTC на проде/стенде, `TZ` в docker-compose.yml
    намеренно не задан), с этого квика пишется московским `msk_now()`. Накопленные строки
    обязаны переехать один раз — иначе карточка заявки, дашборд и фильтр рассылки по дате
    склеили бы UTC и МСК в одной колонке.

    Вызывается из `init_db` НА ТОМ ЖЕ соединении `db`, ДО финального `await db.commit()` —
    гейт и сдвиг ложатся ОДНОЙ транзакцией: либо сдвинуты все колонки и `user_version` поднят,
    либо (при сбое до commit) не тронуто ничего. Свой `_connect()` внутри намеренно не
    открывается.

    Пост-фикс (полный прогон 260912): гейт изначально был служебным ключом `bot_settings`
    (`_ts_msk_migrated`) — обнаружилось, что любой код, читающий/экспортирующий настройки
    построчно (`tests/test_city_admin_phase71.py::test_city_toggle_unknown_code_rejected_no_write`
    считает строки `bot_settings` напрямую), видел эту служебную строку как настройку.
    Перенесено на `PRAGMA user_version` (CLAUDE.md: «PRAGMA user_version вместо custom
    migrations table» — ровно этот случай) — счётчик схемы живёт в заголовке файла БД, не в
    прикладной таблице, и никаким другим кодом проекта не используется (проверено `grep -rn
    user_version`). `_ensure_column`/CREATE TABLE миграции продолжают идти по своему пути
    (idempotent DDL) — этот гейт закрывает только одноразовый ДАННЫЙ сдвиг, а не схему.

    Логика:
    1. `PRAGMA user_version` уже >= `_MSK_MIGRATION_USER_VERSION` -> выходим немедленно (цена
       повторного старта — один `PRAGMA` без сети/диска сверх открытого файла).
    2. `process_clock_is_utc()` (`services.timeutil`) лжёт "нет" -> часы процесса не UTC,
       значит все старые строки уже писались локальным (московским) временем этой же машины
       (ноутбук разработчика, хост с локальным TZ) — сдвигать НЕЧЕГО. Экзотика «часы = UTC+1»
       намеренно тоже попадает в эту ветку: сдвигать вслепую по одному лишь «не UTC» опаснее,
       чем не сдвигать вовсе. `user_version` всё равно поднимается — иначе каждый старт
       пересчитывал бы часы заново.
    3. Иначе — часы процесса UTC, все прежние строки писал UTC-контейнер: сдвигаем `+3 hours`
       по `_MSK_MIGRATION_COLUMNS`/`_MSK_MIGRATION_ISO_COLUMNS`. Таблица/колонка, которой ещё
       нет (старая БД, `_ensure_column` для неё не отработал) — пропускается молча, а не
       роняет миграцию: `_column_exists` фильтрует список ДО SQL.

    Кривые/пустые значения переживают сдвиг: strftime-UPDATE защищён `datetime(c) IS NOT NULL`
    (SQLite вернул бы NULL и затёр бы мусорную строку без этого фильтра), isoformat-путь ловит
    `ValueError`/`TypeError` на построчном разборе и оставляет такую строку как есть.
    """
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    current_version = row[0] if row else 0
    if current_version >= _MSK_MIGRATION_USER_VERSION:
        return

    if not process_clock_is_utc():
        logger.info(
            "_migrate_local_timestamps_to_msk: часы процесса не UTC — старые строки семьи уже "
            "московские, сдвиг не нужен"
        )
        await db.execute(f"PRAGMA user_version = {_MSK_MIGRATION_USER_VERSION}")
        return

    shifted_columns = 0
    for table, column in _MSK_MIGRATION_COLUMNS:
        if not await _column_exists(db, table, column):
            continue
        _assert_identifier(table)
        _assert_identifier(column)
        cursor = await db.execute(
            f"UPDATE {table} SET {column} = datetime({column}, '+3 hours') "
            f"WHERE {column} IS NOT NULL AND TRIM({column}) != '' "
            f"AND datetime({column}) IS NOT NULL"
        )
        logger.info(
            f"_migrate_local_timestamps_to_msk: {table}.{column} — сдвинуто строк: {cursor.rowcount}"
        )
        shifted_columns += 1

    shifted_iso_rows = 0
    for table, column in _MSK_MIGRATION_ISO_COLUMNS:
        if not await _column_exists(db, table, column):
            continue
        _assert_identifier(table)
        _assert_identifier(column)
        async with db.execute(f"SELECT rowid, {column} FROM {table}") as cursor:
            rows = await cursor.fetchall()
        table_rows = 0
        for rowid, raw in rows:
            if not raw:
                continue
            try:
                shifted = datetime.fromisoformat(raw) + timedelta(hours=3)
            except (ValueError, TypeError):
                continue
            await db.execute(
                f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                (shifted.isoformat(), rowid),
            )
            table_rows += 1
        logger.info(
            f"_migrate_local_timestamps_to_msk: {table}.{column} (isoformat) — сдвинуто строк: {table_rows}"
        )
        shifted_iso_rows += table_rows

    logger.info(
        "_migrate_local_timestamps_to_msk: итог — strftime-колонок сдвинуто "
        f"{shifted_columns}/{len(_MSK_MIGRATION_COLUMNS)}, isoformat-строк сдвинуто "
        f"{shifted_iso_rows} по {len(_MSK_MIGRATION_ISO_COLUMNS)} колонкам"
    )
    await db.execute(f"PRAGMA user_version = {_MSK_MIGRATION_USER_VERSION}")


_MENU_SCHEDULE_MIGRATION_USER_VERSION = 2
_MENU_PROGRAM_KEY = "menu_program"
_MENU_SCHEDULE_KEY = "menu_schedule"


async def _migrate_menu_schedule_into_program(db: aiosqlite.Connection) -> None:
    """D-29 (коммит 125ed56) слил две кнопки программы в одну `menu_program` и перестал читать
    `menu_schedule`. Там, где менеджер когда-то выключил старую статичную «📅 Программа форума»
    (`menu_program`=off), а интерактивная «🗓 Программа» (`menu_schedule`) была включена
    (дефолт on, строки нет), после слияния кнопка пропала совсем, хотя сессии заведены.

    Одноразово по `PRAGMA user_version` (та же форма, что `_migrate_local_timestamps_to_msk`,
    то же соединение и та же транзакция): в каждой области — глобально и по каждому городу, где
    есть городской вариант любого из двух ключей (`{key}__city__{code}`) — если
    `menu_schedule` там НЕ выключен явно, `menu_program` становится `on`; если выключен явно —
    `menu_program` остаётся с тем значением, которое делегат этого города видел до миграции.
    Затем все варианты `menu_schedule` удаляются — их больше никто не читает. Пустую кнопку
    всё равно прячет гейт «есть фото или сессии» (`services.program.program_menu_visible`).
    На чистой БД не пишет ни одной строки (значение, равное дефолту/глобальному, не
    записывается)."""
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    if (row[0] if row else 0) >= _MENU_SCHEDULE_MIGRATION_USER_VERSION:
        return

    city_prefix = {k: k + _CITY_OVERRIDE_SEP for k in (_MENU_PROGRAM_KEY, _MENU_SCHEDULE_KEY)}
    values: dict[str, str] = {}
    async with db.execute(
        "SELECT key, value FROM bot_settings WHERE key IN (?, ?) "
        "OR substr(key, 1, ?) = ? OR substr(key, 1, ?) = ?",
        (
            _MENU_PROGRAM_KEY, _MENU_SCHEDULE_KEY,
            len(city_prefix[_MENU_PROGRAM_KEY]), city_prefix[_MENU_PROGRAM_KEY],
            len(city_prefix[_MENU_SCHEDULE_KEY]), city_prefix[_MENU_SCHEDULE_KEY],
        ),
    ) as cursor:
        for key, value in await cursor.fetchall():
            values[key] = value

    def _eff(key: str, code: str | None, default: str) -> str:
        if code is not None:
            own = values.get(city_prefix[key] + code)
            if own:
                return own
        return values.get(key) or default

    def _target(code: str | None) -> str:
        old_program = _eff(_MENU_PROGRAM_KEY, code, "on")
        return "on" if _eff(_MENU_SCHEDULE_KEY, code, "on") != "off" else old_program

    codes = sorted({
        key.split(_CITY_OVERRIDE_SEP, 1)[1] for key in values if _CITY_OVERRIDE_SEP in key
    })
    new_global = _target(None)
    city_targets = {code: _target(code) for code in codes}

    async def _put(key: str, value: str) -> None:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    changed = 0
    if (values.get(_MENU_PROGRAM_KEY) or "on") != new_global:
        await _put(_MENU_PROGRAM_KEY, new_global)
        changed += 1
    for code, target in city_targets.items():
        city_key = city_prefix[_MENU_PROGRAM_KEY] + code
        if city_key in values:
            if values[city_key] != target:
                await _put(city_key, target)
                changed += 1
        elif target != new_global:
            await _put(city_key, target)
            changed += 1

    cursor = await db.execute(
        "DELETE FROM bot_settings WHERE key = ? OR substr(key, 1, ?) = ?",
        (_MENU_SCHEDULE_KEY, len(city_prefix[_MENU_SCHEDULE_KEY]), city_prefix[_MENU_SCHEDULE_KEY]),
    )
    logger.info(
        "_migrate_menu_schedule_into_program: menu_program изменено ключей %s, "
        "menu_schedule удалено ключей %s", changed, cursor.rowcount,
    )
    await db.execute(f"PRAGMA user_version = {_MENU_SCHEDULE_MIGRATION_USER_VERSION}")


# Phase 30 (30-02, A2-03): имя файла снапшота на `kind` — таблица закрытая (`kind` — словарь
# "university"/"city" из `services/lookup.py`), а НЕ строковый шаблон `"<kind>s_ru.json"`: для
# "city" такой шаблон дал бы "citys_ru.json", а не существующий `cities_ru.json`.
_LOOKUP_SNAPSHOT_FILES = {
    "university": "universities_ru.json",
    "city": "cities_ru.json",
}


async def seed_lookup_from_snapshot(db: aiosqlite.Connection, kind: str) -> None:
    """Идемпотентный посев/дозаливка `lookup_entries` из офлайн-снапшота
    `data/lookup/<file>.json` (30-CONTEXT.md решение №1 — без внешних API в рантайме).

    Раньше функция срабатывала ТОЛЬКО на пустой таблице (`if row[0] > 0: return`) — из-за
    этого дополнения снапшота (см. `data/lookup/README.md`, 16.09.2026: 26 пропущенных
    крупных городов) не долетали до уже засеянных стенда/прода без ручной команды. Теперь
    функция запускается на КАЖДОМ старте бота безусловно и полагается на `UNIQUE INDEX
    (kind, alias_norm)` + `INSERT OR IGNORE` как на единственный источник идемпотентности:
    строка с уже существующим нормализованным алиасом просто не вставится повторно — не
    важно, кто её завёл первым (снапшот при посеве, менеджер через «Другое → влить» —
    `services.lookup.enqueue_merge`/`merge_apply`, или закрепление чипа — `pin_chip`).
    Значит и `pinned`, и `added_by`, и `source` уже существующей строки трогать нечем: сам
    факт совпадения `alias_norm` не даёт запросу дойти до записи. Стоимость на каждый
    рестарт — единичный проход по ~1000-2000 строк снапшота с `INSERT OR IGNORE` по
    индексированному полю, доли секунды, ручных команд владельцу не требуется.

    Fail-soft (30-RESEARCH.md § Build Artifacts): отсутствующий/битый/пустой файл — запись в
    лог, не исключение. Бот обязан подняться и без снапшота (просто со скудным справочником)."""
    filename = _LOOKUP_SNAPSHOT_FILES.get(kind)
    if not filename:
        logger.warning("seed_lookup_from_snapshot: неизвестный kind=%r — пропуск", kind)
        return

    path = os.path.join("data", "lookup", filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning(
            "seed_lookup_from_snapshot: не удалось прочитать %s (%s) — старт без справочника %s",
            path, exc, kind,
        )
        return

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        logger.warning("seed_lookup_from_snapshot: %s без списка items — старт без справочника", path)
        return

    # Отложенный импорт (та же дисциплина разрыва цикла, что у `services/i18n_sources.py`/
    # `services/scheduler.py`, читающих `database.db._connect()` тем же приёмом в обратную
    # сторону): `services.lookup` на верхнем уровне не импортирует `database.db`.
    from services.lookup import normalize_alias

    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        canonical = (item.get("canonical") or "").strip()
        if not canonical or item.get("dissolved"):
            continue
        aliases = item.get("aliases") or []
        source = item.get("source") or "seed"
        for alias in [canonical, *aliases]:
            alias = (alias or "").strip()
            if not alias:
                continue
            alias_norm = normalize_alias(alias)
            if not alias_norm:
                continue
            cursor = await db.execute(
                "INSERT OR IGNORE INTO lookup_entries "
                "(kind, canonical, alias, alias_norm, source, pinned, added_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, NULL, ?)",
                (kind, canonical, alias, alias_norm, source, now),
            )
            inserted += cursor.rowcount or 0
    logger.info(
        "seed_lookup_from_snapshot: kind=%s вставлено строк=%d (из %s)", kind, inserted, path,
    )


async def init_db():
    async with _connect() as db:
        await _enable_wal(db)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                email TEXT,
                age INTEGER,
                is_aiesec_member BOOLEAN,
                source TEXT,
                source_details TEXT,
                education_status TEXT,
                university TEXT,
                course TEXT,
                specialty TEXT,
                work_status BOOLEAN,
                work_sphere TEXT,
                missing_skills TEXT,
                expectations TEXT,
                phone TEXT,
                city TEXT,
                referrer_id INTEGER,
                registration_date TEXT,
                is_ambassador_candidate BOOLEAN DEFAULT 0
            )
        ''')

        await _ensure_column(db, "users", "phone", "TEXT")
        await _ensure_column(db, "users", "city", "TEXT")
        await _ensure_column(db, "users", "referrer_id", "INTEGER")
        await _ensure_column(db, "users", "local_committee", "TEXT")
        await _ensure_column(db, "users", "position", "TEXT")
        await _ensure_column(db, "users", "attendance_format", "TEXT")
        await _ensure_column(db, "users", "comments", "TEXT")
        await _ensure_column(db, "users", "expectations_ar", "TEXT")
        await _ensure_column(db, "users", "informal_day", "TEXT")

        # Phase 1 migrations (additive, idempotent — safe against ~590 live users)
        await _ensure_column(db, "users", "status", "TEXT DEFAULT 'approved'")
        await _ensure_column(db, "users", "resume_file_id", "TEXT")
        await _ensure_column(db, "users", "resume_text", "TEXT")  # резюме текстом (альтернатива файлу)
        await _ensure_column(db, "users", "resume_url", "TEXT")  # Nextcloud share link на файл-резюме
        await _ensure_column(db, "users", "subscribed", "INTEGER")

        # Conference (RusCo) reg-flow fields — additive, default-off questions
        await _ensure_column(db, "users", "department", "TEXT")
        await _ensure_column(db, "users", "aiesec_role", "TEXT")
        await _ensure_column(db, "users", "needs_certificate", "TEXT")
        await _ensure_column(db, "users", "english_level", "TEXT")
        await _ensure_column(db, "users", "allergies", "TEXT")
        await _ensure_column(db, "users", "food_pref", "TEXT")
        await _ensure_column(db, "users", "arrival", "TEXT")
        await _ensure_column(db, "users", "housing", "TEXT")
        await _ensure_column(db, "users", "cc_shop", "TEXT")
        await _ensure_column(db, "users", "exp_organizers", "TEXT")
        await _ensure_column(db, "users", "exp_content", "TEXT")
        await _ensure_column(db, "users", "volunteer", "TEXT")

        # Phase 21 (FORM-SYNC-04, D-12/D-15): edit-tracking for an already-submitted
        # application. update_user_answers/mark_user_edited (below) stamp these two — NULL for
        # every row until the first edit, additive against ~590 live users.
        await _ensure_column(db, "users", "edited_at", "TEXT")
        await _ensure_column(db, "users", "edited_source", "TEXT")

        # Phase 23 (APP-TINDER-01, D-02): кеш аватара делегата для карточки Mini App.
        # file_id живёт в Telegram — на диск ничего не кладём (CLAUDE.md, «file_id Pattern»);
        # avatar_checked_at различает «фото нет» от «ещё не проверяли» (getUserProfilePhotos
        # зовётся не чаще, чем раз в TTL — решает вызывающий сервис, не эта колонка).
        await _ensure_column(db, "users", "avatar_file_id", "TEXT")
        await _ensure_column(db, "users", "avatar_checked_at", "TEXT")

        # Phase 27 (27-02, LANG-01): выбранный делегатом язык интерфейса. NULL у всех
        # существующих (1000+ живых) записей — «язык не выбран», resolve_lang() трактует это
        # как повод спросить (или молча остаться на русском, если модуль выключен). Значение
        # пишется ТОЛЬКО через set_user_lang ниже — закрытое множество {"ru", "en", None}.
        await _ensure_column(db, "users", "lang", "TEXT")

        # Phase 31 (31-02, D-20/D-26): состояние автоотказа делегата — РЕАЛЬНЫЕ колонки
        # `users`, а не эфемерная пометка вроде `_edited_note`: их читают три независимых
        # процесса (`tools/rebuild_sheet_headless.py` — колонка «Детали» листа,
        # `dashboard/queries.py` — срез воронки, фильтр рассылки D-28 ниже), и эфемерная
        # пометка не пережила бы ни один из них. Все NULL у существующих (2000+) строк —
        # «автоотказа не было». `auto_reject_rule_ids`/`flagged_rule_ids` — JSON-списки id
        # правил (правило-отказ и правило-пометка — разные списки, D-04: если сработали оба,
        # побеждает отказ, но пометка не стирается). `auto_rule_note` — готовая человеческая
        # строка для колонки «Детали», собирает вызывающий сервис (план 31-05).
        await _ensure_column(db, "users", "auto_reject_rule_ids", "TEXT")
        await _ensure_column(db, "users", "auto_rejected_at", "TEXT")
        await _ensure_column(db, "users", "flagged_rule_ids", "TEXT")
        await _ensure_column(db, "users", "auto_rule_note", "TEXT")

        await db.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')

        # Phase 1: append-only coins ledger (balance = SUM(delta), never UPDATE)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS coins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT,
                changed_by INTEGER,
                timestamp TEXT NOT NULL
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_coins_user ON coins(user_id)')
        # Phase 14 (GAME-09): 'manual' | 'task' | NULL = легаси/система -- distinguishes a
        # manager's hand-edit from a task-award credit at the DATA level (not by parsing the
        # `reason` string prefix, which is fragile -- 14-RESEARCH.md Pitfall 6). Every existing
        # add_coins call site keeps writing NULL until this plan's own call sites pass source=.
        await _ensure_column(db, "coins", "source", "TEXT")

        # Phase 32 (32-01, D-14): ссылка на задание, за которое начислены баллы. NULL у ВСЕХ
        # существующих строк (ручные начисления, легаси, любая строка до этой колонки) — по
        # ней сумма волны собирается через JOIN game_tasks, а не по времени начисления
        # (без неё рейтинг волны врал бы, если делегат сдаёт задание не в дни самой волны).
        # Пишется только в двух местах — обеих точках начисления за задание (add_coins ниже).
        await _ensure_column(db, "coins", "task_id", "INTEGER")

        # Phase 8 (ROLE-02, D-11): staff roster -- who holds which role, audited (added_by/
        # added_at). Composite PRIMARY KEY naturally supports multi-role (D-08, one row per
        # role held) and rejects a duplicate (telegram_id, role) pair without extra machinery.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS staff (
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                added_by INTEGER,
                added_at TEXT NOT NULL,
                PRIMARY KEY (telegram_id, role)
            )
        ''')
        # Phase 09.1 (C, ROLE-03): manager <-> city binding. NULL = all cities (same semantics
        # as every other event_city column) -- every pre-existing row keeps working unchanged.
        # Binding is by telegram_id alone (one city regardless of how many roles a person
        # holds), so the value is duplicated across every (telegram_id, role) row for that
        # person rather than normalized into a separate table -- there are at most a few dozen
        # staff rows, and a single-table SELECT/UPDATE stays simpler than a join.
        await _ensure_column(db, "staff", "city", "TEXT")

        # Идея №6 бэклога чек-ина (общая механика, не только форум): «до какой даты действует
        # эта роль». ISO «YYYY-MM-DD» (сравнимо строкой с `services.staff_expiry.today_iso()`),
        # NULL у ВСЕХ существующих строк -- «бессрочно», байт-в-байт прежнее поведение для
        # каждой роли, выданной до этого квика. Действует ПО этот день включительно -- истекает
        # с НАЧАЛА следующего. Колонка per-role (композитный PRIMARY KEY staff уже per-role, в
        # отличие от city, который per-person и хранится дублем на каждой строке человека) --
        # разные роли одного человека могут иметь разный срок.
        await _ensure_column(db, "staff", "expires_at", "TEXT")

        # 29.09: сотрудник, до которого бот не достучался (заблокировал бота / ни разу не нажал
        # /start / удалил аккаунт). Отдельная таблица, а не колонка `staff`: суперадмины из
        # `config.ADMIN_IDS` в `staff` не лежат, а сама `staff` — строка на роль, не на человека.
        # `since` — первая неудача (не сдвигается повторами), `last_failed_at` — последняя.
        # Строку снимает удачная доставка или любое сообщение человека боту
        # (`services/staff_reach.py`). Время — московское (`msk_now`).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS staff_unreachable (
                telegram_id INTEGER PRIMARY KEY,
                since TEXT NOT NULL,
                last_failed_at TEXT NOT NULL,
                reason TEXT
            )
        ''')

        # Идея №5 бэклога чек-ина: приглашение волонтёров ссылкой. Одна ссылка -- одна строка;
        # `code` -- secrets.token_urlsafe, непубличный секрет (не подбирается перебором),
        # PRIMARY KEY естественно уникален. `city`/`created_by` -- атрибуция; `link_expires_at`/
        # `rights_expires_at` -- ISO даты (см. staff.expires_at выше), NULL = без срока
        # (`rights_expires_at` NULL встречается редко -- по умолчанию «до конца форума», но
        # менеджер может явно снять срок); `max_uses` NULL = без лимита; `used` -- атомарный
        # счётчик занятых слотов (см. `claim_volunteer_invite`), НЕ дублирует
        # `COUNT(*) FROM volunteer_invite_uses` -- инкремент и есть механизм гонки-защиты, сама
        # таблица `volunteer_invite_uses` -- только человекочитаемый список «кто вошёл»;
        # `revoked` -- отзыв ссылки (0/1), уже выданные права НЕ снимает (см. докстринг
        # `revoke_volunteer_invite`).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS volunteer_invites (
                code TEXT PRIMARY KEY,
                city TEXT,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                link_expires_at TEXT,
                rights_expires_at TEXT,
                max_uses INTEGER,
                used INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0
            )
        ''')
        # Кто именно вошёл по каждой ссылке -- список для экрана менеджера («👥 N вошли») и
        # источник для персонального «➖ Снять». Композитный PRIMARY KEY -- тот же приём
        # идемпотентности, что `staff (telegram_id, role)`: повторный INSERT OR IGNORE того же
        # человека по той же ссылке (двойной переход по одной ссылке) не плодит вторую строку.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS volunteer_invite_uses (
                invite_code TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                used_at TEXT NOT NULL,
                PRIMARY KEY (invite_code, telegram_id)
            )
        ''')
        # Ревью 28.09 (D-41): какую роль выдаёт ссылка. NULL у старых ссылок = «volunteer»
        # (только отметка входа); «reg_volunteer» — отметка и одобрение на месте.
        await _ensure_column(db, "volunteer_invites", "role", "TEXT")

        # Phase 14 (CITY-07): city registry moves from `.env` into the DB -- this table is
        # the source of truth from now on. `cities.seed_cities_if_empty()` fills it once from
        # the old .env city list on first boot (empty-table check); after that `.env` is never
        # read again for the city list. `enabled` mirrors the old `bot_settings
        # city_enabled__{code}` toggle at seed time (old keys are NOT deleted -- read-fallback
        # kept for backward compat, see `cities.is_city_enabled`). No FOREIGN KEY -- mirrors
        # every other table in this file, which does not use SQLite FK constraints.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS cities (
                code TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                tab_base TEXT,
                enabled INTEGER DEFAULT 1,
                sort_order INTEGER,
                created_at TEXT
            )
        ''')

        # Phase 1: persistent dropout tracking (survives restart, independent of FSM)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reg_started (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                started_at TEXT NOT NULL
            )
        ''')
        # Phase 3 (SCHED-03): one-shot dropout-nudge stamp (additive, D-15 — reuse reg_started)
        await _ensure_column(db, "reg_started", "nudged_at", "TEXT")
        # Dropout analytics: last question shown before the user abandoned (step_key). NULL
        # for rows created before this column existed / before the user saw any question.
        await _ensure_column(db, "reg_started", "last_step", "TEXT")
        # Приёмка 16.09: язык анкеты, выбранный ДО появления строки users (экран выбора языка
        # на первом /start). Переезжает в users.lang в clear_reg_started при подаче анкеты.
        await _ensure_column(db, "reg_started", "lang", "TEXT")
        # Quick k4y: JSON snapshot of already-answered registration fields (FSM data at the
        # moment of the last question). NULL for rows created before this column existed —
        # no backfill possible, those rows render as "-" on the «Незавершённые» tab. Additive,
        # idempotent — safe against ~590 live records.
        await _ensure_column(db, "reg_started", "partial_data", "TEXT")

        # Phase 21 (FORM-SYNC-02, D-19/D-21): reg_drafts is the ONE shared home of "the
        # questionnaire in flight" — both the bot process and the Mini App process read/write
        # this row instead of anything living only in FSM memory. reg_started above is left
        # completely untouched: it stays the dropout/"Незавершённые" bookkeeping table it
        # always was; reg_drafts is a second, orthogonal table for live sync + conflict
        # resolution (per-field version, LWW) + double-submit protection (submitting_at claim).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reg_drafts (
                telegram_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                participant_type TEXT,
                event_city TEXT,
                answers TEXT NOT NULL DEFAULT '{}',
                meta TEXT NOT NULL DEFAULT '{}',
                step TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                submitting_at TEXT
            )
        ''')
        # Quick 260904-3vm (эстафета вместо двустороннего синхрона): кто сейчас владеет
        # черновиком — 'bot' | 'app' | NULL. NULL у строк, созданных до этой фичи, читается
        # как 'bot' (см. services/reg_handoff.py::draft_holder) — их всегда заводил бот,
        # отдельного backfill не делаем.
        await _ensure_column(db, "reg_drafts", "active_surface", "TEXT")

        # Phase 21 (FORM-SYNC-04, D-15): append-only edit trail — "было → стало" for every
        # narrow update_user_answers() call, shown to the manager via «🕓 История» (plan 21-07)
        # without touching the users row itself (that stays the current-value source of truth).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reg_answer_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                season TEXT,
                changes TEXT NOT NULL
            )
        ''')
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_reg_answer_history_telegram_id ON reg_answer_history(telegram_id)'
        )

        # Phase 3 (SCHED-01): scheduled-broadcast payload store. APScheduler owns the
        # trigger (data/jobs.sqlite); this row holds the message/filter payload keyed by id.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                photo_file_id TEXT,
                filter_spec TEXT,
                scheduled_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_by INTEGER,
                created_at TEXT
            )
        ''')
        # Review 260817 §B2 (п.10): per-recipient checkpoint of a scheduled broadcast. One row per
        # (broadcast, chat) written right after each send attempt — ok or failed — so a send that
        # dies mid-loop can be resumed from where it stopped instead of forfeiting the tail.
        # `sending_since` marks WHEN the row was claimed: the boot reconciliation only reclaims
        # 'sending' rows older than that threshold (see reclaim_stale_sending).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_broadcast_deliveries (
                broadcast_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                sent_at TEXT,
                PRIMARY KEY (broadcast_id, chat_id)
            )
        ''')
        await _ensure_column(db, "scheduled_broadcasts", "sending_since", "TEXT")
        # Квик 260915-twr (Task B1): отложенная рассылка при отправке заводит строку в журнале
        # `broadcasts` (том же, что у мгновенных рассылок) — эта колонка хранит связь, чтобы
        # resume после рестарта переиспользовал ту же строку журнала, а не плодил вторую.
        await _ensure_column(db, "scheduled_broadcasts", "log_broadcast_id", "INTEGER")
        # Форум-ночь п.7 (D-XX, «❗ Важное»): отложенная рассылка тоже может быть помечена
        # важной — читается в services/scheduler.py::send_scheduled_broadcast перед тем, как
        # завести/переиспользовать строку журнала `broadcasts` (важность копируется туда же).
        await _ensure_column(db, "scheduled_broadcasts", "important", "INTEGER DEFAULT 0")

        # Quick 260910-okb (BC-01..06): журнал НЕМЕДЛЕННЫХ рассылок («отправить сейчас» из
        # handlers/admin_broadcasts.py). Отдельный путь от scheduled_broadcasts* выше — те
        # держат отложенные рассылки APScheduler'а, эти таблицы их не трогают и не смешивают.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                text_preview TEXT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                total INTEGER,
                delivered INTEGER,
                blocked INTEGER
            )
        ''')
        # Форум-ночь п.7: важность рассылки + её полный текст (для делегатской кнопки
        # «❗ Важное», database.db.important_messages_today — text_preview выше урезан до 80
        # символов, для «найти потерянное в потоке» этого мало) + счётчик пропущенных
        # тихим-режимом получателей (mute_skipped, для отчёта менеджеру). И для мгновенных
        # (create_broadcast), и для отложенных рассылок (send_scheduled_broadcast переиспользует
        # эту же строку журнала, см. log_broadcast_id выше) — одна точка правды.
        await _ensure_column(db, "broadcasts", "important", "INTEGER DEFAULT 0")
        await _ensure_column(db, "broadcasts", "full_text", "TEXT")
        await _ensure_column(db, "broadcasts", "mute_skipped", "INTEGER DEFAULT 0")
        # БЕЗ PRIMARY KEY: альбом отдаёт несколько message_id на один chat_id — по одной строке
        # на каждое доставленное сообщение, не одна на получателя.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                broadcast_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at TEXT
            )
        ''')
        # Форум-ночь п.7 («🔕 Не присылать сегодня»): MSK-дата ('YYYY-MM-DD'), ДО конца которой
        # (включительно) НЕважные рассылки этому делегату пропускаются при доставке. NULL —
        # делегат ничего не отключал (или уже нажал «🔔 Присылать всё»).
        await _ensure_column(db, "users", "mute_broadcasts_until", "TEXT")
        # Форум-ночь п.7 ревью (находка 🟡): предложение «🔕» альбома — единственная форма
        # доставки, которая ВСЁ ЕЩЁ уходит отдельным сообщением (media_group не принимает
        # reply_markup, см. докстринг services/scheduler.py::send_mute_offer_if_eligible) — MSK-
        # дата последнего показа этого предложения ЭТОМУ делегату не даёт слать его повторно
        # после каждой неважной альбомной рассылки за один день (было — спам).
        await _ensure_column(db, "users", "mute_offer_shown_date", "TEXT")

        # Phase 4 migrations (additive, idempotent — safe against ~590 live users)
        await _ensure_column(db, "users", "payment_status", "TEXT DEFAULT 'not_paid'")
        await _ensure_column(db, "users", "payment_option", "TEXT")
        await _ensure_column(db, "users", "receipt_file_id", "TEXT")
        await _ensure_column(db, "users", "payment_due", "TEXT")
        await _ensure_column(db, "users", "paid_at", "TEXT")

        # YL'26 reg fields (additive, default-off questions — no impact on live flow)
        await _ensure_column(db, "users", "arrival_date", "TEXT")
        await _ensure_column(db, "users", "birth_date", "TEXT")
        await _ensure_column(db, "users", "study_field", "TEXT")
        await _ensure_column(db, "users", "goal", "TEXT")          # multi-select, CSV
        await _ensure_column(db, "users", "formats", "TEXT")       # multi-select, CSV
        await _ensure_column(db, "users", "vk_username", "TEXT")   # ник в ВК (@username) — YL'26
        await _ensure_column(db, "users", "transport", "TEXT")     # трансфер до площадки / самостоятельно
        await _ensure_column(db, "users", "payment_plan_date", "TEXT")  # планируемая дата оплаты взноса
        await _ensure_column(db, "users", "bed_sharing", "TEXT")   # конфа: готов делить двуспальную кровать
        await _ensure_column(db, "users", "bed_partner", "TEXT")   # конфа: с кем именно (условно)
        await _ensure_column(db, "users", "alumni_status", "TEXT")  # аламни / айсекер / ни то, ни другое

        # Phase 4 (CONS-01/02, D-02): per-user consent audit trail. UNIQUE dedupes
        # re-taps; index supports the per-user lookup.
        # Quick 260822 (версионирование согласий): колонка consent_version — какую редакцию
        # текста подписал делегат. Старые строки остаются NULL («до версионирования»).
        # UNIQUE расширен версией: пересогласие новой редакции — НОВАЯ строка, старая не
        # затирается (иначе нечем доказать, что именно подписал делегат раньше). Инлайновый
        # UNIQUE в SQLite не меняется через ALTER — существующая таблица пересобирается один
        # раз (копия строк 1:1, id сохраняются), признак «старая схема» = нет колонки.
        await db.execute(_USER_CONSENTS_DDL)
        if not await _column_exists(db, "user_consents", "consent_version"):
            await db.execute("ALTER TABLE user_consents RENAME TO user_consents_v1")
            await db.execute(_USER_CONSENTS_DDL)
            await db.execute(
                "INSERT INTO user_consents (id, user_id, consent_key, accepted_at) "
                "SELECT id, user_id, consent_key, accepted_at FROM user_consents_v1"
            )
            await db.execute("DROP TABLE user_consents_v1")
        # Quick 260907-4ai: какой ИМЕННО текст был на кнопке в момент подписи — подпись
        # (`consent_button_text`) редактируемая настройка, поэтому доказательством служит
        # снимок текста, а не текущее значение настройки (которое могло смениться позже).
        # Порядок важен: колонка добавляется ПОСЛЕ пересборки выше, иначе одноразовая
        # пересборка её потеряет. Старые строки остаются NULL.
        await _ensure_column(db, "user_consents", "raw_button", "TEXT")
        await db.execute('CREATE INDEX IF NOT EXISTS idx_consents_user ON user_consents(user_id)')

        # Phase 5 migrations (TRACK-01, D-01/D-02) — additive, idempotent, safe against ~590 live users
        await _ensure_column(db, "users", "participant_type", "TEXT DEFAULT 'full'")
        await _ensure_column(db, "reg_started", "participant_type", "TEXT")

        # Phase 8 (ROLE-01, D-13/D-14): one row per delegate question, created ONCE before
        # the D-13 fan-out to every moderate_reg holder (not per-recipient) -- the atomic
        # claim below is keyed by this row's id, never by a recipient's own copy of the
        # notification (08-RESEARCH Pitfall 6). answered_by_name is captured AT claim time:
        # the claiming manager isn't guaranteed a `users` row, so a later get_user() lookup
        # could come back empty.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS delegate_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                asked_at TEXT NOT NULL,
                answered_by INTEGER,
                answered_by_name TEXT,
                answered_at TEXT,
                answer_text TEXT
            )
        ''')
        # T-08-33 (quick task): "delivered" cannot be derived from answer_text -- a
        # successful reply with no text (photo/voice/sticker) writes "" there too, which is
        # indistinguishable from "never delivered" if a reader treats an empty string as
        # falsy. delivered_at is the single unambiguous signal, stamped ONLY on a successful
        # send to the delegate (set_question_answer below), never on a claim alone.
        await _ensure_column(db, "delegate_questions", "delivered_at", "TEXT")

        # Quick 260906-8uq (FAQ-01..06): «❓ Частые вопросы» — city NULL means "все города"
        # (same convention as game_tasks.event_city above), position orders the manager's
        # list (ties broken by id — see reorder_faq_items), enabled toggles delegate
        # visibility without deleting the row. created_at is UTC ISO, same shape as
        # create_question below.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS faq_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by INTEGER
            )
        ''')

        # Phase 07.1 migrations (CITY-01) — additive, idempotent; NO backfill. ~590 rows are
        # accumulated PAST data (only ~100 are live current-event applications); writing
        # "Москва" into old rows would fabricate a fact in storage. NULL means "registered
        # before cities existed" and must stay distinguishable from an explicit Moscow pick.
        # "Москва" is substituted ONLY on read, exclusively via cities.normalize_city — no
        # reader may write that default back into the DB.
        await _ensure_column(db, "users", "event_city", "TEXT")
        await _ensure_column(db, "reg_started", "event_city", "TEXT")

        # Phase 07.3 (A): season as a data entity — additive, idempotent; NO backfill/DEFAULT.
        # NULL means "registered before seasons existed" (same discipline as event_city above).
        # season: written by add_user on every registration (overwritten unconditionally).
        # prev_season: set only on a returning-delegate re-registration (plan 04) — the season
        # the delegate was previously registered under, shown on the moderation card (plan 05).
        await _ensure_column(db, "users", "season", "TEXT")
        await _ensure_column(db, "users", "prev_season", "TEXT")

        # Phase 9 (GAME-01/02/03): task model + submission queue. Tasks are real rows (not a
        # serialized list in bot_settings) — D-07/D-06 audience/second-axis extensions land as
        # one `_ensure_column` later, never a storage rewrite. `deadline_at` is stored in the
        # same ISO-sortable format as services/scheduler.py's `_fmt_dt` ("%Y-%m-%d %H:%M:%S").
        await db.execute('''
            CREATE TABLE IF NOT EXISTS game_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                category TEXT NOT NULL,
                coins INTEGER NOT NULL,
                proof_type TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL
            )
        ''')
        # Phase 09.1 (B, CONTEXT.md "Задания по городам"): NULL = all cities, same semantics
        # as users.event_city (07.1/07.2). No backfill -- every pre-existing task keeps
        # meaning "all cities", which is exactly what it already behaved as.
        await _ensure_column(db, "game_tasks", "event_city", "TEXT")
        # Phase 14 (GAME-08): NULL = active, non-NULL timestamp = archived (delegate never
        # sees/can-submit it; manager can still see it in a separate "🗄 Архив" section and
        # return it). Additive only — no existing game_tasks row changes meaning.
        await _ensure_column(db, "game_tasks", "archived_at", "TEXT")
        # Quick 260819-gtl (CONTEXT.md decisions 1/4): title/photo cover, additive. NULL title
        # means "created before this quick task" -- rendered via task_title()'s fallback (first
        # line of `text`, <=40 chars) everywhere a task is shown, never backfilled in the DB.
        await _ensure_column(db, "game_tasks", "title", "TEXT")
        await _ensure_column(db, "game_tasks", "photo_file_id", "TEXT")

        # Phase 32 (32-01, D-08/D-12/D-28): волна и аудитория задания. `wave_id` NULL —
        # задание вне волн (ровно так уже вело себя каждое существующее задание — оно и
        # дальше видно всем, кому видно сегодня). `audience` NULL или 'all' — задание видит
        # каждый делегат; 'ambassadors' — только действующие амбассадоры (D-28). Бэкафилла
        # нет: у всех текущих заданий обе колонки пусты и читаются как «вне волн, всем».
        await _ensure_column(db, "game_tasks", "wave_id", "INTEGER")
        await _ensure_column(db, "game_tasks", "audience", "TEXT")

        # Phase 23.1-05 (D-10, 23.1-CONTEXT.md O-2): approval date for the delegate profile
        # («одобрена {date}», mockup 04-profile.png). Additive, no backfill — NULL means
        # "approved before this column existed" (same discipline as edited_at/event_city
        # above), the profile simply shows no «одобрена…» fragment for those rows. Stamped by
        # approve_user_atomic/approve_all_pending below — the ONE seam every approval path
        # (bot single approve, bot «Принять всех», web single approve, web «Принять всех»)
        # already funnels through, so profile.py never needs to know which path fired.
        await _ensure_column(db, "users", "approved_at", "TEXT")

        # Quick 260904-aup (UAT D5, «Источник»): персистентный признак «источник пришёл из
        # рекламной метки кампании (src_*), делегат его не вводил» — `_source_from_tag` живёт
        # только в FSM/reg_drafts.meta, а черновик удаляется после финализации, поэтому профиль
        # не мог восстановить причину и печатал метку кампании как ответ делегата. NULL/0 —
        # старые/нетронутые строки: вопрос «Источник» в профиле показывается как раньше.
        await _ensure_column(db, "users", "source_from_tag", "INTEGER DEFAULT 0")

        # Делегации вузов на Москву: принадлежность к делегации — вуз ровно как написан в форме
        # (delegation) и ID ответа формы делегаций (delegation_answer_id). NULL у всех, кто
        # пришёл через анкету; строка users при этом не меняется.
        await _ensure_column(db, "users", "delegation", "TEXT")
        await _ensure_column(db, "users", "delegation_answer_id", "TEXT")
        # Делегат без названия вуза в ответе остаётся делегатом: все гейты и фильтры смотрят на
        # непустой `delegation`.
        await db.execute(
            "UPDATE users SET delegation = 'вуз не указан' WHERE delegation_answer_id IS NOT NULL "
            "AND (delegation IS NULL OR TRIM(delegation) = '')"
        )

        # D-41 (FORUM-CHECKIN.md): регистрация «на месте» — 'walkin' (новый человек прошёл
        # короткую анкету у стойки) или 'door' (существующая заявка одобрена волонтёром у
        # стойки); onsite_at/onsite_by — когда и кто одобрил на месте. NULL у всех старых строк.
        await _ensure_column(db, "users", "onsite_kind", "TEXT")
        await _ensure_column(db, "users", "onsite_at", "TEXT")
        await _ensure_column(db, "users", "onsite_by", "INTEGER")

        # Phase 30 (30-05, задача 3, A2-07): дата решения по отказу — экран статуса заявки
        # подписывает причину отказа ТОЛЬКО датой (30-CONTEXT.md решение владельца №5: без
        # имени менеджера), для чего дата нужна отдельной колонкой — раньше `users` не хранил
        # момент отказа вовсе (в отличие от `approved_at`, D-10). Additive, без бэкафилла —
        # NULL значит «отклонён до этой колонки», экран статуса просто не подписывает причину
        # датой для таких старых решений. Стампится в `reject_user` (единственный атомарный шов
        # и бота, и веба — `services.applications.claim_reject`), второй точки записи нет.
        await _ensure_column(db, "users", "rejected_at", "TEXT")

        # Координатор 25.09 (учёт доставки решения, память auto-approve-incident-260906: 38
        # заявок одобрены молча без письма — ровно то, что эти колонки должны ловить). Четыре
        # аддитивные колонки на ПОСЛЕДНЕЕ решение (тот же приём, что approved_at/rejected_at
        # выше — не append-only журнал, одна строка = текущее состояние доставки):
        #   decision_delivery_status   — 'delivered' | 'failed' | 'queued' (тихие часы)
        #   decision_delivery_decision — 'approved' | 'rejected', К КАКОМУ решению относится запись
        #   decision_delivery_at       — момент последней записи (msk_now, "%Y-%m-%d %H:%M:%S")
        #   decision_delivery_error    — человеческая причина сбоя (только status='failed')
        # NULL у всех четырёх — «неизвестно»: либо решение ещё не было (pending), либо оно
        # принято ДО этой миграции (признака тогда не было вовсе) — БЕЗ бэкафилла, честно не
        # путаем с «не доставлено» (`services/sheet_reconcile.py` отчёт разводит эти две
        # категории). Пишет `services.application_effects.apply_decision_effects`/
        # `mass_approve_effects` — единственный choke-point всех путей решения (бот/веб/
        # автоотказ/квик-скрипт, см. их докстринг). Возврат на модерацию
        # (`revert_user_to_pending` ниже) сбрасывает все четыре в NULL — решения, к которому они
        # относились, больше нет.
        await _ensure_column(db, "users", "decision_delivery_status", "TEXT")
        await _ensure_column(db, "users", "decision_delivery_decision", "TEXT")
        await _ensure_column(db, "users", "decision_delivery_at", "TEXT")
        await _ensure_column(db, "users", "decision_delivery_error", "TEXT")

        # Phase 28 (28-01, SU-01/SU-04/SU-08): СкиллАп 5 — новые вопросы анкеты (default-off,
        # тумблеры reg_q_stack/reg_q_experience/reg_q_readiness/reg_q_resume_link/reg_q_mini_*/
        # reg_q_case_optin) + пять НЕ-REG_FLOW полей резервной цепочки резюме/скоринга/амбассадора.
        # Только добавление колонок — существующая база (Юлид/РилТолк) открывается без изменений,
        # новые колонки пусты у всех текущих строк. Восемь колонок ниже — шаги REG_FLOW, они
        # участвуют в INSERT ниже (add_user); ОСТАЛЬНЫЕ ПЯТЬ (resume_type/link_verified/
        # is_ambassador/score/is_it_3plus) — НЕ ответы на вопросы анкеты, их пишет узкий
        # update_user_answers (планы 28-04/28-05/28-06), тем же приёмом, что source_from_tag/
        # resume_url — в INSERT их сознательно НЕ добавлять (RESEARCH «Don't Hand-Roll»).
        await _ensure_column(db, "users", "stack", "TEXT")
        await _ensure_column(db, "users", "experience", "TEXT")
        await _ensure_column(db, "users", "readiness", "TEXT")
        await _ensure_column(db, "users", "resume_link", "TEXT")
        await _ensure_column(db, "users", "mini_projects", "TEXT")
        await _ensure_column(db, "users", "mini_portfolio", "TEXT")
        await _ensure_column(db, "users", "mini_direction", "TEXT")
        await _ensure_column(db, "users", "case_optin", "TEXT")
        await _ensure_column(db, "users", "resume_type", "TEXT")
        await _ensure_column(db, "users", "link_verified", "INTEGER DEFAULT 0")
        await _ensure_column(db, "users", "is_ambassador", "INTEGER DEFAULT 0")
        await _ensure_column(db, "users", "score", "INTEGER")
        await _ensure_column(db, "users", "is_it_3plus", "INTEGER DEFAULT 0")

        # Phase 32 (32-01, D-24/D-31/D-32): статус амбассадора — сам флаг `is_ambassador`
        # (Phase 28, выше) уже есть, эти три колонки описывают ЕГО ЖИЗНЕННЫЙ ЦИКЛ и не трогают
        # его семантику. `ambassador_path` — NULL, пока делегат не выбрал путь. `ambassador_since`
        # — момент, с которого человек стал амбассадором; NULL значит «был им ещё до этой фазы»
        # (участвует в первой же волне на общих основаниях, а не как новичок). `ambassador_left_at`
        # — момент ухода; NULL = состоит по сей день. Единственная точка записи всех трёх —
        # set_ambassador_flag/set_ambassador_path ниже.
        await _ensure_column(db, "users", "ambassador_path", "TEXT")
        await _ensure_column(db, "users", "ambassador_since", "TEXT")
        await _ensure_column(db, "users", "ambassador_left_at", "TEXT")

        # `content` stores a file_id for photo/pdf proof, raw text for text/link proof — the
        # project never writes uploaded files to disk (README/CLAUDE.md file_id pattern).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS game_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                content TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by INTEGER,
                reviewed_at TEXT,
                coins_awarded INTEGER,
                reject_reason TEXT
            )
        ''')
        # D-05 as a schema-level invariant, not a Python pre-INSERT check: while a submission
        # for (task_id, user_id) is anything other than 'rejected', a second INSERT for that
        # same pair cannot land, even under two concurrent inserts (SQLite raises
        # IntegrityError, which create_submission below turns into a `None` return — T-09-01).
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_game_submissions_active "
            "ON game_submissions(task_id, user_id) WHERE status != 'rejected'"
        )

        # Phase 09.1 (A): free-form multi-part submissions. `game_submissions.content`/
        # `content_type` stay NOT NULL for backward compatibility -- old rows are read as
        # ONE implicit part (get_submission_parts_or_legacy), never migrated into this table.
        # No FOREIGN KEY -- mirrors game_submissions.task_id being a bare INTEGER, the project
        # does not use SQLite FK constraints anywhere else.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS game_submission_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL,
                ord INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                caption TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_submission_parts_sub "
            "ON game_submission_parts(submission_id, ord)"
        )

        # Quick 260822: очередь сдач для дайджеста менеджерам (режим game_submit_notify_mode =
        # digest). FSM — MemoryStorage, поэтому накопленное живёт в БД и дошлётся после
        # рестарта (services/game_digest.py::rearm_pending_digests). city NULL = «без города»
        # (модуль городов выключен) — одна общая джоба game_digest:all.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS game_submit_digest_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                city TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_submit_digest_unsent "
            "ON game_submit_digest_queue(sent_at, city)"
        )

        # Phase 32 (32-01, D-10): амбассадорская волна — без названия (колонки под него нет),
        # у строки есть только номер (свой в пределах города, выдаёт next_wave_number ниже),
        # даты начала/конца, необязательный вводный текст (NULL = без него, D-11), необязательное
        # число призовых мест (NULL = взять общую настройку wave_prize_places, D-18) и состояние.
        # `event_city` NULL — волна общая для всех городов (та же семантика, что у
        # game_tasks.event_city). `started_notified_at` NULL — стартовые ЛС ещё не ушли;
        # непустое значение — состав заданий и даты волны заперты (первая рассылка это фиксирует).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ambassador_waves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number INTEGER NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                intro_text TEXT,
                prize_places INTEGER,
                state TEXT NOT NULL DEFAULT 'draft',
                event_city TEXT,
                started_notified_at TEXT,
                created_by INTEGER,
                created_at TEXT NOT NULL
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ambassador_waves_dates "
            "ON ambassador_waves(event_city, starts_at)"
        )

        # Phase 32 (32-01, D-17): неизменяемый снимок призёров волны. `PRIMARY KEY (wave_id,
        # user_id)` — не отдельный AUTOINCREMENT id — физически не даёт повторному «Объявить
        # итоги» переписать уже объявленную строку: запись идёт только через
        # `INSERT OR IGNORE`, повтор возвращает 0 вставленных строк, а не тихую перезапись.
        #
        # Ревью фазы 32 (CR-06): снимок хранит ВСЕХ участников волны на момент объявления, не
        # только призёров — личное место невыигравшего задним числом пересчитать нечем (D-17),
        # а рассылка итогов должна читать готовый снимок, а не гонять `wave_rating` заново.
        # `is_winner` отличает призовые строки (их и раньше возвращал `get_wave_results`) от
        # рядовых участников — фаза 32 ни разу не выкатывалась, менять схему уже созданной
        # таблицы можно свободно, без `_ensure_column`/миграции старых данных.
        # `notified_at` — персональная отметка «этому участнику итоги уже отправлены»: без неё
        # частичная рассылка (упал бот/джоба потеряна) не возобновляема и либо теряет хвост
        # получателей навсегда, либо задваивает уже отправленным при повторном запуске.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS wave_results (
                wave_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                place INTEGER NOT NULL,
                points INTEGER NOT NULL,
                is_winner INTEGER NOT NULL DEFAULT 1,
                announced_at TEXT NOT NULL,
                notified_at TEXT,
                PRIMARY KEY (wave_id, user_id)
            )
        ''')

        # Phase 32 (32-01, D-22): разовое начисление баллов за приглашённого. `invitee_id` —
        # НАСТОЯЩИЙ PRIMARY KEY (не составной) — один приглашённый получает ровно одну строку
        # НАВСЕГДА, даже через цикл «отказали -> одобрили снова» (T-32-01-01): запись идёт
        # через `INSERT OR IGNORE` + `rowcount == 1` (идиома add_staff), без предварительной
        # проверки в Python — уникальность держит сама схема. `wave_id` — волна, действовавшая
        # в момент начисления; NULL = начисление случилось вне волн либо это бэкафилл (D-23).
        # `source` различает обычное начисление при одобрении от разового бэкафилла задним числом.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS referral_credits (
                invitee_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                coins INTEGER NOT NULL,
                wave_id INTEGER,
                credited_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'approval'
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_referral_credits_referrer "
            "ON referral_credits(referrer_id, wave_id)"
        )

        # Ступени амбассадоров СкиллАп (квалифицированная амбассадорка): одна строка на
        # (амбассадор, ступень), пишется `INSERT OR IGNORE` + `rowcount == 1`
        # (database/amb_tiers_db.py::claim_new_tiers) — повтор «Принять всех», два менеджера,
        # бот и веб одновременно второй строки не создают. Ступень НЕ снимается никогда, даже
        # если приглашённому потом отказали. `o2o_status` — только у ступени 2: 'granted' в
        # пределах квоты, дальше 'waitlist'. `notified_at` ставится ДО отправки уведомления —
        # повторный разбор события из очереди второго сообщения не шлёт.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ambassador_tiers (
                telegram_id INTEGER NOT NULL,
                tier INTEGER NOT NULL,
                reached_at TEXT NOT NULL,
                o2o_status TEXT,
                notified_at TEXT,
                PRIMARY KEY (telegram_id, tier)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ambassador_tiers_o2o "
            "ON ambassador_tiers(tier, o2o_status, reached_at)"
        )
        # Ступень, снятая менеджером вручную: автоматика (check_tiers, сверка раз в 10 минут,
        # бэкафилл) её не возвращает, пока менеджер не нажмёт «Вернуть ступень». Метка
        # привязана к сезону — новый сезон начинается с чистого листа.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS amb_tier_revocations (
                telegram_id INTEGER NOT NULL,
                tier INTEGER NOT NULL,
                season TEXT NOT NULL,
                revoked_at TEXT NOT NULL,
                revoked_by INTEGER,
                PRIMARY KEY (telegram_id, tier, season)
            )
        ''')
        # Ручное исключение приглашённого из зачёта амбассадора (накрутка). Исключённый не
        # входит ни в один счётчик; уже выданные ступени исключение НЕ удаляет.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ambassador_exclusions (
                invitee_id INTEGER PRIMARY KEY,
                reason TEXT NOT NULL,
                excluded_by INTEGER,
                excluded_at TEXT NOT NULL
            )
        ''')

        # Квик 260916: та же очередь, но для НОВЫХ ЗАЯВОК (режим reg_submit_notify_mode =
        # digest, services/reg_digest.py). Отдельная таблица, а не общая с играми: у сдач
        # свои submission_id/task_id, у заявок их нет, а общая таблица с половиной пустых
        # колонок читалась бы хуже, чем два семилинейных CREATE рядом. city NULL = «без
        # города» (модуль городов выключен или город в анкете пуст) — одна общая джоба
        # reg_digest:all.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reg_submit_digest_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                city TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reg_submit_digest_unsent "
            "ON reg_submit_digest_queue(sent_at, city)"
        )
        # Phase 31 (31-02, D-17): признак автоотказа штампуется НА ПОСТАНОВКЕ строки в очередь
        # (enqueue_reg_digest ниже), а не выводится из users.status в момент отправки — к
        # моменту отправки статус мог уже смениться (менеджер вернул заявку из журнала
        # автоотказов, claim_auto_reject_return), и сводка соврала бы. Дефолт 0 — легаси-строки
        # очереди (до этой колонки) читаются как «не автоотказ», что и было их фактическим
        # состоянием (колонки не существовало, автоотказа не было вовсе).
        await _ensure_column(db, "reg_submit_digest_queue", "auto_rejected", "INTEGER NOT NULL DEFAULT 0")
        # Ревью 25.09: причина постановки в очередь, помимо «обычная новая заявка» (NULL) и
        # «автоотказ» (auto_rejected=1 выше) — сейчас единственное непустое значение "revert"
        # («↩️ Вернуть в ожидание», handlers/admin_revert_pending.py) — не даёт пачке
        # уведомлений menеджерам выглядеть НОВОЙ заявкой (services/reg_digest.py::
        # send_reg_digest группирует по этому полю тем же приёмом, что и auto_rejected).
        await _ensure_column(db, "reg_submit_digest_queue", "reason", "TEXT")

        # Quick 260904-dq1: «🌙 Тихие часы» — очередь уведомлений делегату, отложенных до конца
        # окна тишины (services/quiet_hours.py). `kind` — закрытый диспетчер на стороне
        # разборщика (application_decision / text_html), `payload` — JSON. `error` хранит след
        # неотправленной/перерешённой строки (T-dq1-06: без отдельного аудита, масштаб
        # 1000-1500 делегатов). FSM — MemoryStorage, поэтому очередь живёт в БД и переживает
        # рестарт (джоба разбора взводится заново в init_scheduler, ре-арм не нужен — interval).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS delayed_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                error TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delayed_notifications_due "
            "ON delayed_notifications(sent_at, due_at)"
        )

        # Phase 19 (D-01): outbox побочных эффектов Mini App. Веб-процесс `miniapp` пишет
        # сюда события (сдача создана, сдача проверена, задание изменилось, ручные монеты),
        # бот подбирает их джобой (план 19-08) и делает уведомления/дайджест/Sheets. Схемой
        # владеет ТОЛЬКО бот: `miniapp` никогда не зовёт init_db (два мигратора за одним
        # ALTER TABLE), а при отсутствии таблицы его enqueue молча пропускает событие.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS miniapp_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_miniapp_outbox_pending "
            "ON miniapp_outbox(processed_at, id)"
        )

        # Пост рейтинга в чат делегатов (ревью 28.09): за какую неделю пост уже ушёл в чат
        # города. Смена дня/времени после сегодняшнего поста переставляет cron-джобу, и она
        # сработала бы второй раз за ту же неделю — джоба сверяется с этой строкой. `city` —
        # код города, '' у одногородского бота. Ни одного telegram_id — не делегатский след.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_rating_posts (
                city TEXT PRIMARY KEY,
                week TEXT NOT NULL,
                posted_at TEXT NOT NULL
            )
        ''')

        # Phase 27 (27-02, LANG-02/LANG-03): хранилище переводов. Первичный ключ —
        # `(lang, src_hash)`, ГДЕ src_hash — sha256 РЕЗУЛЬТАТА резолюции (services/i18n.py::
        # src_hash), а не ключ реестра bot_settings. Причина: пространство делегатских ключей
        # трёхосное (динамические `reg_prompt_*`/`reg_help_*` вне реестра, трековые суффиксы
        # `__party`/`__short`, городские `__city__{code}`, и они композитны), плюс литералы
        # кода, у которых ключа нет вовсе — а переводится всегда одна реально показанная
        # строка, у неё один хеш. Побочный эффект желательный: правка русского текста меняет
        # src_hash → старая английская строка в этой таблице становится недостижимой (новый
        # хеш её просто не находит) — устаревший перевод физически не может показаться
        # делегату, без отдельной инвалидации. `manual=1` — правка менеджера (LANG-05);
        # `upsert_translation` ниже не даёт машинному переводу затереть её. `origin_key` —
        # метка «откуда пришло» для админского экрана (план 27-06), на идентичность строки
        # не влияет никак.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS translations (
                lang TEXT NOT NULL,
                src_hash TEXT NOT NULL,
                src_text TEXT NOT NULL,
                text TEXT,
                manual INTEGER NOT NULL DEFAULT 0,
                origin_key TEXT,
                updated_at TEXT,
                PRIMARY KEY (lang, src_hash)
            )
        ''')

        # Phase 27 (27-02, LANG-04): очередь на перевод — форма один в один как у
        # `miniapp_outbox` выше (тот же паттерн: attempts/last_error, дошлёт после рестарта).
        # UNIQUE(lang, src_hash) + `INSERT OR IGNORE` в enqueue_translation — дедупликация
        # массового пресета (`handlers/reg_schema.py::_apply_*_preset` кладёт десятки ключей
        # одним нажатием) решена в СХЕМЕ, не в коде воркера плана 27-03. Пишут сюда оба
        # процесса (бот и `miniapp`), в `translations` — только бот (A-04, 27-CONTEXT.md).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS translation_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lang TEXT NOT NULL,
                src_hash TEXT NOT NULL,
                src_text TEXT NOT NULL,
                origin_key TEXT,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                UNIQUE(lang, src_hash)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_translation_queue_pending "
            "ON translation_queue(attempts, id)"
        )

        # Phase 23 (APP-TINDER-01, D-06): журнал решений по заявкам с окном отмены. Решение по
        # заявке УЖЕ записано в users.status (approve_user_atomic/reject_user/будущий сервис),
        # но ПОБОЧНЫЕ эффекты (приветствие делегату/сообщение об отказе/строка в листе)
        # отложены до effects_due_at — окно, в течение которого менеджер может нажать «Отменить»
        # без единого исходящего сообщения. Ровно один из двух исходов — «эффекты заявлены»
        # (effects_sent_at) или «отменено» (undone_at) — может случиться с конкретной строкой;
        # это гарантируют условные UPDATE ... WHERE effects_sent_at IS NULL AND undone_at IS NULL
        # в claim_application_undo/claim_due_application_decisions ниже, а НЕ порядок вызовов —
        # два параллельных сметателя (бот-джоба и веб-отмена) не могут оба выиграть одну строку.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS application_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                decided_by INTEGER NOT NULL,
                decided_at TEXT NOT NULL,
                effects_due_at TEXT NOT NULL,
                effects_sent_at TEXT,
                undone_at TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_application_decisions_due "
            "ON application_decisions(effects_sent_at, undone_at, effects_due_at)"
        )

        # Phase 31 (31-02, D-05/D-15): правила автоотказа по анкете. Условия правила хранятся
        # ОДНОЙ JSON-колонкой (`conditions`), а не нормализованной схемой условий — D-02
        # фиксирует ровно два булевых уровня (группы через ИЛИ, условия внутри группы через
        # И) без вложенности и без скобок, нормализовывать нечего: вторая таблица условий
        # добавила бы JOIN там, где чистый оценщик (`reg_engine.evaluate_reject_rules`, план
        # 31-01) всё равно грузит правило целиком и разбирает JSON в Python. Значения условий
        # внутри `conditions` — ПОДПИСИ вариантов анкеты (та же конвенция, что у
        # `reg_engine.compute_score`, см. 31-01), а не коды: правило хранится в терминах того,
        # что видел менеджер в редакторе, и переживает переименование кода варианта. `enabled`
        # по умолчанию 0 (D-15): на событиях, где правила ещё не настраивали, автоотказ не
        # включается сам — общий рубильник модуля живёт в `bot_settings` (план 31-05), но и на
        # уровне ОДНОГО правила по умолчанию ничего не меняется, пока менеджер явно не включит.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reject_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                city TEXT,
                tracks TEXT NOT NULL,
                conditions TEXT NOT NULL,
                action TEXT NOT NULL,
                reject_text TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                paused_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by INTEGER
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reject_rules_scope ON reject_rules(enabled, city)"
        )

        # Phase 31 (31-02, D-18/D-24): журнал срабатываний автоотказа — ОТДЕЛЬНАЯ таблица от
        # `application_decisions` (комментарий выше), потому что у неё другой жизненный цикл:
        # у `application_decisions` есть окно отмены (`undone_at`) и решение принимается один
        # раз за раз; здесь возврат на модерацию (`returned_to_moderation_at`) возможен в ЛЮБОЙ
        # момент после срабатывания, а повторное срабатывание того же правила не плодит новую
        # строку — растёт `attempt_count` (D-24, лимита попыток нет, это явное решение
        # владельца). Частичный уникальный индекс `idx_auto_reject_log_live` гарантирует, что
        # «живая» (`returned_to_moderation_at IS NULL`) строка у делегата ровно одна: возврат
        # закрывает её, следующее срабатывание заводит новую живую строку, а не второй
        # активный журнал на одного человека.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS auto_reject_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                rule_ids TEXT NOT NULL,
                reject_texts TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                first_triggered_at TEXT NOT NULL,
                last_triggered_at TEXT NOT NULL,
                returned_to_moderation_at TEXT,
                returned_by INTEGER
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_reject_log_tid ON auto_reject_log(telegram_id)"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_reject_log_live "
            "ON auto_reject_log(telegram_id) WHERE returned_to_moderation_at IS NULL"
        )

        # Phase 15 (STAT-03, D-06): append-only registration-funnel event log -- the top of
        # the funnel (start / form_started / form_completed) is otherwise physically
        # unrecoverable from `reg_started` (a keyed UPSERT, not a log). No UNIQUE: every call
        # is a genuine new row, repeats are expected (a delegate can /start many times).
        # Index supports the daily-funnel query (GROUP BY substr(ts, 1, 10), filtered by event).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reg_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                event_city TEXT,
                season TEXT,
                ts TEXT NOT NULL
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_reg_events_event_ts ON reg_events(event, ts)')
        # Квик 260905-qqg: метка кампании из deep-link `src_<метка>`; NULL у органики и у всех
        # строк, записанных до этой миграции. Отдельный индекс не заводится — блок «Метки
        # кампаний» читается один раз на отрисовку дашборда, объём таблицы не требует его.
        await _ensure_column(db, "reg_events", "source_tag", "TEXT")
        # Опросы (native Telegram polls). `polls` — сам опрос и его аудитория (audience_json —
        # тот же filter_spec, что у рассылок; [] = все). `poll_messages` — по строке на
        # ДОСТАВЛЕННЫЙ чат: бот шлёт каждому делегату ОТДЕЛЬНЫЙ Telegram-опрос со своим
        # telegram_poll_id, поэтому это одновременно (а) карта «ответ → наш опрос», (б) список
        # message_id для stop_poll и (в) чекпоинт доставки для дошлёта после рестарта (как
        # scheduled_broadcast_deliveries). `totals_json` — последние счётчики из update `poll`
        # (единственный источник итогов для анонимных опросов). `poll_answers` — по человеку,
        # только для неанонимных (Telegram не присылает poll_answer по анонимным).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                is_anonymous INTEGER NOT NULL DEFAULT 0,
                allows_multiple INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                created_at TEXT,
                city TEXT,
                audience_json TEXT,
                status TEXT NOT NULL DEFAULT 'scheduled',
                scheduled_at TEXT,
                sending_since TEXT,
                closed_at TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS poll_messages (
                poll_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                telegram_poll_id TEXT,
                message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'ok',
                totals_json TEXT,
                PRIMARY KEY (poll_id, chat_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_poll_messages_tg ON poll_messages(telegram_poll_id)"
        )
        await db.execute('''
            CREATE TABLE IF NOT EXISTS poll_answers (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_ids_json TEXT NOT NULL,
                answered_at TEXT,
                UNIQUE (poll_id, user_id)
            )
        ''')

        # Phase 30 (30-02, A2-03): справочники ВУЗов/городов для типа шага `lookup` — общий
        # модуль правил `services/lookup.py` (нормализация/поиск/чипы/очередь) — единственный
        # читатель/писатель этих двух таблиц, бот и Mini App ищут по ОДНИМ данным. Уникальность
        # по (kind, alias_norm): один и тот же нормализованный псевдоним не должен попасть в
        # справочник дважды под разные каноники — на этот индекс полагаются и идемпотентный
        # посев (`seed_lookup_from_snapshot`, `INSERT OR IGNORE`), и `enqueue_merge`, чтобы
        # «СПбГАСУ» и «спбгасу» остались одной записью, а не двумя.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS lookup_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                canonical TEXT NOT NULL,
                alias TEXT NOT NULL,
                alias_norm TEXT NOT NULL,
                source TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                added_by INTEGER,
                created_at TEXT NOT NULL
            )
        ''')
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_lookup_entries_uniq "
            "ON lookup_entries(kind, alias_norm)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_lookup_entries_kind_norm "
            "ON lookup_entries(kind, alias_norm, canonical)"
        )

        # Очередь «Другое → влить как псевдоним» (A2-03, тонкая настройка менеджера, план
        # 30-07, необязательна): делегат ответил свободным текстом на шаге типа `lookup`,
        # `services.lookup.enqueue_merge` кладёт сюда сырой ответ вместо того, чтобы его
        # потерять. `status`: `new` — ждёт решения менеджера, `merged` — менеджер добавил как
        # псевдоним (новой или существующей) каноники, `rejected` — решил не заводить (мусор/
        # опечатка/спам). Дубль по (kind, raw_norm, status='new') не плодится — `enqueue_merge`
        # проверяет наличие необработанной записи перед вставкой (T-30-05: объём сезона
        # 1000-1500 делегатов, rate-limit вне фазы).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS lookup_merge_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                raw_norm TEXT NOT NULL,
                step_key TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                decided_by INTEGER,
                decided_at TEXT,
                created_at TEXT NOT NULL
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_lookup_merge_queue_kind_status "
            "ON lookup_merge_queue(kind, status)"
        )
        await seed_lookup_from_snapshot(db, "university")
        await seed_lookup_from_snapshot(db, "city")

        # Квик 260914-rgr (RGR-01..07, D-9): учёт чата делегатов. Текст сообщений здесь НЕ
        # хранится НИКОГДА — только счётчики активности и таймлайн событий вступления/выхода.
        # Три таблицы: состав (chat_members), обезличенная активность по дням (chat_activity)
        # и лог событий (chat_events, нужен графику «вступления по дням» на дашборде).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                status TEXT,
                joined_at TEXT,
                left_at TEXT,
                updated_at TEXT,
                source TEXT,
                PRIMARY KEY (chat_id, telegram_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_activity (
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                messages INTEGER NOT NULL DEFAULT 0,
                replies INTEGER NOT NULL DEFAULT 0,
                media INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, telegram_id, day)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                ts TEXT NOT NULL
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_events_chat_ts ON chat_events(chat_id, ts)"
        )
        # Квик 260927 (автоочистка служебных уведомлений): статус САМОГО бота в группе и право
        # «Удаление сообщений». can_delete NULL = ещё не проверяли; 0 = удалять нельзя (очистка
        # молча пропускает чат без вызовов API, пока права не вернут — my_chat_member обновит).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_bot_state (
                chat_id INTEGER PRIMARY KEY,
                bot_status TEXT,
                can_delete INTEGER,
                checked_at TEXT
            )
        ''')
        # Квик 260927 (живой рейтинг чата): журнал сообщений БЕЗ ТЕКСТА (D-9 — длина считается и
        # сразу забывается), текущие реакции, ники и админы группы. ts — Москва,
        # "%Y-%m-%d %H:%M:%S". reactions_extra заполняет только импорт экспорта: реакции,
        # посчитанные Telegram, чьи дарители неизвестны.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                text_len INTEGER NOT NULL DEFAULT 0,
                reply_to_message_id INTEGER,
                reply_to_author_id INTEGER,
                is_channel_post INTEGER NOT NULL DEFAULT 0,
                reactions_extra INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'live',
                PRIMARY KEY (chat_id, message_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_ts ON chat_messages(chat_id, ts)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_author "
            "ON chat_messages(chat_id, telegram_id)"
        )
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_reactions (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                reaction TEXT NOT NULL,
                ts TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id, telegram_id, reaction)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_reactions_msg ON chat_reactions(chat_id, message_id)"
        )
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_usernames (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                updated_at TEXT
            )
        ''')
        # Подпись участника без @ника на дашборде — имя из Telegram (только first_name, без
        # фамилии), иначе «без ника»: голый telegram_id менеджеру не показываем.
        await _ensure_column(db, "chat_usernames", "first_name", "TEXT")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_admins (
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                synced_at TEXT,
                PRIMARY KEY (chat_id, telegram_id)
            )
        ''')
        # Отложенное удаление служебных уведомлений (services/chat_cleanup.py): очередь в БД,
        # которую разбирает одна интервальная джоба, — вместо date-джобы на каждое сообщение
        # (массовое вступление по ссылке писало сотни строк в jobstore синхронно).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_cleanup_queue (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_cleanup_queue_due ON chat_cleanup_queue(due_at)"
        )

        # Indexes under the hot admin/scheduler queries. Each one mirrors a real WHERE/ORDER BY
        # in this module (see the comments in _HOT_PATH_INDEXES); nothing speculative.
        await _ensure_hot_path_indexes(db)

        # Квик 260912 (QUICK-260912-LWY): накопленные строки с прежней подписью подстановки
        # `users.source` приводятся к новой. Безопасно — значение НЕ входит в
        # `reg_options.DEFAULT_SOURCE_OPTIONS`, кнопкой шага «Откуда узнал» его выбрать было
        # нельзя, значит любая строка с этой подписью — подстановка бота, а не ответ делегата;
        # единственный теоретический путь честного совпадения — свободный ввод «Другое → напиши
        # свой» ровно этим словом, за сезон таких случаев не встречалось. Идемпотентно: после
        # первого прогона под WHERE не попадает ни одна строка, стоимость — один скан `users`
        # на старте. Колонка `users.transport` с тем же словом («Самостоятельно» — реальный
        # вариант ответа шага «Трансфер») не задета — WHERE привязан к колонке `source`.
        #
        # Пост-фикс (полный прогон 260912): на легаси-схеме `users` без колонки `source`
        # (тестовые БД старых миграций — `test_db_phase1`/`test_db_phase4`/`test_db_phase5`/
        # `test_cities_phase71`/`test_db_hot_path_indexes`/`test_i18n_store_27`) голый UPDATE
        # ронял `init_db` целиком: `sqlite3.OperationalError: no such column: source`. Тот же
        # приём, что у `_ensure_hot_path_indexes` («index skipped when column missing») —
        # `_column_exists` ДО UPDATE, не try/except (глотать реальную ошибку схемы нельзя).
        if await _column_exists(db, "users", "source"):
            await db.execute(
                "UPDATE users SET source = ? WHERE source = ?",
                (reg_options.SOURCE_NOT_ASKED, reg_options.SOURCE_NOT_ASKED_LEGACY),
            )

        # Квик 260923 (форум-чекин, D-01): персональный QR для отметки на форуме. Колонка
        # аддитивная (_ensure_column — существующие записи не трогает), значение НЕ бэкафилится:
        # генерируется лениво при первом запросе QR (get_or_create_checkin_token ниже), поэтому
        # у делегата, ни разу не открывавшего «🎟 Мой QR», колонка остаётся NULL. Частичный
        # уникальный индекс (WHERE checkin_token IS NOT NULL) — тот же приём, что у
        # idx_auto_reject_log_live выше: NULL не участвует в уникальности, а не-NULL значения
        # обязаны различаться (страховка от коллизии secrets.token_urlsafe на уровне схемы, не
        # только по вероятности).
        await _ensure_column(db, "users", "checkin_token", "TEXT")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_checkin_token "
            "ON users(checkin_token) WHERE checkin_token IS NOT NULL"
        )

        # Phase 12 (FORUM-CHECKIN.md, D-09/D-10/D-20): таблица отметок «пришёл» — вход и сессии
        # программы на одной схеме. `point` = "entry" для входа (services.checkin.ENTRY_POINT),
        # `"session:{id}"` (services.program.point_for_session) — для сессий. `UNIQUE(telegram_id,
        # point)` + `INSERT OR IGNORE` (record_checkin ниже) хранит ПЕРВЫЙ скан на точку — верно
        # для входа (D-10: «повторы отбрасываются»). D-20 («на сессии засчитывается ПОСЛЕДНИЙ
        # скан слота») этому идемпотентному INSERT не подчиняется — сессии идут через ОТДЕЛЬНУЮ
        # функцию `record_session_checkin` (delete-then-insert внутри слота параллельных сессий,
        # `services.program.parallel_group`), вход продолжает жить на INSERT OR IGNORE как был.
        # `source` — miniapp (сканер Mini App) | csv (загрузка выгрузки офлайн-сканера) | manual
        # (по фамилии/от руки, D-11/D-12) | auto_session (услуга `services.checkin.record_arrival`
        # сама подтверждает вход, когда делегата отметили на сессии, а на входе он ещё не был).
        # `approx_time` — 1, если время скана не удалось прочитать из файла и подставлено время
        # загрузки (D-10).
        # Вход каждый день: уникальность теперь (telegram_id, point, day) — `_CHECKINS_DDL` и
        # `_migrate_checkins_per_day` выше; старые базы пересоздаются здесь же.
        await db.execute(_CHECKINS_DDL)
        await _migrate_checkins_per_day(db)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkins_point ON checkins(point)"
        )

        # Нагрузочный прогон 25.09: очередь записи «Пришёл» в Google-лист. Отметка/снятие/CSV
        # пишут ТОЛЬКО сюда (оба процесса — бот и Mini App без Google-кредов), джоба бота
        # (`services/sheet_arrival_sync.py`) раз в 30 с разбирает пачку: одно чтение столбца id
        # на вкладку и один batch_update, значение ячейки — всегда из `checkins` (время первого
        # входа), поэтому строка события — лишь «пересчитать этого делегата», повтор безвреден.
        # Дубли по telegram_id не схлопываются в схеме (UNIQUE дал бы гонку «прочитал значение —
        # пришло новое событие — удалил»): джоба удаляет строки id <= прочитанного максимума.
        # `action` — set (отметка) | recompute (снятие), для журнала; на значение не влияет.
        # `next_try_at` — когда брать снова (экспоненциальный backoff после сбоя листа).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sheet_arrival_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                city TEXT,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at TEXT NOT NULL,
                last_error TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sheet_arrival_queue_due "
            "ON sheet_arrival_queue(next_try_at, id)"
        )

        # 29.09: очередь записи «В чате» в Google-лист — вход/выход из чата, одобрение, сверка
        # состава. Та же семантика, что у sheet_arrival_queue выше (строка = «пересчитать
        # делегата», значение всегда из базы, джоба `services/sheet_chat_sync.py` удаляет id <=
        # прочитанного максимума). Отдельная таблица, а не вид события в sheet_arrival_queue:
        # ту пишет и Mini App, а drop/fail по (telegram_id, id <= max) без фильтра по виду
        # снесли бы чужие события уже работающей очереди.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sheet_chat_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at TEXT NOT NULL,
                last_error TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sheet_chat_queue_due "
            "ON sheet_chat_queue(next_try_at, id)"
        )

        # Внешние формы (Яндекс/Google): ответы чужих форм живут в БД бота — она источник
        # правды, лист только зеркало. Анкета хранится целиком снимком «вопрос -> ответ»
        # (payload JSON), чтобы правка формы потом не переписала то, что человек ответил.
        # Секреты приложения Яндекса и токены менеджера лежат в отдельных таблицах, а не
        # в bot_settings: set_setting логирует значение. Согласия на ПД здесь нет намеренно:
        # чужую форму бот не ведёт.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS external_form_secrets (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT,
                updated_by INTEGER
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS external_form_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL DEFAULT 'yandex',
                org_id TEXT,
                org_header TEXT,
                access_token TEXT,
                refresh_token TEXT,
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'ok',
                alerted_at TEXT,
                created_at TEXT NOT NULL,
                created_by INTEGER
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS external_forms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                connection_id INTEGER,
                external_id TEXT NOT NULL,
                gsheet_gid INTEGER,
                title TEXT NOT NULL,
                secret TEXT UNIQUE,
                key_username_q TEXT,
                key_phone_q TEXT,
                mirror_tab TEXT,
                mirror_error TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                notify INTEGER NOT NULL DEFAULT 0,
                notified_at TEXT,
                last_answer_at TEXT,
                last_sync_at TEXT,
                sync_error TEXT,
                created_at TEXT NOT NULL,
                created_by INTEGER
            )
        ''')
        # Одна и та же форма (платформа + источник + вкладка) подключается один раз: двойное
        # нажатие «Верно» в мастере иначе плодило дубли с разными секретами.
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ext_forms_source "
            "ON external_forms(platform, external_id, IFNULL(gsheet_gid, -1))"
        )
        # Режим зеркала формы во вкладку: 'bot' — своя раскладка бота («Дата | Делегат |
        # Статус | ID | вопросы…»), 'yandex_export' — «как выгрузка Яндекса» для вкладки
        # делегаций (UR REGS): колонки в порядке формы, строка узнаётся по ID ответа в колонке A.
        await _ensure_column(db, "external_forms", "mirror_mode", "TEXT DEFAULT 'bot'")
        # Предупреждение менеджеру от зеркала — например, вопрос формы без свободной колонки
        # в листе. В отличие от mirror_error НЕ останавливает очередь листа: иначе неустранимое
        # предупреждение зациклило бы «включить запись → снова ошибка».
        await _ensure_column(db, "external_forms", "mirror_warning", "TEXT")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS external_form_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                form_id INTEGER NOT NULL,
                answer_id TEXT NOT NULL,
                answered_at TEXT,
                received_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                raw TEXT,
                matched_telegram_id INTEGER,
                match_how TEXT,
                sheet_state TEXT NOT NULL DEFAULT 'append',
                sheet_attempts INTEGER NOT NULL DEFAULT 0,
                sheet_next_try_at TEXT,
                UNIQUE(form_id, answer_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ext_answers_tid "
            "ON external_form_answers(matched_telegram_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ext_answers_form "
            "ON external_form_answers(form_id, matched_telegram_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ext_answers_sheet "
            "ON external_form_answers(sheet_state, sheet_next_try_at)"
        )
        await db.execute('''
            CREATE TABLE IF NOT EXISTS external_form_columns (
                form_id INTEGER NOT NULL,
                qkey TEXT NOT NULL,
                label TEXT NOT NULL,
                position INTEGER NOT NULL,
                header_written INTEGER NOT NULL DEFAULT 0,
                UNIQUE(form_id, qkey)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS external_form_pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                form_id INTEGER NOT NULL,
                answer_id TEXT NOT NULL,
                delivery_id TEXT,
                received_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at TEXT NOT NULL,
                last_error TEXT,
                UNIQUE(form_id, answer_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ext_pending_due "
            "ON external_form_pending(next_try_at, id)"
        )
        # Надгробия удалённых анкет: источник (Яндекс/Google) помнит ответ дальше, и без метки
        # ближайшая сверка вернула бы стёртые ПД. Здесь только id, самих данных нет.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS external_form_deleted (
                form_id INTEGER NOT NULL,
                answer_id TEXT NOT NULL,
                deleted_at TEXT,
                UNIQUE(form_id, answer_id)
            )
        ''')
        # Делегации вузов на Москву: оценка ответа формы делегаций (ЦА / не ЦА / проверить) и
        # ручные решения менеджера. Источник правды по самому ответу — external_form_answers
        # (form_id, answer_id); здесь только то, чего там нет: ta_status, вуз/курс как в форме
        # (для сводок без разбора payload), ник для поиска по /start, привязка к Telegram и
        # кто/когда решил. decided_by — id менеджера (ручное решение переоценкой не трогается).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS delegation_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                form_id INTEGER NOT NULL,
                answer_id TEXT NOT NULL,
                ta_status TEXT NOT NULL,
                university TEXT,
                course_raw TEXT,
                course_canonical TEXT,
                username_needle TEXT,
                answered_at TEXT,
                linked_telegram_id INTEGER,
                link_how TEXT,
                decided_by INTEGER,
                decided_at TEXT,
                note TEXT,
                created_at TEXT,
                UNIQUE(form_id, answer_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delegation_answers_tid "
            "ON delegation_answers(linked_telegram_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delegation_answers_needle "
            "ON delegation_answers(username_needle)"
        )

        # Форум-ночь B1 (идея №10, перевыпуск QR): старый токен после reissue_checkin_token
        # ниже уходит сюда — скан УЖЕ недействительного QR отвечает причиной «QR заменён»
        # (services.checkin.resolve_scanned_user), а не общим «не найден», как для по-
        # настоящему чужого/поддельного кода. PRIMARY KEY на old_token — реиссью одного и
        # того же делегата дважды кладёт сюда ДВЕ РАЗНЫЕ строки (два разных сгенерированных
        # токена), коллизия между СВОИМИ старыми токенами по построению невозможна
        # (secrets.token_urlsafe), а с чужим активным (users.checkin_token) — тот же довод,
        # что у idx_users_checkin_token выше: коллизия на масштабе проекта практически
        # невозможна.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS checkin_token_replacements (
                old_token TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                replaced_at TEXT NOT NULL
            )
        ''')

        # Форум-ночь п.3 (D-03, идея №2): кому и когда отправлен QR перед форумом + подтверждение
        # «✅ Сохранил, открывается». `telegram_id` PRIMARY KEY — один QR-делегат = одна строка
        # (в отличие от `checkins`, где `point` даёт несколько строк на человека — здесь точек
        # нет, только «отправлено/подтверждено»). `event_city` — СНИМОК города на момент отправки
        # (не JOIN на users.event_city): счётчик «QR получили N» города обязан оставаться верным,
        # даже если делегат сменит город анкеты уже после рассылки. `sent_at` — время ПЕРВОЙ
        # отправки (вечерняя рассылка/ручная кнопка «Разослать QR сейчас» — INSERT OR IGNORE,
        # см. services.checkin_broadcast.send_broadcast — не двигается утренним повтором,
        # чтобы «получили N» считало людей, а не отправки). `confirmed_at` NULL, пока делегат не
        # нажал кнопку подтверждения (services.checkin_broadcast.confirm_receipt).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS checkin_qr_sends (
                telegram_id INTEGER PRIMARY KEY,
                event_city TEXT,
                sent_at TEXT NOT NULL,
                confirmed_at TEXT
            )
        ''')

        # Форум-ночь п.6 (D-25, идея №14): «Не пришёл» — готовый шаблон рассылки с кнопками
        # ответа делегата («Уже еду»/«Не смогу прийти»/«Я на месте»). Одна строка = один
        # (делегат, день) — `UNIQUE(telegram_id, day)` даёт ДВОЙНУЮ службу без второй таблицы:
        # (1) идемпотентность самой ОТПРАВКИ (повторный тап «Написать не пришедшим» в тот же
        # день — INSERT OR IGNORE, тем, у кого уже есть строка за сегодня, второе сообщение не
        # уходит), и (2) хранилище ОТВЕТА (UPDATE той же строки, когда делегат жмёт кнопку).
        # `day` — календарный день ОТПРАВКИ (МСК), не день форума — двухдневная Москва
        # (30–31.10) может слать этот шаблон оба дня, каждый день независимо идемпотентен.
        # `event_city` — СНИМОК города на момент отправки, тот же приём, что `checkin_qr_sends`
        # выше (счётчик сводки не должен уехать, если делегат сменит город анкеты позже).
        # `response`/`responded_at` — NULL, пока делегат не ответил; значения — `database.db.
        # CNA_COMING`/`CNA_CANT`/`CNA_HERE`.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS checkin_not_arrived (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                event_city TEXT,
                sent_at TEXT NOT NULL,
                response TEXT,
                responded_at TEXT,
                UNIQUE(telegram_id, day)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkin_not_arrived_day ON checkin_not_arrived(day)"
        )

        # D-33 (решение владельца 24.09): шпаргалка волонтёра чек-ина (checkin_volunteer_guide_
        # text) за день до форума ГОРОДА — идемпотентность ПО ДНЮ форума, не по человеку раз и
        # навсегда (`UNIQUE(telegram_id, day)`, тот же приём, что `checkin_not_arrived` выше) —
        # волонтёр нескольких форумов города в разные даты получает напоминание перед КАЖДЫМ.
        # `day` — YYYY-MM-DD дня САМОГО форума (не дня отправки, в отличие от checkin_not_
        # arrived.day — тут ровно один день-до-форума на город, дублей не бывает и без привязки
        # к дате отправки). `city` — снимок города джобы на момент отправки (для отчёта, не для
        # переадресации — тот же приём, что checkin_qr_sends.event_city).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS checkin_volunteer_guide_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                city TEXT,
                sent_at TEXT NOT NULL,
                UNIQUE(telegram_id, day)
            )
        ''')

        # Идея №31 бэклога чек-ина (журнал площадки): кто что сделал в день форума. Живые
        # отметки (сканер Mini App / поиск по фамилии) журналятся ПО ОДНОЙ — строку `checkins`
        # потом может удалить снятие или перенос D-20, а журнал остаётся; CSV — ОДНОЙ строкой
        # на загрузку (счётчики в `details`), построчно его отметки и так лежат в `checkins`
        # с `by_staff_id`/`source="csv"`. Плюс не-отметочные действия: снятие отметки,
        # отмена скана волонтёром, перевыпуск QR, пропуск «разово».
        # `telegram_id` — делегат (NULL у загрузки CSV); `staff_id`/`staff_name` — кто
        # (имя — снимок на момент действия: волонтёр может не быть делегатом и не иметь
        # строки в `users`). `city` — нормализованный код города делегата (у CSV — город
        # точки), фильтр экрана «📓 Журнал площадки». `details` — JSON (точка, время скана,
        # прошлая сессия слота для отката переноса). `undone_at` — у строки отметки, которую
        # волонтёр отменил в окне «↩️ Отменить».
        await db.execute('''
            CREATE TABLE IF NOT EXISTS venue_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                staff_id INTEGER,
                staff_name TEXT,
                telegram_id INTEGER,
                city TEXT,
                point TEXT,
                source TEXT,
                details TEXT,
                undone_at TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_venue_log_staff ON venue_log(staff_id, id)"
        )

        # Форум-ночь п.4 (расписание форума в боте — владелец отверг импорт из таблицы):
        # program_halls/program_sessions, per-city. `day`/`start_time`/`end_time` — простые
        # ISO/24ч строки ('YYYY-MM-DD'/'HH:MM'), не отдельный тип даты/времени — сравнение
        # строк лексикографически совпадает со сравнением значения, лишний парсинг на каждый
        # запрос не нужен (пересечение слотов/сортировка по времени — обычный `ORDER BY`/`<`).
        # `services.program.point_for_session` уже готовит `f"session:{id}"` для будущей
        # отметки на сессиях (FORUM-CHECKIN.md D-18..D-20) — не эта задача, только совместимое
        # API. Удаление зала НЕ каскадит сессии (`delete_program_hall`) — они остаются без
        # зала (`hall_id -> NULL`), а не пропадают из программы.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS program_halls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                name TEXT NOT NULL,
                capacity INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_program_halls_city ON program_halls(city, sort_order)"
        )
        await db.execute('''
            CREATE TABLE IF NOT EXISTS program_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                day TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                title TEXT NOT NULL,
                speaker TEXT,
                hall_id INTEGER REFERENCES program_halls(id),
                description TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_program_sessions_city_day "
            "ON program_sessions(city, day, start_time)"
        )

        # Форум-ночь п.9 (идея №15 бэклога чек-ина, D-24): «⭐ Отзыв о сессии одним тапом» —
        # ОДНА строка на (делегат, сессия): `prompted_at` ставится ПЕРВОЙ (до самой отправки,
        # `INSERT OR IGNORE`) — идемпотентность рассылки живёт на этом же UNIQUE, не на
        # отдельном флаге. `rating`/`comment` пусты, пока делегат не ответил; повторный тап по
        # другой оценке — обычный UPDATE той же строки (одна оценка на сессию, правило плана).
        # `telegram_id` — не FK на users (тот же стиль, что `checkins`/`sos_reports`) —
        # чек-ин/отзыв переживают отсутствие строки users в редких гонках порядка миграций.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS session_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                rating INTEGER,
                comment TEXT,
                prompted_at TEXT NOT NULL,
                rated_at TEXT,
                commented_at TEXT,
                UNIQUE(telegram_id, session_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_feedback_session ON session_feedback(session_id)"
        )

        # Форум-ночь п.8 (идея №19, SOS): «🆘 SOS» — карточка в чат оргов + захват/решение/
        # эскалация. Метки времени — московские (`services.timeutil.msk_now()`, конвенция
        # квика 260912-mcj для нового кода, delegate_questions/reg_answer_history остаются
        # UTC-исключением по docstring `_MSK_MIGRATION_COLUMNS` выше и SOS в него не входит).
        # `city` — СНИМОК города делегата на момент отправки (тот же приём, что
        # `checkin_qr_sends.event_city`) — привязка чата SOS резолвится по нему. `chat_id`/
        # `card_message_id` — где живёт карточка (группа оргов ИЛИ, при фоллбэке без
        # привязанного чата, NULL — тред делегатского ответа в этом случае недоступен,
        # см. `services/sos.py`). `claimed_by`/`resolved_by` — атомарные UPDATE ... WHERE
        # IS NULL, тот же приём, что `claim_question`/T-08-33.
        #
        # D-31 (24.09, «SOS без категорий»): `category` осталась NULL-able ради обратной
        # совместимости (старые строки с категориями сохранены как есть) — новый код её больше
        # не пишет (`create_sos_report` больше не принимает этот аргумент вовсе) и не читает
        # (`services.sos.render_card_text`/`handlers.admin_sos._row_text`).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sos_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                city TEXT,
                category TEXT,
                details_text TEXT,
                details_photo_file_id TEXT,
                latitude REAL,
                longitude REAL,
                chat_id INTEGER,
                card_message_id INTEGER,
                claimed_by INTEGER,
                claimed_by_name TEXT,
                claimed_at TEXT,
                resolved_by INTEGER,
                resolved_by_name TEXT,
                resolved_at TEXT,
                escalated_at TEXT,
                created_at TEXT NOT NULL,
                delivery_failed_at TEXT,
                prior_open_report_id INTEGER
            )
        ''')
        # Ревью 24.09 (находки 1/3): `delivery_failed_at` — карточка не дошла НИКУДА (ни в чат,
        # ни фоллбэком в личку), `services/sos.py::record_delivery_outcome`; `prior_open_report_id`
        # — снимок «у делегата уже был открытый SOS #N», когда окно повторного открытия
        # (`sos_reopen_window_minutes`) истекло и делегат открыл новый, не дожидаясь ответа на
        # старый (см. `handlers/sos.py::sos_pick_category`). `_ensure_column` — на случай, если
        # таблица уже создана более ранней версией этой же ветки (cf0da0b) на стенде.
        await _ensure_column(db, "sos_reports", "delivery_failed_at", "TEXT")
        await _ensure_column(db, "sos_reports", "prior_open_report_id", "INTEGER")
        await _relax_sos_reports_category(db)
        # Сколько напоминаний «у тебя в работе без ✅ Решено» уже ушло взявшему (потолок —
        # services.sos.CLAIMED_REMIND_DELAYS_MINUTES) — в БД, а не в аргументах джобы: рестарт
        # бота не должен начинать лесенку напоминаний заново.
        await _ensure_column(db, "sos_reports", "claimed_remind_count", "INTEGER NOT NULL DEFAULT 0")
        # Ответ делегату реплаем на уже решённую карточку (приёмка 01.10): ответ уходит, а
        # карточка честно показывает, кто и когда дописал после «✅ Решено».
        await _ensure_column(db, "sos_reports", "post_resolve_reply_by_name", "TEXT")
        await _ensure_column(db, "sos_reports", "post_resolve_reply_at", "TEXT")
        # «🔁 Перехватить»: у кого перехватили взятый SOS (взявший пропал — сел телефон, ушёл со
        # смены). Карточка показывает это рядом с новым взявшим. NULL у старых строк.
        await _ensure_column(db, "sos_reports", "taken_over_from_name", "TEXT")
        # Когда делегат последний раз вошёл в режим «дописываю SOS» по этой заявке (повторный
        # «🆘 SOS» в окне переоткрытия перезапускает таймер). `services.sos.may_be_collecting`
        # считает возраст от него, а не от создания заявки. NULL у старых строк.
        await _ensure_column(db, "sos_reports", "collecting_started_at", "TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sos_reports_telegram_id ON sos_reports(telegram_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sos_reports_city ON sos_reports(city)"
        )

        # Форум-ночь п.8: заявка «Привязать чат SOS» ждёт пересылки/команды `/sos_id` из
        # целевой группы — `admin_id` PRIMARY KEY (одна незавершённая заявка на менеджера,
        # повторный тап кнопки перезаписывает). Не делегатский след -> USER_PURGE_EXCLUDED
        # (это бронирование действия менеджера, не данные делегата).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sos_chat_bind_pending (
                admin_id INTEGER PRIMARY KEY,
                city TEXT,
                requested_at TEXT NOT NULL
            )
        ''')

        # Копии карточки SOS, разошедшиеся фоллбэком в личку (чат SOS не привязан/упал): без
        # них «Беру»/«Решено» писали в БД, но карточку не перерисовывали ни у кого — остальные
        # админы не видели, что заявка взята, и брали её второй раз. `chat_id` — личка
        # админа, не делегата -> USER_PURGE_EXCLUDED; копии заявок удаляемого делегата
        # стирает purge_user подзапросом по report_id (как game_submission_parts).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sos_card_copies (
                report_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (report_id, chat_id)
            )
        ''')

        # Дописки делегата, скопированные ботом в тред карточки в чате SOS (или в личку админа,
        # если чата нет): орг отвечает реплаем на последнее сообщение человека, а не на
        # карточку, — по этой таблице реплай находит свою заявку. `chat_id` — чат SOS или
        # личка админа, не делегат -> USER_PURGE_EXCLUDED;
        # строки заявок удаляемого делегата стирает purge_user подзапросом по report_id.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sos_relay_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
        ''')

        # Идея №16 бэклога чек-ина: «📊 Отчёт дня форума» вечером — идемпотентность
        # АВТОМАТИЧЕСКОЙ отправки по (город, день форума), `UNIQUE(city, day)`. `city` хранит
        # сентинел `"_all"` вместо NULL при выключенном модуле городов (SQLite не считает два
        # NULL равными в UNIQUE — с настоящим NULL повторный тик джобы без городов вставлял бы
        # вторую строку и ломал идемпотентность; тот же приём сентинела нужен и ниже у
        # `forum_noshow_poll`). Ручная кнопка «📊 Отчёт дня сейчас» эту таблицу НЕ трогает
        # (handlers/admin_forum_functions.py — ручной запуск не помечает автоматическую
        # отправку «сделанной», см. docstring services/forum_day_report.py).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS forum_day_report_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                day TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(city, day)
            )
        ''')

        # Идея №23 бэклога чек-ина: опрос неявившихся «почему не пришёл». Одна строка —
        # одновременно идемпотентность ОТПРАВКИ (`UNIQUE(telegram_id, season)`, тот же приём,
        # что checkin_not_arrived, но ключ — сезон, не день: опрос уходит РОВНО один раз за
        # сезон+город, не каждый день заново) И хранилище ОТВЕТА (UPDATE той же строки).
        # `season` — снимок `event_season` на момент отправки (пустая строка, не NULL, если
        # настройка ещё не задана — та же причина, что у сентинела `city` выше: NULL не
        # держит уникальность). `city` — снимок `users.event_city` на момент отправки (тот же
        # приём, что `checkin_qr_sends.event_city`) — только для отчётности, не для
        # переадресации.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS forum_noshow_poll (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                city TEXT,
                season TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                reason TEXT,
                comment TEXT,
                answered_at TEXT,
                UNIQUE(telegram_id, season)
            )
        ''')

        # Трек «региональные форумы → Москва» (04.10): предложение одобренному неявившемуся
        # делегата регионального форума перенести заявку на московский форум. Та же идемпотентность
        # ОТПРАВКИ, что у `forum_noshow_poll` выше (`UNIQUE(telegram_id, season)` — одно
        # предложение на сезон, не на регион: делегат мог сменить регион между отправкой и
        # ответом, строка остаётся привязана к сезону). `source_city` — снимок `users.event_city`
        # НА МОМЕНТ отправки (для отчётности, не для маршрутизации самого переноса).
        # `response` — сентинелы `RNM_MOVED`/`RNM_DECLINED`, `NULL` — ещё не ответил.
        # `target_city` — куда реально перевели (заполняется только при `response=RNM_MOVED`;
        # может отличаться от текущей настройки `regional_noshow_target_city`, если её поменяли
        # между отправкой и ответом — строка хранит ФАКТ, не текущую настройку).
        # `notified_at` — когда строка попала в агрегированную сводку менеджеру города
        # назначения (`services/regional_noshow_move.py::_notify_managers_job`), `NULL` — ещё
        # не попала (или ответ не `RNM_MOVED` — сводка считает только реально переехавших).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS regional_noshow_move (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                source_city TEXT,
                season TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                response TEXT,
                responded_at TEXT,
                target_city TEXT,
                notified_at TEXT,
                UNIQUE(telegram_id, season)
            )
        ''')

        # Идея №29 бэклога чек-ина («Твой Юлид в цифрах»): идемпотентность рассылки итоговой
        # картинки-карточки после форума. Та же форма, что `forum_noshow_poll`/
        # `regional_noshow_move` выше — `UNIQUE(telegram_id, season)`, одна карточка на
        # делегата за сезон (не за город: делегат мог сменить город анкеты между отправкой и
        # повторным тапом «Разослать», вторая карточка ему не нужна). `city` — снимок
        # `users.event_city` на момент отправки (для отчётности «отправлено N в городе X»,
        # не для переадресации).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS forum_stats_card_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                city TEXT,
                season TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(telegram_id, season)
            )
        ''')

        # Идея №20 бэклога чек-ина: бюро находок. `chat_id`/`message_id` — где опубликован
        # пост находки (группа делегатов, `services/chat_tracking.py::chat_for_city`), НЕ
        # личность делегата — тот же класс, что `sos_card_copies.chat_id` выше
        # (USER_PURGE_EXCLUDED, не USER_PURGE_TABLES). `posted_by`/`returned_by` — id
        # волонтёра/менеджера (сотрудник, не делегатский след). `returned_at IS NULL` —
        # находка ещё не забрали; идемпотентность кнопки «✅ Нашёлся хозяин»
        # (`mark_lost_found_returned` — UPDATE только по этому условию).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS lost_found (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT,
                photo_file_id TEXT NOT NULL,
                where_text TEXT NOT NULL,
                posted_by INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                returned_at TEXT,
                returned_by INTEGER
            )
        ''')

        # Phase 33 (delegate-card admin actions, задачи 2/3): персональные одноразовые
        # исключения из глобальных положений `reg_resubmit_after_reject`/`reg_edit_policy` —
        # «🔁 Разрешить повторную подачу» и «✏️ Открыть правку после решения»
        # (`services/delegate_overrides.py`). `kind` — 'resubmit' | 'edit'. Активная строка —
        # `revoked_at IS NULL AND consumed_at IS NULL`; инвариант «не больше одной активной на
        # (telegram_id, kind)» держит вызывающий код (grant отказывает, если уже есть активная),
        # не UNIQUE-ограничение — так же, как остальные append-only журналы этого проекта
        # (reg_answer_history и т.п.), где повторная выдача после отзыва — новая строка, не
        # перезапись старой (история «кто и когда выдавал/отзывал» видна целиком).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS admin_delegate_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                granted_by INTEGER NOT NULL,
                granted_at TEXT NOT NULL,
                consumed_at TEXT,
                revoked_at TEXT,
                revoked_by INTEGER
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_delegate_overrides_active "
            "ON admin_delegate_overrides(telegram_id, kind)"
        )
        # Ревью 25.09: частичный UNIQUE поверх «активной» строки — сам constraint, а не только
        # обычный код, не даёт параллельному двойному тапу «Выдать» завести два активных
        # исключения одного вида одному делегату (check-then-insert в services/
        # delegate_overrides.py::grant_override гонялся между запросом и вставкой). WHERE
        # совпадает с фильтром active_override/grant_delegate_override.
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_delegate_overrides_unique_active "
            "ON admin_delegate_overrides(telegram_id, kind) "
            "WHERE revoked_at IS NULL AND consumed_at IS NULL"
        )

        # Квик 260912-mcj: одноразовый сдвиг семьи «сейчас» бота на московское время — на
        # этом же соединении, до финального commit (см. докстринг функции).
        await _migrate_local_timestamps_to_msk(db)
        # D-29: осиротевший menu_schedule -> menu_program (см. докстринг функции).
        await _migrate_menu_schedule_into_program(db)
        # Статус амбассадора (колонки, архив сезонов, миграция user_version = 3) живёт в своём
        # модуле: здесь и так 12 тысяч строк, а писать статус можно только из amb_status_db.
        from database import amb_status_db
        await amb_status_db.ensure_schema(db)
        # Журнал зачётов приглашённых — колонки на referral_credits.
        from database import amb_journal_db
        await amb_journal_db.ensure_schema(db)
        # Заморозка прежних дефолтов ступеней (user_version = 4): строго после статуса (3).
        from database import amb_tiers_db
        await amb_tiers_db.freeze_legacy_tier_defaults(db)

        await db.commit()

# Perf (стенд 17.09: `/app/api/admin/settings/all` — 4.65 с / 2239 отдельных SQLite-
# соединений на один HTTP-запрос — `_connect()` открывается заново на КАЖДЫЙ `get_setting`,
# а экран настроек читает реестр из ~700 ключей, часть из них per-city — N+1 умножается на
# число городов). Ниже — опциональный СНИМОК `bot_settings` на время одного запроса/рендера:
# `ContextVar`, не глобальный кэш модуля — реестр настроек (`settings_schema`/`cities`/
# `settings_ops`) весь читает через `get_setting`/`get_setting_typed`, ни один модуль не
# трогает `bot_settings` в обход него (см. ревью 17.09), поэтому снимок покрывает всю цепочку
# резолвинга без единой правки в этих модулях.
#
# `ContextVar` — не threading.local и не process-global: asyncio копирует контекст при
# создании КАЖДОГО Task (обработка одного HTTP-запроса в FastAPI/Starlette — отдельный Task),
# поэтому снимок одного запроса не течёт в параллельный (в отличие от кэша между запросами,
# который CLAUDE.md прямо запрещает без инвалидации: менеджер переключил тумблер — следующий
# запрос обязан увидеть новое значение). Инвалидация не нужна ВООБЩЕ: снимок живёт строго
# в пределах одного `async with settings_snapshot():` и никогда не переживает границу запроса.
_settings_snapshot_var: ContextVar[dict[str, str] | None] = ContextVar(
    "_settings_snapshot", default=None
)


async def _load_settings_snapshot() -> dict[str, str]:
    async with _connect() as db:
        async with db.execute("SELECT key, value FROM bot_settings") as cursor:
            rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


@asynccontextmanager
async def settings_snapshot():
    """Один запрос `bot_settings` целиком на время блока — `get_setting`/`set_setting`/
    `delete_setting` внутри читают/пишут этот dict вместо открытия соединения на каждый ключ.

    Реентерабельно: вложенный вызов (экран группы настроек вызывает `render_...` и
    `build_...` подряд, оба сами оборачиваются в этот менеджер) не переоткрывает снимок и не
    гасит его на выходе из внутреннего блока — снимком владеет самый внешний вызов."""
    if _settings_snapshot_var.get() is not None:
        yield
        return
    snapshot = await _load_settings_snapshot()
    token = _settings_snapshot_var.set(snapshot)
    try:
        yield
    finally:
        _settings_snapshot_var.reset(token)


async def get_setting(key: str) -> str | None:
    snapshot = _settings_snapshot_var.get()
    if snapshot is not None:
        return snapshot.get(key)
    async with _connect() as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    # Quick 260820-rms: единственная точка записи настроек — единственное место, где можно
    # дёшево получить аудит правок. 20.08 в source_options и approve_text прилетело «/start»,
    # и установить, кто и когда это сделал, было нечем: в логе не было ни строки. Значение
    # режем — это конфиг, а не персональные данные, но длинные тексты приветствия в логе не
    # нужны.
    preview = value if value is None or len(value) <= 60 else value[:60] + "…"
    logger.info(f"setting {key} <- {preview!r}")
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
    # Снимок текущего запроса (если открыт) обновляется той же записью — правка в фазе 2
    # `settings/batch` обязана быть видна немедленному перечитыванию `_item_for` в конце того
    # же запроса, а не только следующему HTTP-запросу.
    snapshot = _settings_snapshot_var.get()
    if snapshot is not None:
        snapshot[key] = value
    await _maybe_enqueue_translation(key, value)


async def _maybe_enqueue_translation(key: str, value) -> None:
    """Phase 27 (27-03, LANG-04): постановка делегатского текста в очередь машинного
    перевода при сохранении настройки — врезана в `set_setting`, ЕДИНСТВЕННУЮ точку записи
    настроек (64 вызова), а не в хендлеры админки: так покрываются бесплатно и веб-настройки
    Mini App (`miniapp/routers/settings.py`), и массовые пресеты
    (`handlers/reg_schema.py::_apply_*_preset`).

    **Fail-soft (T-27-03-04):** обёрнута ЦЕЛИКОМ в `try/except` — запись настройки к этому
    моменту УЖЕ закоммичена, сбой очереди (или движка перевода, которого этот модуль даже не
    импортирует) не имеет права её откатить или уронить вызывающего. Ленивые импорты
    `settings_schema`/`services.*` — `database.db` низкий слой, `settings_schema` САМ
    импортирует `database.db` на уровне модуля (см. `settings_schema.py:30`), статический
    импорт назад создал бы цикл.

    Две независимые ветки:
    1. `key == "delegate_lang_enabled"` — переключение модуля. Значение `"on"` запускает
       фоновый `bulk_seed()` всего корпуса анкеты (`services.background.spawn`, НЕ голый
       `create_task` — слабые ссылки убивают фоновую работу, T-27-03-01). Значение `"off"`
       (или что угодно иное) — ничего не делает. Сам ключ НЕ является делегатским текстом
       (группа `toggles`, не в `DELEGATE_GROUPS`) и во вторую ветку не идёт.
    2. Любой другой ключ — если модуль включён И (ключ делегатский
       (`services.i18n_sources.is_delegate_dynamic_key`) ИЛИ ключ из явного списка
       `_MINIAPP_EXTRA_TRANSLATE_KEYS` ниже) И значение непусто, каждая непустая строка
       значения (список разворачивается построчно — `_parse_setting`'овский формат "по строке
       на вариант") ставится в очередь через `enqueue_translation` (`UNIQUE(lang, src_hash)`
       дедуплицирует повторы и массовые пресеты в схеме, не здесь). Явная проверка префикса
       `consent` — страховка сверх границы `DELEGATE_GROUPS` (согласия и так вне
       `DELEGATE_GROUPS`, LANG-09), а не единственная защита."""
    try:
        if key == "delegate_lang_enabled":
            if value == "on":
                from services import background
                from services.i18n_worker import bulk_seed

                background.spawn(bulk_seed())
            return

        if not value:
            return

        from settings_schema import get_setting_typed

        if await get_setting_typed("delegate_lang_enabled") != "on":
            return
        if key.startswith("consent"):
            return

        from services.i18n_sources import is_delegate_dynamic_key

        if not is_delegate_dynamic_key(key) and not _is_miniapp_extra_translate_key(key):
            return

        from services.i18n import src_hash

        for line in str(value).splitlines():
            text = line.strip()
            if text:
                await enqueue_translation("en", src_hash(text), text, origin_key=key)
    except Exception as exc:  # noqa: BLE001 — намеренно широкий fail-soft (T-27-03-04)
        logger.error(
            "set_setting: постановка в очередь перевода не удалась для %s (%s)", key, exc,
        )


# Задача «делегатский интерфейс на английском»: событийные тексты, которые делегат видит на
# хабе Mini App (`miniapp/routers/hub.py`) ДО начала анкеты и вне её — `event_date`/
# `event_place_name` и их пара `event_time`/`event_place_address` — свободный текст без
# дефолта (менеджер печатает даты/адрес каждый сезон заново, `settings_schema.py: default:
# None`), группа `event` НЕ входит в `DELEGATE_GROUPS` (LANG-08, `services/i18n_sources.py`
# — намеренная граница для КОРПУСА АНКЕТЫ, трогать её нельзя, `tests/test_i18n_sources_27.py
# ::test_non_delegate_groups_excluded`). Этот список — НЕ расширение той границы, а отдельный
# явный whitelist четырёх ключей, которые видны вне анкеты (тот же приём, что у подписи
# города — `_maybe_enqueue_city_label_translation` выше, но здесь ключ уже в `bot_settings`,
# второй функции заводить незачем)."""
_MINIAPP_EXTRA_TRANSLATE_KEYS = frozenset({
    "event_date", "event_time", "event_place_name", "event_place_address",
})


_CITY_OVERRIDE_SEP = "__city__"  # то же самое значение, что cities.PER_CITY_SEP — литерал, а
# не импорт: `database/db.py` не имеет права импортировать `cities` (цикл, `cities.py` сам
# импортирует `database.db`; `tests/test_cities_registry_260818.py::
# test_db_py_never_imports_cities_module` — структурный сторож этого правила).


def _is_miniapp_extra_translate_key(key: str) -> bool:
    """`key` сам ИЛИ его городской вариант (`{key}__city__{code}`, `per_city: True` в схеме
    этих четырёх ключей) — из `_MINIAPP_EXTRA_TRANSLATE_KEYS`."""
    if key in _MINIAPP_EXTRA_TRANSLATE_KEYS:
        return True
    base = key.split(_CITY_OVERRIDE_SEP, 1)[0]
    return base in _MINIAPP_EXTRA_TRANSLATE_KEYS


async def delete_setting(key: str):
    logger.info(f"setting {key} <- (сброшено)")  # Quick 260820-rms: та же линия аудита
    async with _connect() as db:
        await db.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
        await db.commit()
    snapshot = _settings_snapshot_var.get()
    if snapshot is not None:
        snapshot.pop(key, None)


async def add_user(data: dict):
    async with _connect() as db:
        # ON CONFLICT DO UPDATE (not INSERT OR REPLACE): REPLACE is DELETE+INSERT and would
        # wipe columns absent from this list (e.g. status) on re-registration. status is
        # owned by the approval flow + migration default only — never touched here.
        await db.execute('''
            INSERT INTO users (
                telegram_id, username, full_name, email, age,
                is_aiesec_member, source, source_details,
                education_status, university, course, specialty,
                work_status, work_sphere,
                missing_skills, expectations, phone, city,
                referrer_id, registration_date,
                is_ambassador_candidate,
                local_committee, position, attendance_format,
                comments, expectations_ar, informal_day, resume_file_id, resume_text, resume_url,
                department, aiesec_role, needs_certificate, english_level,
                allergies, food_pref, arrival, housing, cc_shop,
                exp_organizers, exp_content, volunteer,
                payment_status, payment_option, receipt_file_id, payment_due, paid_at,
                arrival_date, birth_date, study_field, goal, formats, vk_username,
                transport, payment_plan_date, bed_sharing, bed_partner, participant_type,
                alumni_status, event_city, season, prev_season,
                stack, experience, readiness, resume_link,
                mini_projects, mini_portfolio, mini_direction, case_optin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                email=excluded.email,
                age=excluded.age,
                is_aiesec_member=excluded.is_aiesec_member,
                source=excluded.source,
                source_details=excluded.source_details,
                education_status=excluded.education_status,
                university=excluded.university,
                course=excluded.course,
                specialty=excluded.specialty,
                work_status=excluded.work_status,
                work_sphere=excluded.work_sphere,
                missing_skills=excluded.missing_skills,
                expectations=excluded.expectations,
                phone=excluded.phone,
                city=excluded.city,
                referrer_id=excluded.referrer_id,
                registration_date=excluded.registration_date,
                is_ambassador_candidate=excluded.is_ambassador_candidate,
                local_committee=excluded.local_committee,
                position=excluded.position,
                attendance_format=excluded.attendance_format,
                comments=excluded.comments,
                expectations_ar=excluded.expectations_ar,
                informal_day=excluded.informal_day,
                resume_file_id=COALESCE(excluded.resume_file_id, users.resume_file_id),
                resume_text=COALESCE(excluded.resume_text, users.resume_text),
                resume_url=COALESCE(excluded.resume_url, users.resume_url),
                department=excluded.department,
                aiesec_role=excluded.aiesec_role,
                needs_certificate=excluded.needs_certificate,
                english_level=excluded.english_level,
                allergies=excluded.allergies,
                food_pref=excluded.food_pref,
                arrival=excluded.arrival,
                housing=excluded.housing,
                cc_shop=excluded.cc_shop,
                exp_organizers=excluded.exp_organizers,
                exp_content=excluded.exp_content,
                volunteer=excluded.volunteer,
                receipt_file_id=COALESCE(excluded.receipt_file_id, users.receipt_file_id),
                -- WR-06: payment_status/payment_option/payment_due/paid_at are deliberately
                -- omitted here so re-registration (e.g. a rejected user) never wipes payment
                -- state. They are owned by update_payment_status / the payment flow. COALESCE
                -- would not help payment_status (its bound value defaults to 'not_paid', never
                -- NULL), so omission is the correct guard.
                arrival_date=excluded.arrival_date,
                birth_date=excluded.birth_date,
                study_field=excluded.study_field,
                goal=excluded.goal,
                formats=excluded.formats,
                vk_username=excluded.vk_username,
                transport=excluded.transport,
                payment_plan_date=excluded.payment_plan_date,
                bed_sharing=excluded.bed_sharing,
                bed_partner=excluded.bed_partner,
                participant_type=excluded.participant_type,
                alumni_status=excluded.alumni_status,
                -- Квик 27.09: повторная подача без города (обходной вход, приложение) не затирает
                -- уже известный город заявки — NULL поверх города не пишем никогда.
                event_city=COALESCE(excluded.event_city, users.event_city),
                -- Phase 07.3 (A): unconditional overwrite (not COALESCE) — a new registration
                -- always writes the CURRENT event_season; prev_season is only ever non-NULL when
                -- the caller (plan 04's finalize_registration) explicitly passes it.
                season=excluded.season,
                prev_season=excluded.prev_season,
                stack=excluded.stack,
                experience=excluded.experience,
                readiness=excluded.readiness,
                resume_link=excluded.resume_link,
                mini_projects=excluded.mini_projects,
                mini_portfolio=excluded.mini_portfolio,
                mini_direction=excluded.mini_direction,
                case_optin=excluded.case_optin,
                -- D-41 (ревью 28.09): полная анкета — обычная заявка. Признак регистрации на
                -- месте (walk-in/door) снимается, иначе строка навсегда выпадала бы из очереди
                -- модерации и «Принять всех» (_NOT_WALKIN) — тот же класс потерь, что 14.09.
                onsite_kind=NULL,
                onsite_at=NULL,
                onsite_by=NULL
        ''', (
            data['telegram_id'],
            # UNAME-03 (квик 260911-0zu): канон записи "с @" -- единственная точка, где
            # сходятся ОБА пути анкеты (чат и Mini App, через services/reg_finalize.py).
            store_username(data.get('username')),
            data.get('full_name', ''),
            data.get('email', '-'),
            data.get('age'),
            data.get('is_aiesec_member', False),
            data.get('source', '-'),
            data.get('source_details'),
            data.get('education_status', '-'),
            data.get('university'),
            data.get('course'),
            data.get('specialty'),
            data.get('work_status', False),
            data.get('work_sphere'),
            data.get('missing_skills', '-'),
            data.get('expectations', '-'),
            data.get('phone'),
            data.get('city'),
            data.get('referrer_id'),
            data['registration_date'],
            data.get('is_ambassador_candidate', False),
            data.get('local_committee'),
            data.get('position'),
            data.get('attendance_format'),
            data.get('comments'),
            data.get('expectations_ar'),
            data.get('informal_day'),
            data.get('resume_file_id'),
            data.get('resume_text'),
            data.get('resume_url'),
            data.get('department'),
            data.get('aiesec_role'),
            data.get('needs_certificate'),
            data.get('english_level'),
            data.get('allergies'),
            data.get('food_pref'),
            data.get('arrival'),
            data.get('housing'),
            data.get('cc_shop'),
            data.get('exp_organizers'),
            data.get('exp_content'),
            data.get('volunteer'),
            data.get('payment_status') or 'not_paid',
            data.get('payment_option'),
            data.get('receipt_file_id'),
            data.get('payment_due'),
            data.get('paid_at'),
            data.get('arrival_date'),
            data.get('birth_date'),
            data.get('study_field'),
            data.get('goal'),
            data.get('formats'),
            data.get('vk_username'),
            data.get('transport'),
            data.get('payment_plan_date'),
            data.get('bed_sharing'),
            data.get('bed_partner'),
            data.get('participant_type', 'full'),
            data.get('alumni_status'),
            data.get('event_city'),
            data.get('season'),
            data.get('prev_season'),
            data.get('stack'),
            data.get('experience'),
            data.get('readiness'),
            data.get('resume_link'),
            data.get('mini_projects'),
            data.get('mini_portfolio'),
            data.get('mini_direction'),
            data.get('case_optin'),
        ))
        await db.commit()

async def get_user(telegram_id: int):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            # IN-01: only compute the abspath on the (rare) not-found logging path.
            logger.info(f"get_user: {telegram_id} not found in {os.path.abspath(config.DB_PATH)}")
            return None

def store_username(raw: str | None) -> str | None:
    """Канон ХРАНЕНИЯ username: "user" -> "@user", "@user" -> "@user", "@@user" -> "@user".
    Плейсхолдеры остаются как есть — None -> None, "" -> "", "-" -> "-", "@" -> "@". Не трогать
    их важно: прочерк/пусто в базе значит «юзернейма нет», а не «пользователь @-» — 30 прод-строк
    `users` уже хранят такие плейсхолдеры, и превращать их в «@-» нельзя (квик 260911-0zu)."""
    if raw is None or raw in ("", "-", "@"):
        return raw
    return "@" + raw.lstrip("@")


def username_needle(raw: str | None) -> str | None:
    """Ключ ПОИСКА: срезает пробелы и ведущие «@». Пусто/«-»/«@»/None -> None — вызывающий
    обязан вернуть «не найдено», НЕ делая запроса (иначе срезанный ltrim от username сравнялся
    бы с `''`/`'-'` — совпал бы с плейсхолдерными строками users, и /delete_user показал бы
    карточку случайного делегата, квик 260911-0zu, T-0zu-01/02)."""
    if raw is None:
        return None
    needle = raw.strip().lstrip("@")
    if not needle or needle == "-":
        return None
    return needle


async def get_user_by_username(username: str):
    needle = username_needle(username)
    if needle is None:
        return None
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE ltrim(username, '@') = ? COLLATE NOCASE", (needle,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def get_reg_started_by_username(username: str):
    """Зеркало `get_user_by_username`, но по `reg_started` — для человека, который нажал
    /start, но анкету не подал (правило «строка в users только на подачу», см. quick k4y).
    Тот же двусторонний ltrim/COLLATE NOCASE, что и `find_user_id_by_username` — терпимо
    и к формату ввода, и к формату хранения username в базе. Не проверяет, есть ли уже
    строка в `users` — это забота вызывающего (person_search дергает эту функцию только
    после промаха по `get_user_by_username`)."""
    needle = username_needle(username)
    if needle is None:
        return None
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reg_started WHERE ltrim(username, '@') = ? COLLATE NOCASE", (needle,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def get_reg_started_by_id(telegram_id: int):
    """Зеркало `get_reg_started_by_username`, но по telegram_id — нужен person_search для
    числовых запросов, чтобы найти человека, нажавшего /start, но не подавшего анкету, тем
    же способом, что и по username."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reg_started WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


def _escape_like(q: str) -> str:
    """`%`, `_` и сам `\\` во вводе — буквально, не подстановочно (ESCAPE '\\' в запросе)."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_search_text(s: str | None) -> str:
    """Регистронезависимость + ё/е: на стойке форума и в чате фамилию чаще всего набирают
    без «ё» («Королев» вместо «Королёв») — обе буквы сворачиваются в «е» ПОСЛЕ `.lower()`
    (у «Ё» тоже, `"Ё".lower() == "ё"`). Остальные символы не трогает. Общая для обеих сторон
    сравнения в `search_users_by_name` (колонка через SQL-функцию `py_lower`, и сам паттерн
    поиска) — иначе «королев» не находил бы «Королёв», а «Королёв» не находил бы «Королев»."""
    return (s or "").lower().replace("ё", "е")


async def search_users_by_name(
    q: str, limit: int = 20, *, city_scope=None, include_university: bool = False,
) -> list[dict]:
    """Phase 19 (19-07): поиск получателя монет по части имени (без учёта регистра, ё и е —
    один символ) для Mini App. `city_scope` — дескриптор `cities.city_scope(...)` (как у
    `get_pending_submissions`): привязанный менеджер видит только делегатов своего города.
    Отдаёт только опознавательный минимум — telegram_id, full_name, username, event_city;
    ПД (телефон, e-mail, вуз) сюда не попадают (T-19-47). Пустой запрос -> пусто.

    `include_university=False` (default) сохраняет старое поведение байт-в-байт — Mini App
    коин-пикер как искал без вуза, так и ищет. `include_university=True` (квик: общий сервис
    поиска person_search, различает тёзок вузом) добавляет колонку `university` в SELECT и
    в каждый словарь результата — вызывающий явно просит ПД, это не утечка по умолчанию."""
    needle = (q or "").strip()
    if not needle:
        return []
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    # SQLite COLLATE NOCASE сворачивает регистр только ASCII a-z/A-Z — кириллица (например,
    # «Иван» -> «иван») через неё не находится. Регистронезависимость и ё/е даём вручную
    # питоновским `_normalize_search_text` через пользовательскую SQL-функцию — на ОБЕИХ
    # сторонах сравнения (паттерн и колонка), иначе одна из сторон не свернётся.
    pattern = _normalize_search_text(f"%{_escape_like(needle)}%")
    columns = "telegram_id, full_name, username, event_city"
    if include_university:
        columns += ", university"
    async with _connect() as db:
        await db.create_function("py_lower", 1, _normalize_search_text)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {columns} FROM users "
            f"WHERE py_lower(full_name) LIKE ? ESCAPE '\\'{extra} "
            "ORDER BY py_lower(full_name), telegram_id LIMIT ?",
            (pattern, *city_params, max(1, int(limit))),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_referrals(telegram_id: int) -> list[str]:
    async with _connect() as db:
        async with db.execute(
            # IN-04: coalesce NULL full_name so the referral list never renders "• None".
            "SELECT COALESCE(NULLIF(full_name, ''), 'Без имени') FROM users WHERE referrer_id = ?",
            (telegram_id,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_all_users_ids():
    async with _connect() as db:
        async with db.execute('SELECT telegram_id FROM users') as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_all_users_dicts() -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users ORDER BY registration_date') as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_stats(*, city_scope: tuple | None = None):
    """`city_scope=None` (default) is byte-identical to the pre-Phase-15 query and result --
    this is the parity contract Phase 07.2's stats tests depend on (D-10 city-scoping must
    never touch the unscoped call)."""
    frag, city_params = _city_clause(city_scope)
    total_extra = f" WHERE {frag}" if frag else ""
    uni_extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        async with db.execute(f'SELECT COUNT(*) FROM users{total_extra}', tuple(city_params)) as cursor:
            total = (await cursor.fetchone())[0]

        async with db.execute(f'''
            SELECT university, COUNT(*) as cnt
            FROM users
            WHERE university IS NOT NULL AND TRIM(university) != '' AND university != '-'{uni_extra}
            GROUP BY university
            ORDER BY cnt DESC
            LIMIT 3
        ''', tuple(city_params)) as cursor:
            top_universities = await cursor.fetchall()

    return total, top_universities


# Phase 07.3 (A): season accessors. No side effects (no Sheets, no notifications, no coins) —
# callers (plans 02/04/05) own the ordering of "mark, then set_setting" and any fan-out.

async def count_current_season_users(old_season: str | None) -> int:
    """Delegates counted as "current season" for the «Новый сезон» confirmation screen:
    season IS NULL (never season-stamped) OR season == old_season (the season about to end).
    Rows with a DIFFERENT non-empty season are already past and must not be recounted."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE season IS NULL OR season = ?",
            (old_season,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def mark_season_ended(old_season: str | None) -> int:
    """Bulk-stamps every current-season row as past. CONTEXT A: if old_season is empty, the
    literal 'legacy' is used instead (there is no real season name to stamp rows with). Touches
    ONLY the season column — never bot_settings (the caller sets the new event_season), never
    status/coins/receipts/payment. Returns cursor.rowcount (affected row count)."""
    stamp = old_season if old_season else "legacy"
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE users SET season = ? WHERE season IS NULL OR season = ?",
            (stamp, old_season),
        )
        await db.commit()
        return cursor.rowcount


async def get_returning_count(*, city_scope: tuple | None = None) -> int:
    """Delegates with a non-empty prev_season — set only by a returning-delegate
    re-registration (plan 04), never by a fresh add_user of a new delegate.
    `city_scope=None` (default) is byte-identical to the pre-Phase-15 query/result."""
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM users WHERE prev_season IS NOT NULL AND TRIM(prev_season) != ''{extra}",
            tuple(city_params),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def reset_payment_for_new_season(telegram_id: int) -> None:
    """CONTEXT B: a new season means a new payment cycle. add_user deliberately never touches
    payment columns (WR-06), so a returning delegate's payment state must be reset explicitly
    by the caller (plan 04's finalize_registration) — this accessor is that single point."""
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET payment_status = 'not_paid', payment_option = NULL, "
            "receipt_file_id = NULL, payment_due = NULL, paid_at = NULL WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()


# Phase 07.3 (06, RET-04): import of a past event's forum.db. Columns a foreign file must
# never be allowed to seed — payment/coins/referral are explicitly out of scope (CONTEXT D),
# and season/prev_season are always set by THIS function's own `season` argument, never by a
# column value copied verbatim from the imported file.
IMPORT_EXCLUDED_COLUMNS = {
    "payment_status", "payment_option", "receipt_file_id", "payment_due", "paid_at",
    "referrer_id", "season", "prev_season",
}


async def count_existing_telegram_ids(ids: list[int]) -> int:
    """How many of `ids` already have a row in the LIVE users table — batched at 500 values per
    query (SQLite's default `SQLITE_MAX_VARIABLE_NUMBER`-safe chunk size for `IN (...)`)."""
    if not ids:
        return 0
    total = 0
    async with _connect() as db:
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            placeholders = ", ".join("?" for _ in batch)
            async with db.execute(
                f"SELECT COUNT(*) FROM users WHERE telegram_id IN ({placeholders})", batch
            ) as cursor:
                row = await cursor.fetchone()
                total += int(row[0]) if row and row[0] is not None else 0
    return total


async def bulk_insert_users_if_absent(rows: list[dict], season: str) -> int:
    """Inserts ONLY telegram_ids absent from the LIVE users table. `INSERT OR IGNORE` (not
    add_user's `ON CONFLICT DO UPDATE`) is the exact mechanism that guarantees an existing row
    is never touched in any column — a conflict on the PK is silently dropped.

    No side effects: no Sheets sync, no notifications, no coins, no mark_reg_started. That is
    the entire reason this isn't just add_user() called in a loop (07.3-PATTERNS.md).

    T-073-06-02: the column list written into the INSERT is the intersection of (a) the union
    of keys across all `rows`, minus IMPORT_EXCLUDED_COLUMNS, (b) columns that actually exist
    on the LIVE `users` table (via `_column_exists` — reused, not reinvented), and (c) the
    `^[A-Za-z_][A-Za-z0-9_]*$` identifier shape (`_IDENTIFIER_RE`, reused from `_assert_identifier`)
    as a second, independent barrier. A column name from a foreign file can never reach the SQL
    string unless it clears BOTH checks — values are always bound via `?` placeholders.
    """
    if not rows:
        return 0

    async with _connect() as db:
        candidate_cols: set[str] = set()
        for row in rows:
            candidate_cols.update(row.keys())
        candidate_cols -= IMPORT_EXCLUDED_COLUMNS

        columns: list[str] = []
        for col in candidate_cols:
            if not _IDENTIFIER_RE.fullmatch(col or ""):
                continue
            if await _column_exists(db, "users", col):
                columns.append(col)

        if "telegram_id" not in columns:
            # Nothing to match imported rows against — refuse rather than guess.
            return 0

        columns.append("season")
        col_list = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)

        values: list[tuple] = []
        for row in rows:
            raw_id = row.get("telegram_id")
            try:
                tg_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not tg_id:
                continue
            row_values = []
            for col in columns:
                if col == "season":
                    row_values.append(season)
                elif col == "telegram_id":
                    row_values.append(tg_id)
                else:
                    # CONTEXT D: fields map by matching column name; a column absent from
                    # THIS row (even though present in another row of the batch) is NULL.
                    row_values.append(row.get(col))
            values.append(tuple(row_values))

        if not values:
            return 0

        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            before = (await cursor.fetchone())[0]

        await db.executemany(
            f"INSERT OR IGNORE INTO users ({col_list}) VALUES ({placeholders})",
            values,
        )

        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            after = (await cursor.fetchone())[0]

        await db.commit()
        return after - before


async def get_monthly_registration_stats():
    async with _connect() as db:
        async with db.execute('''
            SELECT substr(registration_date, 1, 7) as month, COUNT(*) as cnt
            FROM users
            WHERE registration_date IS NOT NULL AND TRIM(registration_date) != ''
            GROUP BY month
            ORDER BY month DESC
        ''') as cursor:
            return await cursor.fetchall()

async def get_source_stats():
    async with _connect() as db:
        async with db.execute('''
            SELECT source, COUNT(*) as cnt
            FROM users
            WHERE source IS NOT NULL AND TRIM(source) != '' AND source != '-'
            GROUP BY source
            ORDER BY cnt DESC
        ''') as cursor:
            return await cursor.fetchall()


# Readable RU labels for the full CSV dump. Any column not listed falls back to its raw
# DB name, so a newly-added column still exports (just with its technical name).
CSV_HEADER_LABELS = {
    "telegram_id": "ID Telegram", "username": "Username", "full_name": "ФИО",
    "email": "Email", "age": "Возраст", "phone": "Телефон", "vk_username": "ВК",
    "is_aiesec_member": "Член АЙСЕК", "source": "Источник", "source_details": "Детали источника",
    "referrer_id": "ID реферера", "registration_date": "Дата регистрации",
    "education_status": "Образование", "university": "ВУЗ", "course": "Курс",
    "specialty": "Специальность", "study_field": "Направление обучения",
    "work_status": "Работает", "work_sphere": "Сфера работы", "missing_skills": "Не хватает навыков",
    "expectations": "Ожидания (общие)", "expectations_ar": "Ожидания (AR)",
    "exp_organizers": "Ожидания: организация", "exp_content": "Ожидания: контент",
    "comments": "Доп. комментарии", "city": "Город", "local_committee": "Локальный комитет",
    "position": "Позиция", "department": "Департамент", "aiesec_role": "Роль АЙСЕК",
    "needs_certificate": "Справка в ВУЗ", "english_level": "Английский",
    "alumni_status": "Аламни/айсекер",
    "attendance_format": "Формат участия", "informal_day": "Неформальный день",
    "goal": "Цель участия", "formats": "Форматы форума", "is_ambassador_candidate": "Амбассадор",
    "allergies": "Аллергии", "food_pref": "Питание", "arrival": "Приезд",
    "arrival_date": "Дата приезда", "birth_date": "Дата рождения", "housing": "Проживание",
    "transport": "Трансфер", "cc_shop": "CC-shop", "volunteer": "Волонтёр",
    "bed_sharing": "Общая кровать", "bed_partner": "Сосед по кровати",
    "status": "Статус заявки", "subscribed": "Подписан на канал",
    "resume_file_id": "Резюме (file_id)", "resume_text": "Резюме (текст)", "resume_url": "Резюме (ссылка)",
    "payment_status": "Статус оплаты", "payment_option": "Вариант оплаты",
    "receipt_file_id": "Чек (file_id)", "payment_due": "Срок оплаты", "paid_at": "Оплачено (когда)",
    "payment_plan_date": "Дата план. оплаты",
}


_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Neutralize CSV/Excel formula injection (CWE-1236): prefix a single quote to any
    STRING cell that begins with a formula trigger so spreadsheet apps treat it as text.
    Non-string cells (int/None) pass through unchanged. Export-side only — never mutates
    stored data.

    RESERVED FOR GENUINE CSV/EXCEL EXPORTS (export_users_csv, export_coins_journal_csv) —
    Excel/LibreOffice reopens a raw .csv file and DOES parse a leading =/+/-/@ as a formula.
    Do NOT call this for Google-Sheets row builders — see `_sheet_safe` below."""
    if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + value
    return value


def _sheet_safe(value):
    """Identity function — the Google-Sheets counterpart of `_csv_safe` above (находка
    08-sheets-dashboard, квик 260919). Every gspread write in services/sheets.py passes an
    EXPLICIT `value_input_option=RAW` (see that module's `_RAW` constant), and Google Sheets
    NEVER interprets a RAW cell as a formula/date/number — so a crafted cell like
    `=HYPERLINK(...)` is already inert on arrival, without prefixing a visible apostrophe.
    Prefixing one anyway (the pre-fix behaviour) corrupted real data instead: `'+79991234567`,
    `'@username`, `'-` — phones/usernames no longer matched by filter/ВПР in the sheet.

    Used by handlers/registration.py's *_sheet_row builders (active/incomplete/party/short) and
    by services/sheet_logs.py / services/polls.py's row builders — everywhere a row is destined
    for Sheets, never for a .csv file. Kept as a named no-op (not just removing the call) so the
    row builders stay self-documenting about WHY no neutralization happens here."""
    return value


async def export_users_csv(*, city_scope=None):
    """Full audit dump — every users column (incl. phone & service fields), with readable
    RU headers. Unmapped columns keep their raw name so new columns still export.
    `city_scope=None` (default) exports everything, byte-identical to before Phase 07.2."""
    frag, city_params = _city_clause(city_scope)
    where = f" WHERE {frag}" if frag else ""
    async with _connect() as db:
        async with db.execute(f'SELECT * FROM users{where}', tuple(city_params)) as cursor:
            raw = [description[0] for description in cursor.description]
            rows = await cursor.fetchall()
            headers = [CSV_HEADER_LABELS.get(h, h) for h in raw]
            rows = [tuple(_csv_safe(cell) for cell in row) for row in rows]
            return headers, rows


async def get_city_counts() -> list[tuple]:
    """One row per RAW `event_city` value present in `users` (including NULL and any
    unknown/garbage code) — `(event_city, total, pending, approved)`. Deliberately returns
    the raw column, never collapsed: db.py cannot import `cities` (import cycle — cities.py
    already imports database.db), so folding NULL/garbage into the default city is the
    CALLER's job via `cities.normalize_city`. The stats screen intentionally does NOT filter
    by the admin's selected city — it is a city-vs-city comparison, not a scoped view
    (07.2-CONTEXT.md decision)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT event_city, COUNT(*), "
            # D-41: walk-in без решения — не очередь менеджера (_NOT_WALKIN), его ждёт стойка.
            f"SUM(CASE WHEN status = 'pending' AND {_NOT_WALKIN} THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) "
            "FROM users GROUP BY event_city"
        ) as cursor:
            return await cursor.fetchall()


# ── Phase 1: coins ledger (append-only) ──────────────────────────────────────

async def add_coins(user_id: int, delta: int, reason: str | None = None, changed_by: int | None = None,
                     source: str | None = None, task_id: int | None = None):
    """Append a ledger row. Never UPDATE — balance is the derived SUM(delta).

    Phase 14 (GAME-09): `source` distinguishes a manual manager edit ('manual') from a
    task-award credit ('task') at the data level. Default None preserves every pre-existing
    call site's behavior byte-for-byte (NULL = legacy/system, per Pitfall 6 in 14-RESEARCH.md).

    Phase 32 (32-01, D-14): `task_id` — ссылка на задание этого начисления. Default None
    сохраняет поведение ВСЕХ существующих вызовов байт-в-байт; пишется только двумя точками
    начисления за задание (grev_approve/grev_approve_amount_step, план 32-05)."""
    timestamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, delta, reason, changed_by, timestamp, source, task_id),
        )
        await db.commit()


async def get_balance(user_id: int) -> int:
    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM coins WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def get_leaderboard(limit: int = 10) -> list[dict]:
    """Top users by summed balance, joined to users for display name."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT c.user_id AS user_id,
                   COALESCE(SUM(c.delta), 0) AS balance,
                   u.full_name AS full_name,
                   u.username AS username
            FROM coins c
            LEFT JOIN users u ON u.telegram_id = c.user_id
            GROUP BY c.user_id
            ORDER BY balance DESC
            LIMIT ?
        ''', (limit,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_user_rank(user_id: int) -> int | None:
    """1-based rank by summed balance; None if the user has no ledger rows."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM coins WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return None
            my_balance = int(row[0])
        async with db.execute('''
            SELECT COUNT(*) FROM (
                SELECT user_id, SUM(delta) AS bal
                FROM coins
                GROUP BY user_id
                HAVING bal > ?
            )
        ''', (my_balance,)) as cursor:
            greater = (await cursor.fetchone())[0]
        # confirm the user actually has rows in the ledger
        async with db.execute(
            "SELECT 1 FROM coins WHERE user_id = ? LIMIT 1", (user_id,)
        ) as cursor:
            if await cursor.fetchone() is None:
                return None
        return greater + 1


# ── Phase 14 (14-05, GAME-09): «📜 Журнал монет» — manual-ops screen + full CSV export ──────
#
# `source = 'manual'` is the ONLY filter that decides what lands on the SCREEN (never a
# text-prefix match on `reason` -- Pitfall 6, closed by 14-04's `coins.source` column). `source = 'task'`
# and `source IS NULL` (legacy, pre-Phase-14 rows) never appear on the paginated screen; the
# CSV export is unfiltered and labels every source human-readably instead.

_COIN_JOURNAL_SELECT = (
    "SELECT c.*, u.full_name AS user_full_name, u.username AS user_username, "
    "u.event_city AS user_event_city "
    "FROM coins c LEFT JOIN users u ON u.telegram_id = c.user_id"
)


async def list_manual_coin_entries(limit: int = 10, offset: int = 0) -> list[dict]:
    """Paginated «📜 Журнал монет» screen feed -- same LIMIT/OFFSET + LEFT JOIN shape as
    `get_pending_submissions` (CLAUDE.md: 1000+ rows must never render in one message).
    ONLY `source = 'manual'` rows -- task-award credits and pre-Phase-14 legacy rows are
    deliberately excluded from the screen (they still show up in the CSV export below)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"{_COIN_JOURNAL_SELECT} WHERE c.source = 'manual' ORDER BY c.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def count_manual_coin_entries() -> int:
    """Same WHERE as `list_manual_coin_entries` -- drives the «Страница K из N» label."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM coins WHERE source = 'manual'"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


_COIN_SOURCE_CSV_LABELS = {"manual": "Вручную", "task": "За задание", None: "До обновления"}


async def export_coins_journal_csv() -> tuple[list[str], list[tuple]]:
    """Full журнал dump -- ALL rows regardless of `source` (manual + task + legacy NULL),
    unlike the paginated screen above. `_csv_safe` on every cell (T-14-23, CWE-1236); the raw
    `source` code is never written to the file -- only its RU label via
    `_COIN_SOURCE_CSV_LABELS` (same principle as `_PAYMENT_STATUS_LABELS`)."""
    headers = [
        "ID", "Когда", "Кому (ID)", "Кому (ФИО)", "Юзернейм", "Город", "Сколько", "Тип",
        "Причина", "Кто изменил (ID)",
    ]
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"{_COIN_JOURNAL_SELECT} ORDER BY c.id DESC") as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
    out_rows = []
    for row in rows:
        type_label = _COIN_SOURCE_CSV_LABELS.get(row.get("source"), str(row.get("source")))
        out_rows.append(tuple(_csv_safe(cell) for cell in (
            row.get("id"), row.get("timestamp"), row.get("user_id"), row.get("user_full_name"),
            row.get("user_username"), row.get("user_event_city"), row.get("delta"), type_label,
            row.get("reason"), row.get("changed_by"),
        )))
    return headers, out_rows


# Phase 16 (16-01, GAME-UI-01): per-user paginated coin history — «🪙 Баланс» screen's «📜
# История». Unlike `list_manual_coin_entries` (filters `source = 'manual'` GLOBALLY, for the
# manager's journal), these scope to ONE user_id and include ALL sources (manual/task/legacy
# NULL) — a delegate's own history must show task-award credits too, not just manual edits.

async def list_coin_entries_for_user(user_id: int, limit: int = 5, offset: int = 0) -> list[dict]:
    """Newest-first (`ORDER BY id DESC`), same LIMIT/OFFSET idiom as `list_manual_coin_entries`."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM coins WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def count_coin_entries_for_user(user_id: int) -> int:
    """Total row count for one user_id — drives the «📜 История» screen's «Страница K из N»."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM coins WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


# ── Phase 1: reg_started dropout tracking (independent of FSM) ────────────────

async def mark_reg_started(
    telegram_id: int,
    username: str | None,
    participant_type: str | None = None,
    event_city: str | None = None,
):
    started_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    # UNAME-04 (квик 260911-0zu): канон "с @" -- 2149 прод-строк users уже лежат с собакой,
    # менять их формат без миграции нельзя (миграция вне скоупа квика), поэтому к ним
    # подтягивается reg_started, а не наоборот. Старые 947 строк reg_started без собаки НЕ
    # мигрируем -- их находит двусторонний поиск find_user_id_by_username (ltrim по username).
    username = store_username(username)
    async with _connect() as db:
        await db.execute('''
            INSERT INTO reg_started (telegram_id, username, started_at, participant_type, event_city)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                -- MD-03: do NOT reset started_at on re-entry. A mid-flow user re-sending /start
                -- (or advancing a step) must keep the ORIGINAL start time — otherwise every
                -- restart pushes started_at forward, the nudge cutoff is never crossed, and the
                -- dropout nudge is deferred indefinitely. A genuinely new attempt after
                -- completion is a fresh INSERT (the row was cleared), so it gets a fresh time.
                participant_type=COALESCE(excluded.participant_type, reg_started.participant_type),
                -- Phase 07.1 (CITY-01): same COALESCE semantics as participant_type — a bare
                -- repeat /start with no city arg must NOT clear an already-chosen city.
                event_city=COALESCE(excluded.event_city, reg_started.event_city)
        ''', (telegram_id, username, started_at, participant_type, event_city))
        await db.commit()


async def clear_reg_started(telegram_id: int):
    async with _connect() as db:
        # Язык, выбранный до появления строки users (set_user_lang), не должен пропасть вместе
        # с reg_started — иначе после подачи анкеты делегата снова спросят язык.
        await db.execute(
            """
            UPDATE users SET lang = (SELECT lang FROM reg_started WHERE telegram_id = ?)
            WHERE telegram_id = ? AND (lang IS NULL OR lang = '')
              AND EXISTS (SELECT 1 FROM reg_started WHERE telegram_id = ? AND lang IS NOT NULL)
            """,
            (telegram_id, telegram_id, telegram_id),
        )
        await db.execute("DELETE FROM reg_started WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


# ── Phase 21 (FORM-SYNC-02, D-19/D-21): reg_drafts — shared "questionnaire in flight" ────────
# The bot's FSM lives in MemoryStorage (one process, lost on restart); the Mini App is a
# separate process that never sees it at all. reg_drafts is the ONE table both write to on
# every answer, so either surface can pick up where the other left off. Conflicts are
# resolved per-field, last-write-wins, via `meta["field_versions"][column]` (D-19) — no locks.
# Double-submit (bot finalize race with Mini App submit) is closed by claim_reg_draft's atomic
# `WHERE submitting_at IS NULL` (same idiom as claim_submission, database/db.py:2745).

def _row_to_reg_draft(row: dict) -> dict:
    d = dict(row)
    d["answers"] = json.loads(d["answers"]) if d.get("answers") else {}
    d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
    return d


async def get_reg_draft(telegram_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reg_drafts WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_reg_draft(dict(row)) if row else None


async def upsert_reg_draft(
    telegram_id: int,
    *,
    kind: str,
    participant_type: str | None = None,
    event_city: str | None = None,
    step: str | None = None,
    patch: dict | None = None,
    meta_patch: dict | None = None,
    source: str,
    active_surface: str | None = None,
) -> int:
    """One short transaction: SELECT the current row (if any), merge `patch` into `answers`
    in Python, bump `version`, stamp `meta["field_versions"][col] = new_version` for every
    column present in `patch` (D-19 — LWW is per-field, not per-row), then INSERT or UPDATE.
    Keys with a leading underscore in `patch` are client-side scratch (e.g. `_client_ts`) and
    are filtered out before they ever touch `answers`. `source` is who is writing right now
    ('bot' | 'miniapp') — stored as `updated_by` so the OTHER surface can show "обновлено в
    чате/приложении" on refetch. Returns the new version.

    `active_surface` (quick 260904-3vm, эстафета) — explicit ownership stamp. On UPDATE it is
    written with COALESCE so a caller that does not pass it (the common per-step write) never
    silently steals ownership from whoever holds the draft (see `_sync_draft_out` comment in
    `handlers/registration.py` for why that matters). On INSERT — whoever creates the row owns
    it: `active_surface or ("app" if source == "miniapp" else "bot")`."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    clean_patch = {k: v for k, v in (patch or {}).items() if not k.startswith("_")}

    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT answers, meta, version FROM reg_drafts WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            existing = await cursor.fetchone()

        if existing:
            answers = json.loads(existing["answers"]) if existing["answers"] else {}
            meta = json.loads(existing["meta"]) if existing["meta"] else {}
            new_version = int(existing["version"]) + 1
        else:
            answers = {}
            meta = {}
            new_version = 1

        answers.update(clean_patch)
        field_versions = meta.setdefault("field_versions", {})
        for col in clean_patch:
            field_versions[col] = new_version
        if meta_patch:
            meta.update({k: v for k, v in meta_patch.items() if not k.startswith("_")})

        answers_json = json.dumps(answers, ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False)

        if existing:
            await db.execute(
                "UPDATE reg_drafts SET kind = ?, participant_type = COALESCE(?, participant_type), "
                "event_city = COALESCE(?, event_city), answers = ?, meta = ?, "
                "step = COALESCE(?, step), version = ?, updated_by = ?, updated_at = ?, "
                "active_surface = COALESCE(?, active_surface) "
                "WHERE telegram_id = ?",
                (kind, participant_type, event_city, answers_json, meta_json, step,
                 new_version, source, now, active_surface, telegram_id),
            )
        else:
            surface = active_surface or ("app" if source == "miniapp" else "bot")
            await db.execute(
                "INSERT INTO reg_drafts (telegram_id, kind, participant_type, event_city, "
                "answers, meta, step, version, updated_by, updated_at, created_at, submitting_at, "
                "active_surface) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (telegram_id, kind, participant_type, event_city, answers_json, meta_json,
                 step, new_version, source, now, now, surface),
            )
        await db.commit()
        # Log step/version only — never the answers payload (T-21-08, PII).
        logger.info("reg_draft upsert telegram_id=%s step=%s version=%s", telegram_id, step, new_version)
        return new_version


async def set_reg_draft_surface(telegram_id: int, surface: str) -> None:
    """Меняет ТОЛЬКО владение (quick 260904-3vm) — `version`/`answers`/`updated_at` не трогает,
    т.к. это не правка анкеты, а передача владения между поверхностями. Идемпотентно:
    отсутствующая строка — no-op (черновика ещё нет, отбирать нечего)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE reg_drafts SET active_surface = ? WHERE telegram_id = ?",
            (surface, telegram_id),
        )
        await db.commit()
        logger.info("reg_draft surface telegram_id=%s surface=%s", telegram_id, surface)


async def claim_reg_draft(telegram_id: int) -> dict | None:
    """Atomic single-row claim (same idiom as claim_submission, database/db.py:2745): the
    caller that flips `submitting_at` from NULL wins (rowcount == 1) and gets the draft row
    back; a concurrent second finalize call (bot vs Mini App, T-21-02) gets None and must
    tell the user "уже отправляется"."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE reg_drafts SET submitting_at = ? WHERE telegram_id = ? AND submitting_at IS NULL",
            (now, telegram_id),
        )
        await db.commit()
        if cursor.rowcount != 1:
            return None
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reg_drafts WHERE telegram_id = ?", (telegram_id,)
        ) as sel:
            row = await sel.fetchone()
            return _row_to_reg_draft(dict(row)) if row else None


async def release_reg_draft(telegram_id: int) -> None:
    """Undo a claim after a failed finalize, so the delegate can retry (D-19)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE reg_drafts SET submitting_at = NULL WHERE telegram_id = ?", (telegram_id,)
        )
        await db.commit()


async def delete_reg_draft(telegram_id: int) -> None:
    """Drop the draft after a successful finalize. Idempotent — a repeat call on an already
    gone row is a no-op, not an error."""
    async with _connect() as db:
        await db.execute("DELETE FROM reg_drafts WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def touch_reg_draft_activity(telegram_id: int) -> None:
    """Bump `updated_at` WITHOUT touching `version`/`answers`/`meta` — a pure activity
    heartbeat. Called every time the Mini App writes a step/answer so the delegate stops
    looking abandoned to the dropout-nudge scan while they are actively answering there
    (D-21: reg_started itself is never touched by this). No-op if the draft is already gone."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "UPDATE reg_drafts SET updated_at = ? WHERE telegram_id = ?", (now, telegram_id)
        )
        await db.commit()


# Phase 33 (delegate-card admin actions, перевод города): точечная правка ТОЛЬКО event_city
# открытого черновика — НЕ через upsert_reg_draft (та функция мержит patch в answers/bumps
# version/переносит владение surface, это для делегатской правки шага анкеты, не для админской
# смены города под капотом). Без этого правка reg_finalize.py:296 при финализации открытого
# черновика вернула бы делегату СТАРЫЙ город (33-SEED.md, «Зависимости города»). Не трогает
# version/answers/meta/updated_at — city move не запись делегата.
async def update_reg_draft_city(telegram_id: int, new_city: str | None) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE reg_drafts SET event_city = ? WHERE telegram_id = ?", (new_city, telegram_id)
        )
        await db.commit()
        return cursor.rowcount > 0


# ── Phase 21 (FORM-SYNC-04, D-12/D-13/D-15): narrow answer edit + history ────────────────────
# add_user (605+) is an ON CONFLICT DO UPDATE over ~60 columns — using it for an edit would
# silently overwrite registration_date/referrer_id/source/status/payment_* with whatever the
# edit-form patch happens NOT to include (Pitfall 2/3). update_user_answers instead builds a
# plain `UPDATE users SET col = ?, ...` over ONLY the intersection of `patch` and the caller's
# `allowed_columns` (the caller passes reg_engine.answer_columns(), which never contains
# attribution/status columns — T-21-19) — every other column is left exactly as it was.

async def update_user_answers(telegram_id: int, patch: dict, *, allowed_columns) -> int:
    """Narrow UPDATE over the intersection of `patch` and `allowed_columns`. A patch key
    outside the allowlist is silently ignored (logged at warning, not raised — a stray extra
    key from a client is not the caller's problem to crash on). Returns the number of columns
    actually written; an empty intersection sends no SQL at all."""
    allowed = set(allowed_columns)
    cols = [c for c in patch if c in allowed]
    ignored = [c for c in patch if c not in allowed]
    if ignored:
        logger.warning("update_user_answers: ignoring columns outside allowlist: %s", ignored)
    if not cols:
        return 0
    for c in cols:
        _assert_identifier(c)
    set_clause = ", ".join(f"{c} = ?" for c in cols)
    params = [patch[c] for c in cols] + [telegram_id]
    async with _connect() as db:
        await db.execute(f"UPDATE users SET {set_clause} WHERE telegram_id = ?", params)
        await db.commit()
    return len(cols)


async def record_answer_history(
    telegram_id: int, changes: list[dict], source: str, season: str | None = None
) -> None:
    """Append one edit-trail row (`changes`: list of {"column","old","new"}). No-op on an
    empty `changes` list — a diff with nothing in it is not an edit worth remembering.

    Quick 260906-52m: `changed_at` хранится в UTC (`datetime.utcnow()`), формат строки
    `"%Y-%m-%d %H:%M:%S"` НЕ менялся — его разбирают и `services/questions.py::_parse_stamp`,
    и `services/applications.py::format_edited_date`. Показ переводит метку в МСК на всех трёх
    экранах (`services/sheet_logs.py`, `services/applications.py::_history_entry`,
    `handlers/admin_moderation.py::appr_history`). Соседняя `mark_user_edited` (`edited_at`)
    квиком 260912-mcj переведена на московский `msk_now()` (раньше писала локальное время
    контейнера) — это по-прежнему РАЗНЫЕ семьи: `changed_at` остаётся UTC и переводится на
    показе, `edited_at` теперь пишется уже московским и переводить его больше не нужно."""
    if not changes:
        return
    changed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "INSERT INTO reg_answer_history (telegram_id, changed_at, source, season, changes) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, changed_at, source, season, json.dumps(changes, ensure_ascii=False)),
        )
        await db.commit()
    # Quick 260902-vth: одна врезка покрывает ОБА источника правки (бот и Mini App), потому
    # что через record_answer_history проходят оба, — не трогаем handlers/registration.py и
    # miniapp/. Ленивый импорт: db.py — нижний слой, не тянет services на импорте модуля.
    # Fail-soft: сбой планирования фоновой синхронизации не должен ронять запись правки.
    try:
        from services.sheet_logs import schedule_sheet_logs_sync
        schedule_sheet_logs_sync()
    except Exception as e:
        logger.warning("sheet_logs autosync scheduling after record_answer_history failed: %s", e)


async def get_answer_history(telegram_id: int, limit: int = 5) -> list[dict]:
    """Newest-first edit trail for the «🕓 История» button (plan 21-07). `changes` is
    unpacked from JSON into a list of dicts — never printed as a raw string."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reg_answer_history WHERE telegram_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["changes"] = json.loads(d["changes"]) if d.get("changes") else []
        result.append(d)
    return result


async def list_answer_history(limit: int = 5000) -> list[dict]:
    """Quick 260902-vth: весь журнал правок (не по одному делегату, как `get_answer_history`)
    для полной пересборки листа «История правок» — лист пересобирается целиком, поэтому нужен
    весь журнал, а не хвост. `limit` — предохранитель от бесконечной выгрузки, не
    постраничность экрана."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reg_answer_history ORDER BY id ASC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["changes"] = json.loads(d["changes"]) if d.get("changes") else []
        result.append(d)
    return result


async def mark_user_edited(telegram_id: int, source: str) -> None:
    """Stamp users.edited_at/edited_source ('bot' | 'miniapp') — the «✏️ Изменена» flag on the
    moderation card (D-12). Repeat calls simply advance edited_at to the latest edit time."""
    edited_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET edited_at = ?, edited_source = ? WHERE telegram_id = ?",
            (edited_at, source, telegram_id),
        )
        await db.commit()


# ── Phase 15 (STAT-03, D-06): append-only registration-funnel event log ──────
# Deliberately separate from reg_started above -- reg_started is a keyed UPSERT (one row per
# telegram_id, overwritten on every re-entry), so the moment a delegate re-/start's or the
# recovery flow re-fires, the ORIGINAL "when did the funnel start" fact is gone. This table
# never overwrites: every call is a new row, so the dashboard's top-of-funnel counts survive
# any number of re-entries per person.
REG_EVENT_KINDS = ("start", "form_started", "form_completed")
# Не ступени воронки, а служебные отметки в том же append-only журнале. `nudged` — напоминалка о
# брошенной анкете реально доставлена: `reg_started.nudged_at` стирается при подаче
# (`clear_reg_started`), а по этой строке «догнали напоминалкой → потом подал» считается и после.
# Воронка/KPI дашборда фильтруют по конкретному `event`, поэтому сюда их не подмешать.
REG_EVENT_SIDE_KINDS = ("nudged",)


async def record_reg_event(
    telegram_id: int,
    event: str,
    *,
    event_city: str | None = None,
    season: str | None = None,
    source_tag: str | None = None,
) -> None:
    """Append-only funnel write (D-06). `ts` uses the SAME format as mark_reg_started's
    started_at (not .isoformat()) so the dashboard's daily grouping via substr(ts, 1, 10)
    needs no parsing. `event` outside REG_EVENT_KINDS is still written -- a caller's typo must
    never silently drop a funnel row -- but logged at WARNING so it doesn't go unnoticed.
    `source_tag` (квик 260905-qqg) — метка кампании из deep-link `src_<метка>`; NULL у
    органики и у ручного ответа на вопрос «Источник»."""
    if event not in REG_EVENT_KINDS and event not in REG_EVENT_SIDE_KINDS:
        logger.warning(
            "record_reg_event: unexpected event kind %r for telegram_id=%s", event, telegram_id
        )
    ts = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "INSERT INTO reg_events (telegram_id, event, event_city, season, ts, source_tag) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, event, event_city, season, ts, source_tag),
        )
        await db.commit()


async def get_last_reg_event_city(telegram_id: int, season: str | None = None) -> str | None:
    """Квик 27.09: последний НЕпустой город этого делегата в воронке (`reg_events`) — одно из
    звеньев цепочки «что уже известно о городе» (`services.known_city`). `season` задан —
    только записи этого сезона: город прошлого сезона не должен молча переехать в новый."""
    query = (
        "SELECT event_city FROM reg_events WHERE telegram_id = ? "
        "AND event_city IS NOT NULL AND event_city != ''"
    )
    params: tuple = (telegram_id,)
    if season:
        query += " AND season = ?"
        params = (telegram_id, season)
    query += " ORDER BY id DESC LIMIT 1"
    async with _connect() as db:
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def backfill_reg_event_city(telegram_id: int, event_city: str) -> None:
    """Дозаполняет город в уже записанной строке `start` этого пользователя (D-06 продолжение).

    `record_reg_event(..., "start", ...)` пишется в самом верху `/start`, ДО того, как деп-линк
    успевает подсказать город делегату без городского аргумента -- в этот момент город физически
    ещё не может быть известен. Без этой функции такой делегат навсегда теряет свою строку
    `start` для городской воронки (`event_city IS NULL`), хотя город становится известен уже на
    следующем шаге (`form_started`, где город так или иначе выбран/восстановлен). Условие
    `event_city IS NULL` в WHERE делает вызов идемпотентным и не даёт затереть город, уже
    пришедший из deep-link на этапе `cmd_start` -- backfill только ДОПОЛНЯЕТ пустые строки,
    никогда не перезаписывает заполненные.
    """
    if not event_city:
        return
    async with _connect() as db:
        await db.execute(
            "UPDATE reg_events SET event_city = ? "
            "WHERE telegram_id = ? AND event = 'start' AND event_city IS NULL",
            (event_city, telegram_id),
        )
        await db.commit()


def _reg_started_cutoff(max_age_hours: int | None) -> str | None:
    """Нижняя граница `started_at` для восстановления брошенной анкеты, в том же формате и по
    тем же часам, которыми `mark_reg_started` эту колонку пишет (`msk_now()`, московское время
    — квик 260912-mcj, раньше были часы контейнера). Обе стороны сравнения теперь одинаково
    московские. Специально НЕ `datetime('now')` на стороне SQLite: тот считает в UTC, и
    на сервере в любой не-UTC зоне отсечка уехала бы на несколько часов. `None`/непозитивное
    значение — «без ограничения», прежнее поведение."""
    if not max_age_hours or max_age_hours <= 0:
        return None
    return (msk_now() - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")


# Phase 5 (D-02): read the track recorded at flow start, before finalize_registration clears
# the reg_started row — the source of truth for a bare repeat /start mid-flow.
#
# Quick 260820-rms: `max_age_hours` — окно, в котором строка ещё считается «той же самой
# анкетой». Строка `reg_started` живёт до конца регистрации и никем не чистится (её читают
# «Незавершённые» и dropout-напоминания), поэтому без окна возврат делегата через две недели
# молча наследовал старый трек и старый город — экран выбора города при этом не показывался
# вовсе (`registration._should_show_city_fork` выходит на непустом городе).
async def get_reg_started_track(telegram_id: int, max_age_hours: int | None = None) -> str | None:
    cutoff = _reg_started_cutoff(max_age_hours)
    async with _connect() as db:
        if cutoff is None:
            query, params = (
                "SELECT participant_type FROM reg_started WHERE telegram_id = ?",
                (telegram_id,),
            )
        else:
            query, params = (
                "SELECT participant_type FROM reg_started WHERE telegram_id = ? AND started_at >= ?",
                (telegram_id, cutoff),
            )
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# Phase 07.1 (CITY-01): read the event_city recorded at flow start — same read pattern as
# get_reg_started_track, for restoring an in-progress registration's city choice.
async def get_reg_started_city(telegram_id: int, max_age_hours: int | None = None) -> str | None:
    cutoff = _reg_started_cutoff(max_age_hours)
    async with _connect() as db:
        if cutoff is None:
            query, params = (
                "SELECT event_city FROM reg_started WHERE telegram_id = ?",
                (telegram_id,),
            )
        else:
            query, params = (
                "SELECT event_city FROM reg_started WHERE telegram_id = ? AND started_at >= ?",
                (telegram_id, cutoff),
            )
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# Phase 33 (delegate-card admin actions, перевод города): точечная правка `event_city` строки
# dropout-учёта — БЕЗ окна `max_age_hours` (в отличие от чтения выше, правим ЛЮБУЮ живую
# строку этого telegram_id, свежую или старую: «Незавершённые» и dropout-напоминание должны
# сразу показать новый город, а не подождать, пока делегат вернётся). No-op (False), если
# строки нет вовсе — city move не заводит reg_started, только правит существующую.
async def update_reg_started_city(telegram_id: int, new_city: str | None) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE reg_started SET event_city = ? WHERE telegram_id = ?", (new_city, telegram_id)
        )
        await db.commit()
        return cursor.rowcount > 0


# Phase 7 (07-04, SHORT-06): is there a live abandoned short-track registration right now?
# Used to gate the «Незавершённые» column merge in handlers.registration.incomplete_sheet_headers
# on the STATE of reg_started rows rather than on the live registration_mode setting — so a
# manager reverting the toggle on 2026-08-11 does not make the next 2h auto-sync
# (services/scheduler.py sync_incomplete_sheet_job) collapse already-answered promo fields
# back to "-" before the last abandoned promo delegate is cleared or finishes.
async def has_short_incomplete() -> bool:
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM reg_started WHERE participant_type = 'short' LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row)


# MD-02: a reg_started row is DELETEd on completion (finalize_registration), but that clear is
# fail-soft — a DB hiccup can leave a finished user in reg_started, where the dropout nudge,
# «Незавершённые» sheet, and broadcast segment would then wrongly treat them as a dropout.
# Defensively exclude anyone who already holds a NON-rejected users row (genuinely registered).
# Rejected users are KEPT: D-05a lets them fall through to re-register, so a reg_started row for
# a rejected user is a real in-progress attempt.
_INCOMPLETE_NOT_REGISTERED = (
    "NOT EXISTS (SELECT 1 FROM users u WHERE u.telegram_id = reg_started.telegram_id "
    "AND (u.status IS NULL OR u.status != 'rejected'))"
)


async def get_incomplete_user_ids() -> list[int]:
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM reg_started WHERE {_INCOMPLETE_NOT_REGISTERED}"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def reg_started_only_ids_in_scope(scope) -> set[int]:
    """Начавшие регистрацию, которых ещё нет в `users`, — в границах `cities.city_scope(...)`
    по `reg_started.event_city` (город, выбранный в начале анкеты). Нужно менеджеру города:
    сегмент «📝 Не завершили регистрацию» по `users` не сузить — этих людей там нет.

    При сужении по городу человек без записанного города (нажал /start, до вопроса о городе
    не дошёл) не берётся: `_city_clause` города по умолчанию взял бы `IS NULL`, и менеджеру
    Москвы ушли бы будущие СПб и Тюмень. Такие уходят в «отсеяно» на экране подтверждения."""
    city_frag, params = _city_clause(scope, "event_city")
    where = "telegram_id NOT IN (SELECT telegram_id FROM users)"
    if city_frag:
        where += f" AND event_city IS NOT NULL AND TRIM(event_city) != '' AND {city_frag}"
    async with _connect() as db:
        async with db.execute(f"SELECT telegram_id FROM reg_started WHERE {where}", params) as cursor:
            return {int(row[0]) for row in await cursor.fetchall()}


async def get_incomplete_rows() -> list[tuple]:
    """Full dropout rows for the «Незавершённые» sheet tab: (telegram_id, username,
    started_at, last_step, partial_data). These users hit /start but never finished.
    Quick k4y: partial_data (JSON snapshot of already-answered fields) is now persisted
    alongside last_step — it is NULL for rows created before that column existed."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id, username, started_at, last_step, partial_data FROM reg_started "
            f"WHERE {_INCOMPLETE_NOT_REGISTERED} ORDER BY started_at"
        ) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


async def get_incomplete_rows_with_city() -> list[tuple]:
    """Same rows, filter, and ORDER BY as get_incomplete_rows, plus a sixth field
    (event_city) so the «Незавершённые» tab can be split per city (Phase 07.1, CITY-04).
    get_incomplete_rows() itself is UNTOUCHED -- existing tests rely on its 5-tuple shape."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id, username, started_at, last_step, partial_data, event_city "
            f"FROM reg_started WHERE {_INCOMPLETE_NOT_REGISTERED} ORDER BY started_at"
        ) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


async def set_reg_step(telegram_id: int, step_key: str, partial_json: str | None = None):
    """Stamp the question currently shown to a mid-registration user (dropout analytics),
    and optionally persist a JSON snapshot of already-answered fields (quick k4y). No-op
    if the reg_started row is gone (finished/cleared). Fail-soft at the call site.
    COALESCE keeps any previously-stored partial_data intact when called without a
    snapshot (e.g. the very first question) — it must never be reset to NULL."""
    async with _connect() as db:
        await db.execute(
            "UPDATE reg_started SET last_step = ?, partial_data = COALESCE(?, partial_data) "
            "WHERE telegram_id = ?",
            (step_key, partial_json, telegram_id),
        )
        await db.commit()


async def get_dropout_step_stats() -> list[tuple]:
    """(last_step, count) over all incomplete registrations, most-abandoned first. last_step
    may be NULL for users who dropped before seeing any question."""
    async with _connect() as db:
        async with db.execute(
            "SELECT last_step, COUNT(*) FROM reg_started "
            f"WHERE {_INCOMPLETE_NOT_REGISTERED} GROUP BY last_step ORDER BY COUNT(*) DESC"
        ) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


# ── Phase 1: subscription flag ───────────────────────────────────────────────

async def set_user_subscribed(telegram_id: int, subscribed: bool):
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET subscribed = ? WHERE telegram_id = ?",
            (1 if subscribed else 0, telegram_id),
        )
        await db.commit()


async def get_non_subscriber_ids() -> list[int]:
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM users WHERE subscribed = 0"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ── Phase 2: approval flow ───────────────────────────────────────────────────

async def set_user_status(telegram_id: int, status: str):
    """Set one user's approval status. Used after add_user to land pending/approved."""
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET status = ? WHERE telegram_id = ?",
            (status, telegram_id),
        )
        await db.commit()


async def approve_user_atomic(telegram_id: int) -> bool:
    """Atomically approve one pending user. True iff this call flipped the row
    (rowcount==1) — a concurrent second approve returns False (no double approval).
    approved_at (D-10, Phase 23.1-05) is stamped in the SAME UPDATE — this is the shared seam
    for both the bot's single-approve (appr_approve) and the web's single-approve
    (services.applications.claim_approve), so a second timestamp write is never needed."""
    approved_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE users SET status = 'approved', approved_at = ? "
            "WHERE telegram_id = ? AND status = 'pending'",
            (approved_at, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def reject_user(telegram_id: int) -> bool:
    """Atomically reject one pending user. True iff one row flipped.
    rejected_at (Phase 30, 30-05 задача 3) is stamped in the SAME UPDATE — the shared seam
    for both the bot's single-reject (appr_reject_reason) and the web's single-reject
    (services.applications.claim_reject), same discipline as approve_user_atomic/approved_at."""
    rejected_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE users SET status = 'rejected', rejected_at = ? "
            "WHERE telegram_id = ? AND status = 'pending'",
            (rejected_at, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def record_decision_delivery(telegram_id: int, decision: str, status: str,
                                    error: str | None = None) -> None:
    """Пишет `users.decision_delivery_*` (координатор 25.09, учёт доставки решения) —
    единственная точка записи, зовётся из `services.application_effects.apply_decision_effects`/
    `mass_approve_effects` ПОСЛЕ попытки отправить письмо о решении. `status` —
    'delivered' | 'failed' | 'queued' (тихие часы — попытка ещё не случилась, но факт «решение
    ждёт» уже стоит отдельно от «неизвестно»/pre-migration NULL). `error` — уже готовая
    человеческая строка причины (классификация — на вызывающей стороне,
    `_classify_decision_delivery_error`), NULL для delivered/queued.

    БЕЗ WHERE status-условия (в отличие от approve_user_atomic/reject_user выше) — это не
    первичный флип решения, а его следствие: к моменту вызова решение уже применено, гонка
    здесь только «кто последний записал факт доставки», а не «кто выиграл решение»."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET decision_delivery_status = ?, decision_delivery_decision = ?, "
            "decision_delivery_at = ?, decision_delivery_error = ? WHERE telegram_id = ?",
            (status, decision, now, error, telegram_id),
        )
        await db.commit()


# ── Phase 07.2 Plan 01 (CITY-02): city scope clause builder ──────────────────
#
# `cities.py` imports `database.db` (registry accessors need `get_setting`/`set_setting`),
# so `database/db.py` must NEVER import `cities` — that would be an import cycle. The
# registry's knowledge (which codes exist, which is the default) is therefore handed to
# `_city_clause` BY VALUE as the `(code, exclude)` descriptor `cities.city_scope` builds;
# db.py stays the bottom layer and never learns what a "city" is.
def _city_clause(scope: tuple[str, tuple[str, ...]] | None, column: str = "event_city", *,
                  include_null: bool = False) -> tuple[str, list]:
    """Pure: turn a `cities.city_scope(...)` descriptor into a parameterized SQL fragment
    (no leading AND/WHERE — callers splice it in). `scope is None` -> `("", [])`, no
    filtering at all (this is what keeps module-off / no-scope byte-identical to today).
    Empty `exclude` -> equality (`event_city = ?`, or `(col IS NULL OR col = ?)` when
    `include_null=True` — Phase 09.1 (B): a task's NULL means "all cities", so a delegate's
    own-city fetch must catch NULL even though their own city is the equality branch);
    non-empty `exclude` -> the default-city shape (`event_city IS NULL OR event_city NOT IN
    (?, ...)`), one placeholder per excluded code (already catches NULL, `include_null` is a
    no-op here). `column` lets callers qualify the column for a JOIN (e.g. "t.event_city",
    "u.event_city") — it is ALWAYS one of this file's own literal call-site strings, never
    user/callback-derived (T-091-08); city codes never get interpolated into the SQL string,
    only the ? count does."""
    if scope is None:
        return "", []
    code, exclude = scope
    if not exclude:
        if include_null:
            return f"({column} IS NULL OR {column} = ?)", [code]
        return f"{column} = ?", [code]
    placeholders = ", ".join("?" for _ in exclude)
    return f"({column} IS NULL OR {column} NOT IN ({placeholders}))", list(exclude)


# ── Phase 23 (APP-TINDER-01, D-08): track filter clause builder ──────────────────────────────
#
# Same shape as `_city_clause` — a pure SQL-fragment builder, no leading AND/WHERE, callers
# splice it into the same WHERE as `_city_clause`, so track + city scope combine in ONE query
# (T-23-04: no Python-side post-filter over the whole pending table). Track codes are this
# file's own literals (db.py imports neither `reg_engine` nor `cities` — same import-cycle
# reason as `_city_clause` above); a drift guard against `reg_engine.PARTY_TRACK_CODES` lives
# in plan 23-02 (T-23-05, accepted risk here).
def _track_clause(track: str | None, column: str = "participant_type") -> tuple[str, list]:
    """`track is None` or an unrecognized value -> `("", [])`, no filtering (keeps callers
    without the new kwarg byte-identical to today). `"full"` also matches NULL — an empty
    `participant_type` means the full track, same reading `_render_application_card` uses.
    `"party"` matches both overnight variants. `"short"` is a plain equality."""
    if track == "full":
        return f"({column} IS NULL OR {column} = ?)", ["full"]
    if track == "party":
        return f"{column} IN (?, ?)", ["party_overnight", "party_noovernight"]
    if track == "short":
        return f"{column} = ?", ["short"]
    return "", []


def _changed_only_clause(changed_only: bool, column: str = "edited_at") -> str:
    """No leading AND — `""` when the filter is off (default), else a non-empty edited_at
    check. Kept as its own tiny helper (not inlined) so the WHERE-assembly in
    `get_pending_users`/`get_pending_count` reads as a flat list of fragments, same style
    as `_city_clause`/`_track_clause`."""
    if not changed_only:
        return ""
    return f"{column} IS NOT NULL AND TRIM({column}) != ''"


def _flagged_only_clause(flagged_only: bool, column: str = "flagged_rule_ids") -> str:
    """Phase 31 (31-02, D-20): та же форма, что соседний `_changed_only_clause` — без
    ведущего AND, пустая строка при выключенном фильтре (дефолт — существующие вызывающие
    остаются байт-в-байт), иначе проверка «колонка непуста и не пустой JSON-список»."""
    if not flagged_only:
        return ""
    return f"{column} IS NOT NULL AND TRIM({column}) NOT IN ('', '[]')"


def _pending_where(city_scope, track, changed_only, *, city_column: str = "event_city",
                    flagged_only: bool = False) -> tuple[str, list]:
    """Assemble the shared WHERE tail (city scope + track + changed-only + flagged-only) used
    by `get_pending_users`/`get_pending_count` — ONE place builds the fragment list so both
    functions stay byte-identical in filtering behaviour."""
    parts = []
    params: list = []
    city_frag, city_params = _city_clause(city_scope, city_column)
    if city_frag:
        parts.append(city_frag)
        params.extend(city_params)
    track_frag, track_params = _track_clause(track)
    if track_frag:
        parts.append(track_frag)
        params.extend(track_params)
    changed_frag = _changed_only_clause(changed_only)
    if changed_frag:
        parts.append(changed_frag)
    flagged_frag = _flagged_only_clause(flagged_only)
    if flagged_frag:
        parts.append(flagged_frag)
    # D-41: walk-in (короткая анкета у стойки) решает только стойка — в очередь менеджера нет.
    parts.append(_NOT_WALKIN)
    extra = "".join(f" AND {p}" for p in parts)
    return extra, params


# D-41: фрагмент «не walk-in» — общий для очереди заявок и «Принять всех».
_NOT_WALKIN = "COALESCE(onsite_kind, '') != 'walkin'"


async def get_pending_users(limit: int = 1, offset: int = 0, *, city_scope=None,
                             track: str | None = None, changed_only: bool = False,
                             order_by_score: bool = False, flagged_only: bool = False) -> list[dict]:
    """Pending applications, oldest first by default (registration_date then telegram_id).

    `track`/`changed_only` splice into the SAME WHERE as `city_scope` — SQL does the
    filtering, not a Python post-filter (T-23-04). Defaults keep bot call sites (which never
    pass these kwargs) byte-identical to pre-Phase-23 behaviour.

    Phase 28 (28-08, SU-08, T-23-04): `order_by_score=True` (тумблер `apps_queue_sort_by_score`,
    читает вызывающий — сама функция в реестр не ходит) — сортирует SQL, не Python:
    `score` убывает первым (`COALESCE(score, -1)` — заявка без балла уходит в конец, а не
    смешивается с нулевым баллом), внутри одного балла порядок прежний (registration_date,
    telegram_id). Default `False` — байт-в-байт прежний порядок.

    Phase 31 (31-02, D-20): `flagged_only=True` — фильтр «только помеченные правилами»,
    включая делегатов, сменивших ответ после автоотказа (D-23, попадают сюда же с ⚠️-бейджем
    на карточке). Default `False` — прежнее поведение."""
    extra, params = _pending_where(city_scope, track, changed_only, flagged_only=flagged_only)
    order = (
        "ORDER BY COALESCE(score, -1) DESC, registration_date ASC, telegram_id ASC"
        if order_by_score
        else "ORDER BY registration_date ASC, telegram_id ASC"
    )
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM users WHERE status = 'pending'{extra} "
            f"{order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_resume_upload_backlog(before: str, limit: int = 20) -> list[dict]:
    """Очередь недогруженных резюме — отдельной таблицы нет, это сами строки `users`:
    `resume_url` пуст, а `resume_file_id`/`resume_text` есть (делегат прислал файл/текст на
    финале, но выгрузка в Nextcloud сорвалась или облако было выключено).

    `before` — отсечка «строка не моложе N минут», сравнение СТРОКОВОЕ. Квик 260912-mcj:
    `registration_date` пишется `strftime("%Y-%m-%d %H:%M:%S")` от московского `msk_now()`
    (services/reg_finalize.py:161) — обе стороны сравнения теперь на одних часах, отсечка
    ОБЯЗАНА приходить тоже московской (`msk_now()`), иначе разъезд на 3 часа. Свежие строки
    (моложе отсечки) не берём — финал ещё может быть «в полёте» под своим таймаутом.

    Порядок — по `registration_date` по возрастанию (старые в очереди первыми), лимит батча
    ограничивает число строк за один тик джобы."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE (resume_url IS NULL OR TRIM(resume_url) = '') "
            "AND ("
            "(resume_file_id IS NOT NULL AND TRIM(resume_file_id) != '') "
            "OR (resume_text IS NOT NULL AND TRIM(resume_text) != '')"
            ") "
            "AND (registration_date IS NULL OR registration_date <= ?) "
            "ORDER BY registration_date ASC, telegram_id ASC LIMIT ?",
            (before, limit),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_pending_count(*, city_scope=None, track: str | None = None,
                             changed_only: bool = False, flagged_only: bool = False) -> int:
    extra, params = _pending_where(city_scope, track, changed_only, flagged_only=flagged_only)
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM users WHERE status = 'pending'{extra}", tuple(params)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def approve_all_pending(*, city_scope=None) -> list[int]:
    """Flip every pending row to approved in one atomic statement; return the
    telegram_ids that flipped (each once). IN-07: requires sqlite >= 3.35 for
    RETURNING (bundled in CPython 3.10+, which the project already mandates) —
    there is no pre-3.35 fallback path in this function.

    `city_scope` narrows the WHERE of this SAME atomic UPDATE ... RETURNING (not a second
    query, not a post-filter on the returned ids) — a scoped call structurally cannot flip
    a row belonging to another city (T-072-03).

    approved_at (D-10, Phase 23.1-05) is stamped in the SAME UPDATE — this is the shared seam
    for BOTH mass-approve callers: the bot's appr_all_yes calls this function directly (not
    through services.applications.claim_approve_all), so stamping only in the service wrapper
    would silently miss the chat path.

    D-41: walk-in (onsite_kind='walkin') сюда не попадает — решение по нему принимает стойка."""
    approved_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    extra += f" AND {_NOT_WALKIN}"
    async with _connect() as db:
        async with db.execute(
            f"UPDATE users SET status = 'approved', approved_at = ? "
            f"WHERE status = 'pending'{extra} RETURNING telegram_id",
            (approved_at, *city_params),
        ) as cursor:
            rows = await cursor.fetchall()
        await db.commit()
        return [row[0] for row in rows]


# ── D-41 (FORUM-CHECKIN.md): регистрация на месте ────────────────────────────────────────────
#
# Человек, которого нет в базе или который не одобрен, проходит через стойку проблемных
# случаев. Три функции: короткая строка walk-in (никогда не затирает существующую анкету),
# одобрение ОДНОГО человека у стойки и список walk-in, ждущих у стойки. Массового варианта нет
# и не будет (урок инцидента 06.09 — тихие массовые одобрения).

async def create_onsite_user(telegram_id: int, username: str | None, full_name: str,
                             phone: str | None, university: str | None,
                             event_city: str | None, season: str | None) -> bool:
    """Короткая анкета у стойки: строка `pending`, `onsite_kind='walkin'`. True — строка
    создана; False — у человека уже есть строка `users` (любой статус), она не тронута
    (ON CONFLICT DO NOTHING — защита от затирания настоящей анкеты)."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO users (telegram_id, username, full_name, email, phone, university, "
            "event_city, season, status, onsite_kind, participant_type, source, registration_date, "
            "lang) "
            "VALUES (?, ?, ?, '-', ?, ?, ?, ?, 'pending', 'walkin', 'full', 'На месте', ?, "
            # язык, выбранный до анкеты (живёт в reg_started, как у обычной подачи)
            "(SELECT lang FROM reg_started WHERE telegram_id = ?)) "
            "ON CONFLICT(telegram_id) DO NOTHING",
            (telegram_id, store_username(username), full_name, phone, university,
             event_city, season, now, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def approve_onsite(telegram_id: int, *, by_staff_id: int, season: str | None,
                         event_city: str | None = None, override_reject: bool = False) -> bool:
    """Одобрение ОДНОГО человека у стойки одним атомарным UPDATE. Флипает pending и
    одобренного ПРОШЛОГО сезона (тот переезжает в текущий сезон, старый — в prev_season);
    одобренного текущего сезона не трогает. True — флип выигран этим вызовом (второй вызов
    подряд — False). `event_city` (если передан) переписывает город — для делегата прошлого
    сезона, которого пустили на форум города стойки.

    Отклонённую заявку флипает ТОЛЬКО `override_reject=True` — волонтёр явно подтвердил, что
    пропускает вопреки отказу менеджера. Тогда в том же UPDATE снимаются `rejected_at`, маркеры
    автоотказа и учёт доставки прошлого решения: карточка не должна одновременно говорить
    «одобрен» и «отклонён»."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    clear = (
        "rejected_at = NULL, auto_reject_rule_ids = NULL, auto_rejected_at = NULL, "
        "auto_rule_note = NULL, decision_delivery_status = NULL, "
        "decision_delivery_decision = NULL, decision_delivery_at = NULL, "
        "decision_delivery_error = NULL, "
        if override_reject else ""
    )
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE users SET status = 'approved', approved_at = :now, onsite_at = :now, "
            f"onsite_by = :by, onsite_kind = COALESCE(onsite_kind, 'door'), {clear}"
            # Сезон события не задан (:season NULL) — сезон строки не трогаем: иначе флип
            # обнулил бы его, а prev_season получил бы мусор.
            "prev_season = CASE WHEN :season IS NOT NULL AND COALESCE(season, '') != '' "
            "AND season != :season THEN season ELSE prev_season END, "
            "season = COALESCE(:season, season), event_city = COALESCE(:city, event_city) "
            "WHERE telegram_id = :tid AND ("
            "COALESCE(status, '') NOT IN ('approved', 'rejected') "
            "OR (status = 'rejected' AND :override = 1) "
            "OR (status = 'approved' AND COALESCE(season, '') NOT IN ('', :season)))",
            {"now": now, "by": by_staff_id, "season": season, "city": event_city,
             "tid": telegram_id, "override": 1 if override_reject else 0},
        )
        await db.commit()
        return cursor.rowcount == 1


async def claim_walkin_removal(telegram_id: int) -> bool:
    """Первый шаг «убрать из списка ждущих» (ревью 28.09): walk-in без решения атомарно
    переводится в `rejected` — параллельное одобрение у стойки (`approve_onsite` без
    `override_reject`) его больше не флипнет, и `purge_user` следом не сотрёт уже одобренного.
    True — строка была walk-in в pending и заявлена этим вызовом."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE users SET status = 'rejected' WHERE telegram_id = ? AND status = 'pending' "
            "AND onsite_kind = 'walkin'",
            (telegram_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def list_onsite_pending(*, city_scope=None, day: str, limit: int = 50) -> list[dict]:
    """Walk-in, ждущие у стойки: pending, `onsite_kind='walkin'`, анкета подана в день `day`
    (YYYY-MM-DD, по Москве), в скоупе города; новые сверху."""
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, full_name, username, university, event_city, phone, "
            "registration_date FROM users "
            "WHERE status = 'pending' AND onsite_kind = 'walkin' "
            f"AND substr(registration_date, 1, 10) = ?{extra} "
            "ORDER BY registration_date DESC, telegram_id DESC LIMIT ?",
            (day, *city_params, limit),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


# ── Phase 23 (APP-TINDER-01, D-06): journal of application decisions ─────────────────────────
#
# See the CREATE TABLE comment in init_db() for the full rationale (undo window, at-most-one
# effect delivery). All four accessors follow the `miniapp_outbox` shape (async, plain-dict
# rows, no ORM).

async def record_application_decision(telegram_id: int, decision: str, reason: str | None,
                                       decided_by: int, decided_at: str,
                                       effects_due_at: str,
                                       effects_sent_at: str | None = None) -> int:
    """Records a decision already applied to `users.status`; effects stay pending until
    `effects_due_at`. `effects_sent_at` — необязательный хвостовой параметр (quick
    260904-liz): веб-путь по-прежнему оставляет его пустым (живая строка, ждёт своего
    сметателя), а бот-путь передаёт уже проставленное время — бот применяет эффекты СИНХРОННО,
    сам, до записи журнала, поэтому строка бот-пути должна родиться УЖЕ отмеченной отправленной
    (иначе `claim_due_application_decisions` заберёт её и отправит отказ/приветствие делегату
    ВТОРОЙ раз). Возвращает id новой строки (> 0)."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO application_decisions "
            "(telegram_id, decision, reason, decided_by, decided_at, effects_due_at, "
            "effects_sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, decision, reason, decided_by, decided_at, effects_due_at,
             effects_sent_at),
        )
        await db.commit()
        return cursor.lastrowid


async def claim_decision_with_undo_row(telegram_id: int, decision: str, reason: str | None,
                                       decided_by: int, decided_at: str,
                                       effects_due_at: str) -> int | None:
    """Веб-путь решения с окном отмены: флип `users.status` (pending -> approved/rejected, с
    отметкой approved_at/rejected_at, как `approve_user_atomic`/`reject_user`) И живая строка
    `application_decisions` — ОДНОЙ транзакцией `BEGIN IMMEDIATE`. Раньше это были два
    коммита, и между ними приглашённый выглядел одобренным без строки окна отмены: подсчёт
    ступеней амбассадоров (`database.amb_tiers_db`) засчитывал ещё отменяемое одобрение.
    Теперь другой процесс видит либо ничего, либо статус вместе со строкой. Возвращает id
    строки решения, `None` — флип проиграли (заявка уже решена)."""
    if decision == "approved":
        stamp_col, status = "approved_at", "approved"
    elif decision == "rejected":
        stamp_col, status = "rejected_at", "rejected"
    else:
        raise ValueError(f"unknown decision: {decision!r}")
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                f"UPDATE users SET status = ?, {stamp_col} = ? "
                "WHERE telegram_id = ? AND status = 'pending'",
                (status, msk_now().strftime("%Y-%m-%d %H:%M:%S"), telegram_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            cursor = await db.execute(
                "INSERT INTO application_decisions "
                "(telegram_id, decision, reason, decided_by, decided_at, effects_due_at, "
                "effects_sent_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (telegram_id, decision, reason, decided_by, decided_at, effects_due_at),
            )
            decision_id = cursor.lastrowid
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return decision_id


async def claim_application_undo(decision_id: int) -> dict | None:
    """Claims the undo for one decision — succeeds exactly once. `effects_sent_at IS NULL AND
    undone_at IS NULL` in the WHERE closes the race against `claim_due_application_decisions`
    at the statement level (T-23-01): whichever UPDATE commits first wins the row, the other
    sees rowcount 0 and gets None back, never both."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "UPDATE application_decisions SET undone_at = ? "
            "WHERE id = ? AND effects_sent_at IS NULL AND undone_at IS NULL "
            "RETURNING *",
            (msk_now().strftime("%Y-%m-%d %H:%M:%S"), decision_id),
        ) as cursor:
            row = await cursor.fetchone()
        await db.commit()
        return dict(row) if row else None


async def claim_due_application_decisions(now: str, limit: int = 50) -> list[dict]:
    """Claims every overdue, still-live decision (`effects_sent_at IS NULL AND undone_at IS
    NULL AND effects_due_at <= now`) and marks it `effects_sent_at = now` in the SAME
    condition per row, so a row already claimed by a concurrent call (or already undone) is
    silently skipped — each row is returned to exactly one caller, ever (T-23-01)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM application_decisions "
            "WHERE effects_sent_at IS NULL AND undone_at IS NULL AND effects_due_at <= ? "
            "ORDER BY id LIMIT ?",
            (now, limit),
        ) as cursor:
            candidate_ids = [row["id"] for row in await cursor.fetchall()]

        claimed: list[dict] = []
        for candidate_id in candidate_ids:
            async with db.execute(
                "UPDATE application_decisions SET effects_sent_at = ? "
                "WHERE id = ? AND effects_sent_at IS NULL AND undone_at IS NULL "
                "RETURNING *",
                (now, candidate_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                claimed.append(dict(row))
        await db.commit()
        return claimed


async def get_application_decision(decision_id: int) -> dict | None:
    """Read-only lookup of one `application_decisions` row — used to verify OWNERSHIP
    (`decided_by`) and current state (`effects_sent_at`/`undone_at`) BEFORE attempting the
    mutating `undo_decision` (Phase 23, plan 23-04, T-23-17): the claim itself
    (`claim_application_undo`) stays atomic and blind to who asks — this function never
    decides the claim, only lets a caller refuse early without spending it."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM application_decisions WHERE id = ?", (decision_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_last_application_decision(telegram_id: int) -> dict | None:
    """Quick 260904-liz: последнее НЕ отменённое решение по делегату — `undone_at IS NULL`
    исключает отменённые строки на уровне SQL, а не в Python, потому что отменённый отказ не
    факт истории: менеджер вернул заявку в очередь (`undo_decision`/`revert_user_to_pending`),
    делегат/менеджер не должны видеть решение, которого фактически не было. Берётся ПОСЛЕДНЯЯ
    строка (`ORDER BY id DESC LIMIT 1`), а не первая — если делегата отклоняли дважды (подал
    повторно, отклонили снова), актуальна причина ВТОРОГО отказа, а не первого."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM application_decisions WHERE telegram_id = ? AND undone_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def revert_user_to_pending(telegram_id: int, from_status: str) -> bool:
    """Undo effect on `users`: puts the row back to `pending` ONLY if it is still in the
    status the decision expected (T-23-02) — a concurrent second decision by another manager
    silently wins, this call returns False rather than clobbering it.

    Координатор 25.09 (учёт доставки решения): `decision_delivery_*` сбрасывается в NULL В ТОЙ
    ЖЕ UPDATE — решение, к которому относилась запись, отменено, «доставлено»/«не доставлено»
    предыдущего решения на pending-заявке смысла не несёт (следующее решение запишет своё
    заново)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE users SET status = 'pending', decision_delivery_status = NULL, "
            "decision_delivery_decision = NULL, decision_delivery_at = NULL, "
            "decision_delivery_error = NULL WHERE telegram_id = ? AND status = ?",
            (telegram_id, from_status),
        )
        await db.commit()
        return cursor.rowcount == 1


# ── Phase 33 (delegate-card admin actions, задачи 2/3): персональные одноразовые исключения ──

async def grant_delegate_override(telegram_id: int, kind: str, granted_by: int, granted_at: str) -> int | None:
    """Атомарная вставка новой активной строки исключения — сам constraint
    (`idx_admin_delegate_overrides_unique_active`), не check-then-insert вызывающего кода, не
    даёт завести вторую активную строку того же вида одному делегату (двойной тап «Выдать»,
    гонка двух менеджеров). `INSERT OR IGNORE`: конфликт с уже активной строкой молча не
    вставляет ничего — возвращает `None`, вызывающий (`services/delegate_overrides.py::
    grant_override`) сам решает, что сказать менеджеру (обычно — дочитать активную строку и
    показать её)."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO admin_delegate_overrides "
            "(telegram_id, kind, granted_by, granted_at) VALUES (?, ?, ?, ?)",
            (telegram_id, kind, granted_by, granted_at),
        )
        await db.commit()
        return cursor.lastrowid if cursor.rowcount > 0 else None


async def get_active_delegate_override(telegram_id: int, kind: str) -> dict | None:
    """Последняя активная строка (`revoked_at IS NULL AND consumed_at IS NULL`) — `None`, если
    исключения нет вовсе, уже отозвано или уже использовано."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM admin_delegate_overrides WHERE telegram_id = ? AND kind = ? "
            "AND revoked_at IS NULL AND consumed_at IS NULL ORDER BY id DESC LIMIT 1",
            (telegram_id, kind),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def revoke_delegate_override(telegram_id: int, kind: str, revoked_by: int, revoked_at: str) -> bool:
    """`True` — активная строка была и закрыта этим вызовом; `False` — уже не было активной
    (второй тап по «отозвать», или делегат успел её сам использовать первым — гонка не
    считается ошибкой, просто нечего отзывать)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE admin_delegate_overrides SET revoked_at = ?, revoked_by = ? "
            "WHERE telegram_id = ? AND kind = ? AND revoked_at IS NULL AND consumed_at IS NULL",
            (revoked_at, revoked_by, telegram_id, kind),
        )
        await db.commit()
        return cursor.rowcount > 0


async def consume_delegate_override(telegram_id: int, kind: str, consumed_at: str) -> bool:
    """Гасит активную строку (одноразовость) — `True`, если строка действительно была активна
    и погашена этим вызовом; `False` — ничего активного не было (обычный делегат без
    исключения проходит этот вызов как безвредный no-op, см. вызывающих в
    `services/reg_finalize.py`)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE admin_delegate_overrides SET consumed_at = ? "
            "WHERE telegram_id = ? AND kind = ? AND revoked_at IS NULL AND consumed_at IS NULL",
            (consumed_at, telegram_id, kind),
        )
        await db.commit()
        return cursor.rowcount > 0


# ── Phase 23 (APP-TINDER-01, D-02): delegate avatar cache ────────────────────────────────────

async def set_user_avatar(telegram_id: int, file_id: str, checked_at: str) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET avatar_file_id = ?, avatar_checked_at = ? WHERE telegram_id = ?",
            (file_id, checked_at, telegram_id),
        )
        await db.commit()


async def find_user_by_avatar_file_id(file_id: str) -> dict | None:
    """Reverse lookup: whose avatar is this file_id (used by the file proxy allow-list,
    plan 23-03). Empty/None `file_id` short-circuits to None without a query."""
    if not file_id:
        return None
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE avatar_file_id = ? LIMIT 1", (file_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ── Phase 3: scheduled-broadcast payload store (SCHED-01) ────────────────────

async def create_scheduled_broadcast(
    text: str | None,
    photo_file_id: str | None,
    filter_spec: str | None,
    scheduled_at: str,
    created_by: int,
    *, important: bool = False,
) -> int:
    """Insert a pending scheduled broadcast; return its new id (the job's only arg).

    `important` (форум-ночь п.7, kw-only с дефолтом False — существующие вызовы байт-в-байт
    прежние): копируется в журнал `broadcasts` при отправке (services/scheduler.py::
    send_scheduled_broadcast), read back via get_scheduled_broadcast (SELECT *)."""
    created_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO scheduled_broadcasts "
            "(text, photo_file_id, filter_spec, scheduled_at, status, created_by, created_at, "
            "important) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (text, photo_file_id, filter_spec, scheduled_at, created_by, created_at,
             1 if important else 0),
        )
        await db.commit()
        return cursor.lastrowid


async def get_scheduled_broadcast(broadcast_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scheduled_broadcasts WHERE id = ?", (broadcast_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def mark_broadcast_sending(broadcast_id: int) -> int:
    """ME-02: atomically claim a pending broadcast for sending. Flips 'pending' → 'sending'
    and returns rowcount: 1 = this caller owns the send, 0 = already claimed/sent/cancelled
    (double-schedule race or a re-fire). A crash mid-send leaves the row 'sending'; a re-fire
    in the same process is still rejected here. Recovery is NOT by re-claiming 'sending' —
    it is the boot-time reclaim_stale_sending(), which flips a long-stuck 'sending' row back to
    'pending' so it is re-armed; the send loop then skips every chat already recorded in
    scheduled_broadcast_deliveries, so the re-run reaches only the unsent tail (review 260817
    §B2). `sending_since` is stamped here so that staleness can be measured."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE scheduled_broadcasts SET status = 'sending', sending_since = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, broadcast_id),
        )
        await db.commit()
        return cursor.rowcount


async def mark_broadcast_sent(broadcast_id: int):
    async with _connect() as db:
        await db.execute(
            "UPDATE scheduled_broadcasts SET status = 'sent' WHERE id = ?", (broadcast_id,)
        )
        await db.commit()


async def set_scheduled_log_broadcast_id(scheduled_id: int, log_broadcast_id: int):
    """Link a scheduled_broadcasts row to its journal row in `broadcasts` (Task B1). No getter
    needed — get_scheduled_broadcast() does SELECT * and the field comes back in the same dict."""
    async with _connect() as db:
        await db.execute(
            "UPDATE scheduled_broadcasts SET log_broadcast_id = ? WHERE id = ?",
            (log_broadcast_id, scheduled_id),
        )
        await db.commit()


async def reclaim_stale_sending(max_age_minutes: int) -> list[int]:
    """Review 260817 §B2: flip 'sending' rows claimed more than `max_age_minutes` ago back to
    'pending' so reconcile_scheduled_broadcasts() re-arms them. Returns the reclaimed ids.

    Safe to re-run only because send_scheduled_broadcast() skips chats already present in
    scheduled_broadcast_deliveries — the re-run reaches the unsent tail, not the whole audience.
    Rows with NULL `sending_since` (claimed by a build older than this column) are deliberately
    left alone: they have no delivery log, so a re-run WOULD blast everyone a second time."""
    cutoff = (msk_now() - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        async with db.execute(
            "SELECT id FROM scheduled_broadcasts "
            "WHERE status = 'sending' AND sending_since IS NOT NULL AND sending_since < ?",
            (cutoff,),
        ) as cursor:
            ids = [r[0] for r in await cursor.fetchall()]
        if ids:
            await db.execute(
                "UPDATE scheduled_broadcasts SET status = 'pending' "
                "WHERE status = 'sending' AND sending_since IS NOT NULL AND sending_since < ?",
                (cutoff,),
            )
            await db.commit()
        return ids


async def list_delivered_chat_ids(broadcast_id: int) -> set[int]:
    """Chats already handled for this broadcast — both 'ok' and 'failed'. Failed ones are
    included on purpose: a blocked/deactivated chat must not be hammered again on every
    resume; the admin sees the failed count in the log and re-sends by hand if needed."""
    async with _connect() as db:
        async with db.execute(
            "SELECT chat_id FROM scheduled_broadcast_deliveries WHERE broadcast_id = ?",
            (broadcast_id,),
        ) as cursor:
            return {r[0] for r in await cursor.fetchall()}


# Бенчмарк 25.09 (tools/bench_broadcast.py): новое соединение на КАЖДУЮ запись о доставке
# стоило 15–30 мс — не сам INSERT, а открытие и особенно закрытие: закрывая последнее
# соединение с WAL-файлом, SQLite делает чекпоинт и fsync основной базы. Прогон рассылки
# держит одно соединение на всё время цикла (`delivery_writer`), а `mark_delivery`/
# `record_broadcast_deliveries` пишут через него. Каждая запись по-прежнему коммитится сразу —
# транзакция не висит, пока ждём Telegram, и сканер чек-ина (второй писатель) не блокируется.
_delivery_conn_var: ContextVar[aiosqlite.Connection | None] = ContextVar(
    "_delivery_conn", default=None
)


@asynccontextmanager
async def delivery_writer():
    """Одно соединение для записей о доставке на время блока (цикл рассылки). Вложенный вызов
    переиспользует внешнее соединение. Вне блока `mark_delivery` и соседи открывают своё, как
    раньше."""
    if _delivery_conn_var.get() is not None:
        yield
        return
    async with _connect() as conn:
        token = _delivery_conn_var.set(conn)
        try:
            yield
        finally:
            _delivery_conn_var.reset(token)


@asynccontextmanager
async def _delivery_conn():
    held = _delivery_conn_var.get()
    if held is None:
        async with _connect() as conn:
            yield conn
        return
    try:
        yield held
    except BaseException:
        # Упавшая запись не должна оставить открытую транзакцию на общем соединении — иначе
        # следующая запись продолжила бы её и держала блокировку писателя.
        try:
            await held.rollback()
        except Exception:
            pass
        raise


async def mark_delivery(broadcast_id: int, chat_id: int, ok: bool):
    """Checkpoint one send attempt. INSERT OR REPLACE so a retry after a crash that landed
    between the send and this write just overwrites the row."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _delivery_conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO scheduled_broadcast_deliveries "
            "(broadcast_id, chat_id, status, sent_at) VALUES (?, ?, ?, ?)",
            (broadcast_id, chat_id, "ok" if ok else "failed", now),
        )
        await db.commit()


async def count_deliveries(broadcast_id: int) -> tuple[int, int]:
    """(ok, failed) for the /scheduled progress line of a row that is still 'sending'."""
    async with _connect() as db:
        async with db.execute(
            "SELECT "
            "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) "
            "FROM scheduled_broadcast_deliveries WHERE broadcast_id = ?",
            (broadcast_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0), int(row[1] or 0)


async def cleanup_deliveries(broadcast_id: int):
    """Drop the checkpoint rows once the broadcast is 'sent' — they only matter for resume."""
    async with _connect() as db:
        await db.execute(
            "DELETE FROM scheduled_broadcast_deliveries WHERE broadcast_id = ?", (broadcast_id,)
        )
        await db.commit()


# ── Quick 260910-okb (BC-01..06): immediate-broadcast log store ──────────────
# `services/broadcast_run.py` is the only caller of the write helpers below — kept here (not
# there) so the module stays a pure send-loop with no DB-shape knowledge beyond these calls.

async def create_broadcast(
    admin_id: int, text_preview: str, total: int,
    *, important: bool = False, full_text: str | None = None,
) -> int:
    """Insert a 'sending' row for an immediate broadcast; return its new id.

    Форум-ночь п.7: `important`/`full_text` — тот же ряд, что и у отложенной рассылки
    (services/scheduler.py::send_scheduled_broadcast заводит/переиспользует ЭТУ ЖЕ строку
    журнала через log_broadcast_id). kw-only с дефолтами — существующие позиционные вызовы
    остаются байт-в-байт прежними."""
    started_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO broadcasts "
            "(admin_id, text_preview, started_at, status, total, delivered, blocked, "
            "important, full_text) "
            "VALUES (?, ?, ?, 'sending', ?, 0, 0, ?, ?)",
            (admin_id, text_preview, started_at, total, 1 if important else 0, full_text),
        )
        await db.commit()
        return cursor.lastrowid


async def record_broadcast_delivery(broadcast_id: int, chat_id: int, message_id: int):
    """One row per delivered message (several per chat_id for an album)."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "INSERT INTO broadcast_deliveries (broadcast_id, chat_id, message_id, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (broadcast_id, chat_id, message_id, now),
        )
        await db.commit()


async def record_broadcast_deliveries(rows: list[tuple[int, int, int, str]]):
    """Пачка строк журнала `(broadcast_id, chat_id, message_id, sent_at)` одной транзакцией —
    цикл рассылки копит их и сбрасывает раз в несколько получателей, а не коммитит каждую."""
    if not rows:
        return
    async with _delivery_conn() as db:
        await db.executemany(
            "INSERT INTO broadcast_deliveries (broadcast_id, chat_id, message_id, sent_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        await db.commit()


async def list_stored_langs() -> dict[int, str]:
    """Сохранённый язык ВСЕХ, у кого он есть, одним чтением — то же правило, что
    `get_stored_lang` (сначала `users.lang`, потом `reg_started.lang`), для рассылки, которой
    нужен язык каждого получателя."""
    langs: dict[int, str] = {}
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id, lang FROM reg_started WHERE lang IS NOT NULL AND lang != ''"
        ) as cur:
            for tid, lang in await cur.fetchall():
                langs[tid] = lang
        async with db.execute(
            "SELECT telegram_id, lang FROM users WHERE lang IS NOT NULL AND lang != ''"
        ) as cur:
            for tid, lang in await cur.fetchall():
                langs[tid] = lang
    return langs


async def finish_broadcast(
    broadcast_id: int, status: str, delivered: int, blocked: int, mute_skipped: int = 0,
):
    """`mute_skipped` (форум-ночь п.7, дефолт 0 — существующие вызовы байт-в-байт прежние):
    сколько получателей пропущено, потому что нажали «🔕 Не присылать сегодня» — отдельная
    цифра от `blocked` (недоставленных Telegram'ом), для отчёта менеджеру."""
    finished_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "UPDATE broadcasts SET status = ?, delivered = ?, blocked = ?, finished_at = ?, "
            "mute_skipped = ? WHERE id = ?",
            (status, delivered, blocked, finished_at, mute_skipped, broadcast_id),
        )
        await db.commit()


async def set_broadcast_status(broadcast_id: int, status: str):
    async with _connect() as db:
        await db.execute("UPDATE broadcasts SET status = ? WHERE id = ?", (status, broadcast_id))
        await db.commit()


async def get_broadcast(broadcast_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_recent_broadcasts(limit: int = 10) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def list_broadcast_messages(broadcast_id: int) -> list[tuple[int, int]]:
    """(chat_id, message_id) pairs in insertion order — the send order, for run_revoke."""
    async with _connect() as db:
        async with db.execute(
            "SELECT chat_id, message_id FROM broadcast_deliveries "
            "WHERE broadcast_id = ? ORDER BY rowid",
            (broadcast_id,),
        ) as cursor:
            return [(r[0], r[1]) for r in await cursor.fetchall()]


async def list_sending_broadcasts() -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scheduled_broadcasts WHERE status = 'sending' ORDER BY scheduled_at"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def list_pending_broadcasts() -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scheduled_broadcasts WHERE status = 'pending' ORDER BY scheduled_at"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def cancel_scheduled_broadcast(broadcast_id: int):
    async with _connect() as db:
        await db.execute(
            "UPDATE scheduled_broadcasts SET status = 'cancelled' WHERE id = ?", (broadcast_id,)
        )
        await db.commit()


# ── Форум-ночь п.7 («🔕 Не присылать сегодня» + «❗ Важное») ──────────────────

async def get_muted_today_ids(date_str: str) -> set[int]:
    """Кто отключил НЕважные рассылки на `date_str` (MSK 'YYYY-MM-DD') — один запрос ДО цикла
    доставки (и мгновенной bc_go, и отложенной send_scheduled_broadcast), а не чтение
    `get_user` на каждого получателя."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM users WHERE mute_broadcasts_until = ?", (date_str,)
        ) as cursor:
            return {r[0] for r in await cursor.fetchall()}


async def set_broadcast_mute(telegram_id: int, date_str: str | None) -> None:
    """`date_str` — MSK-дата, ДО конца которой НЕважные рассылки пропускаются; `None` снимает
    заглушку («🔔 Присылать всё»)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET mute_broadcasts_until = ? WHERE telegram_id = ?",
            (date_str, telegram_id),
        )
        await db.commit()


async def get_mute_offer_shown_ids(date_str: str) -> set[int]:
    """Кому УЖЕ показывали предложение «🔕» альбома сегодня (MSK 'YYYY-MM-DD') — один запрос
    перед прогоном, тот же приём, что `get_muted_today_ids`. Только для альбомной ветки
    (`services.scheduler.send_mute_offer_if_eligible`) — text/фото/документ несут кнопку «🔕»
    ВНУТРИ самой рассылки и повторного показа не считают."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM users WHERE mute_offer_shown_date = ?", (date_str,)
        ) as cursor:
            return {r[0] for r in await cursor.fetchall()}


async def mark_mute_offer_shown(telegram_id: int, date_str: str) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET mute_offer_shown_date = ? WHERE telegram_id = ?",
            (date_str, telegram_id),
        )
        await db.commit()


async def important_messages_today(telegram_id: int, date_str: str) -> list[dict]:
    """(text, sent_at) важных рассылок, ДОСТАВЛЕННЫХ этому делегату сегодня (MSK) — для кнопки
    «❗ Важное» (handlers/user_actions.py::show_important_today). `GROUP BY b.id` схлопывает
    альбом (несколько строк broadcast_deliveries на один chat_id) в одну запись."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT b.full_text AS text, MIN(d.sent_at) AS sent_at "
            "FROM broadcast_deliveries d JOIN broadcasts b ON b.id = d.broadcast_id "
            "WHERE d.chat_id = ? AND b.important = 1 AND substr(d.sent_at, 1, 10) = ? "
            "GROUP BY b.id ORDER BY sent_at",
            (telegram_id, date_str),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def has_important_today(telegram_id: int, date_str: str) -> bool:
    """Гейт кнопки меню «❗ Важное» (keyboards/builders.py::get_main_menu_kb) — дешёвый EXISTS,
    без сборки полного списка."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM broadcast_deliveries d JOIN broadcasts b ON b.id = d.broadcast_id "
            "WHERE d.chat_id = ? AND b.important = 1 AND substr(d.sent_at, 1, 10) = ? LIMIT 1",
            (telegram_id, date_str),
        ) as cursor:
            return await cursor.fetchone() is not None


async def has_any_important_today(date_str: str) -> bool:
    """Была ли сегодня хоть одна доставленная важная рассылка кому-либо — для пометки
    «сейчас скрыта» у «❗ Важное» на экране «🔘 Кнопки меню» (keyboards/builders.py)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM broadcast_deliveries d JOIN broadcasts b ON b.id = d.broadcast_id "
            "WHERE b.important = 1 AND substr(d.sent_at, 1, 10) = ? LIMIT 1",
            (date_str,),
        ) as cursor:
            return await cursor.fetchone() is not None


# ── Phase 3: dropout-nudge scan/mark (SCHED-03) ──────────────────────────────

async def get_nudge_candidates(cutoff: str) -> list[int]:
    """Incomplete registrations older than cutoff that were never nudged.
    started_at is ISO ('%Y-%m-%d %H:%M:%S') so lexicographic `<` is chronological.

    Phase 21 (D-21): a delegate answering in the Mini App right now looks abandoned to
    reg_started (the bot never saw an answer), so they'd get nudged mid-flow. The NOT EXISTS
    clause below excludes anyone with a draft activity stamp at or after the SAME cutoff
    (touch_reg_draft_activity keeps it moving forward while they are active) — one threshold,
    no separate config key. reg_started, _INCOMPLETE_NOT_REGISTERED and the «Незавершённые»
    reads (get_incomplete_rows*) are untouched by this — the exclusion applies ONLY here."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM reg_started "
            f"WHERE started_at < ? AND nudged_at IS NULL AND {_INCOMPLETE_NOT_REGISTERED} "
            "AND NOT EXISTS (SELECT 1 FROM reg_drafts d WHERE d.telegram_id = reg_started.telegram_id "
            "AND d.updated_at >= ?)",
            (cutoff, cutoff),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def mark_nudged(telegram_id: int):
    """Stamp nudged_at so a user is never nudged twice (one-shot dedup, D-14)."""
    nudged_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "UPDATE reg_started SET nudged_at = ? WHERE telegram_id = ?",
            (nudged_at, telegram_id),
        )
        await db.commit()


# ── Phase 3: filtered-broadcast audience query (COMM-01/02/03) ───────────────

# Column whitelist — the ONLY place a column name is composed into SQL (Pitfall 5).
_FILTER_COLUMNS = {
    "city", "university", "status", "source", "payment_status",
    # Broadcast segmentation by more user attributes (all real users columns).
    "local_committee", "department", "aiesec_role", "education_status",
    "course", "study_field", "position", "attendance_format",
    "participant_type",  # Phase 5 (D-19, TRACK-06 SC#8)
    # Phase 07.2 (CITY-02) — event city as a broadcast-segment filter. MUST also be in
    # `handlers.admin._PICKER_FIELDS`, otherwise the field is silently dropped and the
    # manager broadcasts to the wrong segment while the screen says otherwise
    # (precedent: Phase 5 D-19). Handled by its own branch in `_build_filter_clause`,
    # not by the generic `{field} = ?` one — see there.
    "event_city",
    # Квик 260910-vfl (SEASON-FILTER-01): сезон события как поле фильтра рассылки. Поле
    # ОБЯЗАНО быть ЗАРЕГИСТРИРОВАНО ДВАЖДЫ (здесь и в `handlers.admin_broadcasts._PICKER_FIELDS`)
    # — иначе фильтр виден на экране и молча не доходит до SQL (тот же прецедент фазы 5, D-19,
    # что уже сработал для `event_city`). Обрабатывается собственной веткой в
    # `_build_filter_clause`, не общей — легаси-строки без сезона нужно ловить сентинелом.
    "season",
    # Квик 260911-0fh (RESUME-FILTER-01): «резюме есть/нет» как поле фильтра рассылки. Та же
    # двойная регистрация (здесь и в `handlers.admin_broadcasts._PICKER_FIELDS`), тот же
    # прецедент D-19. Поле ВИРТУАЛЬНОЕ — колонки `users.resume` не существует, условие
    # собирается из нескольких колонок (`RESUME_COLUMNS`) собственной веткой
    # `_build_filter_clause`; см. `_FILTER_VIRTUAL_FIELDS` ниже.
    "resume",
    # Квик 260914-rgr (RGR-01..07): членство в ЧАТЕ мероприятия как поле фильтра рассылки. Та
    # же двойная регистрация (здесь и в `handlers.admin_broadcasts._PICKER_FIELDS`), тот же
    # прецедент D-19. Поле ВИРТУАЛЬНОЕ — колонки `users.delegate_chat` не существует, условие
    # собирается по `chat_members` собственной веткой `_build_filter_clause`; см.
    # `_FILTER_VIRTUAL_FIELDS` ниже.
    "delegate_chat",
    # Phase 31 (31-02, D-28): «автоотказ по правилу» как поле фильтра рассылки. Та же двойная
    # регистрация (здесь и в `handlers.admin_broadcasts._PICKER_FIELDS`, план 31-07), тот же
    # прецедент D-19 — поле, зарегистрированное только тут, видно на экране и молча не
    # доходит до SQL. Поле ВИРТУАЛЬНОЕ — условие собирается по `users.auto_reject_rule_ids`
    # (не `users.auto_reject`, такой колонки нет) собственной веткой `_build_filter_clause`;
    # см. `_FILTER_VIRTUAL_FIELDS` ниже.
    "auto_reject",
    # Форум-ночь п.6 (D-25, идея №14): «Отметка на форуме» (пришли/не пришли) как поле
    # фильтра рассылки. Та же двойная регистрация (здесь и в
    # `handlers.admin_broadcasts._PICKER_FIELDS`), тот же прецедент D-19. Поле ВИРТУАЛЬНОЕ —
    # условие собирается по таблице `checkins` собственной веткой `_build_filter_clause`; см.
    # `_FILTER_VIRTUAL_FIELDS` ниже.
    "checkin_entry",
    # «Сессия программы» (были/не были на конкретной сессии) — та же двойная регистрация,
    # тот же прецедент D-19. Поле ВИРТУАЛЬНОЕ — условие тоже собирается по `checkins`, но
    # значение сессии едет ВНУТРИ записи фильтра (`session_id`), а не выбирается из
    # `get_distinct_filter_values` — у него собственный UI-мастер (город → день → сессия),
    # не входит в `_PICKER_FIELDS`.
    "checkin_session",
    # Внешние формы: двойная регистрация с handlers/admin_broadcasts.py, поле ВИРТУАЛЬНОЕ —
    # значение (форма + заполнил/не заполнил) задаёт шов admin_broadcast_ext_form_filter.
    "ext_form",
    # Делегации вузов (D-07): `delegation` — вуз делегации ИЗ ФОРМЫ (users.delegation), не
    # путать с `university` анкеты выше; обычная колонка, общая ветка `{field} = ?`.
    # `delegation_any` — ВИРТУАЛЬНОЕ «делегация вуза / не делегация» по той же колонке,
    # собственная ветка `_build_filter_clause`. Та же двойная регистрация с
    # `handlers.admin_broadcasts._PICKER_FIELDS`, тот же прецедент D-19.
    "delegation", "delegation_any",
}

# Квик 260911-0fh (RESUME-FILTER-01): поля whitelist'а `_FILTER_COLUMNS`, у которых НЕТ
# одноимённой колонки `users` — условие собирается из нескольких колонок, а не читается
# как `{field} = ?`. Whitelist читают ДВОЕ: `_build_filter_clause` (у него для таких полей
# есть собственная ветка ВЫШЕ общей) и `get_distinct_filter_values`, который для обычного
# поля подставляет имя прямо в `SELECT DISTINCT {field} ...` — на виртуальном поле это
# `OperationalError: no such column`. Поэтому у `get_distinct_filter_values` условие ветки —
# `elif field in _FILTER_COLUMNS and field not in _FILTER_VIRTUAL_FIELDS`, виртуальное поле
# уходит в уже существующий `return []` (мина обезврежена ДО того, как её кто-то заденет —
# сегодня `get_distinct_filter_values("resume")` никто не зовёт, но так не будет всегда).
_FILTER_VIRTUAL_FIELDS = {
    "resume", "delegate_chat", "auto_reject",
    # Форум-ночь п.6 (D-25, идея №14) — те же виртуальные поля, что резюме/чат/автоотказ выше.
    "checkin_entry", "checkin_session", "ext_form",
    # Делегации вузов (D-07): колонки `users.delegation_any` нет, условие — по `delegation`.
    "delegation_any",
}

# Квик 260910-vfl (SEASON-FILTER-03): маркер «строк без сезона» в спеке фильтра рассылки.
# Это НЕ значение из БД (`users.season` для таких строк — NULL/пустая строка), а сентинел,
# который ездит внутри спеки фильтра ([{field: "season", value: SEASON_NONE}]) и обязан
# пережить `json.dumps`/`json.loads` отложенной рассылки (`services/scheduler.py`) — поэтому
# строка, а не `None` (JSON отдаёт `None` обратно как `null`, а не как питоний `None`-объект
# внутри списка словарей — риск не в этом, риск в читаемости и в случайном совпадении с
# легитимным отсутствующим ключом; явная строка исключает оба случая).
SEASON_NONE = "__none__"

# Квик 260911-0fh (RESUME-FILTER-01/03): единственный источник правды о том, какие колонки
# `users` считаются «резюме». Прод-инцидент: с 05.09 по 10.09 у 203 делегатов молча
# потерялось резюме, приложенное файлом (баг починен, `ef315f9`) — их нужно попросить
# прислать резюме заново, отсекая тех, у кого резюме уже есть.
#
# `RESUME_RECALL_COLUMNS` — набор для чат-recall (`reg_engine.has_prior_resume`): «есть
# артефакт, который можно ПЕРЕИСПОЛЬЗОВАТЬ на шаге резюме вместо повторного вопроса».
# `RESUME_COLUMNS` — тот же набор ПЛЮС `resume_link` (СкиллАп 5, развилка резюме R2b,
# `handlers/reg_resume_fork.py`) — ссылка на профиль ВМЕСТО файла. Для менеджера это тоже
# «резюме есть» (просить прислать заново такого делегата нельзя), но переиспользовать эту
# ссылку на шаге резюме recall не станет — это не тот же артефакт. Заведено производной
# (`RESUME_RECALL_COLUMNS + (...)`), а не вторым литералом, чтобы паритет трёх из четырёх
# колонок был гарантирован кодом, а не совпадением при правке.
#
# Константы живут ИМЕННО здесь (а не в `reg_engine.py`), потому что `reg_engine.py` уже
# импортирует `database.db` (строка 38) — обратный импорт был бы циклом.
RESUME_RECALL_COLUMNS = ("resume_file_id", "resume_text", "resume_url")
RESUME_COLUMNS = RESUME_RECALL_COLUMNS + ("resume_link",)

# Сентинелы значений поля фильтра «Резюме» — строки (не булево), чтобы пережить
# `json.dumps`/`json.loads` спеки отложенной рассылки (`services/scheduler.py`), тот же
# приём, что `SEASON_NONE` выше. Это НЕ значения из БД.
RESUME_HAS = "has"
RESUME_MISSING = "none"

# Сентинелы значений поля фильтра «Автоотказ» (Phase 31, 31-02, D-28) — та же причина строки,
# не булева: спека фильтра переживает `json.dumps`/`json.loads` отложенной рассылки.
AUTO_REJECT_YES = "yes"
AUTO_REJECT_NO = "no"

# Сентинелы значений поля фильтра «Делегация вуза» (делегации вузов, D-07) — та же причина
# строки, не булева: спека фильтра переживает `json.dumps`/`json.loads` отложенной рассылки.
DELEGATION_YES = "yes"
DELEGATION_NO = "no"

# Сентинелы значений поля фильтра «Чат делегатов» (квик 260914-rgr) — та же причина строки,
# не булева: спека фильтра переживает `json.dumps`/`json.loads` отложенной рассылки.
CHAT_IN = "in"
CHAT_OUT = "out"

# Форум-ночь п.6 (D-25, идея №14): поле фильтра рассылки «Отметка на форуме» — «пришли» / «не
# пришли». Литерал ниже ОБЯЗАН побайтово совпадать с `services.checkin.ENTRY_POINT`
# (`checkins.point` для входа) — не импортирован напрямую (services.checkin импортирует ЭТОТ
# модуль, обратный импорт был бы циклом), совпадение проверяет
# tests/test_checkin_broadcast_filter_260924.py::test_entry_point_literal_matches_service.
CHECKIN_ENTRY_POINT = "entry"
CHECKIN_YES = "yes"
CHECKIN_NO = "no"
# Вход каждый день: запись фильтра `checkin_entry` может нести `day` — «YYYY-MM-DD» (конкретный
# день форума) или этот сентинел «сегодня», который пересчитывается по Москве на КАЖДЫЙ вызов
# (превью и отложенная отправка). Без `day` — «хоть один день форума» / «ни разу».
CHECKIN_DAY_TODAY = "today"

# Поле фильтра рассылки «Сессия программы» — «были» / «не были» на КОНКРЕТНОЙ сессии (внутри
# записи фильтра едет `session_id`, тот же приём, что `chats`/`exclude` у delegate_chat/
# event_city выше — `database/db.py` не может импортировать `services.program`).
SESSION_ATTENDED = "attended"
SESSION_NOT_ATTENDED = "not_attended"


def _approved_current_season_frag(event_season: str | None) -> tuple[str, list]:
    """Общий гард «approved + текущий сезон» — `status = 'approved' AND (season IS NULL OR
    season = ?)`. Переиспользуется в `checkin_entry`=`CHECKIN_NO` и в обеих ветках
    `checkin_session` (баг форум-ночи: у «🚫 Не были на сессии X» этого гарда не было вовсе, в
    отличие от соседней `checkin_entry`=`CHECKIN_NO` — в аудиторию попадали pending/rejected и
    approved-делегаты прошлого сезона, 482 импортированных 26/1 без QR). `event_season=None`
    (настройка не задана) — тот же fail-soft приём, что у `count_approved_current_season`:
    сезон не фильтруется, ограничение остаётся только по `status`."""
    season_frag = "(season IS NULL OR season = ?)" if event_season else "1=1"
    params = [event_season] if event_season else []
    return f"status = 'approved' AND {season_frag}", params


def _resume_has_fragment() -> str:
    """SQL fragment: «резюме есть» — любая из `RESUME_COLUMNS` непуста (`-` тоже пусто).
    Единственное место, где это условие собрано — и `_build_filter_clause`, и
    `get_resume_filter_options` вызывают этот хелпер, второй копии условия не заводится."""
    return " OR ".join(
        f"COALESCE(TRIM({col}), '') NOT IN ('', '-')" for col in RESUME_COLUMNS
    )


def _resume_missing_fragment() -> str:
    """SQL fragment: «резюме нет» — ВСЕ `RESUME_COLUMNS` пусты (`-` тоже пусто)."""
    return " AND ".join(
        f"COALESCE(TRIM({col}), '') IN ('', '-')" for col in RESUME_COLUMNS
    )


def _build_filter_clause(filters: list[dict]) -> tuple[str, list]:
    """Pure: build a parameterized AND WHERE clause from a filter spec.

    Column names come only from `_FILTER_COLUMNS` (or the literal `registration_date`);
    values are NEVER interpolated — they bind as `?`. Non-whitelisted fields are dropped.

    `event_city` is the one field that is NOT a plain equality: the DEFAULT city is described
    by EXCLUSION of the other known cities, so it also catches `event_city IS NULL` (every
    application registered before the cities module existed) — same semantics as
    `cities.normalize_city` and the Sheets tabs. The list of "other known city codes" arrives
    in the filter dict itself under the `exclude` key, put there by the caller
    (`handlers/admin.py`, via `cities.city_scope`), because `database/db.py` may NEVER import
    `cities` — `cities.py` already imports this module, so that would be an import cycle.
    The `exclude` key must therefore also survive the `json.dumps`/`json.loads` round-trip a
    scheduled broadcast's filter spec goes through.

    `season` (квик 260910-vfl) is also not a plain equality when the value is `SEASON_NONE`:
    that sentinel means "rows with no season stamp at all" (legacy pre-season registrations),
    which is `season IS NULL OR TRIM(season) = ''`, not a literal `season = '__none__'` bind.
    A real season value still binds as a plain `season = ?`.

    `resume` (квик 260911-0fh) — виртуальное поле, нет колонки `users.resume`: условие
    собирается по `RESUME_COLUMNS` (четыре реальные колонки). `RESUME_HAS`/`RESUME_MISSING` —
    сентинелы, не значения из БД. Прочерк `-` считается «не заполнено» — та же конвенция, что
    в `reg_engine.has_prior_resume`/`prior_answers_for`. Любое другое значение (в т.ч. пустое)
    — fail closed (`clauses.append("0")`), тот же довод WR-01, что у `event_city`/`season`.
    """
    clauses: list[str] = []
    params: list = []
    for f in filters:
        field = f.get("field")
        if field == "registration_date":
            op = ">=" if f.get("op") == "after" else "<"
            clauses.append(f"registration_date {op} ?")
            params.append(f.get("value"))
        elif field == "event_city":
            # Must come BEFORE the generic `_FILTER_COLUMNS` branch below, which would emit a
            # plain `event_city = ?` and silently drop every NULL row from the default city.
            if not f.get("value"):
                # WR-01: НЕ «пропустить». Пропуск снимал условие целиком, и ME-04
                # (`if filters and not where`) спасал только когда отброшены ВСЕ фильтры.
                # Спека [{status: approved}, {event_city: ""}] давала `WHERE status = ?`,
                # т.е. рассылка уходила во все города, хотя сводка называла один. Эмитим
                # заведомо ложное условие — аудитория гарантированно пуста (fail closed),
                # что совпадает с наблюдаемым поведением ME-04.
                clauses.append("0")
                continue
            frag, city_params = _city_clause((f.get("value"), tuple(f.get("exclude") or ())))
            clauses.append(frag)
            params.extend(city_params)
        elif field == "season":
            # Must come BEFORE the generic `_FILTER_COLUMNS` branch below — the sentinel
            # SEASON_NONE is not a real value to bind, it means "no season stamp at all".
            value = f.get("value")
            if value == SEASON_NONE:
                clauses.append("(season IS NULL OR TRIM(season) = '')")
            elif not value:
                # WR-01, same reasoning as event_city above: an empty value must NOT drop the
                # condition (that would fan out to the whole base) — emit a false clause.
                clauses.append("0")
            else:
                clauses.append("season = ?")
                params.append(value)
        elif field == "resume":
            # Квик 260911-0fh (RESUME-FILTER-01): must come BEFORE the generic
            # `_FILTER_COLUMNS` branch below — there is no `users.resume` column, the general
            # branch would emit `resume = ?` and blow up with `OperationalError`. Column names
            # come ONLY from `RESUME_COLUMNS` (code, not user input) — the "value never
            # interpolated" rule still holds, no binds at all are added by this branch.
            # `-` (прочерк) считается «не заполнено» — та же конвенция, что в
            # `reg_engine.has_prior_resume`/`prior_answers_for`.
            value = f.get("value")
            if value == RESUME_HAS:
                clauses.append(f"({_resume_has_fragment()})")
            elif value == RESUME_MISSING:
                clauses.append(f"({_resume_missing_fragment()})")
            else:
                # WR-01, same reasoning as event_city/season above: an empty or unknown value
                # must NOT drop the condition (that would fan out to the whole base).
                clauses.append("0")
        elif field == "auto_reject":
            # Phase 31 (31-02, D-28): must come BEFORE the generic `_FILTER_COLUMNS` branch
            # below — there is no `users.auto_reject` column, the general branch would emit
            # `auto_reject = ?` and blow up with `OperationalError`. Same fail-closed shape as
            # the `resume` branch above (WR-01).
            value = f.get("value")
            if value == AUTO_REJECT_YES:
                clauses.append(
                    "(auto_reject_rule_ids IS NOT NULL AND TRIM(auto_reject_rule_ids) NOT IN ('', '[]'))"
                )
            elif value == AUTO_REJECT_NO:
                clauses.append(
                    "(auto_reject_rule_ids IS NULL OR TRIM(auto_reject_rule_ids) IN ('', '[]'))"
                )
            else:
                clauses.append("0")
        elif field == "delegation_any":
            # Делегации вузов (D-07): must come BEFORE the generic `_FILTER_COLUMNS` branch
            # below — there is no `users.delegation_any` column, the condition is built on
            # `users.delegation`. Same fail-closed shape as `auto_reject` above (WR-01).
            value = f.get("value")
            if value == DELEGATION_YES:
                clauses.append("(delegation IS NOT NULL AND TRIM(delegation) != '')")
            elif value == DELEGATION_NO:
                clauses.append("(delegation IS NULL OR TRIM(delegation) = '')")
            else:
                clauses.append("0")
        elif field == "ext_form":
            # Внешние формы: форма едет внутри спеки (`form_id`), значение — белый список.
            # Fail closed (WR-01): неизвестное значение / битый form_id -> никому.
            value = f.get("value")
            try:
                form_id = int(f.get("form_id"))
            except (TypeError, ValueError):
                form_id = None
            if form_id is None or value not in ("filled", "not_filled"):
                clauses.append("0")
            else:
                neg = "NOT " if value == "not_filled" else ""
                clauses.append(
                    f"{neg}EXISTS (SELECT 1 FROM external_form_answers WHERE "
                    "matched_telegram_id = users.telegram_id AND form_id = ?)"
                )
                params.append(form_id)
        elif field == "delegate_chat":
            # Квик 260914-rgr (RGR-01..07, D-5/D-6): must come BEFORE the generic
            # `_FILTER_COLUMNS` branch below — there is no `users.delegate_chat` column.
            # Карта «город -> chat_id» едет ВНУТРИ спеки фильтра под ключом `chats` (список
            # `{"city": код|None, "chat_id": int, "exclude": [коды]}`), потому что этот модуль
            # не может импортировать `cities` (цикл) — тот же приём, что `exclude` у
            # `event_city`. Условие — ИЛИ по чатам, для каждого чата — «городской фрагмент»
            # (тот же `_city_clause`, что у `event_city`) И «присутствие/отсутствие»
            # (`EXISTS`/`NOT EXISTS` по `chat_members`). «Не в чате» НАМЕРЕННО ограничено
            # городами с привязанным чатом (D-6) — иначе рассылка «вступай в чат» уехала бы
            # тем, кому вступать некуда.
            value = f.get("value")
            chats = f.get("chats") or []
            if value not in (CHAT_IN, CHAT_OUT) or not chats:
                # WR-01: неизвестное/пустое значение или пустая карта чатов — fail closed,
                # НЕ «всем» (тот же довод, что у resume/event_city/season выше).
                clauses.append("0")
                continue
            present_ph = ",".join("?" for _ in CHAT_PRESENT_STATUSES)
            block_parts: list[str] = []
            block_params: list = []
            for chat in chats:
                chat_id = chat.get("chat_id")
                if chat_id is None:
                    continue
                city = chat.get("city")
                exclude = tuple(chat.get("exclude") or ())
                if city is None:
                    city_frag, city_params = "", []
                else:
                    city_frag, city_params = _city_clause((city, exclude))
                exists_frag = (
                    "EXISTS (SELECT 1 FROM chat_members cm WHERE "
                    "cm.telegram_id = users.telegram_id AND cm.chat_id = ? "
                    f"AND cm.status IN ({present_ph}))"
                )
                presence_frag = exists_frag if value == CHAT_IN else f"NOT {exists_frag}"
                if city_frag:
                    block_parts.append(f"({city_frag} AND {presence_frag})")
                    block_params.extend(city_params)
                else:
                    block_parts.append(f"({presence_frag})")
                block_params.append(chat_id)
                block_params.extend(CHAT_PRESENT_STATUSES)
            if not block_parts:
                clauses.append("0")
                continue
            clauses.append("(" + " OR ".join(block_parts) + ")")
            params.extend(block_params)
        elif field == "checkin_entry":
            # Форум-ночь п.6 (D-25, идея №14): «✅ Пришли на форум» / «❌ Не пришли». «Пришли» —
            # просто EXISTS отметки входа (только одобренные текущего сезона вообще МОГЛИ её
            # получить, D-02 — второй раз это условие здесь не проверяем). «Не пришли» — этого
            # НЕДОСТАТОЧНО инвертировать: NOT EXISTS сам по себе поймал бы ещё и отклонённых, и
            # ожидающих, и approved-делегатов ПРОШЛОГО сезона (482 импортированных 26/1 — им QR
            # вообще не выдаётся, D-02, и «мы тебя не видим на форуме» им писать нельзя). Поэтому
            # «Не пришли» = approved ТЕКУЩЕГО сезона (сезон — снимок `event_season` на МОМЕНТ
            # вызова, кладёт `_resolve_checkin_entry_season` в `count_and_list_filtered` НИЖЕ,
            # тот же приём, что `exclude` у `event_city`/`chats` у `delegate_chat` — пересчитан
            # заново на КАЖДЫЙ вызов, включая отложенную отправку) AND NOT EXISTS.
            value = f.get("value")
            day = f.get("day")
            if day == CHECKIN_DAY_TODAY:
                day = msk_now().strftime("%Y-%m-%d")
            exists_frag = (
                "EXISTS (SELECT 1 FROM checkins c WHERE c.telegram_id = users.telegram_id "
                "AND c.point = ?" + (" AND c.day = ?)" if day else ")")
            )
            exists_params = [CHECKIN_ENTRY_POINT] + ([day] if day else [])
            if value == CHECKIN_YES:
                clauses.append(exists_frag)
                params.extend(exists_params)
            elif value == CHECKIN_NO:
                guard_frag, guard_params = _approved_current_season_frag(f.get("event_season"))
                # Только города, где в этот день идёт форум (`forum_scopes` кладёт
                # `_resolve_checkin_entry_season`): иначе «не пришли сегодня» в день
                # регионального форума уходило и Москве. None — модуль городов выключен.
                scopes = f.get("forum_scopes")
                city_frag, city_params = "", []
                if scopes is not None:
                    parts = [_city_clause(tuple(sc) if sc else None) for sc in scopes]
                    city_frag = " AND (" + (" OR ".join(p for p, _ in parts) or "0") + ")"
                    city_params = [x for _, ps in parts for x in ps]
                clauses.append(f"({guard_frag} AND NOT {exists_frag}{city_frag})")
                params.extend(guard_params)
                params.extend(exists_params)
                params.extend(city_params)
            else:
                # WR-01, тот же довод, что у resume/event_city/season выше: неизвестное значение
                # — fail closed, не «всем».
                clauses.append("0")
        elif field == "checkin_session":
            # «Были на сессии …» / «Не были на сессии …» — `session_id` едет ВНУТРИ записи
            # фильтра (см. докстринг `SESSION_ATTENDED`/`SESSION_NOT_ATTENDED` выше).
            # `_invalid` (проставляет `_resolve_checkin_session_validity` в
            # `count_and_list_filtered` НИЖЕ) — сессия могла быть удалена между планированием и
            # отправкой: без этой проверки «не были» на несуществующей сессии совпало бы С КАЖДЫМ
            # (NOT EXISTS на point, которого никогда не было ни у кого) — тот же WR-01 fail-closed
            # довод, что у неизвестного event_city.
            # Гард «approved + текущий сезон» (`_approved_current_season_frag`, `event_season` —
            # снимок настройки, наполняет `_resolve_checkin_session_validity` НИЖЕ, тот же приём,
            # что `_resolve_checkin_entry_season`) — БЕЗ него «не были на сессии» ловил бы ещё и
            # pending/rejected/прошлый сезон, в точности та дыра, что у `checkin_entry` уже
            # закрыта. На «были на сессии» гард тоже стоит: отмеченный неодобренный/чужого
            # сезона — аномалия данных, но рассылка о программе форума должна оставаться
            # согласованной с «Отметка на форуме», не только «не были».
            value = f.get("value")
            session_id = f.get("session_id")
            if f.get("_invalid") or value not in (SESSION_ATTENDED, SESSION_NOT_ATTENDED) \
                    or not isinstance(session_id, int):
                clauses.append("0")
            else:
                exists_frag = (
                    "EXISTS (SELECT 1 FROM checkins c WHERE c.telegram_id = users.telegram_id "
                    "AND c.point = ?)"
                )
                presence_frag = exists_frag if value == SESSION_ATTENDED else f"NOT {exists_frag}"
                guard_frag, guard_params = _approved_current_season_frag(f.get("event_season"))
                clauses.append(f"({guard_frag} AND {presence_frag})")
                params.extend(guard_params)
                params.append(f"session:{session_id}")
        elif field == "payment_status" and f.get("value") in ("not_paid", "overdue"):
            # Делегаты вузов оплаты не имеют (у них payment_status остаётся дефолтным
            # not_paid) — в «не оплатили» им не место, напоминание об оплате им не уйдёт.
            clauses.append("(payment_status = ? AND delegation_answer_id IS NULL)")
            params.append(f.get("value"))
        elif field in _FILTER_COLUMNS:
            clauses.append(f"{field} = ?")
            params.append(f.get("value"))
        # non-whitelisted field → silently skipped (never interpolated)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# NOT USABLE FOR `event_city` (Phase 07.2, CITY-02): this function returns raw column values
# and, by construction (`IS NOT NULL AND TRIM(...) != ''`), drops NULL rows — so the DEFAULT
# city, under which every pre-cities application still sits as NULL, would simply not appear
# in the list of offered values, and a city with no applications yet would be unofferable.
# The source of city values for the picker is the registry (`cities.CITIES`), resolved on the
# `handlers/admin.py` side; this module cannot import `cities` (import cycle).
async def get_distinct_filter_values(field: str) -> list[str]:
    """Distinct non-empty values present in the users table for a whitelisted filter column —
    feeds the broadcast value picker (buttons pulled from real data, no free-text typing).
    `registration_date` returns distinct calendar dates (YYYY-MM-DD). Field is validated against
    the same whitelist as _build_filter_clause, so the f-string column is never user-derived."""
    if field == "registration_date":
        sql = (
            "SELECT DISTINCT date(registration_date) AS v FROM users "
            "WHERE registration_date IS NOT NULL AND TRIM(registration_date) != '' ORDER BY v"
        )
    elif field in _FILTER_COLUMNS and field not in _FILTER_VIRTUAL_FIELDS:
        sql = (
            f"SELECT DISTINCT {field} AS v FROM users "
            f"WHERE {field} IS NOT NULL AND TRIM({field}) != '' ORDER BY v"
        )
    else:
        # Квик 260911-0fh: виртуальные поля (`resume`) не читаются как обычная колонка — у
        # `resume` нет одноимённой колонки `users`, `SELECT DISTINCT resume` упал бы
        # `OperationalError`. Такое поле уходит сюда же, что и незарегистрированное.
        return []
    async with _connect() as db:
        async with db.execute(sql) as cursor:
            rows = await cursor.fetchall()
    return [str(r[0]) for r in rows if r[0] is not None and str(r[0]).strip()]


async def get_season_filter_options() -> list[str]:
    """Значения для пикера поля «Сезон» — квик 260910-vfl (SEASON-FILTER-03/04).

    Реальные сезоны берёт у `get_distinct_filter_values("season")` (та функция по построению
    отбрасывает NULL и пустые — поэтому легаси-строки без сезона в списке не появляются) и,
    если такие строки в базе реально есть, дописывает `SEASON_NONE` В КОНЕЦ списка отдельной
    кнопкой «Без сезона». Решение по требованию 3: легаси-строки показываем отдельным
    вариантом выбора, а не прячем — ветка в `_build_filter_clause` стоит трёх строк, а
    спрятанные строки означали бы аудиторию, до которой менеджеру не дотянуться ничем, кроме
    «Всем». Сентинел предлагается только когда такие строки реально есть — на базе без легаси
    экран не меняется.

    Длина этого списка — ровно то число, по которому экран решает, рисовать ли саму кнопку
    «Сезон» (`len(options) > 1`): фильтровать по одному сезону не по чему, кнопка была бы шумом.
    """
    options = await get_distinct_filter_values("season")
    async with _connect() as db:
        async with db.execute(
            "SELECT EXISTS(SELECT 1 FROM users WHERE season IS NULL OR TRIM(season) = '')"
        ) as cursor:
            row = await cursor.fetchone()
    if row and row[0]:
        options.append(SEASON_NONE)
    return options


async def get_resume_filter_options() -> list[str]:
    """Значения для пикера поля «Резюме» — квик 260911-0fh (RESUME-FILTER-01/06).

    ОДИН запрос — оба `EXISTS` на фрагментах из `_resume_has_fragment`/
    `_resume_missing_fragment` (тех же, что и ветка `_build_filter_clause`, второй копии
    условия не заводится). Возвращает `[RESUME_HAS]` и/или `[RESUME_MISSING]` в этом
    порядке, `[]` на пустой базе.

    Длина этого списка — ровно то число, по которому экран решает, рисовать ли кнопку
    «Резюме» (`len(options) > 1`): если все делегаты по одну сторону, фильтровать не по
    чему — «нет резюме у всех» это кнопка «Всем», а «есть у всех» дало бы пустую выборку.
    Тот же приём, что у `get_season_filter_options`.
    """
    async with _connect() as db:
        async with db.execute(
            f"SELECT EXISTS(SELECT 1 FROM users WHERE {_resume_has_fragment()}), "
            f"EXISTS(SELECT 1 FROM users WHERE {_resume_missing_fragment()})"
        ) as cursor:
            row = await cursor.fetchone()
    options: list[str] = []
    if row and row[0]:
        options.append(RESUME_HAS)
    if row and row[1]:
        options.append(RESUME_MISSING)
    return options


async def get_auto_reject_filter_options() -> list[str]:
    """Значения для пикера поля «Автоотказ» (Phase 31, 31-02, D-28) — та же роль порога показа
    кнопки, что у `get_resume_filter_options`: оба сентинела только если в базе реально есть
    обе стороны, `[]` на пустой базе (фильтровать не по чему)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT EXISTS(SELECT 1 FROM users WHERE auto_reject_rule_ids IS NOT NULL AND "
            "TRIM(auto_reject_rule_ids) NOT IN ('', '[]')), "
            "EXISTS(SELECT 1 FROM users WHERE auto_reject_rule_ids IS NULL OR "
            "TRIM(auto_reject_rule_ids) IN ('', '[]'))"
        ) as cursor:
            row = await cursor.fetchone()
    options: list[str] = []
    if row and row[0]:
        options.append(AUTO_REJECT_YES)
    if row and row[1]:
        options.append(AUTO_REJECT_NO)
    return options


async def get_delegation_filter_options() -> list[str]:
    """Значения для пикера поля «Делегация вуза» (делегации вузов, D-07) — та же роль порога
    показа кнопки, что у `get_auto_reject_filter_options`: оба сентинела только если в базе
    реально есть обе стороны, `[]` на пустой базе (фильтровать не по чему)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT EXISTS(SELECT 1 FROM users WHERE delegation IS NOT NULL AND "
            "TRIM(delegation) != ''), "
            "EXISTS(SELECT 1 FROM users WHERE delegation IS NULL OR TRIM(delegation) = '')"
        ) as cursor:
            row = await cursor.fetchone()
    options: list[str] = []
    if row and row[0]:
        options.append(DELEGATION_YES)
    if row and row[1]:
        options.append(DELEGATION_NO)
    return options


async def get_chat_filter_options(chats: list[dict]) -> list[str]:
    """Значения для пикера поля «Чат делегатов» — квик 260914-rgr. Та же роль «порога показа
    кнопки», что у `get_resume_filter_options`/`get_season_filter_options`: `[CHAT_IN]` и/или
    `[CHAT_OUT]` только когда по обе стороны реально есть люди, `[]` при пустой карте чатов
    (фильтровать не по чему). Переиспользует ветку `_build_filter_clause` через саму себя —
    второй копии условия не заводится."""
    if not chats:
        return []
    where_in, params_in = _build_filter_clause(
        [{"field": "delegate_chat", "value": CHAT_IN, "chats": chats}]
    )
    where_out, params_out = _build_filter_clause(
        [{"field": "delegate_chat", "value": CHAT_OUT, "chats": chats}]
    )
    async with _connect() as db:
        async with db.execute(f"SELECT EXISTS(SELECT 1 FROM users{where_in})", params_in) as cursor:
            has_in = (await cursor.fetchone())[0]
        async with db.execute(f"SELECT EXISTS(SELECT 1 FROM users{where_out})", params_out) as cursor:
            has_out = (await cursor.fetchone())[0]
    options: list[str] = []
    if has_in:
        options.append(CHAT_IN)
    if has_out:
        options.append(CHAT_OUT)
    return options


async def get_checkin_entry_filter_options() -> list[str]:
    """Порог показа кнопки «Отметка на форуме» — та же роль, что у `get_chat_filter_options`
    выше: показываем сторону, только если по ней реально кто-то есть, иначе фильтровать не по
    чему (до дня форума `checkins` пуста — кнопка «Пришли» не появится вовсе)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT EXISTS(SELECT 1 FROM checkins WHERE point = ?)", (CHECKIN_ENTRY_POINT,),
        ) as cursor:
            has_yes = (await cursor.fetchone())[0]
    event_season = (await get_setting("event_season") or "").strip() or None
    season_frag = "(season IS NULL OR season = ?)" if event_season else "1=1"
    params = [event_season] if event_season else []
    async with _connect() as db:
        async with db.execute(
            f"SELECT EXISTS(SELECT 1 FROM users WHERE status = 'approved' AND {season_frag} "
            "AND NOT EXISTS (SELECT 1 FROM checkins c WHERE c.telegram_id = users.telegram_id "
            "AND c.point = ?))",
            [*params, CHECKIN_ENTRY_POINT],
        ) as cursor:
            has_no = (await cursor.fetchone())[0]
    options: list[str] = []
    if has_yes:
        options.append(CHECKIN_YES)
    if has_no:
        options.append(CHECKIN_NO)
    return options


async def get_checkin_entry_days() -> list[str]:
    """Дни форума («YYYY-MM-DD»), в которые был хоть один вход — для выбора дня в фильтре
    рассылки «Отметка на форуме»."""
    async with _connect() as db:
        async with db.execute(
            "SELECT DISTINCT day FROM checkins WHERE point = ? ORDER BY day",
            (CHECKIN_ENTRY_POINT,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall() if row[0]]


async def get_checkin_entry_picker_options() -> list[str]:
    """Варианты фильтра «Отметка на форуме» с НЕПУСТОЙ аудиторией: «пришли / не пришли» за
    форум (`yes`/`no`), сегодня (`yes@today`/`no@today`) и за каждый прошлый день со входами
    (`yes@YYYY-MM-DD`). Сторона без людей не показывается. Пустой список — входов ещё не было
    ни одного (до форума фильтровать не по чему) — кнопка поля скрыта.

    Порог не «обе стороны у одного варианта»: на второй день двухдневки все одобренные могли
    прийти в первый, а сегодня ещё никто — у каждого варианта одна сторона пустая, но «не
    пришли сегодня» (= все) — главный сценарий дня, кнопка обязана быть."""
    days = await get_checkin_entry_days()
    if not days:
        return []
    today = msk_now().strftime("%Y-%m-%d")
    event_season = (await get_setting("event_season") or "").strip() or None
    guard_frag, guard_params = _approved_current_season_frag(event_season)
    variants = [(None, None), (CHECKIN_DAY_TODAY, today)] + [(d, d) for d in days if d != today]
    options: list[str] = []
    async with _connect() as db:
        for key, day in variants:
            day_sql = " AND c.day = ?" if day else ""
            day_params = [day] if day else []
            async with db.execute(
                f"SELECT EXISTS(SELECT 1 FROM checkins c WHERE c.point = ?{day_sql})",
                [CHECKIN_ENTRY_POINT, *day_params],
            ) as cursor:
                has_yes = (await cursor.fetchone())[0]
            async with db.execute(
                f"SELECT EXISTS(SELECT 1 FROM users WHERE {guard_frag} AND NOT EXISTS "
                "(SELECT 1 FROM checkins c WHERE c.telegram_id = users.telegram_id "
                f"AND c.point = ?{day_sql}))",
                [*guard_params, CHECKIN_ENTRY_POINT, *day_params],
            ) as cursor:
                has_no = (await cursor.fetchone())[0]
            suffix = f"@{key}" if key else ""
            if has_yes:
                options.append(f"{CHECKIN_YES}{suffix}")
            if has_no:
                options.append(f"{CHECKIN_NO}{suffix}")
    return options


async def any_program_sessions_exist() -> bool:
    """Порог показа кнопок «Были на сессии …» / «Не были на сессии …» — прежде чем менеджер
    завёл хотя бы одну сессию программы (`handlers/admin_program.py`), фильтровать по сессиям
    не по чему."""
    async with _connect() as db:
        async with db.execute("SELECT EXISTS(SELECT 1 FROM program_sessions)") as cursor:
            row = await cursor.fetchone()
    return bool(row and row[0])


async def _resolve_checkin_entry_season(filters: list[dict]) -> list[dict]:
    """Наполняет `event_season` СНИМКОМ настройки на МОМЕНТ вызова для каждой записи
    `checkin_entry`=`CHECKIN_NO` («Не пришли») — тот же приём, что `cities.
    refresh_city_filter_spec` для `event_city.exclude`: пересчитывается заново на КАЖДЫЙ вызов
    (превью и отложенная отправка), а не замораживается на момент, когда менеджер нажал кнопку
    в мастере. Единая точка — здесь (внутри `count_and_list_filtered`), а не в каждом
    вызывающем месте, чтобы будущий третий вызывающий не забыл про пересчёт."""
    if not any(
        isinstance(f, dict) and f.get("field") == "checkin_entry" and f.get("value") == CHECKIN_NO
        for f in filters
    ):
        return filters
    event_season = (await get_setting("event_season") or "").strip() or None
    from services.forum_days import forum_city_scopes  # ленивый: модуль читает cities

    today = msk_now().date()
    out = []
    for f in filters:
        if isinstance(f, dict) and f.get("field") == "checkin_entry" and f.get("value") == CHECKIN_NO:
            day = f.get("day")
            if day == CHECKIN_DAY_TODAY:
                day = today.strftime("%Y-%m-%d")
            f = {**f, "event_season": event_season, "forum_scopes": await forum_city_scopes(day, today)}
        out.append(f)
    return out


async def _resolve_checkin_session_validity(filters: list[dict]) -> list[dict]:
    """WR-01-style fail-closed: помечает `checkin_session`-записи с уже удалённым
    `session_id` (`_invalid=True`) — без этой проверки удалённая между планированием и
    отправкой сессия молча превратила бы «не были на сессии X» во «все» (см. докстринг ветки
    `checkin_session` в `_build_filter_clause`). Заодно кладёт `event_season` СНИМКОМ настройки
    на МОМЕНТ вызова — тот же приём, что `_resolve_checkin_entry_season` выше, переиспользован
    здесь (не отдельная функция), потому что уже итерирует ровно те же записи `checkin_session`,
    которым нужен гард `status = 'approved' AND (season IS NULL OR season = ?)`
    (`_approved_current_season_frag`, обе ветки `attended`/`not_attended`)."""
    if not any(isinstance(f, dict) and f.get("field") == "checkin_session" for f in filters):
        return filters
    event_season = (await get_setting("event_season") or "").strip() or None
    cache: dict[int, bool] = {}
    result: list[dict] = []
    for f in filters:
        if isinstance(f, dict) and f.get("field") == "checkin_session":
            sid = f.get("session_id")
            valid = False
            if isinstance(sid, int):
                if sid not in cache:
                    cache[sid] = (await get_program_session(sid)) is not None
                valid = cache[sid]
            f = {**f, "event_season": event_season}
            result.append(f if valid else {**f, "_invalid": True})
        else:
            result.append(f)
    return result


async def count_and_list_filtered(filters: list[dict]) -> list[int]:
    """Materialize the matched telegram_id list; the count preview is len(...)."""
    filters = await _resolve_checkin_entry_season(filters)
    filters = await _resolve_checkin_session_validity(filters)
    where, params = _build_filter_clause(filters)
    # ME-04: if the caller supplied filter(s) but every one was dropped (non-whitelisted field
    # / malformed spec), `where` degenerates to empty and the query would fan out to ALL users.
    # A filtered broadcast must NEVER silently blast the whole base — the "all users" broadcast
    # has its own dedicated path. Fail safe to an empty audience.
    if filters and not where:
        logger.warning(
            "count_and_list_filtered: %d filter(s) supplied but none produced a valid clause — "
            "returning empty audience (refusing to fan out to all users)", len(filters)
        )
        return []
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM users{where}", params
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ── Квик 260914-rgr (RGR-01..07): учёт чата делегатов ─────────────────────────
#
# `CHAT_PRESENT_STATUSES` — единственный источник правды «состоит в чате» для всего проекта
# (D-8: `restricted` — «не в чате», та же конвенция, что `_membership_status_to_bool` в
# `handlers/registration.py`). `services`/`handlers`/дашборд читают её отсюда; `db.py` сам
# ничего из `services`/`cities` не импортирует (циклы).
CHAT_PRESENT_STATUSES = ("creator", "administrator", "member")

_CHAT_PRESENT_PLACEHOLDERS = ",".join("?" for _ in CHAT_PRESENT_STATUSES)


async def upsert_chat_member(chat_id: int, telegram_id: int, status: str, source: str, *,
                              joined_at: str | None = None, left_at: str | None = None) -> None:
    """UPSERT в `chat_members`. `joined_at` проставляется САМ при первом переходе в статус из
    `CHAT_PRESENT_STATUSES` (если явно не передан и ещё не проставлен раньше), `left_at` — при
    уходе из присутствия (если явно не передан) — оба не затирают прежнее значение на любом
    другом переходе (D-9: сообщение не хранится, только счётчики и таймлайн событий)."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        async with db.execute(
            "SELECT status, joined_at, left_at FROM chat_members WHERE chat_id = ? AND telegram_id = ?",
            (chat_id, telegram_id),
        ) as cursor:
            row = await cursor.fetchone()
        prev_status, prev_joined_at, prev_left_at = row if row else (None, None, None)
        was_present = prev_status in CHAT_PRESENT_STATUSES
        now_present = status in CHAT_PRESENT_STATUSES

        resolved_joined_at = joined_at or prev_joined_at
        if now_present and not resolved_joined_at:
            resolved_joined_at = now

        resolved_left_at = left_at if left_at is not None else prev_left_at
        if left_at is None and not now_present and was_present:
            resolved_left_at = now

        await db.execute(
            "INSERT INTO chat_members (chat_id, telegram_id, status, joined_at, left_at, "
            "updated_at, source) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, telegram_id) DO UPDATE SET status=excluded.status, "
            "joined_at=excluded.joined_at, left_at=excluded.left_at, "
            "updated_at=excluded.updated_at, source=excluded.source",
            (chat_id, telegram_id, status, resolved_joined_at, resolved_left_at, now, source),
        )
        # 29.09: колонка «В чате» в листе — событие в очередь при смене присутствия (и на первую
        # запись: «не проверено» -> «да»/«нет»). Одобренность отсекает сам SELECT — посторонние
        # из чата в очередь не попадают. Какой чат «свой» для делегата, решает джоба
        # (`chat_tracking.chat_cell_values`), значение всегда из базы — лишний пересчёт безвреден.
        # Сбой вставки не рвёт учёт чата: он важнее листа.
        if row is None or was_present != now_present:
            try:
                await db.execute(
                    "INSERT INTO sheet_chat_queue (telegram_id, created_at, next_try_at) "
                    "SELECT telegram_id, ?, ? FROM users WHERE telegram_id = ? AND status = 'approved'",
                    (now, now, telegram_id),
                )
            except Exception as e:
                logger.warning("sheet_chat_queue: не поставил событие для %s: %s", telegram_id, e)
        await db.commit()


async def log_chat_event(chat_id: int, telegram_id: int, event: str) -> None:
    """`event` — `join`/`leave`/`kick`. Только id и код события — никакого текста (D-9)."""
    ts = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "INSERT INTO chat_events (chat_id, telegram_id, event, ts) VALUES (?, ?, ?, ?)",
            (chat_id, telegram_id, event, ts),
        )
        await db.commit()


async def bump_chat_activity(chat_id: int, telegram_id: int, *, reply: bool, media: bool) -> None:
    """UPSERT счётчиков за СЕГОДНЯ (по Москве). Текст сообщения сюда не попадает вовсе —
    только факт (D-9)."""
    day = msk_now().strftime("%Y-%m-%d")
    async with _connect() as db:
        await db.execute(
            "INSERT INTO chat_activity (chat_id, telegram_id, day, messages, replies, media) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(chat_id, telegram_id, day) DO UPDATE SET "
            "messages = messages + 1, replies = replies + excluded.replies, "
            "media = media + excluded.media",
            (chat_id, telegram_id, day, 1 if reply else 0, 1 if media else 0),
        )
        await db.commit()


async def set_chat_bot_state(chat_id: int, status: str | None, can_delete: bool | None) -> None:
    """UPSERT состояния бота в группе. `status=None` — статус не меняем (очистка узнала только
    про право удалять, не про статус); `can_delete=None` — «не проверяли»."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    flag = None if can_delete is None else (1 if can_delete else 0)
    async with _connect() as db:
        await db.execute(
            "INSERT INTO chat_bot_state (chat_id, bot_status, can_delete, checked_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "bot_status = COALESCE(excluded.bot_status, chat_bot_state.bot_status), "
            "can_delete = excluded.can_delete, checked_at = excluded.checked_at",
            (chat_id, status, flag, now),
        )
        await db.commit()


async def get_chat_bot_state(chat_id: int) -> dict | None:
    """`{"chat_id", "bot_status", "can_delete", "checked_at"}` или `None` (бота не видели)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT chat_id, bot_status, can_delete, checked_at FROM chat_bot_state WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


# ── Квик 260927: живой рейтинг чата — журнал сообщений без текста, реакции, ники, админы ──

async def log_chat_message(chat_id: int, message_id: int, telegram_id: int, ts: str, *,
                           kind: str, text_len: int, reply_to_message_id: int | None,
                           reply_to_author_id: int | None, is_channel_post: bool = False,
                           reactions_extra: int = 0, source: str = "live") -> None:
    """Одна строка на сообщение. INSERT OR IGNORE по (chat_id, message_id): повторный апдейт
    не задваивает счёт. Текста здесь нет и быть не может — только длина (D-9)."""
    async with _connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, "
            "text_len, reply_to_message_id, reply_to_author_id, is_channel_post, "
            "reactions_extra, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, telegram_id, ts, kind, int(text_len), reply_to_message_id,
             reply_to_author_id, 1 if is_channel_post else 0, int(reactions_extra), source),
        )
        await db.commit()


async def update_chat_message_len(chat_id: int, message_id: int, text_len: int) -> None:
    """Правка сообщения меняет только длину — и только у уже записанной строки."""
    async with _connect() as db:
        await db.execute(
            "UPDATE chat_messages SET text_len = ? WHERE chat_id = ? AND message_id = ?",
            (int(text_len), chat_id, message_id),
        )
        await db.commit()


async def set_chat_reactions(chat_id: int, message_id: int, telegram_id: int,
                             reactions: list[str], ts: str) -> None:
    """Текущие реакции человека на сообщение — зеркало апдейта message_reaction (в нём всегда
    ПОЛНЫЙ новый набор): стираем тройку и пишем набор заново, одной транзакцией."""
    async with _connect() as db:
        await db.execute(
            "DELETE FROM chat_reactions WHERE chat_id = ? AND message_id = ? AND telegram_id = ?",
            (chat_id, message_id, telegram_id),
        )
        for reaction in dict.fromkeys(reactions):
            await db.execute(
                "INSERT OR IGNORE INTO chat_reactions (chat_id, message_id, telegram_id, reaction, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, message_id, telegram_id, reaction, ts),
            )
        await db.commit()


async def upsert_chat_username(telegram_id: int, username: str | None,
                               first_name: str | None = None) -> None:
    """@ник автора из Telegram (в анкете его может не быть) и его имя (first_name, без фамилии)
    — подпись на дашборде для тех, у кого ника нет. Пустое значение прежнее не стирает —
    человек мог просто написать с клиента, где ник не пришёл."""
    value = str(username or "").strip().lstrip("@") or None
    name = str(first_name or "").strip()[:64] or None
    if value is None and name is None:
        return
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "INSERT INTO chat_usernames (telegram_id, username, first_name, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
            "username = COALESCE(excluded.username, chat_usernames.username), "
            "first_name = COALESCE(excluded.first_name, chat_usernames.first_name), "
            "updated_at = excluded.updated_at",
            (telegram_id, value, name, now),
        )
        await db.commit()


async def replace_chat_admins(chat_id: int, telegram_ids) -> None:
    """Админы группы по итогу getChatAdministrators — список заменяется целиком."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute("DELETE FROM chat_admins WHERE chat_id = ?", (chat_id,))
        for tid in dict.fromkeys(telegram_ids):
            await db.execute(
                "INSERT OR IGNORE INTO chat_admins (chat_id, telegram_id, synced_at) VALUES (?, ?, ?)",
                (chat_id, tid, now),
            )
        await db.commit()


async def enqueue_chat_cleanup(chat_id: int, message_id: int, code: str, due_at: str) -> None:
    """Служебное уведомление в очередь отложенного удаления (повтор того же id — не дубль)."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_cleanup_queue (chat_id, message_id, code, due_at, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, message_id, code, due_at, now),
        )
        await db.commit()


async def due_chat_cleanup(now_ts: str) -> list[dict]:
    """Уведомления, чей срок удаления наступил: `{chat_id, message_id, code, created_at}`."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT chat_id, message_id, code, created_at FROM chat_cleanup_queue "
            "WHERE due_at <= ? ORDER BY due_at, chat_id, message_id",
            (now_ts,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def drop_chat_cleanup(chat_id: int, message_ids) -> None:
    ids = list(message_ids)
    if not ids:
        return
    async with _connect() as db:
        await db.executemany(
            "DELETE FROM chat_cleanup_queue WHERE chat_id = ? AND message_id = ?",
            [(chat_id, mid) for mid in ids],
        )
        await db.commit()


async def prune_chat_history(cutoff_ts: str) -> dict[str, int]:
    """Срок хранения истории рейтинга: сообщения старше `cutoff_ts`, реакции старше него или на
    уже удалённые сообщения, и ники тех, от кого в журнале не осталось ни сообщения, ни
    реакции (ник без следа в чате — лишние ПД). Возвращает счётчики для лога."""
    async with _connect() as db:
        cur = await db.execute("DELETE FROM chat_messages WHERE ts < ?", (cutoff_ts,))
        messages = cur.rowcount
        cur = await db.execute(
            "DELETE FROM chat_reactions WHERE ts < ? OR NOT EXISTS ("
            "SELECT 1 FROM chat_messages m WHERE m.chat_id = chat_reactions.chat_id "
            "AND m.message_id = chat_reactions.message_id)",
            (cutoff_ts,),
        )
        reactions = cur.rowcount
        cur = await db.execute(
            "DELETE FROM chat_usernames WHERE telegram_id NOT IN ("
            "SELECT telegram_id FROM chat_messages UNION SELECT telegram_id FROM chat_reactions)"
        )
        usernames = cur.rowcount
        await db.commit()
    return {"messages": messages, "reactions": reactions, "usernames": usernames}


async def chat_member_ids(chat_id: int) -> set[int]:
    """Множество telegram_id, реально присутствующих (`CHAT_PRESENT_STATUSES`) в чате."""
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM chat_members WHERE chat_id = ? "
            f"AND status IN ({_CHAT_PRESENT_PLACEHOLDERS})",
            (chat_id, *CHAT_PRESENT_STATUSES),
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0] for row in rows}


_IN_CHUNK = 500  # плейсхолдеров на один IN (…): держимся ниже лимита SQLite на параметры


async def users_status_city(telegram_ids: list[int]) -> dict[int, tuple[str | None, str | None]]:
    """{telegram_id: (status, event_city)} по списку id — чанками, для колонки «В чате»
    (`services.chat_tracking.chat_cell_values`). Незарегистрированных в ответе нет."""
    ids = [int(t) for t in telegram_ids]
    out: dict[int, tuple[str | None, str | None]] = {}
    async with _connect() as db:
        for i in range(0, len(ids), _IN_CHUNK):
            part = ids[i:i + _IN_CHUNK]
            placeholders = ",".join("?" for _ in part)
            async with db.execute(
                f"SELECT telegram_id, status, event_city FROM users WHERE telegram_id IN ({placeholders})",
                part,
            ) as cursor:
                for tid, status, city in await cursor.fetchall():
                    out[tid] = (status, city)
    return out


async def chat_member_statuses(chat_id: int, telegram_ids: list[int]) -> dict[int, str | None]:
    """{telegram_id: статус в `chat_members`} для тех из списка, у кого запись в чате есть."""
    ids = [int(t) for t in telegram_ids]
    out: dict[int, str | None] = {}
    async with _connect() as db:
        for i in range(0, len(ids), _IN_CHUNK):
            part = ids[i:i + _IN_CHUNK]
            placeholders = ",".join("?" for _ in part)
            async with db.execute(
                f"SELECT telegram_id, status FROM chat_members WHERE chat_id = ? "
                f"AND telegram_id IN ({placeholders})",
                (chat_id, *part),
            ) as cursor:
                for tid, status in await cursor.fetchall():
                    out[tid] = status
    return out


async def chat_member_row(chat_id: int, telegram_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chat_members WHERE chat_id = ? AND telegram_id = ?",
            (chat_id, telegram_id),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def stale_chat_member_candidates(chat_id: int, telegram_ids: list[int],
                                        older_than_ts: str) -> list[int]:
    """Кому из `telegram_ids` нужна сверка `getChatMember`: нет записи в `chat_members` ВООБЩЕ
    ИЛИ `updated_at` старше `older_than_ts` (строки формата `%Y-%m-%d %H:%M:%S` сравнимы
    лексикографически). Нужна периодической сверке (`services/chat_tracking.refresh_chat`)."""
    if not telegram_ids:
        return []
    async with _connect() as db:
        placeholders = ",".join("?" for _ in telegram_ids)
        async with db.execute(
            f"SELECT telegram_id, updated_at FROM chat_members WHERE chat_id = ? "
            f"AND telegram_id IN ({placeholders})",
            (chat_id, *telegram_ids),
        ) as cursor:
            rows = await cursor.fetchall()
    known = {row[0]: row[1] for row in rows}
    return [
        tid for tid in telegram_ids
        if tid not in known or not known[tid] or known[tid] < older_than_ts
    ]


async def chat_counts(chat_id: int, city_scope: tuple | None) -> dict:
    """Четыре сходящихся числа экрана «💬 Чат»: `approved` (одобрено в этом скоупе города),
    `in_chat`/`not_in_chat` (из них — в чате / не в чате) и `unknown_members` (присутствующие
    в чате `chat_members`, которых нет в `users` вовсе — «в чате, но не зарегистрированы»).
    `city_scope` — дескриптор `cities.city_scope(...)`, тот же приём, что у `_city_clause`
    везде (`db.py` не импортирует `cities` — цикл)."""
    frag, city_params = _city_clause(city_scope, "u.event_city")
    where_city = f" AND {frag}" if frag else ""
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM users u WHERE u.status = 'approved'{where_city}",
            city_params,
        ) as cursor:
            approved = (await cursor.fetchone())[0]
        async with db.execute(
            f"SELECT COUNT(*) FROM users u WHERE u.status = 'approved'{where_city} "
            "AND EXISTS (SELECT 1 FROM chat_members cm WHERE cm.chat_id = ? "
            f"AND cm.telegram_id = u.telegram_id AND cm.status IN ({_CHAT_PRESENT_PLACEHOLDERS}))",
            (*city_params, chat_id, *CHAT_PRESENT_STATUSES),
        ) as cursor:
            in_chat = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM chat_members cm WHERE cm.chat_id = ? "
            f"AND cm.status IN ({_CHAT_PRESENT_PLACEHOLDERS}) "
            "AND NOT EXISTS (SELECT 1 FROM users u WHERE u.telegram_id = cm.telegram_id)",
            (chat_id, *CHAT_PRESENT_STATUSES),
        ) as cursor:
            unknown_members = (await cursor.fetchone())[0]
    return {
        "approved": approved,
        "in_chat": in_chat,
        "not_in_chat": approved - in_chat,
        "unknown_members": unknown_members,
    }


async def chat_last_sync_at(chat_id: int) -> str | None:
    """Дата последней сверки/события по этому чату — MAX(`updated_at`) по `chat_members`.
    Правка 15.09: единственный вызывающий (экран «💬 Чат», `handlers/admin_chat.py`) снесён
    вместе с экраном — функция временно без вызывающих на стороне бота (веб-дашборд читает
    ту же дату своей независимой read-only копией, `dashboard/queries.py::chat_last_sync_at`,
    не эту — read-only периметр не импортирует `database/db.py`, см. докстринг того модуля).
    Оставлена как маленькая read-only утилита, не архитектурное расширение."""
    async with _connect() as db:
        async with db.execute(
            "SELECT MAX(updated_at) FROM chat_members WHERE chat_id = ?", (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


# Служебный аккаунт Telegram (автопересылки связанного канала): до квика 260927 его сообщения
# засчитывались в chat_activity как делегату — накопленные строки в итоги не входят.
TELEGRAM_SERVICE_USER_ID = 777000


async def chat_activity_totals(chat_id: int) -> dict:
    """Сумма `messages` за СЕГОДНЯ и за последние 7 дней (по Москве, включительно) — нужна
    `/chat_stats` (`handlers/group_chat.py`), чтобы админ, разбирающийся прямо в группе, не
    шёл за этими цифрами в бота отдельно. Служебный 777000 не в счёт."""
    today = msk_now().strftime("%Y-%m-%d")
    week_ago = (msk_now() - timedelta(days=6)).strftime("%Y-%m-%d")
    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(messages), 0) FROM chat_activity WHERE chat_id = ? AND day = ? "
            "AND telegram_id != ?",
            (chat_id, today, TELEGRAM_SERVICE_USER_ID),
        ) as cursor:
            today_total = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COALESCE(SUM(messages), 0) FROM chat_activity WHERE chat_id = ? AND day >= ? "
            "AND telegram_id != ?",
            (chat_id, week_ago, TELEGRAM_SERVICE_USER_ID),
        ) as cursor:
            week_total = (await cursor.fetchone())[0]
    return {"today": today_total, "week": week_total}


async def purge_chat_data(chat_id: int) -> None:
    """Отвязка чата из админки (задача 2): удаляет строки трёх таблиц по этому чату. Сам
    Telegram-чат и люди в нём не трогаются — это только локальные данные учёта."""
    async with _connect() as db:
        await db.execute("DELETE FROM chat_members WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM chat_activity WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM chat_events WHERE chat_id = ?", (chat_id,))
        # Квик 260927: журнал рейтинга этого чата. chat_usernames — общие на все чаты, не трогаем.
        await db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM chat_reactions WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM chat_admins WHERE chat_id = ?", (chat_id,))
        await db.commit()


# ── Phase 4: consent acceptances (CONS-01/02, D-02) ──────────────────────────

# Quick 260822: дефолт редакции согласия. Единственный источник — settings_schema берёт
# его отсюда для ключа consent_version (db.py не может импортировать реестр: цикл).
DEFAULT_CONSENT_VERSION = "1"


async def current_consent_version() -> str:
    """Текущая редакция согласия (настройка consent_version; пусто -> DEFAULT_CONSENT_VERSION)."""
    raw = await get_setting("consent_version")
    return (raw or "").strip() or DEFAULT_CONSENT_VERSION


async def record_user_consent(
    user_id: int, consent_key: str, consent_version: str | None = None, raw_button: str | None = None,
):
    """Idempotent consent write — re-tapping «Принимаю» never raises (INSERT OR IGNORE).
    Quick 260822: пишет редакцию согласия на момент подписи (по умолчанию — текущая
    consent_version); повтор того же (user, key, version) дедупится, новая редакция — новая
    строка. Quick 260907-4ai: `raw_button` — снимок текста нажатой кнопки (не перечитывается
    из настройки); опционален, старые вызовы без аргумента пишут NULL."""
    accepted_at = msk_now().isoformat()
    if consent_version is None:
        consent_version = await current_consent_version()
    async with _connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_consents (user_id, consent_key, accepted_at, consent_version, raw_button) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, consent_key, accepted_at, consent_version, raw_button),
        )
        await db.commit()


async def get_user_consents(user_id: int) -> list[str]:
    async with _connect() as db:
        async with db.execute(
            "SELECT consent_key FROM user_consents WHERE user_id = ? ORDER BY accepted_at",
            (user_id,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_user_consent_versions(user_id: int) -> list[tuple[str, str | None]]:
    """Quick 260822: все подписи делегата как [(consent_key, consent_version)] в порядке
    записи; NULL-версия = строка до версионирования."""
    async with _connect() as db:
        async with db.execute(
            "SELECT consent_key, consent_version FROM user_consents WHERE user_id = ? ORDER BY id",
            (user_id,),
        ) as cursor:
            return [(row[0], row[1]) for row in await cursor.fetchall()]


# ── Phase 4: payment receipt queue + status (PAY-05, D-10/D-12) ──────────────

async def get_receipt_pending_users(limit: int = 50, offset: int = 0, *, city_scope=None) -> list[dict]:
    """Users awaiting receipt verification, oldest first (tinder queue source).

    Phase 09.3 (09.3-02, CITY-08): `event_city` added to the SELECT list — the ALL_CITIES
    receipt card needs to name each row's own city, which requires the raw column on the row
    (previously not selected; the scope filter used it via WHERE without returning it)."""
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, full_name, payment_option, receipt_file_id, payment_status, event_city "
            f"FROM users WHERE payment_status = 'receipt_sent'{extra} ORDER BY rowid LIMIT ? OFFSET ?",
            (*city_params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_receipt_pending_count(*, city_scope=None) -> int:
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM users WHERE payment_status = 'receipt_sent'{extra}",
            tuple(city_params),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def update_payment_status(
    telegram_id: int, status: str, *, require_status: str | None = None, **kwargs
) -> int:
    """Transition one user's payment_status; returns cursor.rowcount.

    Confirm guard (STRIDE T-04-05-02): status='paid' only flips a row currently in
    'receipt_sent' — a second concurrent confirm matches 0 rows (rowcount=0 is the
    double-confirm signal the admin handler relies on).

    Reject guard (H-01): pass require_status='receipt_sent' to add the same conditional
    WHERE to a not_paid transition — so a stale/already-confirmed card tapped ❌ Отклонить
    cannot flip a 'paid' row back to 'not_paid' (rowcount=0 signals no-op to the handler).
    Callers that legitimately reset unconditionally (payment-option pick) omit it.

    Additive UPDATE only — never INSERT OR REPLACE."""
    sets = ["payment_status = ?"]
    params: list = [status]
    extras = dict(kwargs)
    if status == "paid" and "paid_at" not in extras:
        extras["paid_at"] = msk_now().isoformat()
    for col in ("receipt_file_id", "paid_at", "payment_option", "payment_due"):
        if col in extras:
            sets.append(f"{col} = ?")
            params.append(extras[col])
    # status='paid' keeps its historical hard-coded guard unless the caller overrides it.
    guard = require_status if require_status is not None else ("receipt_sent" if status == "paid" else None)
    where = "telegram_id = ?"
    params.append(telegram_id)
    if guard is not None:
        where += " AND payment_status = ?"
        params.append(guard)
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE {where}", params
        )
        await db.commit()
        return cursor.rowcount


async def set_payment_due(telegram_id: int, payment_due: str) -> None:
    """WR-03: persist the deadline a user owes payment by, WITHOUT touching payment_status.
    Lets the overdue sweep catch users who deferred from the option picker (payment_option NULL)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET payment_due = ? WHERE telegram_id = ?",
            (payment_due, telegram_id),
        )
        await db.commit()


# ── Phase 8 (ROLE-02, D-11): staff roster accessors ─────────────────────────────────────

async def add_staff(
    telegram_id: int, role: str, added_by: int | None, expires_at: str | None = None,
) -> bool:
    """Grant `role` to `telegram_id`. INSERT OR IGNORE against the composite PRIMARY KEY
    (telegram_id, role) makes re-adding an already-held role a no-op, not a duplicate row --
    an already-held role's `expires_at` is NOT touched by a no-op call (Идея №5: «если у
    человека уже есть роль шире -- не понижать» -- a repeat grant, including via the volunteer
    invite link, never shortens/extends an existing grant). Returns True iff this call
    actually inserted a new row.

    `expires_at` (Идея №6, `_ensure_column` above) -- ISO «YYYY-MM-DD» date or None
    (бессрочно). Optional kwarg, every pre-existing call site keeps granting an unlimited
    role, byte-identical to before this column existed."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO staff (telegram_id, role, added_by, added_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, role, added_by, datetime.utcnow().isoformat(), expires_at),
        )
        await db.commit()
        return cursor.rowcount == 1


async def remove_staff(telegram_id: int, role: str) -> bool:
    """Revoke `role` from `telegram_id`. Returns True iff a row was actually removed."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM staff WHERE telegram_id = ? AND role = ?",
            (telegram_id, role),
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_staff_expiry(telegram_id: int, role: str, expires_at: str | None) -> bool:
    """Идея №6: поменять (или снять, `expires_at=None`) срок у уже выданной роли -- ОДНОЙ
    (telegram_id, role) строки, не всех ролей человека (в отличие от `set_staff_city`, срок у
    разных ролей одного человека может отличаться -- см. докстринг миграции колонки). Returns
    True iff a matching row existed."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE staff SET expires_at = ? WHERE telegram_id = ? AND role = ?",
            (expires_at, telegram_id, role),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_staff_roles(telegram_id: int) -> list[str]:
    """All CURRENTLY ACTIVE roles held by one person (empty list if they hold none). Идея №6
    (D-6): «истёкшая роль не даёт НИКАКИХ прав» -- filtered here, the ONE place
    `handlers.admin_caps.resolve_capabilities` reads roles from, so the expiry check applies
    to every capability decision downstream without touching admin_caps.py itself. A row is
    active when `expires_at` is NULL (бессрочно) or still >= today (действует ПО этот день
    включительно) -- string comparison is safe because both sides are ISO `YYYY-MM-DD`."""
    today = msk_now().date().isoformat()
    async with _connect() as db:
        async with db.execute(
            "SELECT role FROM staff WHERE telegram_id = ? "
            "AND (expires_at IS NULL OR expires_at >= ?)",
            (telegram_id, today),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def list_staff() -> list[dict]:
    """Full roster, oldest grant first -- feeds the "Роли и доступы" admin screen (08-02).
    `city` (Phase 09.1, C) is NULL for every pre-existing row -- "all cities", byte-identical
    to today's behavior for anyone who never gets a binding. Идея №6: includes EXPIRED rows
    too (unlike `get_staff_roles`, which is a capability-resolution primitive) -- D-6 «ничего
    не удаляем», the roster screen renders «⌛ истекла 04.10» for a past `expires_at`, not a
    hole where the person used to be."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, role, added_by, added_at, city, expires_at "
            "FROM staff ORDER BY added_at"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ── 29.09: сотрудник, до которого не доходят уведомления ──────────────────────────────────

async def mark_staff_unreachable(telegram_id: int, reason: str | None) -> bool:
    """Отметить «бот не может ему написать». True — отметка новая; повтор только обновляет
    `last_failed_at`/`reason`, `since` остаётся датой ПЕРВОЙ неудачи."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO staff_unreachable (telegram_id, since, last_failed_at, reason) "
            "VALUES (?, ?, ?, ?)",
            (telegram_id, now, now, reason),
        )
        inserted = cursor.rowcount == 1
        if not inserted:
            await db.execute(
                "UPDATE staff_unreachable SET last_failed_at = ?, reason = ? WHERE telegram_id = ?",
                (now, reason, telegram_id),
            )
        await db.commit()
        return inserted


async def clear_staff_unreachable(telegram_id: int) -> bool:
    """Снять отметку. True — отметка была."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM staff_unreachable WHERE telegram_id = ?", (telegram_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_staff_unreachable() -> dict[int, dict]:
    """`{telegram_id: {"since", "last_failed_at", "reason"}}` — все текущие отметки."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, since, last_failed_at, reason FROM staff_unreachable"
        ) as cursor:
            rows = await cursor.fetchall()
    return {row["telegram_id"]: dict(row) for row in rows}


# ── Phase 09.1 (C, ROLE-03): manager <-> city binding accessors ────────────────────────────

async def get_staff_city(telegram_id: int) -> str | None:
    """The city bound to this person, or None (unbound -- "all cities", same as every
    pre-09.1 record). Binding is per-person, not per-role -- any one of their role-rows
    carrying a non-NULL city is enough to answer (set_staff_city keeps them all in sync)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT city FROM staff WHERE telegram_id = ? AND city IS NOT NULL LIMIT 1",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_staff_city(telegram_id: int, city: str | None) -> bool:
    """Bind (or clear, when `city` is None) this person's city across EVERY role-row they
    hold in one statement -- the binding is by telegram_id alone (CONTEXT.md C), not by
    (telegram_id, role). Returns True iff at least one row existed to update; a person with
    no staff row at all gets False and nothing is written."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE staff SET city = ? WHERE telegram_id = ?", (city, telegram_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_staff_ids_by_role(role: str) -> list[int]:
    """Every telegram_id currently holding exactly this role -- feeds notification fan-out
    (D-13, wired in a later phase-8 plan). Идея №6: expired rows excluded -- an expired
    volunteer must not keep receiving `checkin`-gated fan-out (e.g. a post-forum broadcast to
    capability_holders) even though the DB row is kept for history."""
    today = msk_now().date().isoformat()
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM staff WHERE role = ? "
            "AND (expires_at IS NULL OR expires_at >= ?)",
            (role, today),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


# ── Идея №5 бэклога чек-ина: приглашение волонтёров ссылкой ────────────────────────────────

async def create_volunteer_invite(
    code: str, city: str | None, created_by: int | None,
    link_expires_at: str | None, rights_expires_at: str | None, max_uses: int | None,
    *, role: str | None = None,
) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO volunteer_invites "
            "(code, city, created_by, created_at, link_expires_at, rights_expires_at, "
            "max_uses, used, revoked, role) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
            (
                code, city, created_by, msk_now().strftime("%Y-%m-%d %H:%M:%S"),
                link_expires_at, rights_expires_at, max_uses, role,
            ),
        )
        await db.commit()


async def get_volunteer_invite(code: str) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM volunteer_invites WHERE code = ?", (code,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_volunteer_invites(
    city: str | None = None, created_by: int | None = None,
) -> list[dict]:
    """`city`/`created_by` -- optional AND-filters (both None = every invite ever created).
    Newest first -- the manager screen cares about recent links, not archaeology."""
    query = "SELECT * FROM volunteer_invites"
    conds: list[str] = []
    params: list = []
    if city is not None:
        conds.append("city = ?")
        params.append(city)
    if created_by is not None:
        conds.append("created_by = ?")
        params.append(created_by)
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY created_at DESC"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def revoke_volunteer_invite(code: str) -> bool:
    """Ссылка перестаёт открывать новые слоты (`claim_volunteer_invite` -> "revoked"); уже
    выданные роли волонтёрам, которые успели пройти, НЕ снимает -- снимать их по одному менеджер
    может отдельной кнопкой у каждого имени в списке вошедших (`remove_staff`)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE volunteer_invites SET revoked = 1 WHERE code = ? AND revoked = 0",
            (code,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_volunteer_invite_uses(code: str) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT invite_code, telegram_id, used_at FROM volunteer_invite_uses "
            "WHERE invite_code = ? ORDER BY used_at",
            (code,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def claim_volunteer_invite(code: str, telegram_id: int) -> str:
    """Атомарная попытка занять один слот приглашения. Возвращает машинный код исхода:
    "not_found" | "revoked" | "link_expired" | "already_used" | "exhausted" | "ok".

    Обе гонки закрыты ОДНИМ атомарным UPDATE (не read-then-write из Python):
    - "гонка двух переходов на последний слот" -- `used < max_uses` в WHERE самого UPDATE.
      SQLite сериализует запись по файлу (WAL: один писатель разом) -- вторая параллельная
      попытка блокируется до commit первой и затем перечитывает УЖЕ увеличенный `used` в
      своём собственном WHERE, а не устаревшее значение, увиденное более ранним `SELECT`.
    - "двойной переход не жжёт второй слот" -- `NOT EXISTS (... volunteer_invite_uses ...)` в
      том же WHERE. INSERT в `volunteer_invite_uses` происходит В ТОЙ ЖЕ транзакции (до
      `commit()`), поэтому вторая попытка того же telegram_id видит уже вставленную строку и
      её UPDATE не совпадает ни с одной строкой (rowcount=0) -- не тот же псевдо-race, что
      описан выше для чужих слотов, а его же механизм, примененный к дублю самого себя."""
    today = msk_now().date().isoformat()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT revoked, link_expires_at FROM volunteer_invites WHERE code = ?", (code,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return "not_found"
        if row["revoked"]:
            return "revoked"
        link_expires_at = row["link_expires_at"]
        if link_expires_at and link_expires_at < today:
            return "link_expired"

        upd = await db.execute(
            """
            UPDATE volunteer_invites
            SET used = used + 1
            WHERE code = ?
              AND revoked = 0
              AND (link_expires_at IS NULL OR link_expires_at >= ?)
              AND (max_uses IS NULL OR used < max_uses)
              AND NOT EXISTS (
                  SELECT 1 FROM volunteer_invite_uses u
                  WHERE u.invite_code = volunteer_invites.code AND u.telegram_id = ?
              )
            """,
            (code, today, telegram_id),
        )
        if upd.rowcount == 0:
            await db.commit()
            async with db.execute(
                "SELECT 1 FROM volunteer_invite_uses WHERE invite_code = ? AND telegram_id = ?",
                (code, telegram_id),
            ) as cursor2:
                already = await cursor2.fetchone()
            return "already_used" if already else "exhausted"

        await db.execute(
            "INSERT OR IGNORE INTO volunteer_invite_uses (invite_code, telegram_id, used_at) "
            "VALUES (?, ?, ?)",
            (code, telegram_id, msk_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
        return "ok"


# ── Phase 8 (ROLE-01, D-13/D-14): delegate_questions accessors ─────────────────────────────

async def create_question(user_id: int, question_text: str) -> int:
    """One row per delegate question, created ONCE before the D-13 fan-out (never per
    recipient -- 08-RESEARCH Pitfall 6). Returns the new row's id, embedded in every fanned-
    out copy of the notification so any recipient's reply resolves to the same claim target."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO delegate_questions (user_id, question_text, asked_at) VALUES (?, ?, ?)",
            (user_id, question_text, datetime.utcnow().isoformat()),
        )
        await db.commit()
    # Quick 260902-vth: та же врезка, что у record_answer_history (см. её комментарий) — сюда
    # заходит только источник «вопрос делегата», второй источник правки анкеты не касается.
    try:
        from services.sheet_logs import schedule_sheet_logs_sync
        schedule_sheet_logs_sync()
    except Exception as e:
        logger.warning("sheet_logs autosync scheduling after create_question failed: %s", e)
    return cursor.lastrowid


async def get_question(question_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM delegate_questions WHERE id = ?", (question_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def claim_question(question_id: int, admin_id: int, admin_name: str) -> bool:
    """Atomic single-row claim (D-14, same idiom as approve_user_atomic): True iff THIS call
    flipped the row (rowcount==1) -- a concurrent second claim on the same question_id
    returns False, and the loser reads answered_by_name back via get_question()."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE delegate_questions SET answered_by = ?, answered_by_name = ?, "
            "answered_at = ? WHERE id = ? AND answered_by IS NULL",
            (admin_id, admin_name, datetime.utcnow().isoformat(), question_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_question_answer(question_id: int, answer_text: str):
    """Record the answer text AND stamp delivered_at together -- this is only ever called
    after `bot.send_message`/`send_copy` to the delegate has actually SUCCEEDED (T-08-33
    quick task). delivered_at, not answer_text, is the detector `get_stuck_questions()`
    relies on -- see the column's comment in init_db for why answer_text alone can't do it."""
    async with _connect() as db:
        await db.execute(
            "UPDATE delegate_questions SET answer_text = ?, delivered_at = ? WHERE id = ?",
            (answer_text, datetime.utcnow().isoformat(), question_id),
        )
        await db.commit()


async def get_stuck_questions() -> list[dict]:
    """T-08-33 quick task, part D: rows that were claimed (answered_by set) but never
    successfully delivered (delivered_at still NULL) -- the admin "stuck questions" screen.
    Deliberately does NOT look at answer_text (see set_question_answer's docstring)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM delegate_questions "
            "WHERE answered_by IS NOT NULL AND delivered_at IS NULL "
            "ORDER BY answered_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def list_questions(limit: int = 5000) -> list[dict]:
    """Quick 260902-vth: весь журнал вопросов делегатов (все статусы, не только «застрявшие»,
    как `get_stuck_questions`) для полной пересборки листа «Вопросы» — лист пересобирается
    целиком, поэтому нужен весь журнал, а не хвост. `limit` — предохранитель от бесконечной
    выгрузки, не постраничность."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM delegate_questions ORDER BY id ASC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ── Quick 260904-2cj (QJRN-01/02/03/04): постраничный журнал вопросов делегатов ─────────────
#
# ЗЕРКАЛО `services.questions.question_status` — три фрагмента WHERE ниже обязаны отвечать
# ТОЧНО так же, как чистая функция статуса, для каждой строки; расхождение ловит паритет-тест
# `tests/test_questions_journal_260904.py`. Второй карты «статус вопроса» в проекте нет и не
# будет — правило объявлено ОДИН раз в services/questions.py, это только SQL-версия того же
# правила для фильтрации/подсчёта прямо в базе (без выгрузки всего журнала в Python).
_QUESTION_STATUS_SQL = {
    "new": "q.answered_by IS NULL",
    "in_work": "q.answered_by IS NOT NULL AND q.delivered_at IS NULL",
    "answered": "q.delivered_at IS NOT NULL",
}


_QUESTION_ORDER_SQL = {
    # Квик 260919 (P3, находка #03-moderation): раньше «all»/"answered" оба падали в один
    # `ORDER BY q.id DESC` — на проде 53 из 123 вопросов без ответа (старейшему 33 дня)
    # уезжали вглубь пагинации за свежими. Каждый статус теперь несёт СВОЙ порядок:
    "new": "ORDER BY q.asked_at ASC, q.id ASC",  # дольше ждёт -> выше (старые без ответа первыми)
    "in_work": "ORDER BY q.answered_at ASC, q.id ASC",  # не менялось: «залипло дольше всех» первым
    "answered": "ORDER BY q.delivered_at DESC, q.id DESC",  # свежие ответы сверху
}
# `status is None`/неизвестный ("all"): смешанный список — сначала НЕотвеченные
# (delivered_at IS NULL, покрывает и "new", и "in_work" разом — обеим ждать ответа делегату),
# среди них старые первыми (asked_at ASC); отвеченные — ниже, свежие первыми (delivered_at
# DESC). Один `ORDER BY` с тремя ключами: первый ключ разводит две группы (0 = не отвечен,
# 1 = отвечен), CASE-выражения дают каждой группе свой ключ и свою прежнюю логику; NULL в
# "чужом" для строки CASE безопасен — группа уже разведена первым ключом, второй ей не важен.
_QUESTION_ORDER_ALL_SQL = (
    "ORDER BY (q.delivered_at IS NOT NULL) ASC, "
    "CASE WHEN q.delivered_at IS NULL THEN q.asked_at END ASC, "
    "CASE WHEN q.delivered_at IS NOT NULL THEN q.delivered_at END DESC, "
    "q.id ASC"
)


async def list_questions_page(*, status: str | None = None, city_scope=None,
                               limit: int = 6, offset: int = 0) -> list[dict]:
    """Страница журнала вопросов для экрана бота и API Mini App. Неизвестный `status`
    трактуется как None (фильтр — чип экрана, а не контракт: тот же приём, что `track_filter`
    в `miniapp/routers/applications.py`). Городской фильтр — по `u.event_city` (город
    ДЕЛЕГАТА, не менеджера), LEFT JOIN не роняет вопрос делегата, которого уже нет в `users`.

    Порядок (квик 260919, P3): у каждого статуса-фильтра свой (`_QUESTION_ORDER_SQL`); «all»/
    неизвестный статус — смешанный порядок `_QUESTION_ORDER_ALL_SQL` (неотвеченные сверху,
    старые первыми; отвеченные ниже, свежие первыми) — см. докстринг константы выше."""
    where = []
    params: list = []
    frag = _QUESTION_STATUS_SQL.get(status)
    if frag:
        where.append(frag)
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    if city_frag:
        where.append(city_frag)
        params.extend(city_params)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_sql = _QUESTION_ORDER_SQL.get(status, _QUESTION_ORDER_ALL_SQL)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT q.*, u.full_name AS user_full_name, u.username AS user_username, "
            "u.event_city AS user_event_city "
            "FROM delegate_questions q LEFT JOIN users u ON u.telegram_id = q.user_id "
            f"{where_sql} {order_sql} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def count_questions_by_status(*, city_scope=None) -> dict[str, int]:
    """Один запрос, три `SUM(CASE …)` по тем же фрагментам `_QUESTION_STATUS_SQL` плюс
    `COUNT(*)` в "all" (`all` == сумме трёх — паритет закреплён тестом)."""
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    where_sql = f"WHERE {city_frag}" if city_frag else ""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*), "
            f"SUM(CASE WHEN {_QUESTION_STATUS_SQL['new']} THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {_QUESTION_STATUS_SQL['in_work']} THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {_QUESTION_STATUS_SQL['answered']} THEN 1 ELSE 0 END) "
            "FROM delegate_questions q LEFT JOIN users u ON u.telegram_id = q.user_id "
            f"{where_sql}",
            tuple(city_params),
        ) as cursor:
            row = await cursor.fetchone()
    total, new_n, in_work_n, answered_n = row if row else (0, 0, 0, 0)
    return {
        "all": int(total or 0),
        "new": int(new_n or 0),
        "in_work": int(in_work_n or 0),
        "answered": int(answered_n or 0),
    }


# ── Форум-ночь п.8 (идея №19, SOS): sos_reports аксессоры ───────────────────────────────────
#
# Та же форма, что «Phase 8 (ROLE-01, D-13/D-14): delegate_questions accessors» выше — строка
# создаётся ОДИН раз, атомарный захват (`claim_sos_report`) переворачивает `claimed_by` только
# из NULL (T-08-33/D-14 идиома), `resolve_sos_report` закрывает случай «✅ Решено» без
# предварительного «Беру» (COALESCE подставляет резолвера захватчиком одним UPDATE).
#
# D-31 («SOS без категорий»): `create_sos_report` больше не принимает `category` — карточка
# публикуется МГНОВЕННО (без вопроса «что случилось»), `details_text`/`details_photo_file_id`
# заполняются ПОЗЖЕ, в режиме «дописываю SOS» (`add_sos_details` ниже), `latitude`/`longitude`
# опциональны на входе по той же причине (геопозиция чаще приходит уже после карточки,
# `set_sos_location`).

async def create_sos_report(
    telegram_id: int, city: str | None, details_text: str | None = None,
    details_photo_file_id: str | None = None, latitude: float | None = None,
    longitude: float | None = None, *, prior_open_report_id: int | None = None,
) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO sos_reports (telegram_id, city, details_text, "
            "details_photo_file_id, latitude, longitude, created_at, prior_open_report_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, city, details_text, details_photo_file_id, latitude,
             longitude, msk_now().strftime("%Y-%m-%d %H:%M:%S"), prior_open_report_id),
        )
        await db.commit()
        return cursor.lastrowid


async def add_sos_details(report_id: int, *, text: str | None = None,
                           photo_file_id: str | None = None) -> bool:
    """Режим «дописываю SOS» (`handlers/sos.py::SosReport.collecting`, D-31) — первый текст/
    фото делегата садится в карточку (`services.sos.render_card_text` снимает пометку «подробности
    ещё не прислали»); КАЖДОЕ поле — первый непустой раз побеждает (`WHERE ... IS NULL`), дальше
    сообщения делегата всё равно уходят в тред карточки (`services.sos.relay_delegate_message`),
    просто не переписывают уже сохранённые подробности.

    Возвращает True, если в карточку лёг именно ЭТОТ текст (`rowcount`). Решать «встанет ли мой
    текст» по прочитанной заранее строке нельзя: два быстрых сообщения делегата обрабатываются
    параллельно, оба видят пустые подробности, а записывается только первое — второе без этого
    ответа терялось целиком (ни в карточке, ни в треде)."""
    text_landed = False
    async with _connect() as db:
        if text:
            cursor = await db.execute(
                "UPDATE sos_reports SET details_text = ? WHERE id = ? AND details_text IS NULL",
                (text, report_id),
            )
            text_landed = cursor.rowcount == 1
        if photo_file_id:
            await db.execute(
                "UPDATE sos_reports SET details_photo_file_id = ? WHERE id = ? "
                "AND details_photo_file_id IS NULL",
                (photo_file_id, report_id),
            )
        await db.commit()
    return text_landed


async def mark_sos_collecting_started(report_id: int) -> None:
    """Делегат вошёл в режим «дописываю SOS» по этой заявке (новой или переоткрытой)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE sos_reports SET collecting_started_at = ? WHERE id = ?",
            (msk_now().strftime("%Y-%m-%d %H:%M:%S"), report_id),
        )
        await db.commit()


async def set_sos_location(report_id: int, latitude: float, longitude: float) -> None:
    """Геопозиция в режиме «дописываю SOS» — в отличие от `add_sos_details` ПЕРЕЗАПИСЫВАЕТ
    координаты при повторной отправке (делегат мог сдвинуться, «последняя известная точка»
    полезнее первой, в отличие от текстового описания)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE sos_reports SET latitude = ?, longitude = ? WHERE id = ?",
            (latitude, longitude, report_id),
        )
        await db.commit()


async def get_sos_report(report_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sos_reports WHERE id = ?", (report_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_open_sos_report(telegram_id: int) -> dict | None:
    """Анти-спам (пункт 1 плана): «не чаще одного открытого SOS на делегата» — открытый значит
    ещё не решённый (`resolved_at IS NULL`), взятый в работу тоже считается открытым. Последняя
    (`ORDER BY id DESC`) — если строк несколько (не должно, но fail-soft на случай гонки)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sos_reports WHERE telegram_id = ? AND resolved_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_sos_card(report_id: int, chat_id: int, message_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE sos_reports SET chat_id = ?, card_message_id = ? WHERE id = ?",
            (chat_id, message_id, report_id),
        )
        await db.commit()


async def add_sos_card_copy(report_id: int, chat_id: int, message_id: int) -> None:
    """Копия карточки в личке админа (фоллбэк без чата). Повторная рассылка тому же
    админу (ретрай доставки) заменяет message_id — перерисовывать нужно последнюю копию."""
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO sos_card_copies (report_id, chat_id, message_id) "
            "VALUES (?, ?, ?)",
            (report_id, chat_id, message_id),
        )
        await db.commit()


async def list_sos_card_copies(report_id: int) -> list[tuple[int, int]]:
    async with _connect() as db:
        async with db.execute(
            "SELECT chat_id, message_id FROM sos_card_copies WHERE report_id = ?", (report_id,),
        ) as cursor:
            return [(int(r[0]), int(r[1])) for r in await cursor.fetchall()]


async def add_sos_relay_message(report_id: int, chat_id: int, message_id: int) -> None:
    """Копия дописки делегата в треде карточки (чат SOS) — чтобы реплай орга на неё нашёл
    заявку (`find_sos_report_by_relay`)."""
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO sos_relay_messages (chat_id, message_id, report_id) "
            "VALUES (?, ?, ?)",
            (chat_id, message_id, report_id),
        )
        await db.commit()


async def find_sos_report_by_relay(chat_id: int, message_id: int) -> int | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT report_id FROM sos_relay_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else None


async def claim_sos_report(report_id: int, admin_id: int, admin_name: str) -> bool:
    """Атомарный захват «🙋 Беру» — True только у ТОГО вызова, что перевернул строку
    (rowcount==1); конкурентный второй тап того же момента получает False (та же идиома, что
    `claim_question`)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE sos_reports SET claimed_by = ?, claimed_by_name = ?, claimed_at = ? "
            "WHERE id = ? AND claimed_by IS NULL",
            (admin_id, admin_name, msk_now().strftime("%Y-%m-%d %H:%M:%S"), report_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def takeover_sos_report(report_id: int, expected_claimant: int, admin_id: int,
                              admin_name: str) -> bool:
    """«🔁 Перехватить» — атомарно: True только если заявка всё ещё у `expected_claimant` и не
    решена (между вопросом «перехватить?» и подтверждением её могли решить или перехватить
    другие). Лесенка напоминаний начинается заново — уже для нового взявшего."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE sos_reports SET taken_over_from_name = claimed_by_name, claimed_by = ?, "
            "claimed_by_name = ?, claimed_at = ?, claimed_remind_count = 0 "
            "WHERE id = ? AND claimed_by = ? AND resolved_at IS NULL",
            (admin_id, admin_name, msk_now().strftime("%Y-%m-%d %H:%M:%S"), report_id,
             expected_claimant),
        )
        await db.commit()
        return cursor.rowcount == 1


async def resolve_sos_report(report_id: int, admin_id: int, admin_name: str) -> bool:
    """«✅ Решено» — атомарно и независимо от того, был ли уже захват: `COALESCE` подставляет
    резолвера захватчиком ОДНИМ UPDATE, если строка ещё открыта (`claimed_by IS NULL`) — орг,
    решивший вопрос без предварительного «Беру», не оставляет карточку без ответственного."""
    async with _connect() as db:
        now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            "UPDATE sos_reports SET resolved_by = ?, resolved_by_name = ?, resolved_at = ?, "
            "claimed_by = COALESCE(claimed_by, ?), claimed_by_name = COALESCE(claimed_by_name, ?), "
            "claimed_at = COALESCE(claimed_at, ?) WHERE id = ? AND resolved_at IS NULL",
            (admin_id, admin_name, now, admin_id, admin_name, now, report_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def mark_sos_post_resolve_reply(report_id: int, admin_name: str) -> None:
    """Орг ответил делегату реплаем на уже решённую карточку — последний такой ответ."""
    async with _connect() as db:
        await db.execute(
            "UPDATE sos_reports SET post_resolve_reply_by_name = ?, post_resolve_reply_at = ? "
            "WHERE id = ?",
            (admin_name, msk_now().strftime("%Y-%m-%d %H:%M:%S"), report_id),
        )
        await db.commit()


async def set_sos_delivery_failed(report_id: int, failed: bool) -> None:
    """Находка 1 ревью 24.09: `services.sos.record_delivery_outcome` зовёт это ПОСЛЕ каждой
    попытки доставки карточки (изначальной и повторной, `delivery_retry_job`) —
    `failed=True` штампует момент, `failed=False` (доставка удалась) снимает пометку.
    Идемпотентно в обе стороны — повторный вызов с тем же `failed` просто перезаписывает
    метку тем же смыслом."""
    async with _connect() as db:
        await db.execute(
            "UPDATE sos_reports SET delivery_failed_at = ? WHERE id = ?",
            (msk_now().strftime("%Y-%m-%d %H:%M:%S") if failed else None, report_id),
        )
        await db.commit()


async def set_sos_escalated(report_id: int) -> bool:
    """Штамп эскалации — идемпотентно (`WHERE escalated_at IS NULL`): повторный тик той же
    джобы (не должен случиться при корректном `replace_existing=True`, но fail-soft) не
    перезатирает первую метку и не шлёт повтор дважды."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE sos_reports SET escalated_at = ? WHERE id = ? AND escalated_at IS NULL",
            (msk_now().strftime("%Y-%m-%d %H:%M:%S"), report_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def advance_sos_claimed_remind(report_id: int, claimant_id: int, expected_count: int) -> bool:
    """Сдвиг счётчика напоминаний взявшему `expected_count -> expected_count + 1` — True только
    у того вызова, что сдвинул (compare-and-set): заявка всё ещё у ТОГО ЖЕ взявшего и не
    решена. Счётчик сдвигается ДО отправки — повторный тик той же ступени (рестарт посреди
    джобы) ничего не шлёт второй раз."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE sos_reports SET claimed_remind_count = ? WHERE id = ? AND claimed_by = ? "
            "AND resolved_at IS NULL AND COALESCE(claimed_remind_count, 0) = ?",
            (expected_count + 1, report_id, claimant_id, expected_count),
        )
        await db.commit()
        return cursor.rowcount == 1


# Статус строки sos_reports — ТРИ состояния, зеркало идиомы `_QUESTION_STATUS_SQL` выше:
# открыт (никто не взял) / взят (claimed_by ЕСТЬ, ещё не решён) / решён.
_SOS_STATUS_SQL = {
    "open": "s.claimed_by IS NULL AND s.resolved_at IS NULL",
    "claimed": "s.claimed_by IS NOT NULL AND s.resolved_at IS NULL",
    "resolved": "s.resolved_at IS NOT NULL",
}
_SOS_ORDER_SQL = {
    "open": "ORDER BY s.created_at ASC, s.id ASC",       # дольше без ответа -> выше
    "claimed": "ORDER BY s.claimed_at ASC, s.id ASC",     # дольше в работе -> выше
    "resolved": "ORDER BY s.resolved_at DESC, s.id DESC",  # свежие решённые сверху
}


async def list_sos_reports_page(*, status: str | None = None, city_scope=None,
                                 today: str | None = None, limit: int = 6,
                                 offset: int = 0) -> list[dict]:
    """Страница экрана менеджера «🆘 SOS» (пункт 5 плана). `today` — «ГГГГ-ММ-ДД» (московская
    дата, `services.timeutil.msk_now()`) — применяется ТОЛЬКО к фильтру "resolved" (пункт 5:
    «решённые ЗА СЕГОДНЯ»), открытые/взятые видны независимо от даты (они ждут действия сейчас,
    а не журнала). Неизвестный `status` -> без фильтра статуса вовсе (чип «Все»)."""
    where = []
    params: list = []
    frag = _SOS_STATUS_SQL.get(status)
    if frag:
        where.append(frag)
    if status == "resolved" and today:
        where.append("date(s.resolved_at) = ?")
        params.append(today)
    city_frag, city_params = _city_clause(city_scope, "s.city")
    if city_frag:
        where.append(city_frag)
        params.extend(city_params)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_sql = _SOS_ORDER_SQL.get(status, "ORDER BY s.created_at DESC, s.id DESC")
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, u.full_name AS user_full_name, u.username AS user_username "
            "FROM sos_reports s LEFT JOIN users u ON u.telegram_id = s.telegram_id "
            f"{where_sql} {order_sql} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def count_sos_by_status(*, city_scope=None, today: str | None = None) -> dict[str, int]:
    """Счётчики для шапки экрана: открыто/взято — за всё время (ждут действия ПРЯМО СЕЙЧАС),
    решено — ТОЛЬКО за `today` (та же граница, что `list_sos_reports_page`)."""
    city_frag, city_params = _city_clause(city_scope, "s.city")
    base_where = [city_frag] if city_frag else []
    resolved_where = base_where + (["date(s.resolved_at) = ?"] if today else [])
    resolved_params = list(city_params) + ([today] if today else [])

    def _where(fragments: list[str]) -> str:
        return f"WHERE {' AND '.join(fragments)}" if fragments else ""

    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM sos_reports s {_where([*base_where, _SOS_STATUS_SQL['open']])}",
            tuple(city_params),
        ) as cursor:
            open_n = (await cursor.fetchone())[0]
        async with db.execute(
            f"SELECT COUNT(*) FROM sos_reports s {_where([*base_where, _SOS_STATUS_SQL['claimed']])}",
            tuple(city_params),
        ) as cursor:
            claimed_n = (await cursor.fetchone())[0]
        async with db.execute(
            f"SELECT COUNT(*) FROM sos_reports s "
            f"{_where([*resolved_where, _SOS_STATUS_SQL['resolved']])}",
            tuple(resolved_params),
        ) as cursor:
            resolved_n = (await cursor.fetchone())[0]
    return {
        "open": int(open_n or 0),
        "claimed": int(claimed_n or 0),
        "resolved": int(resolved_n or 0),
    }


async def set_sos_bind_pending(admin_id: int, city: str | None) -> None:
    """Заявка «Привязать чат SOS» (пункт 2 плана) — `INSERT OR REPLACE`: повторный тап кнопки
    тем же менеджером перезаписывает (город мог смениться, старая заявка не должна ожить)."""
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO sos_chat_bind_pending (admin_id, city, requested_at) "
            "VALUES (?, ?, ?)",
            (admin_id, city, msk_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()


async def get_sos_bind_pending(admin_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sos_chat_bind_pending WHERE admin_id = ?", (admin_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def clear_sos_bind_pending(admin_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM sos_chat_bind_pending WHERE admin_id = ?", (admin_id,))
        await db.commit()


# ── Квик 260914-rgq (RGQ-01): постраничный список заявок ────────────────────────────────────
#
# Та же идиома «фрагменты WHERE словарём + LIMIT/OFFSET в SQL», что у `list_questions_page`/
# `count_questions_by_status` выше. Статус здесь — чип экрана, а не контракт: неизвестное
# значение трактуется как "approved" в обеих функциях, второй логики статуса нет.
_APPLICATION_STATUS_SQL = {
    "approved": "u.status = 'approved'",
    "rejected": "u.status = 'rejected'",
    # D-41: walk-in без решения — не заявка на модерацию (его ждёт стойка), как в очереди.
    "pending": "u.status = 'pending' AND COALESCE(u.onsite_kind, '') != 'walkin'",
}

# Порядок COALESCE в каждом выражении — три рубежа даты решения, от самого точного к самому
# надёжному:
#   1) собственная колонка `users` (`approved_at`/`rejected_at`) — момент ТОГО ЖЕ решения,
#      стампится в атомарном UPDATE (`approve_user_atomic`/`reject_user`), но не заполнена у
#      старых строк («rejected_at» заведена БЕЗ бэкафилла, db.py L790-797 — у отказов ДО этой
#      колонки NULL);
#   2) последняя ЖИВАЯ (`undone_at IS NULL`) строка `application_decisions` того же решения —
#      фолбэк для строк без бэкафилла; строка исчезает из этого рубежа, если решение отменено
#      (WR: отменённое решение датой решения не считается);
#   3) `registration_date` — последний рубеж, всегда заполнен при регистрации, гарантирует, что
#      строка никогда не выпадет из ORDER BY.
_APPLICATION_DATE_SQL = {
    "approved": (
        "COALESCE(u.approved_at, "
        "(SELECT d.decided_at FROM application_decisions d "
        "WHERE d.telegram_id = u.telegram_id AND d.decision = 'approved' "
        "AND d.undone_at IS NULL ORDER BY d.decided_at DESC, d.id DESC LIMIT 1), "
        "u.registration_date)"
    ),
    "rejected": (
        "COALESCE(u.rejected_at, "
        "(SELECT d.decided_at FROM application_decisions d "
        "WHERE d.telegram_id = u.telegram_id AND d.decision = 'rejected' "
        "AND d.undone_at IS NULL ORDER BY d.decided_at DESC, d.id DESC LIMIT 1), "
        "u.registration_date)"
    ),
    "pending": "u.registration_date",
}

# Владелец 16.09: строка списка обязана показывать, КТО принял решение — тот же рубеж
# «последняя ЖИВАЯ (`undone_at IS NULL`) строка `application_decisions`», что у
# `_APPLICATION_DATE_SQL`, но отдаёт `decided_by`, а не `decided_at`. Для "pending" решения
# ещё нет — константа `NULL`, а не подзапрос (запрос по несуществующему decision-у на pending
# строке просто вернул бы NULL каждый раз, но так честнее и дешевле читать). Отсутствие живой
# строки (отменённое решение / автоодобрение без записи в журнал, см. `services/reg_finalize.py
# ::post_finalize`) тоже даёт NULL — экран (`handlers/admin_app_list.py`) читает это как
# «автоматически», а не как ошибку.
_APPLICATION_DECIDER_SQL = {
    "approved": (
        "(SELECT d.decided_by FROM application_decisions d "
        "WHERE d.telegram_id = u.telegram_id AND d.decision = 'approved' "
        "AND d.undone_at IS NULL ORDER BY d.decided_at DESC, d.id DESC LIMIT 1)"
    ),
    "rejected": (
        "(SELECT d.decided_by FROM application_decisions d "
        "WHERE d.telegram_id = u.telegram_id AND d.decision = 'rejected' "
        "AND d.undone_at IS NULL ORDER BY d.decided_at DESC, d.id DESC LIMIT 1)"
    ),
    "pending": "NULL",
}


async def list_applications_page(*, status: str = "approved", city_scope=None,
                                   limit: int = 15, offset: int = 0) -> list[dict]:
    """Страница списка заявок для экрана «📇 Список заявок» (handlers/admin_app_list.py).
    Неизвестный `status` трактуется как "approved". Городской фильтр — по `u.event_city`
    (город ДЕЛЕГАТА, та же колонка, что у очереди заявок). `SELECT *` не используется — экрану
    нужны шесть полей, а `users` — широкая таблица (десятки колонок анкеты), таскать её в память
    постранично незачем. Порядок — `decided_at DESC, telegram_id DESC` (новые сверху, при
    равных датах — стабильный тай-брейк). `decided_by` — сырой telegram_id менеджера (или
    NULL); имя в человекочитаемую подпись резолвит `resolve_decision_managers` ОДНИМ запросом
    на страницу — не здесь, чтобы не дублировать JOIN на каждую строку."""
    if status not in _APPLICATION_STATUS_SQL:
        status = "approved"
    where = [_APPLICATION_STATUS_SQL[status]]
    params: list = []
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    if city_frag:
        where.append(city_frag)
        params.extend(city_params)
    where_sql = f"WHERE {' AND '.join(where)}"
    date_sql = _APPLICATION_DATE_SQL[status]
    decider_sql = _APPLICATION_DECIDER_SQL[status]
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT u.telegram_id, u.full_name, u.username, u.status, u.event_city, "
            f"{date_sql} AS decided_at, {decider_sql} AS decided_by "
            f"FROM users u {where_sql} "
            "ORDER BY decided_at DESC, u.telegram_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def resolve_decision_managers(decided_by_ids: list[int]) -> dict[int, str]:
    """Человекочитаемая подпись менеджера по его telegram_id (владелец 16.09, экран «📇 Список
    заявок»): один запрос на СПИСОК id, а не по одному на строку (вызывающий сам собирает
    неповторяющиеся `decided_by` со страницы перед вызовом — не N+1). `staff` (`list_staff()`,
    db.py ~3986) имени/ника не хранит — только telegram_id/role/added_by/added_at/city, поэтому
    источник подписи — собственная строка менеджера в `users`: `full_name`, а если её нет —
    `@username`. Кого не нашли вовсе (менеджер никогда не писал боту, роль выдана вручную по
    id) — подпись `менеджер #<id>`, голый id без слова наружу не идёт (CLAUDE.md: кодовые
    значения человеку не показываем).

    Phase 31 (31-02): `i > 0` (не просто `if i`) — сентинел автоотказа `AUTO_DECIDED_BY = -1`
    (единственное объявление — план 31-05, `services/reject_journal.py`) не должен уезжать в
    этот запрос: у него нет строки в `users`, и подпись «менеджер #-1» была бы враньём."""
    ids = sorted({i for i in decided_by_ids if i and i > 0})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT telegram_id, full_name, username FROM users "
            f"WHERE telegram_id IN ({placeholders})",
            ids,
        ) as cursor:
            rows = await cursor.fetchall()
    labels: dict[int, str] = {}
    for row in rows:
        full_name = (row["full_name"] or "").strip()
        username = (row["username"] or "").strip()
        if full_name:
            labels[row["telegram_id"]] = full_name
        elif username:
            labels[row["telegram_id"]] = "@" + username.lstrip("@")
    for manager_id in ids:
        labels.setdefault(manager_id, f"менеджер #{manager_id}")
    return labels


async def count_applications(*, city_scope=None) -> dict[str, int]:
    """Один запрос, три `SUM(CASE …)` по тем же фрагментам `_APPLICATION_STATUS_SQL` и тому же
    city-фрагменту, что `list_applications_page` — счётчик в шапке экрана не может разойтись со
    списком под ней (тот же приём WR-05, что у `count_questions_by_status`)."""
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    where_sql = f"WHERE {city_frag}" if city_frag else ""
    async with _connect() as db:
        async with db.execute(
            "SELECT "
            f"SUM(CASE WHEN {_APPLICATION_STATUS_SQL['approved']} THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {_APPLICATION_STATUS_SQL['rejected']} THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {_APPLICATION_STATUS_SQL['pending']} THEN 1 ELSE 0 END) "
            f"FROM users u {where_sql}",
            tuple(city_params),
        ) as cursor:
            row = await cursor.fetchone()
    approved_n, rejected_n, pending_n = row if row else (0, 0, 0)
    return {
        "approved": int(approved_n or 0),
        "rejected": int(rejected_n or 0),
        "pending": int(pending_n or 0),
    }


# ── Quick 260906-8uq (FAQ-01..06): аксессоры faq_items ───────────────────────────────────────
#
# Правило «городской пункт перекрывает общий» здесь НЕ живёт — это одноразовая городская
# ФИЛЬТРАЦИЯ (см. `_city_clause`), сама логика перекрытия объявлена ровно один раз в чистом
# модуле `services/faq.py::apply_city_overrides`, который вызывающий (бот/Mini App) применяет
# поверх результата `list_faq_for_city`.

# Белый список колонок для `update_faq_item` (T-FAQ-04): имя колонки никогда не приходит из
# callback_data — SET собирается только из этих литералов, значения — параметрами.
_FAQ_UPDATABLE_FIELDS = ("question", "answer", "city", "enabled", "position")


async def list_faq_items(*, city_scope=None, enabled_only: bool = False) -> list[dict]:
    """Список для экрана МЕНЕДЖЕРА: `city_scope` — дескриптор шапки города (`cities.city_scope`),
    include_null=True — тот же приём, что `list_active_tasks` (NULL = «все города» обязан
    попасть в выборку и при конкретном городе шапки). `enabled_only` — фильтр «показывается
    делегатам» поверх городского, для делегатских поверхностей используйте `list_faq_for_city`
    вместо этого (там же живёт вся делегатская видимость)."""
    frag, city_params = _city_clause(city_scope, "city", include_null=True)
    where_parts = []
    params: list = []
    if frag:
        where_parts.append(frag)
        params.extend(city_params)
    if enabled_only:
        where_parts.append("enabled = 1")
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM faq_items {where_sql} ORDER BY position ASC, id ASC",
            tuple(params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def list_faq_for_city(city_code: str | None) -> list[dict]:
    """Список для ДЕЛЕГАТА: включённые пункты, у которых city IS NULL или city = его город.
    `city_code=None` (модуль городов выключен / город не резолвится) отдаёт только общие
    пункты — параметр `?` со значением None никогда не совпадает с `city = ?` в SQLite, так
    что вторая ветка OR молчаливо не срабатывает, а первая (`city IS NULL`) уже покрывает этот
    случай. Перекрытие общего пункта городским (тот же нормализованный вопрос) — забота
    вызывающего через `services.faq.apply_city_overrides`, не этой функции."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM faq_items WHERE enabled = 1 AND (city IS NULL OR city = ?) "
            "ORDER BY position ASC, id ASC",
            (city_code,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def has_faq_for_city(city_code: str | None) -> bool:
    """Тот же WHERE, что `list_faq_for_city`, но LIMIT 1 -> bool — используется, чтобы решить,
    рисовать ли делегату кнопку меню «❓ Частые вопросы» (fail-soft со стороны вызывающего)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM faq_items WHERE enabled = 1 AND (city IS NULL OR city = ?) LIMIT 1",
            (city_code,),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def get_faq_item(item_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM faq_items WHERE id = ?", (item_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_faq_item(*, city: str | None, question: str, answer: str,
                           created_by: int | None) -> int:
    """Кладёт пункт в конец: `position = MAX(position) + 1` через всю таблицу (не по городскому
    ведру) — тот же простой приём, что и остальные списки этого проекта; вторая карта позиций
    по городу не заводится (см. докстринг `reorder_faq_items` про ограничение перестановки)."""
    created_at = datetime.utcnow().isoformat()
    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(MAX(position), -1) FROM faq_items"
        ) as cursor:
            row = await cursor.fetchone()
        next_position = (row[0] if row and row[0] is not None else -1) + 1
        cursor = await db.execute(
            "INSERT INTO faq_items (city, question, answer, position, enabled, created_at, "
            "created_by) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (city, question, answer, next_position, created_at, created_by),
        )
        await db.commit()
        return cursor.lastrowid


async def update_faq_item(item_id: int, **fields) -> bool:
    """T-FAQ-04: SET собирается ТОЛЬКО из `_FAQ_UPDATABLE_FIELDS` — ключ вне списка молча
    игнорируется (не поднимает исключение), значения уходят параметрами, имя колонки никогда
    не строится из пользовательского ввода."""
    updates = {k: v for k, v in fields.items() if k in _FAQ_UPDATABLE_FIELDS}
    if not updates:
        return False
    set_sql = ", ".join(f"{col} = ?" for col in updates)
    params = list(updates.values()) + [item_id]
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE faq_items SET {set_sql} WHERE id = ?", params
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_faq_item(item_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM faq_items WHERE id = ?", (item_id,))
        await db.commit()
        return cursor.rowcount > 0


async def reorder_faq_items(ordered_ids: list[int]) -> None:
    """Одна транзакция, `position` = индекс в `ordered_ids`. Пишет позиции ТОЛЬКО переданным
    id — вызывающий (админ-экран) передаёт видимое в его scope подмножество, порядок пунктов
    другого города доопределяется вторичным ключом `id` (документированное ограничение,
    handlers/admin_faq.py)."""
    async with _connect() as db:
        for idx, item_id in enumerate(ordered_ids):
            await db.execute(
                "UPDATE faq_items SET position = ? WHERE id = ?", (idx, item_id)
            )
        await db.commit()


# ── Phase 31 (31-02, D-05/D-16): аксессоры reject_rules — CRUD правил автоотказа ────────────

# Белый список колонок для `update_reject_rule` (T-31-02-01): имя колонки никогда не приходит
# из вызывающего в сыром виде — SET собирается только из этих литералов, тот же приём, что у
# `_FAQ_UPDATABLE_FIELDS`.
_REJECT_RULE_UPDATABLE_FIELDS = (
    "name", "city", "tracks", "conditions", "action", "reject_text", "enabled", "paused_reason",
)


async def list_reject_rules(*, city_scope=None, enabled_only: bool = False) -> list[dict]:
    """Список для экрана менеджера. `city_scope` — тот же дескриптор `cities.city_scope`, что
    и `list_faq_items`: `include_null=True`, потому что правило «все города» (`city IS NULL`)
    обязано быть видно из ЛЮБОГО городского скоупа (та же семантика, что у
    `admin_faq._card_out_of_scope`). Порядок — правила «все города» первыми (`city IS NULL
    DESC`), затем по городу и id — стабильный порядок списка между перезагрузками экрана."""
    frag, city_params = _city_clause(city_scope, "city", include_null=True)
    where_parts = []
    params: list = []
    if frag:
        where_parts.append(frag)
        params.extend(city_params)
    if enabled_only:
        where_parts.append("enabled = 1")
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM reject_rules {where_sql} "
            "ORDER BY city IS NULL DESC, city, id",
            tuple(params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_reject_rule(rule_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reject_rules WHERE id = ?", (rule_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_reject_rule(*, name: str | None, city: str | None, tracks: str,
                              conditions: str, action: str, reject_text: str | None,
                              enabled: int, created_by: int | None) -> int:
    """`tracks`/`conditions` приходят уже сериализованными JSON-строками — сервисный слой
    (план 31-04) владеет форматом, эта функция его не разбирает и не проверяет."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO reject_rules (name, city, tracks, conditions, action, reject_text, "
            "enabled, created_at, updated_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, city, tracks, conditions, action, reject_text, enabled, now, now, created_by),
        )
        await db.commit()
        return cursor.lastrowid


async def update_reject_rule(rule_id: int, **fields) -> bool:
    """T-31-02-01: SET собирается ТОЛЬКО из `_REJECT_RULE_UPDATABLE_FIELDS` — ключ вне списка
    молча игнорируется (не поднимает исключение), значения уходят параметрами. `updated_at`
    проставляется сам при любом непустом наборе изменений."""
    updates = {k: v for k, v in fields.items() if k in _REJECT_RULE_UPDATABLE_FIELDS}
    if not updates:
        return False
    updates["updated_at"] = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    set_sql = ", ".join(f"{col} = ?" for col in updates)
    params = list(updates.values()) + [rule_id]
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE reject_rules SET {set_sql} WHERE id = ?", params
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_reject_rule(rule_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM reject_rules WHERE id = ?", (rule_id,))
        await db.commit()
        return cursor.rowcount > 0


async def count_reject_rules(*, enabled_only: bool = False) -> int:
    where_sql = " WHERE enabled = 1" if enabled_only else ""
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM reject_rules{where_sql}"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


# ── Phase 31 (31-02, D-18/D-24): аксессоры auto_reject_log — журнал автоотказов ─────────────

# Тот же приём, что `_COIN_JOURNAL_SELECT`: одна строка SELECT с JOIN переиспользуется списком,
# счётчиком и выгрузкой — второй копии условия/join не заводится. LEFT JOIN (не INNER) на
# случай, если строка users когда-нибудь пропадёт (users.telegram_id — не FK в этой схеме).
_AUTO_REJECT_LOG_SELECT = (
    "SELECT l.*, u.full_name, u.username, u.event_city FROM auto_reject_log l "
    "LEFT JOIN users u ON u.telegram_id = l.telegram_id"
)


def _auto_reject_log_where(city_scope, include_returned: bool) -> tuple[str, list]:
    """Общий WHERE для list_auto_reject_log/count_auto_reject_log — счётчик и список ОБЯЗАНЫ
    ходить по одному набору условий (тот же принцип, что у queue_page), иначе «Всего: N»
    расходится со списком под ним."""
    where = []
    params: list = []
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    if city_frag:
        where.append(city_frag)
        params.extend(city_params)
    if not include_returned:
        where.append("l.returned_to_moderation_at IS NULL")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return where_sql, params


async def upsert_auto_reject_log(telegram_id: int, rule_ids_json: str, reject_texts_json: str,
                                  now: str) -> int:
    """D-24: НЕТ лимита попыток — это явное решение владельца, зафиксированное здесь
    докстрингом, чтобы позже никто не «починил» это ограничением. Если у делегата есть ЖИВАЯ
    строка (`returned_to_moderation_at IS NULL`) — один UPDATE, увеличивающий attempt_count и
    переписывающий rule_ids/reject_texts/last_triggered_at; иначе INSERT с attempt_count = 1 и
    first_triggered_at = last_triggered_at = now. RETURNING id — тот же приём, что
    `approve_all_pending`."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "UPDATE auto_reject_log SET attempt_count = attempt_count + 1, "
            "rule_ids = ?, reject_texts = ?, last_triggered_at = ? "
            "WHERE telegram_id = ? AND returned_to_moderation_at IS NULL "
            "RETURNING id",
            (rule_ids_json, reject_texts_json, now, telegram_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.commit()
            return row["id"]
        async with db.execute(
            "INSERT INTO auto_reject_log (telegram_id, rule_ids, reject_texts, attempt_count, "
            "first_triggered_at, last_triggered_at) VALUES (?, ?, ?, 1, ?, ?) RETURNING id",
            (telegram_id, rule_ids_json, reject_texts_json, now, now),
        ) as cursor:
            row = await cursor.fetchone()
        await db.commit()
        return row["id"]


async def list_auto_reject_log(*, city_scope=None, limit: int = 15, offset: int = 0,
                                include_returned: bool = False) -> list[dict]:
    """Страница журнала «🤖 Автоотказы» — JOIN к users ради full_name/username/event_city,
    городской скоуп той же `_city_clause(scope, "u.event_city")`, что и остальные городские
    выборки. Порядок — last_triggered_at DESC, id DESC (новые срабатывания сверху)."""
    where_sql, params = _auto_reject_log_where(city_scope, include_returned)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"{_AUTO_REJECT_LOG_SELECT} {where_sql} "
            "ORDER BY l.last_triggered_at DESC, l.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def count_auto_reject_log(*, city_scope=None, include_returned: bool = False) -> int:
    where_sql, params = _auto_reject_log_where(city_scope, include_returned)
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM auto_reject_log l LEFT JOIN users u "
            f"ON u.telegram_id = l.telegram_id {where_sql}",
            tuple(params),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def count_auto_reject_log_for_rule(rule_id: int, *, city_scope=None,
                                          include_returned: bool = False) -> int:
    """Число строк журнала, где сработало ИМЕННО это правило — COUNT в SQL вместо вычитывания
    журнала целиком в Python (карточка «🗑 Удалить правило» раньше грузила до 100000 строк с
    JOIN на каждый рендер). `rule_ids` — JSON-массив id (`record_auto_reject`); `json_each`
    разворачивает массив построчно, `je.value = ?` — точное совпадение элемента (не LIKE по
    сериализованной строке: правило 1 не имеет права засчитать срабатывание 10/11/21). Тот же
    WHERE-билдер, что у списка/счётчика журнала (`_auto_reject_log_where`) — городской скоуп и
    include_returned ведут себя одинаково."""
    where_sql, params = _auto_reject_log_where(city_scope, include_returned)
    rule_match = "EXISTS (SELECT 1 FROM json_each(l.rule_ids) je WHERE je.value = ?)"
    where_sql = f"{where_sql} AND {rule_match}" if where_sql else f"WHERE {rule_match}"
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM auto_reject_log l LEFT JOIN users u "
            f"ON u.telegram_id = l.telegram_id {where_sql}",
            (*params, rule_id),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def get_auto_reject_log_entry(entry_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM auto_reject_log WHERE id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_live_auto_reject_log_entry(telegram_id: int) -> dict | None:
    """Живая (`returned_to_moderation_at IS NULL`) строка журнала автоотказов ОДНОГО делегата
    — план 31-06 (`services/reg_finalize.py::post_finalize`, D-21/D-25): снимок текстов правил
    на МОМЕНТ срабатывания (записала `upsert_auto_reject_log`/`record_auto_reject`), а не
    текущий текст правила — который к моменту хвоста финала (может быть отложенным ретраем
    очереди Mini App) уже могли отредактировать. Та же дисциплина, что `get_last_application_
    decision`: берётся ПОСЛЕДНЯЯ строка (`ORDER BY id DESC LIMIT 1`) среди живых — после
    возврата на модерацию следующее срабатывание заводит НОВУЮ живую строку
    (`upsert_auto_reject_log`), старая перестаёт быть «живой» и больше не попадает в эту
    выборку."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM auto_reject_log WHERE telegram_id = ? AND "
            "returned_to_moderation_at IS NULL ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def claim_auto_reject_return(entry_id: int, admin_id: int, now: str) -> dict | None:
    """Условный UPDATE ... WHERE returned_to_moderation_at IS NULL — выигрывает ровно один
    вызов, та же дисциплина, что `claim_application_undo`: двойной тап по кнопке «вернуть на
    модерацию» не имеет права сработать дважды."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "UPDATE auto_reject_log SET returned_to_moderation_at = ?, returned_by = ? "
            "WHERE id = ? AND returned_to_moderation_at IS NULL RETURNING *",
            (now, admin_id, entry_id),
        ) as cursor:
            row = await cursor.fetchone()
        await db.commit()
        return dict(row) if row else None


async def export_auto_reject_log_rows(*, city_scope=None) -> tuple[list[str], list[tuple]]:
    """D-29: выгрузка журнала файлом для отчёта партнёрам. ВСЕ значения, пришедшие из анкеты
    делегата или текста менеджера (ФИО/ник/тексты правил), проходят существующий `_csv_safe`
    (T-31-02-02, CWE-1236) — вторую копию этой защиты не заводим. Возврат включается в выгрузку
    (`include_returned=True`) — отчёт партнёрам обязан показывать полную историю, а не только
    текущих отказников."""
    headers = [
        "ФИО", "Ник", "Город", "Первое срабатывание", "Последнее срабатывание",
        "Попытки", "ID правил", "Тексты правил", "Возврат на модерацию",
    ]
    rows = await list_auto_reject_log(
        city_scope=city_scope, limit=-1, offset=0, include_returned=True,
    )
    out_rows = []
    for row in rows:
        out_rows.append(tuple(_csv_safe(cell) for cell in (
            row.get("full_name"), row.get("username"), row.get("event_city"),
            row.get("first_triggered_at"), row.get("last_triggered_at"),
            row.get("attempt_count"), row.get("rule_ids"), row.get("reject_texts"),
            "да" if row.get("returned_to_moderation_at") else "нет",
        )))
    return headers, out_rows


# ── Квик 260923 (AUTOREJ-REPORT): честные цифры автоотказа для отчётности менеджерам ─────────
#
# Одна общая функция вместо трёх мест, где отдельно считали «сколько людей отсеяли правила»:
# «Итоги дня» (D-A), сводка ожидания (D-B) и пачка уведомлений (D-D) — у всех троих одна и та
# же форма ответа (число людей + разбивка по правилам), различаются только фильтры. Имена
# правил — намеренно СВОЯ копия логики `dashboard.queries._reject_rule_labels` (имя правила ->
# первые 6 слов текста отказа -> «Правило без названия»): `database/db.py` не имеет права
# импортировать `dashboard/*` (разные процессы, dashboard — read-only читатель этого файла).

async def auto_reject_summary(*, since: str | None = None, until: str | None = None,
                               telegram_ids: list[int] | None = None, city_scope=None,
                               live_only: bool = True) -> tuple[int, list[tuple[str, int]]]:
    """Число людей + разбивка «какое правило сколько отсеяло» (по убыванию) по строкам
    `auto_reject_log`.

    `live_only=True` (дефолт, «Итоги дня»/сводка ожидания) — строка ЕЩЁ живая
    (`returned_to_moderation_at IS NULL`) И делегат всё ещё `status='rejected'` (сам поправивший
    анкету делегат больше не считается, D-H). `live_only=False` (пачка уведомлений, D-D) — счёт
    идёт по переданным `telegram_ids` независимо от текущего статуса: к моменту отправки пачки
    менеджер мог уже вернуть заявку из журнала, но пачка описывает то, что произошло НА
    ПОСТАНОВКЕ (тот же принцип, что у `services.reg_digest.send_reg_digest`).

    `since`/`until` фильтруют `last_triggered_at` в полуинтервале `[since, until)`.
    `telegram_ids=[]` (пустой список, не `None`) -> `(0, [])` без обращения к БД — пачка без
    автоотказов не имеет права ходить в БД зря. Битая строка `rule_ids` пропускается (fail-soft,
    тот же приём, что у `dashboard.queries.auto_reject_breakdown`)."""
    if telegram_ids is not None and not telegram_ids:
        return 0, []

    conditions: list[str] = []
    params: list = []
    if live_only:
        conditions.append("l.returned_to_moderation_at IS NULL")
        conditions.append("u.status = 'rejected'")
    if since is not None:
        conditions.append("l.last_triggered_at >= ?")
        params.append(since)
    if until is not None:
        conditions.append("l.last_triggered_at < ?")
        params.append(until)
    if telegram_ids is not None:
        placeholders = ", ".join("?" for _ in telegram_ids)
        conditions.append(f"l.telegram_id IN ({placeholders})")
        params.extend(telegram_ids)
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    if city_frag:
        conditions.append(city_frag)
        params.extend(city_params)
    where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT l.telegram_id, l.rule_ids FROM auto_reject_log l "
            f"JOIN users u ON u.telegram_id = l.telegram_id{where_sql}",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        async with db.execute(
            "SELECT id, name, reject_text FROM reject_rules"
        ) as cursor:
            rule_rows = await cursor.fetchall()

    labels: dict[int, str] = {}
    for r in rule_rows:
        name = (r["name"] or "").strip()
        if name:
            labels[r["id"]] = name
            continue
        text = (r["reject_text"] or "").strip()
        labels[r["id"]] = " ".join(text.split()[:6]) if text else "Правило без названия"

    people_ids: set[int] = set()
    counter: dict[str, int] = {}
    for row in rows:
        people_ids.add(row["telegram_id"])
        try:
            rule_ids = json.loads(row["rule_ids"])
        except (TypeError, ValueError):
            continue
        if not isinstance(rule_ids, list):
            continue
        for rid in rule_ids:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            label = labels.get(rid, "Правило без названия")
            counter[label] = counter.get(label, 0) + 1

    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return len(people_ids), ranked


async def auto_reject_names(*, since: str | None = None, city_scope=None) -> list[str]:
    """ФИО живых автоотклонённых (та же выборка, что `auto_reject_summary(live_only=True)`)
    в порядке срабатывания — для блока «🤖 Автоотказ» в периодической сводке ожидания."""
    conditions = ["l.returned_to_moderation_at IS NULL", "u.status = 'rejected'"]
    params: list = []
    if since is not None:
        conditions.append("l.last_triggered_at >= ?")
        params.append(since)
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    if city_frag:
        conditions.append(city_frag)
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(NULLIF(u.full_name, ''), CAST(u.telegram_id AS TEXT)) "
            "FROM auto_reject_log l JOIN users u ON u.telegram_id = l.telegram_id "
            f"WHERE {' AND '.join(conditions)} ORDER BY l.last_triggered_at, l.id",
            params,
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def auto_reject_sheet_rows() -> tuple[list[str], list[list]]:
    """D-E: шапка + строки живых автоотклонённых для вкладки «🤖 Автоотказы» (полная
    перезапись, `services.sheets.sync_named_worksheet`). Живая строка = не возвращена журналом
    И делегат всё ещё `status='rejected'` — та же дисциплина, что у `auto_reject_summary`
    (`live_only=True`)/`dashboard.queries.auto_reject_breakdown` (D-H). Правила — человеческими
    именами (та же логика имён, что в `auto_reject_summary` выше), ID правил в выгрузку не
    попадают. Город — код (`u.event_city`), человеческую подпись подставляет вызывающая джоба
    (`services/scheduler.py`, у неё есть `cities`, этот файл его не импортирует). Все строковые
    ячейки — через `_csv_safe` (T-en3-01, CWE-1236)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT u.full_name, u.username, u.phone, u.email, u.event_city, u.university, "
            "u.course, l.rule_ids, l.reject_texts, l.first_triggered_at, l.last_triggered_at, "
            "l.attempt_count, l.telegram_id "
            "FROM auto_reject_log l JOIN users u ON u.telegram_id = l.telegram_id "
            "WHERE l.returned_to_moderation_at IS NULL AND u.status = 'rejected' "
            "ORDER BY l.last_triggered_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        async with db.execute(
            "SELECT id, name, reject_text FROM reject_rules"
        ) as cursor:
            rule_rows = await cursor.fetchall()

    labels: dict[int, str] = {}
    for r in rule_rows:
        name = (r["name"] or "").strip()
        if name:
            labels[r["id"]] = name
            continue
        text = (r["reject_text"] or "").strip()
        labels[r["id"]] = " ".join(text.split()[:6]) if text else "Правило без названия"

    headers = [
        "ФИО", "Ник", "Телефон", "Почта", "Город", "Вуз", "Курс", "Правила",
        "Текст отказа", "Первое срабатывание", "Последнее срабатывание", "Попыток",
        "Telegram ID",
    ]
    out_rows: list[list] = []
    for row in rows:
        try:
            rule_ids = json.loads(row["rule_ids"])
        except (TypeError, ValueError):
            rule_ids = []
        if not isinstance(rule_ids, list):
            rule_ids = []
        rule_names = []
        for rid in rule_ids:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            rule_names.append(labels.get(rid, "Правило без названия"))
        try:
            reject_texts = json.loads(row["reject_texts"])
            if not isinstance(reject_texts, list):
                reject_texts = []
        except (TypeError, ValueError):
            reject_texts = []
        out_rows.append([_csv_safe(cell) for cell in (
            row["full_name"], row["username"], row["phone"], row["email"], row["event_city"],
            row["university"], row["course"], ", ".join(rule_names), " / ".join(reject_texts),
            row["first_triggered_at"], row["last_triggered_at"], row["attempt_count"],
            row["telegram_id"],
        )])
    return headers, out_rows


# ── Phase 9 (GAME-01/02/03): task model + submission queue ──────────────────────────────────
#
# GAME_CATEGORIES (D-06) — a single classification axis, no RESULT/INTERACTIVE/NETWORK track
# and no `participant_type` audience field (D-07); both deferred additions land as one
# `_ensure_column` later, not a storage rewrite. GAME_PROOF_TYPES (D-01/D-08) — the four
# confirmation shapes a task can require. Exported here (not duplicated in handlers) so
# handlers/admin.py and handlers/user_actions.py can never drift on the list of valid values.
GAME_CATEGORIES = ["Light", "Medium", "Hard", "Referral", "Special"]
GAME_PROOF_TYPES = ["photo", "pdf", "text", "link"]

# Phase 32 (32-01, D-27): «конца времён» — единственный литерал метки «без срока» для
# game_tasks.deadline_at (колонка остаётся NOT NULL, перестройки таблицы в проекте нет ни
# одной, поэтому NULL здесь недопустим). Объявлена РОВНО ОДИН РАЗ здесь; показ человеку и
# помощники чтения — не задача этого плана (заводит план 32-04, читателей переводят 32-06/
# 32-07/32-14, сторож от повторного литерала/собственного strptime — тоже план 32-14).
NO_DEADLINE_AT = "9999-12-31 23:59:59"

# Phase 32 (32-01, D-08/D-28): закрытое множество значений game_tasks.audience — 'ambassadors'
# требует активный статус амбассадора, любое другое (включая NULL, читается как 'all') видно
# всем. TASK_AUDIENCES — единственный источник правды для update_task_audience ниже.
TASK_AUDIENCES = ("all", "ambassadors")

# Phase 32 (32-01, D-10/D-17): жизненный цикл волны — 'draft' (черновик, не разослана),
# 'active' (идёт), 'closing' (даты вышли, итоги ещё не объявлены), 'announced' (снимок
# wave_results записан). set_wave_state ниже — единственная точка перехода между ними.
WAVE_STATES = ("draft", "active", "closing", "announced")

# Phase 09.1 (A): the free-form submission's part storage kind vocabulary -- distinct from
# GAME_PROOF_TYPES ("pdf" narrows to "document": any file type is accepted now, not only PDF).
GAME_PART_KINDS = ["photo", "document", "text", "link"]

# Legacy single-column content_type -> new part `kind`, used ONLY by
# get_submission_parts_or_legacy to synthesize one part from a pre-migration row.
_LEGACY_KIND_MAP = {"photo": "photo", "pdf": "document", "text": "text", "link": "link"}


def parse_proof_types(raw: str | None) -> list[str]:
    """The ONE place that owns the storage format for a task's (possibly multiple)
    proof_type: a comma-separated string in the existing `game_tasks.proof_type` column
    (no migration needed -- a single old value parses as a one-element list). Unknown codes
    are dropped; order follows GAME_PROOF_TYPES, not the order codes appear in `raw`."""
    if not raw:
        return []
    codes = {segment.strip() for segment in raw.split(",") if segment.strip()}
    return [p for p in GAME_PROOF_TYPES if p in codes]


async def create_task(text: str, category: str, coins: int, proof_type: str,
                       deadline_at: str, created_by: int | None, *,
                       event_city: str | None = None, title: str | None = None,
                       photo_file_id: str | None = None,
                       wave_id: int | None = None, audience: str = "all") -> int:
    """`event_city` is kwarg-only (Phase 09.1 B) so every existing positional call site
    (including pre-09.1 tests) stays valid and keeps creating a NULL-city ("all cities")
    task unless a caller opts in. `title`/`photo_file_id` (quick 260819-gtl) are kwarg-only
    for the same reason -- every pre-existing call site keeps creating a NULL-title/NULL-photo
    task (rendered via task_title()'s fallback) unless a caller opts in.
    `wave_id`/`audience` (Phase 32, 32-01, D-08/D-12) — same discipline: every existing call
    site keeps creating a task outside any wave, visible to everyone, exactly as before."""
    created_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO game_tasks (text, category, coins, proof_type, deadline_at, "
            "created_by, created_at, event_city, title, photo_file_id, wave_id, audience) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (text, category, coins, proof_type, deadline_at, created_by, created_at,
             event_city, title, photo_file_id, wave_id, audience),
        )
        await db.commit()
        return cursor.lastrowid


def task_title(task: dict) -> str:
    """The ONE place that owns a task's display title (quick 260819-gtl, CONTEXT.md decision
    2): `task["title"]` when set, else a fallback derived from `task["text"]` -- the first
    line, truncated to 40 chars with a trailing "…" if it was cut. Accepts either a real
    `game_tasks` row (keys `title`/`text`) or a synthesized dict with those two keys (e.g. a
    `game_submissions` JOIN row remapped to `title`=task_title/`text`=task_text by the
    caller) -- never touches the DB itself, never backfills NULL titles."""
    title = str(task.get("title") or "").strip()
    if title:
        return title
    text = str(task.get("text") or "")
    first_line = text.splitlines()[0] if text else ""
    if len(first_line) > 40:
        return first_line[:40] + "…"
    return first_line


async def update_task_title(task_id: int, title: str) -> bool:
    """True iff the task existed. `title` must already be validated/truncated by the caller
    (handlers/admin_gamification.py's wizard-shared validator) -- this is a plain write, no
    business rules live here (same division of labor as create_task)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET title = ? WHERE id = ?", (title, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_task_photo(task_id: int, photo_file_id: str | None) -> bool:
    """Sets or clears (photo_file_id=None) the task's cover photo. True iff the task existed.
    Not a "resettable to NULL only if not-NULL" idiom like archive/unarchive -- CONTEXT.md
    decision 4 treats replace/remove as the SAME non-destructive write (no confirm step)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET photo_file_id = ? WHERE id = ?", (photo_file_id, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


# Phase 16 (16-03, GAME-UI-03): the remaining point-edit accessors -- same plain-UPDATE /
# rowcount idiom as update_task_title/update_task_photo above; validation (non-empty text,
# positive coins, "%Y-%m-%d %H:%M:%S" deadline string) is the caller's job.
async def update_task_text(task_id: int, text: str) -> bool:
    """True iff the task existed."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET text = ? WHERE id = ?", (text, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_task_coins(task_id: int, coins: int) -> bool:
    """True iff the task existed."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET coins = ? WHERE id = ?", (coins, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_task_deadline(task_id: int, deadline_at: str) -> bool:
    """True iff the task existed. `deadline_at` is the already-formatted
    "%Y-%m-%d %H:%M:%S" string (same format create_task stores)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET deadline_at = ? WHERE id = ?", (deadline_at, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_task_wave(task_id: int, wave_id: int | None) -> bool:
    """Привязывает (или снимает, `wave_id=None`) задание к волне. True iff задание
    существовало. Плоский UPDATE — та же идиома, что у соседних update_task_*."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET wave_id = ? WHERE id = ?", (wave_id, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_task_audience(task_id: int, audience: str) -> bool:
    """True iff задание существовало. `audience` вне TASK_AUDIENCES — ValueError (не
    молчаливое игнорирование неизвестного значения)."""
    if audience not in TASK_AUDIENCES:
        raise ValueError(f"Unknown task audience: {audience!r}")
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET audience = ? WHERE id = ?", (audience, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def list_wave_tasks(wave_id: int, *, active_only: bool = True) -> list[dict]:
    """Задания одной волны. `active_only=True` (по умолчанию) исключает архивные — та же
    идиома, что `list_active_tasks`; `active_only=False` — все задания волны, включая архив
    (для менеджерского экрана истории волны)."""
    extra = " AND archived_at IS NULL" if active_only else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM game_tasks WHERE wave_id = ?{extra} ORDER BY deadline_at ASC",
            (wave_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_task(task_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_active_tasks(*, city_scope=None, include_null: bool = True) -> list[dict]:
    """No deadline filter — A-05 (call 13.08): the deadline is soft, the bot keeps accepting
    submissions after it expires, so a past-deadline task must stay visible to a delegate or
    it becomes physically impossible to submit. Sorted by nearest deadline first.
    Phase 09.1 (B): `city_scope=None` (default) -> byte-identical to pre-09.1 (all tasks,
    no filter). `include_null=True` by default -- a task's NULL event_city means "all
    cities" (CONTEXT.md B), and the equality branch of `_city_clause` does NOT catch NULL on
    its own, so it must be asked for explicitly here.
    Phase 14 (GAME-08): always excludes archived tasks (archived_at IS NOT NULL) — a
    delegate must never see or be able to submit to an archived task, regardless of
    city_scope. The base `archived_at IS NULL` clause is unconditional; the city fragment
    (if any) is appended via AND."""
    frag, city_params = _city_clause(city_scope, "event_city", include_null=include_null)
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM game_tasks WHERE archived_at IS NULL{extra} "
            "ORDER BY deadline_at ASC",
            tuple(city_params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def list_all_tasks(*, city_scope=None, include_null: bool = True) -> list[dict]:
    """Phase 14 (GAME-08): deliberately NOT filtered by archived_at — the manager screen
    ("🎯 Задания" + "🗄 Архив") splits active/archived in the RENDERING layer, and the
    gamification sheet rebuild wants both (archived tasks stay in the sheet with a marker).
    Do NOT add an archived_at filter here; that belongs to list_active_tasks only."""
    frag, city_params = _city_clause(city_scope, "event_city", include_null=include_null)
    extra = f" WHERE {frag}" if frag else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM game_tasks{extra} ORDER BY created_at DESC", tuple(city_params)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


# ── Phase 32 (32-01, D-10): волны — аксессоры ───────────────────────────────────────────────

async def next_wave_number(event_city: str | None) -> int:
    """Следующий номер волны внутри города — своя нумерация на каждый город (вторая волна
    того же города получает номер 2, первая волна другого города — снова 1). `event_city`
    сравнивается точно (IS ? при NULL, равенство иначе), а не через _city_clause — здесь
    нужна не «фильтрация с учётом NULL=все», а строгая группа «этот город» / «без города»."""
    async with _connect() as db:
        if event_city is None:
            async with db.execute(
                "SELECT COALESCE(MAX(number), 0) FROM ambassador_waves WHERE event_city IS NULL"
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with db.execute(
                "SELECT COALESCE(MAX(number), 0) FROM ambassador_waves WHERE event_city = ?",
                (event_city,),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) + 1


async def create_wave(starts_at: str, ends_at: str, *, intro_text: str | None = None,
                       prize_places: int | None = None, event_city: str | None = None,
                       created_by: int | None = None) -> int:
    """Номер волны берёт `next_wave_number`, состояние всегда стартует 'draft'."""
    number = await next_wave_number(event_city)
    created_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO ambassador_waves (number, starts_at, ends_at, intro_text, "
            "prize_places, state, event_city, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
            (number, starts_at, ends_at, intro_text, prize_places, event_city, created_by,
             created_at),
        )
        await db.commit()
        return cursor.lastrowid


async def get_wave(wave_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ambassador_waves WHERE id = ?", (wave_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_waves(*, city_scope=None, include_null: bool = True,
                      states: tuple[str, ...] | None = None) -> list[dict]:
    """Ближайшая по датам старта — первая (`starts_at DESC, id DESC`)."""
    frag, params = _city_clause(city_scope, "event_city", include_null=include_null)
    clauses = [frag] if frag else []
    params = list(params)
    if states:
        placeholders = ", ".join("?" for _ in states)
        clauses.append(f"state IN ({placeholders})")
        params += list(states)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM ambassador_waves{where} ORDER BY starts_at DESC, id DESC",
            tuple(params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


_WAVE_UPDATABLE_FIELDS = {"starts_at", "ends_at", "intro_text", "prize_places", "event_city"}


async def update_wave(wave_id: int, **fields) -> bool:
    """Белый список ровно `_WAVE_UPDATABLE_FIELDS` — любой другой ключ (например `state`,
    у него своя атомарная точка записи `set_wave_state`) поднимает ValueError, не
    игнорируется молча (в отличие от `update_user_answers`, где чужой ключ — не ошибка
    вызывающего, а здесь вызывающий — только код этого проекта)."""
    unknown = set(fields) - _WAVE_UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown wave field(s): {sorted(unknown)}")
    if not fields:
        return False
    for c in fields:
        _assert_identifier(c)
    set_clause = ", ".join(f"{c} = ?" for c in fields)
    params = list(fields.values()) + [wave_id]
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE ambassador_waves SET {set_clause} WHERE id = ?", params,
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_wave_state(wave_id: int, state: str, *, expected_state: str | None = None) -> bool:
    """Тот же приём, что `approve_user_atomic` — необязательный `expected_state` в WHERE:
    два одновременных перехода (два клика «Объявить итоги») выигрывает ровно один."""
    if state not in WAVE_STATES:
        raise ValueError(f"Unknown wave state: {state!r}")
    sql = "UPDATE ambassador_waves SET state = ? WHERE id = ?"
    params = [state, wave_id]
    if expected_state is not None:
        sql += " AND state = ?"
        params.append(expected_state)
    async with _connect() as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.rowcount == 1


async def mark_wave_started(wave_id: int, when: str) -> bool:
    """True iff это первый вызов для этой волны — `started_notified_at IS NULL` в WHERE не
    даёт второй рассылке «волна началась» перезаписать метку."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE ambassador_waves SET started_notified_at = ? "
            "WHERE id = ? AND started_notified_at IS NULL",
            (when, wave_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def delete_wave(wave_id: int) -> bool:
    """Задания волны НЕ удаляются — становятся «вне волн» (wave_id=NULL), обе операции в
    одной транзакции (общий `_connect()` без промежуточного commit).

    IN-07 (32-REVIEW.md): защита состояния — теперь ПРЯМО в SQL (`state != 'announced'`), не
    только в хендлере по заранее прочитанной строке волны. Раньше гонка «менеджер А объявил
    итоги — менеджер Б в ту же секунду жмёт "Удалить" на карточке, открытой ДО объявления»
    удаляла уже объявленную волну и оставляла осиротевший `wave_results`: SQL ничего не
    перепроверял, только хендлер сверял устаревшее чтение. Detach заданий (`wave_id = NULL`)
    выполняется, только если DELETE реально сработал (`rowcount == 1`) — если волна не
    удалилась (проиграна гонка или её уже нет), её задания не должны потерять привязку к
    волне, которая осталась стоять. Сигнатура и поведение при успехе не меняются — вызывающий
    код (`handlers/admin_game_waves.py::wave_delete_go`) уже не проверяет результат."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM ambassador_waves WHERE id = ? AND state != 'announced'", (wave_id,),
        )
        deleted = cursor.rowcount == 1
        if deleted:
            await db.execute("UPDATE game_tasks SET wave_id = NULL WHERE wave_id = ?", (wave_id,))
        await db.commit()
        return deleted


async def waves_overlapping(starts_at: str, ends_at: str, event_city: str | None, *,
                             exclude_id: int | None = None) -> list[dict]:
    """Пересечение отрезков — `starts_at <= ?[новый ends_at] AND ends_at >= ?[новый
    starts_at]`. Тот же город ИЛИ волна «все города»; состояние 'draft' пересечение не
    обходит — черновикам тоже запрещено пересекаться.

    WR-04 (32-REVIEW.md): проверка симметрична. Новая волна ОДНОГО города конфликтует с
    существующей волной того же города ИЛИ волной «все города» (`event_city IS NULL OR
    event_city = ?`) — это направление работало и раньше. Новая волна «все города»
    (`event_city is None`) обязана конфликтовать с ЛЮБОЙ существующей волной в тех же датах,
    какого бы города та ни была — общая волна физически перекрывает все города разом, поэтому
    здесь фильтр по городу не добавляется вовсе (раньше оставалось `event_city IS NULL`, и
    городская волна в тех же датах пролетала мимо проверки)."""
    sql = "SELECT * FROM ambassador_waves WHERE starts_at <= ? AND ends_at >= ?"
    params = [ends_at, starts_at]
    if event_city is not None:
        sql += " AND (event_city IS NULL OR event_city = ?)"
        params.append(event_city)
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def wave_at(ts: str, event_city: str | None) -> dict | None:
    """Волна, в чьи даты попадает метка `ts`, состояние не 'draft', ближайшая по
    `starts_at DESC` (несколько волн одного города физически не пересекаются —
    `waves_overlapping` это гарантирует на записи, но черновик исключён явно здесь)."""
    sql = (
        "SELECT * FROM ambassador_waves WHERE starts_at <= ? AND ends_at >= ? "
        "AND state != 'draft' AND (event_city IS NULL"
    )
    params = [ts, ts]
    if event_city is not None:
        sql += " OR event_city = ?"
        params.append(event_city)
    sql += ") ORDER BY starts_at DESC LIMIT 1"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ── Phase 32 (32-01, D-24/D-31/D-32): амбассадоры — аксессоры ──────────────────────────────

async def set_ambassador_flag(telegram_id: int, *, active: bool, at: str) -> bool:
    """Единственная точка записи is_ambassador/ambassador_since/ambassador_left_at.
    `active=True` — is_ambassador=1, ambassador_since=at, ambassador_left_at=NULL (новый
    заход снова активен). `active=False` — is_ambassador=0, ambassador_left_at=at,
    ambassador_since НЕ трогает (когда человек стал амбассадором — исторический факт).

    CR-08 (32-REVIEW.md): `active=True` идемпотентна на уровне SQL — `WHERE ... AND
    COALESCE(is_ambassador, 0) = 0` не даёт ДЕЙСТВУЮЩЕМУ амбассадору переставить свой
    `ambassador_since` повторным тапом «Хочу свою ссылку»/перезаполнением анкеты (кнопки
    Telegram не истекают, предложение приходит после каждого финала анкеты). `rowcount == 0`
    у уже активного амбассадора — не ошибка вызывающего, а нормальный итог: дата вступления
    исторический факт, трогать её нечего. Возврат ПОСЛЕ выхода (D-38, `is_ambassador = 0`)
    проходит условие как обычно и получает свежую дату — это осознанно другой случай."""
    # Тонкая обёртка: писать статус и зеркало is_ambassador можно только в amb_status_db.
    # `expect` без 'active' и есть условие CR-08 — у действующего амбассадора rowcount 0.
    from database import amb_status_db
    if active:
        return await amb_status_db.set_status(
            telegram_id, amb_status_db.STATUS_ACTIVE, at=at, by=None,
            expect=(amb_status_db.STATUS_NONE, amb_status_db.STATUS_CANDIDATE,
                    amb_status_db.STATUS_LEFT, amb_status_db.STATUS_DECLINED),
        )
    return await amb_status_db.set_status(telegram_id, amb_status_db.STATUS_LEFT, at=at, by=None)


async def set_ambassador_path(telegram_id: int, path: str | None) -> bool:
    """`path=None` очищает выбор. True iff пользователь существовал."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE users SET ambassador_path = ? WHERE telegram_id = ?",
            (path, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def list_ambassadors(*, city_scope=None, include_null: bool = True) -> list[dict]:
    """Все users с is_ambassador = 1."""
    frag, params = _city_clause(city_scope, "event_city", include_null=include_null)
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, full_name, username, event_city, ambassador_since, "
            f"ambassador_path FROM users WHERE is_ambassador = 1{extra}",
            tuple(params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


# ── Phase 32 (32-01, D-22): начисления за приглашённых — аксессоры ─────────────────────────

async def claim_referral_credit(invitee_id: int, referrer_id: int, coins: int,
                                 wave_id: int | None, *, source: str = "approval") -> bool:
    """Идиома `add_staff` дословно: `INSERT OR IGNORE` против PRIMARY KEY (invitee_id) +
    `rowcount == 1`. Никакой предварительной проверки «а не начисляли ли уже» в Python —
    уникальность держит первичный ключ, повтор/гонка/ретрай физически не создают вторую
    строку (T-32-01-01)."""
    credited_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO referral_credits "
            "(invitee_id, referrer_id, coins, wave_id, credited_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invitee_id, referrer_id, coins, wave_id, credited_at, source),
        )
        await db.commit()
        return cursor.rowcount == 1


async def claim_referral_credit_atomic(invitee_id: int, referrer_id: int, coins: int,
                                        wave_id: int | None, *, reason: str,
                                        changed_by: int | None, source: str = "approval") -> bool:
    """WR-01 (32-REVIEW.md): `claim_referral_credit` + `add_coins` в ОДНОЙ транзакции вместо
    двух отдельных соединений/коммитов. Раньше сбой (`database is locked`, рестарт бота)
    МЕЖДУ строкой-квитанцией `referral_credits` и записью в леджер `coins` навсегда терял
    начисление: квитанция уже есть, монет нет, а повторный вызов и бэкафилл видят квитанцию
    (`get_referral_credit`) и молча пропускают — начислить второй раз уже нельзя.

    `INSERT OR IGNORE` против `PRIMARY KEY (invitee_id)` побеждает первым, как и раньше;
    `rowcount != 1` значит, что этого приглашённого уже начислил другой вызов (гонка/повтор) —
    вторая половина (`INSERT INTO coins`) не выполняется вовсе, транзакция коммитится без
    изменений. `source` — это `referral_credits.source` (`'approval'`/`'backfill'`);
    `coins.source` жёстко `'referral'`, byte-identical прежнему отдельному
    `add_coins(..., source="referral")`. Старый `claim_referral_credit` НЕ удалён — им
    по-прежнему пользуются тесты и код, которым нужна голая квитанция без начисления."""
    credited_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO referral_credits "
            "(invitee_id, referrer_id, coins, wave_id, credited_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invitee_id, referrer_id, coins, wave_id, credited_at, source),
        )
        won = cursor.rowcount == 1
        if won:
            await db.execute(
                "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp, source, "
                "task_id) VALUES (?, ?, ?, ?, ?, 'referral', NULL)",
                (referrer_id, coins, reason, changed_by, credited_at),
            )
        await db.commit()
        return won


_JOURNAL_COUNTED = (
    "COALESCE(referrer_was_ambassador, 1) = 1 AND excluded_at IS NULL AND revoked_at IS NULL"
)


async def get_referral_credit(invitee_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM referral_credits WHERE invitee_id = ?", (invitee_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_referral_credits(*, referrer_id: int | None = None,
                                 wave_id: int | None = None) -> list[dict]:
    # Журнал пишет строки и для приглашённых обычных делегатов (баллы 0) — в списке только
    # засчитанные амбассадору. NULL = строка до журнала: тогда писали только амбассадорам.
    clauses, params = [_JOURNAL_COUNTED], []
    if referrer_id is not None:
        clauses.append("referrer_id = ?")
        params.append(referrer_id)
    if wave_id is not None:
        clauses.append("wave_id = ?")
        params.append(wave_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM referral_credits{where} ORDER BY credited_at ASC", tuple(params),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def count_referral_credits(referrer_id: int, wave_id: int | None) -> int:
    """Сколько приглашённых этого амбассадора уже начислено в этой волне — подсказка
    модератору (план 32-05). `wave_id=None` считает начисления ВНЕ волн (`IS NULL`, обычное
    равенство `= NULL` в SQL не матчит NULL-строки)."""
    cond = "wave_id IS NULL" if wave_id is None else "wave_id = ?"
    params = [referrer_id] if wave_id is None else [referrer_id, wave_id]
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM referral_credits WHERE referrer_id = ? AND {cond} "
            f"AND {_JOURNAL_COUNTED}",
            params,
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


# ── Phase 32 (32-01/32-11, D-16/D-17): снимок итогов волны — аксессоры ─────────────────────

async def insert_wave_results(wave_id: int, rows: list[tuple[int, int, int]],
                               announced_at: str) -> int:
    """`rows` — список (user_id, place, points), каждая строка пишется как призовая
    (`is_winner = 1`) — прежнее, всё ещё живое поведение этого аксессора (используется вне
    `announce_results`, например бэкафиллом/тестами очистки пользователя). Одна транзакция,
    `INSERT OR IGNORE` против `PRIMARY KEY (wave_id, user_id)` — повторный вызов вставляет 0
    строк, снимок остаётся неизменным (D-17). Возвращает число РЕАЛЬНО вставленных строк.

    Полный снимок ВСЕХ участников волны (не только призёров), нужный `announce_results`,
    пишет `announce_wave_atomic` ниже — она же атомарно переводит волну в `announced`."""
    inserted = 0
    async with _connect() as db:
        for user_id, place, points in rows:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO wave_results "
                "(wave_id, user_id, place, points, is_winner, announced_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (wave_id, user_id, place, points, announced_at),
            )
            inserted += cursor.rowcount
        await db.commit()
        return inserted


async def announce_wave_atomic(
    wave_id: int, rows: list[tuple[int, int, int, bool]], announced_at: str,
) -> bool:
    """CR-06: переход `closing -> announced` и запись снимка ВСЕХ участников волны ОДНОЙ
    транзакцией/одним `_connect()` — тот же приём, что `approve_user_atomic`: `rowcount == 1`
    у `UPDATE ... WHERE state = 'closing'` — единственный арбитр гонки (двойной клик «Объявить
    итоги», два менеджера одновременно). Проигравший вызов не пишет ни строки — до этой правки
    переход состояния коммитился ПЕРВЫМ отдельным вызовом, а снимок — вторым, и падение между
    ними (`database is locked`, рестарт) оставляло волну `announced` без единой строки снимка,
    без пути восстановления из UI.

    `rows` — (user_id, place, points, is_winner) для КАЖДОГО участника волны на момент
    объявления, посчитанного вызывающим ДО открытия этой транзакции (рейтинг — read-only,
    коротким чтением до флипа; сам флип и остаётся арбитром гонки, а не повторным чтением
    рейтинга внутри транзакции). Возвращает True, только если ИМЕННО этот вызов выполнил
    переход — вызывающий в этом случае и только в этом обязан ставить джобу рассылки."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE ambassador_waves SET state = 'announced' WHERE id = ? AND state = 'closing'",
            (wave_id,),
        )
        if cursor.rowcount != 1:
            await db.commit()
            return False
        for user_id, place, points, is_winner in rows:
            await db.execute(
                "INSERT OR IGNORE INTO wave_results "
                "(wave_id, user_id, place, points, is_winner, announced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (wave_id, user_id, place, points, 1 if is_winner else 0, announced_at),
            )
        await db.commit()
        return True


async def get_wave_results(wave_id: int, *, winners_only: bool = True) -> list[dict]:
    """По умолчанию — ТОЛЬКО призёры (`is_winner = 1`), прежнее поведение этого аксессора для
    существующих читателей (карточка волны, тесты очистки пользователя, бэкафилл). CR-06:
    рассылке итогов и «сколько ещё не разослано» нужен ПОЛНЫЙ снимок — `winners_only=False`."""
    where = " AND is_winner = 1" if winners_only else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM wave_results WHERE wave_id = ?{where} ORDER BY place ASC", (wave_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def mark_wave_result_notified(wave_id: int, user_id: int, when: str) -> bool:
    """CR-06: персональная отметка «этому участнику итоги волны уже отправлены» —
    `notified_at IS NULL` в WHERE делает рассылку идемпотентной и возобновляемой (тот же приём,
    что `mark_wave_started`/`mark_nudged`): повторный тик джобы после сбоя/переармирования не
    шлёт второе сообщение уже отправленным, а недослав хвост — отправляет только его."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE wave_results SET notified_at = ? "
            "WHERE wave_id = ? AND user_id = ? AND notified_at IS NULL",
            (when, wave_id, user_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def count_wave_results_pending_notify(wave_id: int) -> int:
    """CR-06: сколько строк снимка волны ещё не разослано — `reconcile_wave_jobs` этим числом
    решает, нужно ли заново ставить джобу рассылки итогов для `announced`-волны."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM wave_results WHERE wave_id = ? AND notified_at IS NULL",
            (wave_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


# ── Phase 32 (32-01, D-14): суммы для рейтинга волны — сырой SQL, правила в сервисе плана
# 32-03 ────────────────────────────────────────────────────────────────────────────────────

async def sum_task_coins_for_wave(wave_id: int) -> dict[int, int]:
    """JOIN строго по coins.task_id -> game_tasks.id, НИКОГДА по user_id — иначе у делегата
    с двумя сдачами разных заданий одной волны сумма удвоилась бы через второй join-путь
    (RESEARCH Pitfall 4). Легаси-строки coins без task_id в сумму волны не попадают."""
    async with _connect() as db:
        async with db.execute(
            "SELECT c.user_id, SUM(c.delta) FROM coins c "
            "JOIN game_tasks t ON t.id = c.task_id "
            "WHERE t.wave_id = ? AND c.source = 'task' GROUP BY c.user_id",
            (wave_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: int(row[1]) for row in rows}


async def sum_referral_coins_for_wave(wave_id: int) -> dict[int, int]:
    """Ключ — referrer_id (кому начислено за приглашённого), а не invitee_id."""
    async with _connect() as db:
        async with db.execute(
            "SELECT referrer_id, SUM(coins) FROM referral_credits WHERE wave_id = ? "
            "AND excluded_at IS NULL GROUP BY referrer_id",
            (wave_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: int(row[1]) for row in rows}


async def create_submission(task_id: int, user_id: int, content_type: str, content: str,
                             submitted_at: str) -> int | None:
    """Returns the new row's id, or None if `idx_game_submissions_active` rejected a second
    non-rejected submission for this (task_id, user_id) pair — T-09-01, D-05. The caller
    (wave 3) treats None as "already submitted", never re-raises."""
    async with _connect() as db:
        try:
            cursor = await db.execute(
                "INSERT INTO game_submissions (task_id, user_id, content_type, content, "
                "submitted_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, user_id, content_type, content, submitted_at),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            return None


async def get_submission(submission_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_submissions WHERE id = ?", (submission_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ── Quick 260822: очередь дайджеста сдач ────────────────────────────────────────────────────

async def enqueue_game_digest(submission_id: int, user_id: int, task_id: int,
                              city: str | None, created_at: str) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO game_submit_digest_queue (submission_id, user_id, task_id, city, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (submission_id, user_id, task_id, city, created_at),
        )
        await db.commit()
        return cursor.lastrowid


async def list_unsent_game_digest(city: str | None = None, *, all_cities: bool = False) -> list[dict]:
    """Неотправленные строки очереди. `all_cities=True` — вся очередь (для ре-арма на старте);
    иначе строго по `city` (None = строки без города, НЕ «все»)."""
    where = "sent_at IS NULL"
    params: tuple = ()
    if not all_cities:
        if city is None:
            where += " AND city IS NULL"
        else:
            where += " AND city = ?"
            params = (city,)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM game_submit_digest_queue WHERE {where} ORDER BY id", params
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def mark_game_digest_sent(ids: list[int], sent_at: str) -> None:
    if not ids:
        return
    async with _connect() as db:
        await db.executemany(
            "UPDATE game_submit_digest_queue SET sent_at = ? WHERE id = ?",
            [(sent_at, i) for i in ids],
        )
        await db.commit()


# Phase 33 (delegate-card admin actions, перевод города): правит город ТОЛЬКО у ещё
# НЕОТПРАВЛЕННЫХ строк (`sent_at IS NULL`) этого делегата — уже ушедший дайджест адресован
# менеджеру старого города по факту события на момент отправки, переписывать историю нельзя.
# Возвращает число задетых строк (0 — нет живых строк на этого делегата, не ошибка).
async def update_unsent_game_digest_city(user_id: int, new_city: str | None) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_submit_digest_queue SET city = ? WHERE user_id = ? AND sent_at IS NULL",
            (new_city, user_id),
        )
        await db.commit()
        return cursor.rowcount


# ── Квик 260916: очередь дайджеста заявок ───────────────────────────────────────────────────

async def enqueue_reg_digest(telegram_id: int, city: str | None, created_at: str, *,
                              auto_rejected: int = 0, reason: str | None = None) -> int:
    """`auto_rejected` (Phase 31, 31-02, D-17) и `reason` (ревью 25.09) — хвостовые kwargs с
    дефолтами, существующие вызывающие без новых аргументов остаются байт-в-байт прежними.
    `reason` — та же идея, что `auto_rejected`, но для остальных причин постановки, помимо
    обычной новой заявки (NULL): сейчас единственное значение `"revert"` («↩️ Вернуть в
    ожидание»). Штампуется здесь, на постановке в очередь, не выводится позже из
    `users.status` — тот же довод, что у `auto_rejected` (см. `_ensure_column` в `init_db`)."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO reg_submit_digest_queue (telegram_id, city, created_at, auto_rejected, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, city, created_at, auto_rejected, reason),
        )
        await db.commit()
        return cursor.lastrowid


async def list_unsent_reg_digest(city: str | None = None, *, all_cities: bool = False) -> list[dict]:
    """Неотправленные строки очереди. `all_cities=True` — вся очередь (для ре-арма на старте);
    иначе строго по `city` (None = строки без города, НЕ «все»). `SELECT *` — колонка
    `auto_rejected` (D-17) возвращается автоматически, отдельного проецирования не требуется."""
    where = "sent_at IS NULL"
    params: tuple = ()
    if not all_cities:
        if city is None:
            where += " AND city IS NULL"
        else:
            where += " AND city = ?"
            params = (city,)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM reg_submit_digest_queue WHERE {where} ORDER BY id", params
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def mark_reg_digest_sent(ids: list[int], sent_at: str) -> None:
    if not ids:
        return
    async with _connect() as db:
        await db.executemany(
            "UPDATE reg_submit_digest_queue SET sent_at = ? WHERE id = ?",
            [(sent_at, i) for i in ids],
        )
        await db.commit()


# Phase 33 (delegate-card admin actions, перевод города): та же логика, что у
# update_unsent_game_digest_city выше — правит ТОЛЬКО неотправленные строки этого делегата,
# уже отправленный дайджест не переписываем (адресован менеджеру старого города по факту).
async def update_unsent_reg_digest_city(telegram_id: int, new_city: str | None) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE reg_submit_digest_queue SET city = ? WHERE telegram_id = ? AND sent_at IS NULL",
            (new_city, telegram_id),
        )
        await db.commit()
        return cursor.rowcount


# ── Quick 260904-dq1: очередь «🌙 Тихие часы» ──────────────────────────────────────────────

async def enqueue_delayed_notification(user_id: int, kind: str, payload: dict, due_at: str,
                                       created_at: str, *, replace: bool) -> int:
    """Кладёт строку в очередь; `replace=True` — сначала удаляет ЛЮБУЮ ещё не отправленную
    строку той же пары (user_id, kind) в ОДНОЙ транзакции («последнее решение выигрывает» —
    services.quiet_hours.REPLACEABLE_KINDS). `replace=False` — строки копятся списком."""
    async with _connect() as db:
        if replace:
            await db.execute(
                "DELETE FROM delayed_notifications WHERE user_id = ? AND kind = ? "
                "AND sent_at IS NULL",
                (user_id, kind),
            )
        cursor = await db.execute(
            "INSERT INTO delayed_notifications (user_id, kind, payload, due_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, json.dumps(payload, ensure_ascii=False), due_at, created_at),
        )
        await db.commit()
        return cursor.lastrowid


async def list_due_delayed_notifications(now: str, limit: int = 100) -> list[dict]:
    """Неотправленные строки с `due_at <= now`, в порядке постановки; `payload` уже разобран
    из JSON обратно в dict."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM delayed_notifications WHERE sent_at IS NULL AND due_at <= ? "
            "ORDER BY id LIMIT ?",
            (now, int(limit)),
        ) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        try:
            row["payload"] = json.loads(row["payload"])
        except (TypeError, ValueError):
            row["payload"] = {}
    return rows


async def mark_delayed_notification_sent(row_id: int, sent_at: str, error: str | None = None) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE delayed_notifications SET sent_at = ?, error = ? WHERE id = ?",
            (sent_at, error, row_id),
        )
        await db.commit()


async def count_pending_delayed_notifications() -> int:
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM delayed_notifications WHERE sent_at IS NULL"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


# ── Phase 19 (D-01): outbox побочных эффектов Mini App ─────────────────────────────────────

MINIAPP_OUTBOX_ERROR_MAX = 500


async def enqueue_miniapp_outbox(kind: str, payload: dict, created_at: str) -> int:
    """Кладёт событие в outbox; `payload` сериализуется в JSON (ensure_ascii=False).
    Бросает `aiosqlite.OperationalError`, если таблицы ещё нет — fail-soft делает
    вызывающий (`miniapp.outbox.enqueue`)."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO miniapp_outbox (kind, payload, created_at) VALUES (?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), created_at),
        )
        await db.commit()
        return cursor.lastrowid


async def get_chat_rating_posted_week(city: str | None) -> str | None:
    """Неделя (дата её понедельника), за которую пост рейтинга уже ушёл в чат города."""
    async with _connect() as db:
        async with db.execute(
            "SELECT week FROM chat_rating_posts WHERE city = ?", (city or "",)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_chat_rating_posted_week(city: str | None, week: str) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO chat_rating_posts (city, week, posted_at) VALUES (?, ?, ?) "
            "ON CONFLICT(city) DO UPDATE SET week = excluded.week, posted_at = excluded.posted_at",
            (city or "", week, msk_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()


async def enqueue_miniapp_outbox_once(kind: str, payload: dict, created_at: str) -> int | None:
    """То же, что `enqueue_miniapp_outbox`, но не дублирует: событие того же `kind` с тем же
    payload (обработанное или нет) уже есть — ничего не пишет и возвращает None. Проверка и
    вставка — один INSERT … WHERE NOT EXISTS. Для событий, которые повтор нажатия обязан
    восстановить, но не размножить (одобрение у стойки: payload несёт `onsite_at` решения)."""
    text = json.dumps(payload, ensure_ascii=False)
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO miniapp_outbox (kind, payload, created_at) SELECT ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM miniapp_outbox WHERE kind = ? AND payload = ?)",
            (kind, text, created_at, kind, text),
        )
        await db.commit()
        return cursor.lastrowid if cursor.rowcount == 1 else None


async def list_unprocessed_miniapp_outbox(limit: int = 50) -> list[dict]:
    """Необработанные события в порядке `id`; `payload` уже разобран из JSON."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM miniapp_outbox WHERE processed_at IS NULL ORDER BY id LIMIT ?",
            (int(limit),),
        ) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
    for row in rows:
        try:
            row["payload"] = json.loads(row["payload"])
        except (TypeError, ValueError):
            row["payload"] = {}
    return rows


async def mark_miniapp_outbox_processed(ids: list[int], processed_at: str) -> None:
    if not ids:
        return
    async with _connect() as db:
        await db.executemany(
            "UPDATE miniapp_outbox SET processed_at = ? WHERE id = ?",
            [(processed_at, i) for i in ids],
        )
        await db.commit()


async def mark_miniapp_outbox_failed(row_id: int, error: str) -> None:
    """Неудачная попытка: `attempts + 1`, текст ошибки усечён до 500 символов, строка
    остаётся необработанной (джоба бота решает, когда сдаться)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE miniapp_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            ((error or "")[:MINIAPP_OUTBOX_ERROR_MAX], row_id),
        )
        await db.commit()


async def purge_miniapp_outbox_for_user(telegram_id: int) -> int:
    """Квик 260911-mx6 (сеялка состояний приёмки): хвост, который `purge_user` сознательно
    НЕ трогает — `miniapp_outbox` в `USER_PURGE_EXCLUDED`, потому что это очередь побочных
    эффектов Mini App (объект бота, не делегатский след, см. комментарий у
    USER_PURGE_EXCLUDED выше). Но сеялке эта таблица всё же нужна: если после сброса тестера
    в ней остаётся необработанное событие по старому состоянию, делегату после /start
    прилетает стухшее уведомление Mini App (и, отдельно, напоминание об оплате — то снимает
    `cancel_payment_reminders`, это не про jobs.sqlite).

    Удаляет только НЕобработанные (`processed_at IS NULL`) события, чей payload несёт этого
    человека (`user_id` ИЛИ `telegram_id` внутри JSON) — уже обработанные и чужие события не
    трогает. Разбор JSON в Python (`json.loads` в try/except, битая строка пропускается) —
    НЕ `json_extract`: JSON1 нигде в проекте не используется, заводить зависимость от сборки
    SQLite ради одной функции незачем. Возвращает число удалённых строк; на пустой очереди —
    0 без падения."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, payload FROM miniapp_outbox WHERE processed_at IS NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        matched_ids: list[int] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("user_id") == telegram_id or payload.get("telegram_id") == telegram_id:
                matched_ids.append(row["id"])
        if not matched_ids:
            return 0
        placeholders = ",".join("?" * len(matched_ids))
        cursor = await db.execute(
            f"DELETE FROM miniapp_outbox WHERE id IN ({placeholders})", matched_ids
        )
        await db.commit()
        return cursor.rowcount


# ── Очередь записи «Пришёл» в Google-лист (нагрузочный прогон 25.09) ───────────────────────

SHEET_ARRIVAL_SET = "set"
SHEET_ARRIVAL_RECOMPUTE = "recompute"
SHEET_ARRIVAL_ERROR_MAX = 500


async def enqueue_sheet_arrival(telegram_id: int, action: str, city: str | None = None) -> None:
    """Событие «пересчитать ячейку «Пришёл» делегата». Зовут отметка/снятие/CSV в ОБОИХ
    процессах; Google здесь не трогается. Fail-soft: таблицы ещё нет (Mini App поднялся раньше
    миграции бота) или база занята — предупреждение в лог, отметка в `checkins` уже сохранена."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        async with _connect() as db:
            await db.execute(
                "INSERT INTO sheet_arrival_queue (telegram_id, city, action, created_at, next_try_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(telegram_id), city, action, now, now),
            )
            await db.commit()
    except Exception as e:
        logger.warning("sheet_arrival_queue: не поставил событие для %s: %s", telegram_id, e)


async def list_due_sheet_arrivals(now: str, limit: int) -> list[dict]:
    """Созревшие события (`next_try_at <= now`) в порядке id."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sheet_arrival_queue WHERE next_try_at <= ? ORDER BY id LIMIT ?",
            (now, int(limit)),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def drop_sheet_arrivals(upto: dict[int, int]) -> int:
    """Удалить события делегатов `{telegram_id: max_id}` с id <= max_id — всё, что было в
    очереди к моменту чтения значения; событие, пришедшее позже, остаётся на следующий тик."""
    if not upto:
        return 0
    async with _connect() as db:
        cursor = await db.executemany(
            "DELETE FROM sheet_arrival_queue WHERE telegram_id = ? AND id <= ?",
            [(tid, max_id) for tid, max_id in upto.items()],
        )
        await db.commit()
        return cursor.rowcount


async def fail_sheet_arrivals(upto: dict[int, int], error: str, next_try_at: str) -> None:
    """Сбой записи: attempts + 1, текст ошибки (уже без секретов), следующий заход не раньше
    `next_try_at`. Событие остаётся в очереди."""
    if not upto:
        return
    async with _connect() as db:
        await db.executemany(
            "UPDATE sheet_arrival_queue SET attempts = attempts + 1, last_error = ?, next_try_at = ? "
            "WHERE telegram_id = ? AND id <= ?",
            [((error or "")[:SHEET_ARRIVAL_ERROR_MAX], next_try_at, tid, max_id)
             for tid, max_id in upto.items()],
        )
        await db.commit()


# ── Очередь записи «В чате» в Google-лист (29.09) ─────────────────────────────────────────

async def enqueue_sheet_chat_cells(telegram_ids: list[int]) -> None:
    """События «пересчитать ячейку «В чате»» пачкой (одобрение, сверка состава). Fail-soft:
    сбой — предупреждение в лог, лист догонит следующая сверка или пересборка."""
    ids = [int(t) for t in telegram_ids]
    if not ids:
        return
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        async with _connect() as db:
            await db.executemany(
                "INSERT INTO sheet_chat_queue (telegram_id, created_at, next_try_at) VALUES (?, ?, ?)",
                [(tid, now, now) for tid in ids],
            )
            await db.commit()
    except Exception as e:
        logger.warning("sheet_chat_queue: не поставил %s событий: %s", len(ids), e)


async def list_due_sheet_chat(now: str, limit: int) -> list[dict]:
    """Созревшие события очереди «В чате» в порядке id."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sheet_chat_queue WHERE next_try_at <= ? ORDER BY id LIMIT ?",
            (now, int(limit)),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def drop_sheet_chat(upto: dict[int, int]) -> int:
    """Как `drop_sheet_arrivals`: удалить события `{telegram_id: max_id}` с id <= max_id."""
    if not upto:
        return 0
    async with _connect() as db:
        cursor = await db.executemany(
            "DELETE FROM sheet_chat_queue WHERE telegram_id = ? AND id <= ?",
            [(tid, max_id) for tid, max_id in upto.items()],
        )
        await db.commit()
        return cursor.rowcount


async def fail_sheet_chat(upto: dict[int, int], error: str, next_try_at: str) -> None:
    """Как `fail_sheet_arrivals`: attempts + 1, текст ошибки (уже без секретов), backoff."""
    if not upto:
        return
    async with _connect() as db:
        await db.executemany(
            "UPDATE sheet_chat_queue SET attempts = attempts + 1, last_error = ?, next_try_at = ? "
            "WHERE telegram_id = ? AND id <= ?",
            [((error or "")[:SHEET_ARRIVAL_ERROR_MAX], next_try_at, tid, max_id)
             for tid, max_id in upto.items()],
        )
        await db.commit()


async def sheet_arrival_queue_stats(*, exclude_error: str | None = None) -> tuple[int, str | None]:
    """(сколько событий в очереди, created_at самого старого) — для «🚦 Готовность к форуму».
    `exclude_error` — не считать события с этой последней ошибкой (ждущие строку в листе:
    это не затор записи, а делегат, которого ещё нет в листе)."""
    where, params = "", []
    if exclude_error is not None:
        where, params = " WHERE last_error IS NULL OR last_error != ?", [exclude_error]
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*), MIN(created_at) FROM sheet_arrival_queue{where}", params
        ) as cursor:
            row = await cursor.fetchone()
    return (row[0] or 0, row[1]) if row else (0, None)


async def sheet_arrival_count_with_error(error: str) -> int:
    """Сколько делегатов в очереди «Пришёл» с этой последней ошибкой."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT telegram_id) FROM sheet_arrival_queue WHERE last_error = ?", (error,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] or 0 if row else 0


async def get_active_submission(task_id: int, user_id: int) -> dict | None:
    """Most recent non-rejected submission for this pair, or None. Rejected submissions are
    invisible here on purpose — a fresh resubmission after rejection is a NEW row (D-05)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_submissions WHERE task_id = ? AND user_id = ? "
            "AND status != 'rejected' ORDER BY id DESC LIMIT 1",
            (task_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ── Phase 14 (GAME-08/GAME-10): archive/delete + submission counters ─────────────────────
# Same connect/row_factory-free/single-statement idiom as claim_submission's rowcount==1
# atomic-flip contract — no read-then-write race window.

async def archive_task(task_id: int) -> bool:
    """True iff THIS call archived the task (rowcount == 1) — a no-op on an already-archived
    task returns False, same idiom as claim_submission."""
    archived_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (archived_at, task_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def unarchive_task(task_id: int) -> bool:
    """Mirror of archive_task — True iff THIS call returned the task to active."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_tasks SET archived_at = NULL WHERE id = ? AND archived_at IS NOT NULL",
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def count_task_submissions(task_id: int) -> int:
    """ALL statuses, including rejected — a rejected submission is still history, a task
    with one attached can never be hard-deleted (delete_task's own NOT EXISTS gate relies on
    this being non-zero for any submission at all, not just non-rejected ones)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM game_submissions WHERE task_id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def count_task_submissions_by_status(task_ids: list[int]) -> dict[int, dict[str, int]]:
    """Phase 19 (Mini App, менеджерский список заданий): счётчики сдач по статусам для
    НЕСКОЛЬКИХ заданий одним запросом — `{task_id: {"pending": n, "approved": n,
    "rejected": n, "total": n}}`. Задания без сдач в словарь не попадают (вызывающий
    подставляет нули). Пустой список -> пустой словарь без обращения к БД."""
    ids = [int(t) for t in task_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    out: dict[int, dict[str, int]] = {}
    async with _connect() as db:
        async with db.execute(
            f"SELECT task_id, status, COUNT(*) FROM game_submissions "
            f"WHERE task_id IN ({placeholders}) GROUP BY task_id, status",
            tuple(ids),
        ) as cursor:
            for task_id, status, n in await cursor.fetchall():
                bucket = out.setdefault(int(task_id), {"pending": 0, "approved": 0, "rejected": 0, "total": 0})
                if status in bucket:
                    bucket[status] = int(n)
                bucket["total"] += int(n)
    return out


async def delete_task(task_id: int) -> bool:
    """Hard delete — True iff the row existed AND had zero submissions of any status. The
    "no submissions" gate lives INSIDE the single DELETE statement (NOT EXISTS), not as a
    separate Python read-then-decide step — T-14-02: a manager deleting a task the same
    second a delegate submits to it must never silently drop that submission's parent row."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM game_tasks WHERE id = ? AND NOT EXISTS "
            "(SELECT 1 FROM game_submissions WHERE task_id = game_tasks.id)",
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def count_rejected_submissions(task_id: int, user_id: int) -> int:
    """Rejected-only count for one (task, user) pair — GAME-10's resubmit-limit gate. Model
    is get_active_submission's WHERE shape, COUNT instead of a single row."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM game_submissions WHERE task_id = ? AND user_id = ? "
            "AND status = 'rejected'",
            (task_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


# ── Phase 09.1 (A): game_submission_parts accessors ──────────────────────────────────────

async def add_submission_part(submission_id: int, ord: int, kind: str, content: str | None,
                               caption: str | None = None) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO game_submission_parts (submission_id, ord, kind, content, caption) "
            "VALUES (?, ?, ?, ?, ?)",
            (submission_id, ord, kind, content, caption),
        )
        await db.commit()
        return cursor.lastrowid


async def list_submission_parts(submission_id: int) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_submission_parts WHERE submission_id = ? "
            "ORDER BY ord ASC, id ASC",
            (submission_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def find_submissions_by_file_id(file_id: str) -> list[dict]:
    """Phase 19 (T-19-20): сдачи, в которых встречается этот `file_id` — как часть
    (`game_submission_parts.content`) или как legacy-контент первой части
    (`game_submissions.content`). `[{id, user_id, status}]` без дублей."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT DISTINCT s.id, s.user_id, s.status FROM game_submissions s "
            "LEFT JOIN game_submission_parts p ON p.submission_id = s.id "
            "WHERE s.content = ? OR p.content = ?",
            (file_id, file_id),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def is_active_task_cover(file_id: str) -> bool:
    """Phase 19: `file_id` — обложка неархивного задания (её видят все делегаты)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM game_tasks WHERE photo_file_id = ? AND archived_at IS NULL LIMIT 1",
            (file_id,),
        ) as cursor:
            return await cursor.fetchone() is not None


async def get_submission_parts_or_legacy(submission: dict) -> list[dict]:
    """Backward-compat read: a pre-migration submission has no game_submission_parts rows --
    synthesize exactly one part from its legacy content/content_type columns. A submission
    with real parts rows ignores the legacy columns entirely (CONTEXT.md A)."""
    parts = await list_submission_parts(submission["id"])
    if parts:
        return parts
    content = submission.get("content")
    if not content:
        return []
    kind = _LEGACY_KIND_MAP.get(submission.get("content_type"), "text")
    return [{"ord": 0, "kind": kind, "content": content, "caption": None}]


async def get_pending_submissions(limit: int = 1, offset: int = 0, *, city_scope=None) -> list[dict]:
    """Paginated moderation queue (CLAUDE.md: 1000+ submissions must never be one message per
    row), same LIMIT/OFFSET shape as get_pending_users. Joins game_tasks/users so the card
    (wave 4) needs zero extra queries — task_deadline_at lets the card flag "after deadline"
    per the soft-deadline decision (A-05, call 13.08) without a second lookup.
    Phase 09.1 (B): `city_scope` filters by u.event_city (the DELEGATE's city, same pattern
    as the applications/receipts queues in 07.2), NOT the task's city — a manager scoped to
    spb must see spb delegates' submissions regardless of which city the task itself was
    addressed to. No `include_null` -- for the default city, NULL already lands via the
    exclusion-shape branch of `_city_clause`, exactly like get_pending_users; passing
    include_null here would also surface delegates with no city to every non-default-city
    manager, which is not what CONTEXT.md's "тот же паттерн, что заявки/чеки в 07.2" asks
    for."""
    frag, city_params = _city_clause(city_scope, "u.event_city")
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, t.text AS task_text, t.title AS task_title, "
            "t.category AS task_category, "
            "t.coins AS task_coins, t.proof_type AS task_proof_type, "
            "t.deadline_at AS task_deadline_at, t.event_city AS task_event_city, "
            "t.archived_at AS task_archived_at, "
            "u.full_name AS user_full_name, u.username AS user_username, "
            "u.event_city AS user_event_city "
            "FROM game_submissions s "
            "JOIN game_tasks t ON t.id = s.task_id "
            "LEFT JOIN users u ON u.telegram_id = s.user_id "
            f"WHERE s.status = 'pending'{extra} "
            "ORDER BY s.submitted_at ASC, s.id ASC LIMIT ? OFFSET ?",
            (*city_params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_pending_submissions_count(*, city_scope=None) -> int:
    frag, city_params = _city_clause(city_scope, "u.event_city")
    extra = f" AND {frag}" if frag else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) FROM game_submissions s "
            "LEFT JOIN users u ON u.telegram_id = s.user_id "
            f"WHERE s.status = 'pending'{extra}",
            tuple(city_params),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def claim_submission(submission_id: int, admin_id: int, status: str, *,
                            coins_awarded: int | None = None,
                            reject_reason: str | None = None) -> bool:
    """Atomic single-row claim (same idiom as approve_user_atomic/claim_question — T-08-27):
    True iff THIS call flipped the row (rowcount==1); a concurrent second claim on the same
    submission_id returns False and its coins_awarded/reject_reason never lands (T-09-02).
    Crediting coins via add_coins is NOT done here — the caller (wave 4) does it as a separate
    step, only after this returns True, same two-step shape as appr_approve -> approve_user."""
    reviewed_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE game_submissions SET status = ?, reviewed_by = ?, reviewed_at = ?, "
            "coins_awarded = ?, reject_reason = ? WHERE id = ? AND status = 'pending'",
            (status, admin_id, reviewed_at, coins_awarded, reject_reason, submission_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def list_all_submissions() -> list[dict]:
    """Full submission history (wave 5's "История сдач" sheet/list) — same join shape as
    get_pending_submissions, no status filter, oldest first. No `city_scope` here — Phase
    09.1 (B, CONTEXT.md "Уточнение (ночь 17→18.08…)"): the sheet tabs are whole-event
    exports rebuilt by a background debounce with no admin identity, unlike the live queue
    above; `user_event_city` is exposed so the sheet builder can add a "Город" COLUMN
    instead of filtering rows."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, t.text AS task_text, t.title AS task_title, "
            "t.category AS task_category, "
            "t.coins AS task_coins, t.proof_type AS task_proof_type, "
            "t.deadline_at AS task_deadline_at, t.event_city AS task_event_city, "
            "t.archived_at AS task_archived_at, "
            "u.full_name AS user_full_name, u.username AS user_username, "
            "u.event_city AS user_event_city "
            "FROM game_submissions s "
            "JOIN game_tasks t ON t.id = s.task_id "
            "LEFT JOIN users u ON u.telegram_id = s.user_id "
            "ORDER BY s.submitted_at ASC"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_game_stats() -> dict:
    """Four aggregate reads for the stats screen (wave 6): distinct participants, counts by
    submission status, and an approved-only breakdown by task category."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM game_submissions"
        ) as cursor:
            row = await cursor.fetchone()
            participants = int(row[0]) if row and row[0] is not None else 0

        stats = {"participants": participants, "pending": 0, "approved": 0, "rejected": 0}
        async with db.execute(
            "SELECT status, COUNT(*) FROM game_submissions GROUP BY status"
        ) as cursor:
            for status, count in await cursor.fetchall():
                if status in stats:
                    stats[status] = int(count)

        by_category: dict[str, int] = {}
        async with db.execute(
            "SELECT t.category, COUNT(*) FROM game_submissions s "
            "JOIN game_tasks t ON t.id = s.task_id "
            "WHERE s.status = 'approved' GROUP BY t.category"
        ) as cursor:
            for category, count in await cursor.fetchall():
                by_category[category] = int(count)
        stats["by_category"] = by_category

        return stats


# ── Phase 14 (CITY-07): `cities` table accessors ────────────────────────────────────────────
#
# Pure SQL layer only -- this module NEVER imports `cities.py` (that would create an import
# cycle: `cities.py` already imports `database.db`). Business logic (cache reload, seed-from-
# .env, transliteration) lives entirely in `cities.py`; this file only stores/reads/counts rows.

async def list_cities_rows() -> list[dict]:
    """Every city row, ordered the way the registry and every UI screen renders them."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM cities ORDER BY sort_order ASC, code ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def count_cities() -> int:
    async with _connect() as db:
        async with db.execute("SELECT COUNT(*) FROM cities") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def insert_city(code: str, label: str, tab_base: str | None, sort_order: int, enabled: int = 1) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO cities (code, label, tab_base, enabled, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, label, tab_base, enabled, sort_order, msk_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
    await _maybe_enqueue_city_label_translation(label, origin_key=f"city_label__{code}")


# Closed whitelist of updatable columns -- column names are NEVER taken from a caller argument
# (WR-08 discipline, same as `_assert_identifier`): only these four literals ever reach SQL.
_CITY_UPDATABLE_COLUMNS = ("label", "tab_base", "enabled", "sort_order")


async def update_city(
    code: str, *, label: str | None = None, tab_base: str | None = None,
    enabled: int | None = None, sort_order: int | None = None,
) -> bool:
    """Updates only the passed (non-None) fields. Returns False on an unknown `code` or when
    no field was passed (nothing to update -- rowcount stays 0)."""
    fields = {
        "label": label, "tab_base": tab_base, "enabled": enabled, "sort_order": sort_order,
    }
    set_parts = [f"{col} = ?" for col in _CITY_UPDATABLE_COLUMNS if fields[col] is not None]
    values = [fields[col] for col in _CITY_UPDATABLE_COLUMNS if fields[col] is not None]
    if not set_parts:
        return False
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE cities SET {', '.join(set_parts)} WHERE code = ?", (*values, code),
        )
        await db.commit()
        result = cursor.rowcount == 1
    if result and label is not None:
        await _maybe_enqueue_city_label_translation(label, origin_key=f"city_label__{code}")
    return result


async def _maybe_enqueue_city_label_translation(label: str, *, origin_key: str) -> None:
    """Задача «делегатский интерфейс на английском»: подпись города мероприятия
    («Москва, 30-31 октября») — свободный текст, который менеджер вводит на сезон, не дефолт
    реестра — `services.i18n_sources.is_delegate_dynamic_key` (рассчитана на ключи
    `bot_settings`, не на строки таблицы `cities`) его не узнаёт, поэтому копия
    `database.db._maybe_enqueue_translation`, а не вызов той функции: тот же гейт
    `delegate_lang_enabled`, тот же fail-soft (T-27-03-04 — запись города к этому моменту уже
    закоммичена, сбой очереди её не откатывает), но без обращения к `is_delegate_dynamic_key`,
    которое здесь всегда сказало бы «нет» и молча не поставило бы подпись города в очередь."""
    try:
        if not label:
            return
        from settings_schema import get_setting_typed

        if await get_setting_typed("delegate_lang_enabled") != "on":
            return

        from services.i18n import src_hash

        await enqueue_translation("en", src_hash(label), label, origin_key=origin_key)
    except Exception as exc:  # noqa: BLE001 — намеренно широкий fail-soft (T-27-03-04)
        logger.error(
            "update_city/insert_city: постановка подписи города в очередь перевода не удалась (%s)",
            exc,
        )


async def delete_city_row(code: str) -> bool:
    """Removes the row. No cascade -- checking "no users/tasks reference this city" is the
    caller's job (plan 14-07); this accessor only performs the DELETE."""
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM cities WHERE code = ?", (code,))
        await db.commit()
        return cursor.rowcount == 1


async def count_users_by_city(code: str) -> int:
    """Delegates bound to this `event_city`. NULL rows ("no city on record") are never counted
    here -- they are not a binding to any specific city, per `cities.normalize_city`'s own
    read-time-only resolution."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE event_city = ?", (code,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def count_tasks_by_city(code: str) -> int:
    """Tasks scoped to this `event_city`. NULL rows ("all cities") are never counted here --
    same NULL-is-not-a-binding semantics as `count_users_by_city`."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM game_tasks WHERE event_city = ?", (code,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


# ── Опросы (native Telegram polls) ───────────────────────────────────────────────────────────
#
# Статусы `polls.status`: 'scheduled' (ждёт джобу / отправку) → 'sending' (клейм, идёт цикл
# send_poll) → 'open' (разослан, принимает ответы) → 'closed' (stop_poll разослан). Удаление —
# физическое (delete_poll): вместе с poll_messages и poll_answers.

POLL_STATUS_LABELS = {
    "scheduled": "🕒 Запланирован",
    "sending": "⏳ Отправляется",
    "open": "🟢 Открыт",
    "closed": "⏹ Закрыт",
}


def _now_str() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


async def create_poll(
    question: str,
    options: list[str],
    *,
    is_anonymous: bool,
    allows_multiple: bool,
    created_by: int,
    city: str | None,
    audience: list[dict] | None,
    scheduled_at: str,
) -> int:
    """Новый опрос в статусе 'scheduled'. `audience` — filter_spec рассылок ([]/None = все)."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, created_by, "
            "created_at, city, audience_json, status, scheduled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?)",
            (
                question, json.dumps(list(options), ensure_ascii=False),
                1 if is_anonymous else 0, 1 if allows_multiple else 0,
                created_by, _now_str(), city,
                json.dumps(audience or [], ensure_ascii=False), scheduled_at,
            ),
        )
        await db.commit()
        return cursor.lastrowid


def _poll_row(row) -> dict:
    d = dict(row)
    try:
        d["options"] = json.loads(d.get("options_json") or "[]")
    except (TypeError, ValueError):
        d["options"] = []
    try:
        d["audience"] = json.loads(d.get("audience_json") or "[]")
    except (TypeError, ValueError):
        d["audience"] = []
    return d


async def get_poll(poll_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
            return _poll_row(row) if row else None


async def list_polls(*, statuses: tuple[str, ...] | None = None, city_scope=None) -> list[dict]:
    """Опросы, новые сверху. `city_scope` — (code, exclude) из cities.city_scope: опрос попадает
    в список, если адресован этому городу или всем городам (city IS NULL)."""
    where, params = [], []
    if statuses:
        where.append(f"status IN ({', '.join('?' * len(statuses))})")
        params.extend(statuses)
    if city_scope:
        where.append("(city IS NULL OR city = ?)")
        params.append(city_scope[0])
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT * FROM polls{clause} ORDER BY id DESC", params) as cursor:
            return [_poll_row(r) for r in await cursor.fetchall()]


async def claim_poll_sending(poll_id: int) -> int:
    """Атомарный клейм 'scheduled' → 'sending' (как mark_broadcast_sending). 0 = уже занят."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE polls SET status = 'sending', sending_since = ? "
            "WHERE id = ? AND status = 'scheduled'",
            (_now_str(), poll_id),
        )
        await db.commit()
        return cursor.rowcount


async def reclaim_stale_sending_polls(max_age_minutes: int) -> list[int]:
    """Опросы, застрявшие в 'sending' дольше порога (крах посреди рассылки) → 'scheduled',
    чтобы реконсиляция на буте дослала хвост. Повтор безопасен: deliver пропускает чаты,
    уже записанные в poll_messages."""
    cutoff = (msk_now() - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        async with db.execute(
            "SELECT id FROM polls WHERE status = 'sending' AND sending_since IS NOT NULL "
            "AND sending_since < ?",
            (cutoff,),
        ) as cursor:
            ids = [r[0] for r in await cursor.fetchall()]
        if ids:
            await db.execute(
                f"UPDATE polls SET status = 'scheduled' WHERE id IN ({', '.join('?' * len(ids))})",
                ids,
            )
            await db.commit()
        return ids


async def set_poll_status(poll_id: int, status: str):
    async with _connect() as db:
        if status == "closed":
            await db.execute(
                "UPDATE polls SET status = ?, closed_at = ? WHERE id = ?",
                (status, _now_str(), poll_id),
            )
        else:
            await db.execute("UPDATE polls SET status = ? WHERE id = ?", (status, poll_id))
        await db.commit()


async def delete_poll(poll_id: int):
    async with _connect() as db:
        await db.execute("DELETE FROM poll_answers WHERE poll_id = ?", (poll_id,))
        await db.execute("DELETE FROM poll_messages WHERE poll_id = ?", (poll_id,))
        await db.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
        await db.commit()


async def record_poll_message(
    poll_id: int, chat_id: int, telegram_poll_id: str | None, message_id: int | None, ok: bool
):
    """Чекпоинт одной попытки send_poll (ok | failed) — INSERT OR REPLACE, как mark_delivery."""
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO poll_messages "
            "(poll_id, chat_id, telegram_poll_id, message_id, status) VALUES (?, ?, ?, ?, ?)",
            (poll_id, chat_id, telegram_poll_id, message_id, "ok" if ok else "failed"),
        )
        await db.commit()


async def list_poll_sent_chat_ids(poll_id: int) -> set[int]:
    """Чаты, уже обработанные (ok И failed) — дошлёт после рестарта их пропускает."""
    async with _connect() as db:
        async with db.execute(
            "SELECT chat_id FROM poll_messages WHERE poll_id = ?", (poll_id,)
        ) as cursor:
            return {r[0] for r in await cursor.fetchall()}


async def list_poll_messages(poll_id: int) -> list[dict]:
    """Только доставленные (есть message_id) — цели для stop_poll и источник totals_json."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT poll_id, chat_id, telegram_poll_id, message_id, totals_json FROM poll_messages "
            "WHERE poll_id = ? AND status = 'ok' AND message_id IS NOT NULL",
            (poll_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def count_poll_deliveries(poll_id: int) -> tuple[int, int]:
    async with _connect() as db:
        async with db.execute(
            "SELECT SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) "
            "FROM poll_messages WHERE poll_id = ?",
            (poll_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0), int(row[1] or 0)


async def get_poll_id_by_telegram_poll(telegram_poll_id: str) -> int | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT poll_id FROM poll_messages WHERE telegram_poll_id = ?", (telegram_poll_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else None


async def set_poll_message_totals(telegram_poll_id: str, totals: dict) -> bool:
    """Последние счётчики Telegram-опроса ({"total": N, "options": [n0, n1, ...]}) из update
    `poll`. True — строка найдена (это наш опрос)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE poll_messages SET totals_json = ? WHERE telegram_poll_id = ?",
            (json.dumps(totals, ensure_ascii=False), telegram_poll_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def upsert_poll_answer(poll_id: int, user_id: int, option_ids: list[int]):
    """Ответ делегата (перезаписывает прошлый). Пустой список = отзыв голоса — строка удаляется."""
    async with _connect() as db:
        if not option_ids:
            await db.execute(
                "DELETE FROM poll_answers WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)
            )
        else:
            await db.execute(
                "INSERT INTO poll_answers (poll_id, user_id, option_ids_json, answered_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(poll_id, user_id) DO UPDATE SET "
                "option_ids_json = excluded.option_ids_json, answered_at = excluded.answered_at",
                (poll_id, user_id, json.dumps(sorted(int(i) for i in option_ids)), _now_str()),
            )
        await db.commit()


async def list_poll_answers(poll_id: int) -> list[dict]:
    """Ответы с данными делегата (ФИО/username/город) — для экрана и выгрузки. `option_ids` —
    уже распарсенный список индексов."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT a.user_id, a.option_ids_json, a.answered_at, "
            "u.full_name, u.username, u.event_city "
            "FROM poll_answers a LEFT JOIN users u ON u.telegram_id = a.user_id "
            "WHERE a.poll_id = ? ORDER BY a.answered_at, a.user_id",
            (poll_id,),
        ) as cursor:
            out = []
            for r in await cursor.fetchall():
                d = dict(r)
                try:
                    d["option_ids"] = [int(i) for i in json.loads(d.pop("option_ids_json") or "[]")]
                except (TypeError, ValueError):
                    d["option_ids"] = []
                out.append(d)
            return out


async def count_poll_respondents(poll_id: int) -> int:
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM poll_answers WHERE poll_id = ?", (poll_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def get_poll_results(poll_id: int) -> dict | None:
    """Итоги без aiogram — для экрана админки, выгрузки и будущего дашборда.

    {"poll": row, "counts": [n per option], "respondents": N, "delivered": N, "failed": N,
     "source": "answers" | "totals"}.
    Неанонимный опрос считается по poll_answers (есть «кто»). Анонимный — суммой totals_json
    всех poll_messages (каждому делегату уходит свой Telegram-опрос; Telegram присылает только
    счётчики, не людей), respondents = сумма total_voter_count."""
    poll = await get_poll(poll_id)
    if poll is None:
        return None
    n_opts = len(poll["options"])
    counts = [0] * n_opts
    delivered, failed = await count_poll_deliveries(poll_id)
    if poll["is_anonymous"]:
        respondents = 0
        for msg in await list_poll_messages(poll_id):
            try:
                totals = json.loads(msg.get("totals_json") or "{}")
            except (TypeError, ValueError):
                continue
            respondents += int(totals.get("total") or 0)
            for i, n in enumerate((totals.get("options") or [])[:n_opts]):
                counts[i] += int(n or 0)
        source = "totals"
    else:
        answers = await list_poll_answers(poll_id)
        respondents = len(answers)
        for a in answers:
            for i in a["option_ids"]:
                if 0 <= i < n_opts:
                    counts[i] += 1
        source = "answers"
    return {
        "poll": poll, "counts": counts, "respondents": respondents,
        "delivered": delivered, "failed": failed, "source": source,
    }


# ── Phase 27 (27-02, LANG-02/03/04/05/08): хранилище переводов ─────────────────────────────
# Контракт значения `translations.text` (нужен `list_translations` ниже и плану 27-06):
# NULL — перевод ещё не пришёл («pending»); '' (пустая строка) — движок отработал, но
# результат отброшен fail-soft'ом плана 27-03 (битая HTML-разметка, потерянный DNT-сентинел —
# см. 27-CONTEXT.md «Находки замера») («failed»); непустая строка — есть перевод, `manual`
# отличает ручную правку менеджера («manual») от машинной. `fetch_translations`
# (единственная точка чтения ядра `services/i18n.py::load_map`) видит только непустые строки —
# «pending»/«failed» для делегата неотличимы от отсутствия перевода (fail-soft, D-04: делегат
# всегда видит русский, а не дыру).

async def fetch_translations(lang: str) -> dict[str, str]:
    """Одна выборка карты `src_hash -> text` для языка — `services/i18n.py::load_map` зовёт
    это РОВНО один раз на запрос делегата, не N раз на текст (`form_spec()` резолвит ~43 шага,
    поход в БД на каждый текст удвоил бы чтения на рендер анкеты)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT src_hash, text FROM translations "
            "WHERE lang = ? AND text IS NOT NULL AND text != ''",
            (lang,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def upsert_translation(
    lang: str, src_hash: str, src_text: str, text: str | None, *,
    manual: int = 0, origin_key: str | None = None,
) -> None:
    """Пишет строку перевода (машинную — план 27-03, ручную — план 27-06).

    **Ключевое правило (LANG-05, T-27-02-01):** ручная правка менеджера (`manual=1`) машинным
    переводом затереть НЕЛЬЗЯ. `ON CONFLICT ... WHERE` ниже кодирует это как условие апдейта,
    а не как проверку в коде: строка обновляется, только если новое значение само ручное
    (`excluded.manual = 1` — менеджер правит поверх чего угодно, включая свою же прошлую
    правку) ИЛИ старая строка ещё не ручная (`translations.manual = 0` — машинный перевод
    вправе перезаписывать только машинный же). Если оба условия ложны (машинная попытка
    поверх ручной правки), INSERT ... DO UPDATE молча ничего не делает — это и есть защита,
    не побочный эффект."""
    updated_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            '''
            INSERT INTO translations (lang, src_hash, src_text, text, manual, origin_key, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lang, src_hash) DO UPDATE SET
                src_text = excluded.src_text,
                text = excluded.text,
                manual = excluded.manual,
                origin_key = COALESCE(excluded.origin_key, translations.origin_key),
                updated_at = excluded.updated_at
            WHERE excluded.manual = 1 OR translations.manual = 0
            ''',
            (lang, src_hash, src_text, text, int(manual), origin_key, updated_at),
        )
        await db.commit()


async def clear_translation_manual(lang: str, src_hash: str) -> None:
    """Снимает `manual=1` — единственный явный обход защиты `upsert_translation` (план 27-06,
    «↻ Перевести заново»): менеджер осознанно возвращает строку машине, подтвердив, что его
    правка пропадёт. Это НЕ дыра в LANG-05 — та защита не даёт МАШИННОЙ записи тихо перебить
    ручную правку; здесь ручное, подтверждённое действие человека через отдельный вызов, не
    `upsert_translation(..., manual=0)` (та ветка намеренно no-op поверх `manual=1`, см. её
    докстринг)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE translations SET manual = 0 WHERE lang = ? AND src_hash = ?",
            (lang, src_hash),
        )
        await db.commit()


async def get_translation(lang: str, src_hash: str) -> dict | None:
    """Одна строка перевода целиком (админский экран правки, план 27-06) — либо `None`,
    если для этой пары `(lang, src_hash)` ещё ничего не приходило (ни машины, ни менеджера)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM translations WHERE lang = ? AND src_hash = ?",
            (lang, src_hash),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


_TRANSLATION_STATES = (None, "pending", "manual", "failed")


async def list_translations(
    lang: str, *, offset: int = 0, limit: int = 20, state: str | None = None,
) -> tuple[list[dict], int]:
    """Срез + общее число для пагинированного экрана правки (план 27-06) — при 265+ строках
    английского корпуса сообщение-на-строку не годится (CLAUDE.md, «масштаб модерации»).

    `state` (см. контракт `text` в комментарии над этим блоком): `None` — все строки;
    `"manual"` — правка менеджера; `"pending"` — перевод ещё не пришёл; `"failed"` — движок
    отработал, но результат отброшен fail-soft'ом."""
    if state not in _TRANSLATION_STATES:
        raise ValueError(f"unknown translations state: {state!r}")
    where = "WHERE lang = ?"
    params: list = [lang]
    if state == "manual":
        where += " AND manual = 1"
    elif state == "pending":
        where += " AND text IS NULL"
    elif state == "failed":
        where += " AND text = ''"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT COUNT(*) AS n FROM translations {where}", params) as cursor:
            total_row = await cursor.fetchone()
            total = int(total_row["n"]) if total_row else 0
        async with db.execute(
            f"SELECT * FROM translations {where} ORDER BY updated_at DESC, src_hash LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
    return rows, total


async def enqueue_translation(
    lang: str, src_hash: str, src_text: str,
    origin_key: str | None = None, created_at: str | None = None,
) -> int | None:
    """Кладёт строку в очередь на перевод (план 27-03 — фон при сохранении настройки,
    план 27-01 — bulk-seed). `INSERT OR IGNORE` — дедупликация массового пресета
    (`handlers/reg_schema.py::_apply_*_preset` кладёт десятки ключей одним нажатием) решена в
    СХЕМЕ через `UNIQUE(lang, src_hash)` (T-27-02-02), не в коде воркера. Возвращает
    `lastrowid` новой строки или `None`, если строка с этим `(lang, src_hash)` уже стоит
    в очереди."""
    ts = created_at or msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO translation_queue (lang, src_hash, src_text, origin_key, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lang, src_hash, src_text, origin_key, ts),
        )
        await db.commit()
        return cursor.lastrowid if cursor.rowcount else None


async def list_pending_translations(
    lang: str, *, limit: int = 32, max_attempts: int = 5,
) -> list[dict]:
    """Строки очереди, ещё не сдавшиеся (`attempts < max_attempts`), в порядке `attempts, id`
    — сначала непопробованные/меньше пытавшиеся. Безнадёжные строки (`attempts >=
    max_attempts`) исключены из выборки (T-27-02-02): одна битая строка в массовом пресете не
    забивает джобу воркера навсегда, а просто перестаёт в неё попадать."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM translation_queue WHERE lang = ? AND attempts < ? "
            "ORDER BY attempts, id LIMIT ?",
            (lang, int(max_attempts), int(limit)),
        ) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
    return rows


async def drop_translation_queue(ids: list[int]) -> int:
    """Удаляет обработанные строки очереди по `id` (воркер плана 27-03 после успешного
    перевода). Пустой список — no-op без похода в БД. Возвращает число реально удалённых
    строк (может быть меньше `len(ids)`, если строка уже исчезла — второй писатель очереди,
    `miniapp`, теоретически мог её тоже тронуть)."""
    id_list = [int(i) for i in ids]
    if not id_list:
        return 0
    placeholders = ",".join("?" for _ in id_list)
    async with _connect() as db:
        cursor = await db.execute(
            f"DELETE FROM translation_queue WHERE id IN ({placeholders})", tuple(id_list),
        )
        await db.commit()
        return cursor.rowcount


async def bump_translation_attempt(row_id: int, error: str | None = None) -> None:
    """Неудачная попытка перевода строки очереди: `attempts + 1`, текст ошибки усечён до 500
    символов — тот же паттерн, что `mark_miniapp_outbox_failed` выше. Строка остаётся в
    очереди; `list_pending_translations` перестаёт её отдавать сама, как только `attempts`
    достигнет `max_attempts` вызывающего."""
    async with _connect() as db:
        await db.execute(
            "UPDATE translation_queue SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:500] if error else None, row_id),
        )
        await db.commit()


async def reset_translation_attempts(lang: str) -> int:
    """Квик 260912 (W5, Задача 4) — «догонялка перевода»: сбрасывает `attempts`/`last_error`
    ВСЕМ строкам очереди этого языка, включая застрявшие после `MAX_ATTEMPTS`
    (`services/i18n_worker.py::drain` их больше не выбирает — `list_pending_translations`
    фильтрует `attempts < max_attempts`). Возврат `rowcount` — сколько строк реально ожило.

    Почему сброс безопасен: строка либо переведётся движком на следующем прогоне `drain()`,
    либо снова упрётся в тот же потолок попыток — данные не теряются ни в каком исходе, это
    не «прощение» ошибки, а просто новый шанс той же строке."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE translation_queue SET attempts = 0, last_error = NULL WHERE lang = ?",
            (lang,),
        )
        await db.commit()
        return cursor.rowcount


_ALLOWED_USER_LANGS = frozenset({"ru", "en"})


async def set_user_lang(telegram_id: int, lang: str | None) -> None:
    """Пишет `users.lang`. Допустимые значения — закрытое множество `{"ru", "en", None}`
    (T-27-02-04): кодовые значения приходят из наших же кнопок (план 27-04/27-05), чужого
    сюда попасть не должно. Мусор не пишется и логируется, а не бросает исключение —
    вызывающий обработчик тапа не обязан ловить `ValueError` на каждое нажатие."""
    if lang is not None and lang not in _ALLOWED_USER_LANGS:
        logger.error(f"set_user_lang: недопустимое значение {lang!r} для {telegram_id}, игнорирую")
        return
    async with _connect() as db:
        cursor = await db.execute("UPDATE users SET lang = ? WHERE telegram_id = ?", (lang, telegram_id))
        if cursor.rowcount == 0:
            # Приёмка 16.09: у нового делегата строки users ещё нет — UPDATE молча ничего не
            # писал, выбор терялся, и /start спрашивал язык снова по кругу. До подачи анкеты
            # язык живёт в reg_started (clear_reg_started переносит его в users).
            await db.execute(
                """
                INSERT INTO reg_started (telegram_id, started_at, lang) VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET lang = excluded.lang
                """,
                (telegram_id, msk_now().strftime("%Y-%m-%d %H:%M:%S"), lang),
            )
        await db.commit()


async def get_stored_lang(telegram_id: int) -> str | None:
    """Сохранённый выбор языка делегата: `users.lang`, а до подачи анкеты — `reg_started.lang`
    (см. `set_user_lang`). `None` — выбора не было."""
    async with _connect() as db:
        async with db.execute("SELECT lang FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            return row[0]
        async with db.execute("SELECT lang FROM reg_started WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row and row[0] else None


# ── Quick 260910-ro7 (DELU-01..08): удаление тестового делегата одной транзакцией ──────────
# Приёмка требует «чистого» тестового аккаунта — суперадмин должен уметь стереть человека из
# ВСЕХ делегатских таблиц одним нажатием (handlers/admin_purge.py), без ручного лазания в
# SQLite на сервере. USER_PURGE_TABLES — ЕДИНСТВЕННЫЙ источник правды: и счёт следа
# (count_user_footprint), и само удаление (purge_user) ходят по одному и тому же списку,
# второго списка в коде нет.
#
# USER_PURGE_EXCLUDED существует ради теста-сторожа дрейфа схемы (tests/test_delete_user_
# 260910.py): каждая таблица из DDL init_db с колонкой user_id/telegram_id/chat_id обязана
# попасть либо в USER_PURGE_TABLES, либо сюда — иначе новая таблица с делегатским следом
# молча остаётся неудаляемой, и тест краснеет.
#
# НЕ трогаем (и почему): staff — роли менеджера, это не делегатский след, удаление аккаунта
# делегата не должно снимать чужие права; bot_settings/cities/faq_items/game_tasks/polls/
# broadcasts/scheduled_broadcasts/translations/translation_queue/miniapp_outbox — справочники
# и объекты, созданные менеджером, а не делегатом. Авторские колонки других таблиц
# (changed_by/decided_by/answered_by/reviewed_by/created_by/added_by/admin_id) хранят id
# менеджера, а не удаляемого делегата — таблицы, у которых ТОЛЬКО такие id-колонки, сторог
# вообще не находит, в список их добавлять не нужно.
USER_PURGE_TABLES: tuple[tuple[str, str, str], ...] = (
    ("users", "telegram_id", "application"),
    ("reg_started", "telegram_id", "draft"),
    ("reg_drafts", "telegram_id", "draft"),
    ("reg_answer_history", "telegram_id", "history"),
    ("reg_events", "telegram_id", "events"),
    ("user_consents", "user_id", "consents"),
    ("coins", "user_id", "coins"),
    ("delegate_questions", "user_id", "questions"),
    ("game_submissions", "user_id", "game"),
    ("game_submit_digest_queue", "user_id", "queue"),
    # Квик 260916: очередь дайджеста заявок — такой же делегатский след в очереди, как
    # соседи выше: строка ждёт отправки сводки менеджерам и уходит вместе с человеком.
    ("reg_submit_digest_queue", "telegram_id", "queue"),
    ("delayed_notifications", "user_id", "queue"),
    ("application_decisions", "telegram_id", "decisions"),
    ("poll_answers", "user_id", "deliveries"),
    ("poll_messages", "chat_id", "deliveries"),
    ("broadcast_deliveries", "chat_id", "deliveries"),
    ("scheduled_broadcast_deliveries", "chat_id", "deliveries"),
    # Phase 30 (30-03, дефект дрейфа схемы после 30-02): lookup_merge_queue.telegram_id —
    # сырой ответ «Другое» делегата в очереди слияния справочника (`services/lookup.py::
    # enqueue_merge`). Это делегатский след — уходит вместе с человеком. Группа "draft" —
    # та же, что у reg_drafts: запись рождается из того же незавершённого шага анкеты.
    # decided_by в этой таблице — id менеджера, принявшего решение по очереди, не трогаем
    # (авторская колонка, не делегатский след).
    ("lookup_merge_queue", "telegram_id", "draft"),
    # Квик 260914-rgr (RGR-01): chat_members/chat_activity/chat_events — след делегата в
    # групповом чате (статус участника, счётчики активности, лог join/leave/kick). Ключ у
    # всех трёх — telegram_id, чат не фильтруется: покидает человек проект — стираем след во
    # ВСЕХ чатах, не только в одном привязанном. Группа "chat" — отдельная, не смешиваем со
    # "queue"/"draft".
    ("chat_members", "telegram_id", "chat"),
    ("chat_activity", "telegram_id", "chat"),
    ("chat_events", "telegram_id", "chat"),
    # Квик 260927: живой рейтинг чата — свои сообщения, поставленные реакции, ник, строка админа.
    ("chat_messages", "telegram_id", "chat"),
    ("chat_reactions", "telegram_id", "chat"),
    ("chat_usernames", "telegram_id", "chat"),
    ("chat_admins", "telegram_id", "chat"),
    # Phase 31 (31-02): auto_reject_log — журнал срабатываний автоотказа, персональный след
    # делегата (кто, сколько раз, каким текстом отказали). returned_by в той же строке — id
    # менеджера, вернувшего заявку на ручную модерацию, отдельно не трогаем: строка целиком
    # уходит вместе с делегатом.
    ("auto_reject_log", "telegram_id", "reject_log"),
    # Phase 32 (32-01): wave_results — неизменяемый снимок призёров волны с ЭТИМ user_id.
    # Как и другие личные записи делегата (заявка, монеты, сдачи), уходит вместе с ним.
    ("wave_results", "user_id", "waves"),
    # Phase 32 (32-01, D-22): referral_credits.referrer_id — начисление, которое ЗАРАБОТАЛ
    # удаляемый амбассадор за приглашённого; удаляется вместе с ним, как и его "coins" —
    # это его личный след, а не след приглашённого. Колонку invitee_id НЕ трогаем: если
    # удаляемый человек сам был приглашённым, начисление его пригласившему уже состоялось
    # и по правилу продукта не отзывается (earned points не списываются), а PRIMARY KEY на
    # invitee_id вдобавок не даёт начислить второй раз, если тот же Telegram-аккаунт
    # зарегистрируется заново — удаление строки открыло бы дублирующее начисление.
    ("referral_credits", "referrer_id", "referral_credits"),
    # Ступени амбассадора СкиллАп — его личный след (как referral_credits по referrer_id выше),
    # уходят вместе с ним.
    ("ambassador_tiers", "telegram_id", "referral_credits"),
    # Метка «ступень снята менеджером» — тот же личный след амбассадора.
    ("amb_tier_revocations", "telegram_id", "referral_credits"),
    # Исключение из зачёта по invitee_id: причина — свободный текст менеджера о человеке (ПДн),
    # уходит вместе с исключённым. Начисленное пригласившему это не отзывает: обратная строка
    # монет и отметка в журнале остаются.
    ("ambassador_exclusions", "invitee_id", "referral_credits"),
    # Сохранённое ручное закрепление ещё не одобренного приглашённого: заметка менеджера
    # («скрины в чате») — свободный текст о человеке, уходит вместе с ним.
    ("amb_manual_attach", "invitee_id", "referral_credits"),
    # Статусы амбассадора прошлых сезонов (database/amb_status_db.py): кем был человек в
    # прошлом отборе — его личный след, уходит вместе с ним.
    ("ambassador_season_archive", "telegram_id", "ambassador"),
    # Phase 12 (FORUM-CHECKIN.md): checkins.telegram_id — личная отметка «пришёл» делегата
    # (вход/сессия форума). Тот же журнал делегатского следа, что chat_activity/reg_events
    # выше — уходит вместе с человеком. by_staff_id в той же строке — id волонтёра/менеджера,
    # который отметил (CSV/manual), это авторская колонка, не трогаем отдельно: строка
    # целиком уходит вместе с делегатом.
    ("checkins", "telegram_id", "checkin"),
    # Форум-ночь B1 (идея №10): checkin_token_replacements.telegram_id — тот же личный след,
    # что checkins выше (кто когда-то держал такой токен), группа общая "checkin".
    ("checkin_token_replacements", "telegram_id", "checkin"),
    # Форум-ночь п.3 (D-03, идея №2): checkin_qr_sends.telegram_id — кому и когда отправлен
    # персональный QR + его подтверждение, тот же личный след, группа общая "checkin".
    ("checkin_qr_sends", "telegram_id", "checkin"),
    # Форум-ночь п.6 (D-25, идея №14): checkin_not_arrived.telegram_id — кому и когда ушёл
    # шаблон «не пришёл» + его ответ, тот же личный след, группа общая "checkin".
    ("checkin_not_arrived", "telegram_id", "checkin"),
    # D-33 (шпаргалка волонтёра накануне форума): checkin_volunteer_guide_sends.telegram_id —
    # кому и когда ушла шпаргалка, тот же журнал отправки человеку, что checkin_qr_sends выше,
    # группа общая "checkin". day/city — снимок дня/города рассылки, не трогаем отдельно.
    ("checkin_volunteer_guide_sends", "telegram_id", "checkin"),
    # Идея №31 (журнал площадки): venue_log.telegram_id — строки «что сделали С ЭТИМ
    # делегатом» (отметки, снятия, перевыпуск QR). Личный след, уходит вместе с человеком —
    # тот же довод, что у checkins выше (удаляют тестовые аккаунты; журнал удалённого
    # тестера — мусор в разборе дня). staff_id в той же строке — авторская колонка, не
    # трогаем: действия САМОГО удаляемого как волонтёра (строки, где он staff_id) остаются.
    ("venue_log", "telegram_id", "checkin"),
    # Форум-ночь п.8 (идея №19, SOS): sos_reports.telegram_id — личная заявка SOS делегата
    # (категория/текст/фото/геопозиция), тот же личный след, что chat_activity/checkins выше.
    # claimed_by/resolved_by в той же строке — id менеджера, авторские колонки, не трогаем
    # отдельно (строка целиком уходит вместе с делегатом, как и у соседей этой таблицы).
    ("sos_reports", "telegram_id", "sos"),
    # Форум-ночь п.9 (идея №15, D-24): session_feedback.telegram_id — личная оценка/комментарий
    # делегата к сессии, тот же личный след, что checkins/sos_reports выше.
    ("session_feedback", "telegram_id", "session_feedback"),
    # Идея №23 бэклога чек-ина: forum_noshow_poll.telegram_id — кому и когда ушёл опрос
    # «почему не пришёл» + сам ответ (причина/комментарий), тот же личный след, группа общая
    # "checkin" (соседи checkin_not_arrived/checkin_qr_sends выше — тот же журнал отправки
    # делегату + его ответ).
    ("forum_noshow_poll", "telegram_id", "checkin"),
    # Идея №5 бэклога чек-ина: volunteer_invite_uses.telegram_id — кто вошёл по ссылке
    # приглашения волонтёров, личный след (тот же класс, что checkins/sos_reports выше).
    # invite_code не трогаем — сама ссылка (volunteer_invites) остаётся, её счётчик
    # использования не откатывается удалением одного вошедшего (см. докстринг
    # `revoke_volunteer_invite`/`claim_volunteer_invite` — used не пересчитывается по
    # содержимому uses-таблицы, только инкрементируется атомарно).
    ("volunteer_invite_uses", "telegram_id", "invites"),
    # Нагрузочный прогон 25.09: sheet_arrival_queue.telegram_id — несделанная запись «Пришёл»
    # удаляемого делегата в лист. Строки листа удаления не переживают, писать некуда — событие
    # уходит вместе с человеком, группа общая "checkin" (соседи checkins/venue_log выше).
    ("sheet_arrival_queue", "telegram_id", "checkin"),
    # 29.09: sheet_chat_queue.telegram_id — несделанная запись «В чате», та же логика.
    ("sheet_chat_queue", "telegram_id", "checkin"),
    # Трек «региональные форумы → Москва»: regional_noshow_move.telegram_id — кому и когда ушло
    # предложение переноса + сам ответ (перенёсся/отказался), тот же личный след, группа общая
    # "checkin" (соседи forum_noshow_poll/checkin_not_arrived выше — тот же журнал отправки
    # делегату + его ответ).
    ("regional_noshow_move", "telegram_id", "checkin"),
    # Phase 33 (delegate-card admin actions, задачи 2/3): admin_delegate_overrides.telegram_id
    # — персональные исключения из reg_resubmit_after_reject/reg_edit_policy, личный след
    # делегата (кому что разрешили). granted_by/revoked_by — id менеджера, авторские колонки,
    # не трогаем отдельно (строка целиком уходит вместе с делегатом).
    ("admin_delegate_overrides", "telegram_id", "overrides"),
    # Идея №29 бэклога чек-ина («Твой Юлид в цифрах»): forum_stats_card_sends.telegram_id —
    # кому и когда ушла итоговая картинка-карточка, тот же личный след, группа общая "checkin"
    # (соседи forum_noshow_poll/regional_noshow_move выше — тот же журнал отправки делегату).
    ("forum_stats_card_sends", "telegram_id", "checkin"),
    # Ответы внешних форм, привязанные к делегату, — его ПД (имя, телефон, ответы).
    ("external_form_answers", "matched_telegram_id", "forms"),
    # Оценка ответа формы делегаций, привязанная к делегату, — его след (вуз, курс, ник).
    # decided_by — id менеджера, авторская колонка: строка уходит целиком вместе с делегатом,
    # отдельно по менеджеру не чистим.
    ("delegation_answers", "linked_telegram_id", "forms"),
)

USER_PURGE_EXCLUDED: frozenset[str] = frozenset({
    "staff",
    # 29.09: staff_unreachable — отметка «уведомления сотруднику не доходят», про роль
    # менеджера, не про делегата (тот же класс, что staff выше); снимается сама при доставке.
    "staff_unreachable",
    "bot_settings",
    "cities",
    "faq_items",
    "game_tasks",
    "polls",
    "broadcasts",
    "scheduled_broadcasts",
    "translations",
    "translation_queue",
    "miniapp_outbox",
    "sos_card_copies",
    # sos_relay_messages.chat_id — чат SOS (копии дописок в треде карточки), не делегат.
    "sos_relay_messages",
    # Идея №20 бэклога чек-ина: lost_found.chat_id — та же группа делегатов, не личный чат
    # делегата (тот же класс, что sos_card_copies.chat_id выше); posted_by/returned_by — id
    # сотрудника (волонтёра/менеджера), не удаляемого делегата.
    "lost_found",
    # Квик 260927: chat_bot_state.chat_id — группа делегатов, состояние САМОГО бота в ней (статус
    # и право удалять), не след делегата.
    "chat_bot_state",
    # chat_cleanup_queue.chat_id — та же группа делегатов: очередь служебных уведомлений на
    # удаление (id сообщения и тип), без автора.
    "chat_cleanup_queue",
    # Служебные таблицы внешних форм: ключи приложения, токены менеджера, описания форм,
    # позиции колонок и очередь дочитывания — не след делегата.
    "external_form_secrets",
    "external_form_connections",
    "external_forms",
    "external_form_columns",
    "external_form_pending",
    # Надгробия: только id удалённых анкет, без ПД; нужны, чтобы сверка не вернула стёртое.
    "external_form_deleted",
})

# Человеческие группы, по которым считается/удаляется след — выведены из USER_PURGE_TABLES,
# второго списка групп тоже нет. "game" уже включает game_submissions; game_submission_parts
# (своей user_id/telegram_id колонки у неё нет — только submission_id) суммируется в ту же
# группу отдельным запросом-подзапросом.
_PURGE_RESULT_GROUPS: tuple[str, ...] = tuple(sorted({g for _, _, g in USER_PURGE_TABLES}))


# «Обезличить, не удалять»: строка referral_credits, где удаляемый был ПРИГЛАШЁННЫМ, остаётся
# (по ней считаются баллы и очки волны пригласившего), но заметка менеджера о нём, автор
# закрепления и причина исключения — текст о человеке — обнуляются.
_PURGE_ANONYMIZE_CREDIT_WHERE = (
    "invitee_id = ? AND (manual_note IS NOT NULL OR manual_by IS NOT NULL "
    "OR exclude_reason IS NOT NULL)"
)


async def count_user_footprint(telegram_id: int) -> dict[str, int]:
    """Что пропадёт при purge_user(telegram_id) — заранее, для карточки подтверждения
    (handlers/admin_purge.py). Все ключи из _PURGE_RESULT_GROUPS присутствуют в результате
    ВСЕГДА, даже нулевые — вызывающему не приходится гадать, какие бывают. Плюс
    `referrals_kept` — сколько делегатов привёл этот человек (users.referrer_id): в удаление
    НЕ входит (purge_user эту связь не трогает), считается только чтобы честно предупредить
    менеджера в карточке, что чужие заявки останутся."""
    result: dict[str, int] = {g: 0 for g in _PURGE_RESULT_GROUPS}
    async with _connect() as db:
        for table, column, group in USER_PURGE_TABLES:
            _assert_identifier(table)
            _assert_identifier(column)
            async with db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (telegram_id,)
            ) as cursor:
                row = await cursor.fetchone()
                result[group] += row[0] if row else 0
        async with db.execute(
            "SELECT COUNT(*) FROM game_submission_parts WHERE submission_id IN "
            "(SELECT id FROM game_submissions WHERE user_id = ?)",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            result["game"] += row[0] if row else 0
        async with db.execute(
            f"SELECT COUNT(*) FROM referral_credits WHERE {_PURGE_ANONYMIZE_CREDIT_WHERE}",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            result["referral_credits"] += row[0] if row else 0
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            result["referrals_kept"] = row[0] if row else 0
    return result


async def purge_user(telegram_id: int) -> dict[str, int]:
    """Необратимо удаляет делегата из ВСЕХ таблиц USER_PURGE_TABLES одной транзакцией — одно
    соединение, один `await db.commit()` в конце, промежуточных коммитов нет: либо стирается
    всё, либо (при сбое до commit) не стирается ничего. Идемпотентна — повторный вызов на уже
    удалённом id возвращает нули по всем группам и не падает.

    НЕ трогает: staff (роли менеджера переживают удаление аккаунта делегата), bot_settings/
    cities/faq_items/game_tasks/polls/broadcasts/scheduled_broadcasts/translations/
    translation_queue/miniapp_outbox (справочники и объекты менеджера, не делегата) и
    авторские колонки других таблиц (changed_by/decided_by/answered_by/reviewed_by/
    created_by/added_by/admin_id) — там id менеджера, не удаляемого делегата. Заявки
    делегатов, которых этот человек когда-то привёл (users.referrer_id), тоже не трогает —
    только пересчитывает их в возвращаемом `referrals_kept` (та же логика, что у
    count_user_footprint), карточка честно предупреждает менеджера до нажатия.

    `game_submission_parts` удаляется ПЕРВОЙ, по подзапросу на submission_id — если удалить
    её ПОСЛЕ game_submissions, подзапрос вернёт пусто и части останутся сиротами."""
    result: dict[str, int] = {g: 0 for g in _PURGE_RESULT_GROUPS}
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM game_submission_parts WHERE submission_id IN "
            "(SELECT id FROM game_submissions WHERE user_id = ?)",
            (telegram_id,),
        )
        result["game"] += cursor.rowcount
        # Копии карточек SOS этого делегата в личках админов — до sos_reports, иначе
        # подзапрос вернёт пусто (тот же приём, что game_submission_parts выше).
        await db.execute(
            "DELETE FROM sos_card_copies WHERE report_id IN "
            "(SELECT id FROM sos_reports WHERE telegram_id = ?)",
            (telegram_id,),
        )
        await db.execute(
            "DELETE FROM sos_relay_messages WHERE report_id IN "
            "(SELECT id FROM sos_reports WHERE telegram_id = ?)",
            (telegram_id,),
        )
        # Квик 260927: чужие ответы этому человеку остаются в журнале, но без адресата — иначе
        # удалённый продолжал бы получать отклик и всплывал в рейтинге голым id.
        await db.execute(
            "UPDATE chat_messages SET reply_to_author_id = NULL WHERE reply_to_author_id = ?",
            (telegram_id,),
        )
        # Анкеты внешних форм: источник их помнит, поэтому перед стиранием оставляем надгробия,
        # иначе ближайшая сверка вернула бы удалённые данные.
        await db.execute(
            "INSERT OR IGNORE INTO external_form_deleted (form_id, answer_id, deleted_at) "
            "SELECT form_id, answer_id, ? FROM external_form_answers WHERE matched_telegram_id = ?",
            (msk_now().strftime("%Y-%m-%d %H:%M:%S"), telegram_id),
        )
        for table, column, group in USER_PURGE_TABLES:
            _assert_identifier(table)
            _assert_identifier(column)
            cursor = await db.execute(f"DELETE FROM {table} WHERE {column} = ?", (telegram_id,))
            result[group] += cursor.rowcount
        cursor = await db.execute(
            "UPDATE referral_credits SET manual_note = NULL, manual_by = NULL, "
            f"exclude_reason = NULL WHERE {_PURGE_ANONYMIZE_CREDIT_WHERE}",
            (telegram_id,),
        )
        result["referral_credits"] += cursor.rowcount
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            result["referrals_kept"] = row[0] if row else 0
        await db.commit()
    return result


async def find_user_id_by_username(username: str) -> int | None:
    """Ищет telegram_id по @username — сперва среди завершивших регистрацию (`users`),
    потом среди бросивших анкету на середине (`reg_started`; у `reg_drafts` колонки username
    нет вовсе). Терпимо и к формату ВВОДА, и к формату ХРАНЕНИЯ («@» можно не писать ни там,
    ни там — двусторонний ltrim по username, квик 260911-0zu, UNAME-02): 947 прод-строк
    `reg_started` заведены без собаки и без этого сравнения не находились НИКОГДА. Регистр
    не важен (COLLATE NOCASE). Пустой ввод/«-»/«@»/None -> None без запроса (иначе плейсхолдер
    в базе стал бы находимым, T-0zu-01)."""
    needle = username_needle(username)
    if needle is None:
        return None
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM users WHERE ltrim(username, '@') = ? COLLATE NOCASE", (needle,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
        async with db.execute(
            "SELECT telegram_id FROM reg_started WHERE ltrim(username, '@') = ? COLLATE NOCASE", (needle,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# ── Квик 260916: «📊 Итоги дня» — цифры одной вечерней сводки менеджерам ─────────────────────
#
# Одна функция вместо восьми мелких: сводка всегда спрашивает ВСЁ и сразу, за один день и один
# городской скоуп, и держать восемь публичных имён ради одного вызывающего значило бы
# разложить по восьми местам логику, которую читают целиком. `day` — «ГГГГ-ММ-ДД» по Москве
# (services/timeutil.msk_now), сравнение через substr(...) — тот же приём, что у дашборда
# (dashboard/queries.py) и у остальных дневных срезов этого файла.
#
# `city_scope` — дескриптор `cities.city_scope(...)`, как везде; город берётся у ДЕЛЕГАТА
# (users.event_city), а не у менеджера: сводку Москвы наполняют московские заявки, кто бы их
# ни разобрал. JOIN на users поэтому внутренний — строка без делегата (человека снесли через
# /delete_user) в городской срез попасть не может по определению.

async def daily_digest_stats(day: str, *, city_scope=None) -> dict:
    """Цифры за один московский день в одном городском скоупе — см. блок выше.

    Решения, отменённые кнопкой «↩️ Отменить» (`undone_at IS NOT NULL`), не считаются вовсе:
    менеджер их «не принял», и в сводке им делать нечего. Монеты — только ПЛЮСОВЫЕ дельты
    («начислено за день»); списания и штрафы в эту цифру не входят и её не уменьшают.
    """
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    join_where = f" AND {city_frag}" if city_frag else ""
    plain_frag, plain_params = _city_clause(city_scope)
    users_where = f" AND {plain_frag}" if plain_frag else ""

    stats: dict = {
        "apps_new": 0, "apps_approved": 0, "apps_rejected": 0, "apps_pending": 0,
        "apps_walkin_pending": 0,
        "app_managers": [], "game_submissions": 0, "game_reviewed": 0,
        "coins_awarded": 0, "game_managers": [],
    }
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users u WHERE substr(u.registration_date, 1, 10) = ?"
            + join_where, [day, *city_params],
        ) as cursor:
            stats["apps_new"] = (await cursor.fetchone())[0] or 0

        # D-41: «ждут» — та же очередь, что у менеджера; walk-in без решения ждут у стойки и
        # считаются отдельной цифрой.
        async with db.execute(
            f"SELECT SUM(CASE WHEN {_NOT_WALKIN} THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {_NOT_WALKIN} THEN 0 ELSE 1 END) "
            "FROM users WHERE status = 'pending'" + users_where,
            list(plain_params),
        ) as cursor:
            row = await cursor.fetchone()
            stats["apps_pending"] = (row[0] if row else 0) or 0
            stats["apps_walkin_pending"] = (row[1] if row else 0) or 0

        per_manager: dict[int, list[int]] = {}
        # Квик 260923: сентинел автоотказа (`services.reject_journal.AUTO_DECIDED_BY == -1`)
        # не менеджер — `d.decided_by > 0` убирает автоотказ И из этой выборки (per_manager),
        # И из `stats["apps_rejected"]` (она считается из тех же строк ниже) разом: менеджер
        # больше не видит строку «менеджер #-1» и «отклонено» больше не путает решение
        # человека со срабатыванием правила (D-A). Честные цифры автоотказа — отдельно, через
        # `auto_reject_summary` (см. ниже по файлу).
        async with db.execute(
            "SELECT d.decided_by, d.decision, COUNT(*) FROM application_decisions d "
            "JOIN users u ON u.telegram_id = d.telegram_id "
            "WHERE substr(d.decided_at, 1, 10) = ? AND d.undone_at IS NULL "
            "AND d.decided_by > 0" + join_where +
            " GROUP BY d.decided_by, d.decision", [day, *city_params],
        ) as cursor:
            for decided_by, decision, count in await cursor.fetchall():
                slot = per_manager.setdefault(int(decided_by or 0), [0, 0])
                if decision == "approved":
                    slot[0] += count
                    stats["apps_approved"] += count
                elif decision == "rejected":
                    slot[1] += count
                    stats["apps_rejected"] += count
        stats["app_managers"] = sorted(
            ((mid, ok, no) for mid, (ok, no) in per_manager.items()),
            key=lambda row: (-(row[1] + row[2]), row[0]),
        )

        async with db.execute(
            "SELECT COUNT(*) FROM game_submissions s JOIN users u ON u.telegram_id = s.user_id "
            "WHERE substr(s.submitted_at, 1, 10) = ?" + join_where, [day, *city_params],
        ) as cursor:
            stats["game_submissions"] = (await cursor.fetchone())[0] or 0

        reviewers: dict[int, int] = {}
        async with db.execute(
            "SELECT s.reviewed_by, COUNT(*) FROM game_submissions s "
            "JOIN users u ON u.telegram_id = s.user_id "
            "WHERE substr(s.reviewed_at, 1, 10) = ? AND s.reviewed_by IS NOT NULL "
            "AND s.status IN ('approved', 'rejected')" + join_where +
            " GROUP BY s.reviewed_by", [day, *city_params],
        ) as cursor:
            for reviewed_by, count in await cursor.fetchall():
                reviewers[int(reviewed_by)] = reviewers.get(int(reviewed_by), 0) + count
                stats["game_reviewed"] += count
        stats["game_managers"] = sorted(reviewers.items(), key=lambda row: (-row[1], row[0]))

        async with db.execute(
            "SELECT COALESCE(SUM(c.delta), 0) FROM coins c "
            "JOIN users u ON u.telegram_id = c.user_id "
            "WHERE substr(c.timestamp, 1, 10) = ? AND c.delta > 0" + join_where,
            [day, *city_params],
        ) as cursor:
            stats["coins_awarded"] = (await cursor.fetchone())[0] or 0
    return stats


async def get_display_names(ids) -> dict[int, str]:
    """`{telegram_id: ФИО}` ОДНИМ запросом по `users` — для сводок, где имён много, а ходить
    за каждым по отдельности значило бы открыть по соединению на менеджера. Ключей меньше,
    чем спрошено: у кого строки/ФИО нет (менеджер, не заполнявший анкету), тот в ответе не
    появляется вовсе — подпись «менеджер #id» выбирает вызывающий, БД имён не выдумывает."""
    wanted = [int(i) for i in dict.fromkeys(ids) if i is not None]
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id, full_name FROM users WHERE telegram_id IN ({placeholders})",
            wanted,
        ) as cursor:
            return {
                int(row[0]): row[1] for row in await cursor.fetchall()
                if row[1] and str(row[1]).strip()
            }


# Квик 260923 (форум-чекин, D-01): сколько раз перегенерировать `secrets.token_urlsafe` при
# встрече с уже занятым значением, прежде чем сдаться и упасть громко. Коллизия на масштабе
# проекта (1000-1500 строк, CLAUDE.md) при 8 байтах энтропии практически невозможна — предел
# только защита от бесконечного цикла, если что-то в схеме сломано.
_CHECKIN_TOKEN_MAX_ATTEMPTS = 5


async def get_or_create_checkin_token(telegram_id: int) -> str | None:
    """Ленивая выдача токена чек-ина: первый запрос генерирует случайный
    `secrets.token_urlsafe(8)` (~11 символов, НЕ Telegram ID — не угадать) и сохраняет в
    `users.checkin_token`; второй и последующие запросы того же делегата отдают ТОТ ЖЕ токен.
    Бэкафилла нет — у пользователя, не запросившего QR ни разу, колонка остаётся NULL.

    Возвращает `None`, если пользователя нет вовсе. Гонка (два параллельных запроса одного
    делегата) закрыта самим SQL: `UPDATE ... WHERE checkin_token IS NULL` — при проигрыше
    (rowcount == 0) функция перечитывает строку и отдаёт токен, который успел записать
    конкурентный вызов, а не молча перезаписывает его своим кандидатом."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT checkin_token FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if row["checkin_token"]:
            return row["checkin_token"]

        for _ in range(_CHECKIN_TOKEN_MAX_ATTEMPTS):
            candidate = secrets.token_urlsafe(8)
            try:
                cursor = await db.execute(
                    "UPDATE users SET checkin_token = ? "
                    "WHERE telegram_id = ? AND checkin_token IS NULL",
                    (candidate, telegram_id),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                continue
            if cursor.rowcount:
                return candidate
            # Строку между SELECT и UPDATE успел заполнить конкурентный вызов — читаем то, что
            # он записал, вместо того чтобы молча потерять его результат.
            async with db.execute(
                "SELECT checkin_token FROM users WHERE telegram_id = ?", (telegram_id,)
            ) as cursor2:
                row2 = await cursor2.fetchone()
            return row2["checkin_token"] if row2 else None

        raise RuntimeError(
            f"get_or_create_checkin_token: не удалось выдать уникальный токен для {telegram_id} "
            f"за {_CHECKIN_TOKEN_MAX_ATTEMPTS} попыток"
        )


async def get_user_by_checkin_token(token: str | None) -> dict | None:
    """Делегат по токену из QR (последнее поле, `services.checkin.build_payload`/
    `parse_qr_payload`). Тёзки не путаются (D-13) — токен уникален по построению (частичный
    индекс `idx_users_checkin_token` выше)."""
    if not token:
        return None
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE checkin_token = ?", (token,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def reissue_checkin_token(telegram_id: int) -> str | None:
    """Форум-ночь B1 (идея №10): менеджер перевыпускает QR делегату — старый токен (если он
    вообще был выдан, лениво через `get_or_create_checkin_token`) уходит в
    `checkin_token_replacements`, `users.checkin_token` получает новый случайный токен. Скан
    старого QR после этого находит его в `checkin_token_replacements`
    (`get_checkin_token_replacement` ниже) вместо `users` — `services.checkin.
    resolve_scanned_user` превращает это в причину «QR заменён», а не «не найден».

    Возвращает новый токен или `None`, если пользователя нет вовсе. Та же защита от
    коллизии `secrets.token_urlsafe`, что `get_or_create_checkin_token` — до
    `_CHECKIN_TOKEN_MAX_ATTEMPTS` попыток, затем громкий `RuntimeError` (не проглатываем
    молча испорченную схему/исчерпанную энтропию)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT checkin_token FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        old_token = row["checkin_token"]

        for _ in range(_CHECKIN_TOKEN_MAX_ATTEMPTS):
            candidate = secrets.token_urlsafe(8)
            try:
                if old_token:
                    await db.execute(
                        "INSERT OR IGNORE INTO checkin_token_replacements "
                        "(old_token, telegram_id, replaced_at) VALUES (?, ?, ?)",
                        (old_token, telegram_id, msk_now().strftime("%Y-%m-%d %H:%M:%S")),
                    )
                cursor2 = await db.execute(
                    "UPDATE users SET checkin_token = ? WHERE telegram_id = ?",
                    (candidate, telegram_id),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                continue
            if cursor2.rowcount:
                return candidate
            return None

        raise RuntimeError(
            f"reissue_checkin_token: не удалось выдать уникальный токен для {telegram_id} "
            f"за {_CHECKIN_TOKEN_MAX_ATTEMPTS} попыток"
        )


async def get_checkin_token_replacement(old_token: str | None) -> dict | None:
    """`{"telegram_id": ..., "replaced_at": ...}`, если `old_token` был перевыпущен
    (`reissue_checkin_token` выше) — `None`, если этот токен никогда не заменяли (или он
    вообще никому не принадлежал)."""
    if not old_token:
        return None
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, replaced_at FROM checkin_token_replacements WHERE old_token = ?",
            (old_token,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def record_checkin(
    telegram_id: int,
    point: str,
    *,
    source: str,
    scanned_at: str | None = None,
    approx: bool = False,
    by_staff_id: int | None = None,
) -> tuple[str, str]:
    """Идемпотентно по (telegram_id, point, день скана по Москве) — хранит ПЕРВЫЙ скан на точку
    ЗА ДЕНЬ (вход каждый день двухдневного форума; D-10; про D-20
    «последний скан слота» для будущих сессий — см. докстринг таблицы `checkins` в `init_db`).
    Возвращает ("new", время_этой_отметки) при первой отметке, ("duplicate",
    время_ПЕРВОЙ_отметки) — если отметка уже была (для строки «уже был в ЧЧ:ММ»).

    T-12-03 (Rule 1, ревью): new/duplicate решает СТРОГО `cursor.rowcount` самой INSERT OR
    IGNORE, а не отдельная предварительная SELECT + сравнение `scanned_at == stamp`. Прежняя
    версия делала SELECT-check ДО инсерта и потом сверяла время: два скана одного делегата на
    одну точку в ОДНУ и ту же секунду (частый случай при параллельных волонтёрах на форуме,
    03.10 несколько стоек одновременно) считают одинаковый `stamp`, и «проигравший» гонку
    инсерт видел чужую (уже вставленную конкурентом) строку с ТЕМ ЖЕ значением `scanned_at` —
    сравнение молча признавало его «new» вместо «duplicate», двойной счёт в «Пришли: N из M».
    `rowcount` не зависит от совпадения секунд: ровно один конкурентный вызов физически
    вставляет строку (rowcount == 1 -- "new"), остальные получают rowcount == 0 от `UNIQUE
    (telegram_id, point)` и обязаны перечитать ПЕРВУЮ отметку для строки «уже был в ЧЧ:ММ»."""
    stamp = scanned_at or msk_now().strftime("%Y-%m-%d %H:%M:%S")
    created = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    day = stamp[:10]
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT OR IGNORE INTO checkins "
            "(telegram_id, point, scanned_at, source, approx_time, by_staff_id, created_at, day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, point, stamp, source, 1 if approx else 0, by_staff_id, created, day),
        )
        await db.commit()
        if cursor.rowcount:
            return "new", stamp
        async with db.execute(
            "SELECT scanned_at FROM checkins WHERE telegram_id = ? AND point = ? AND day = ?",
            (telegram_id, point, day),
        ) as cur2:
            existing = await cur2.fetchone()
    if existing is None:
        return "new", stamp  # не должно случаться (rowcount==0 без строки в базе), но не роняем вызывающего
    return "duplicate", existing["scanned_at"]


async def record_session_checkin(
    telegram_id: int,
    session_id: int,
    slot_session_ids: list[int],
    *,
    source: str,
    scanned_at: str | None = None,
    approx: bool = False,
    by_staff_id: int | None = None,
    previous_out: dict | None = None,
) -> tuple[str, str, int | None]:
    """Отметка на СЕССИИ (форум-ночь п.5, FORUM-CHECKIN.md D-18..D-20) — в отличие от
    `record_checkin` выше (первый скан побеждает, точка «Вход»), здесь «последний скан СЛОТА
    засчитывается» (D-20): делегат, ушедший с одной параллельной сессии на другую в ТОМ ЖЕ
    временном слоте, обязан считаться на НОВОЙ, а не на старой. `slot_session_ids` — id ДРУГИХ
    сессий слота (без самой `session_id`, слот строит `services.program.parallel_group`) — эта
    функция ничего не знает о времени/пересечении сессий, только про то, какие point-строки
    (`session:{id}`) — слот-соседи текущей.

    Возвращает `(status, scanned_at, previous_session_id)`:
      - `"duplicate"` — уже была отметка НА ЭТОЙ ЖЕ сессии — время первой отметки не трогаем
        (тот же принцип D-10, что у входа), `previous_session_id` всегда `None`.
      - `"moved"` — была отметка на ДРУГОЙ сессии этого же слота — та строка удаляется (делегат
        физически не может быть на двух параллельных сессиях одновременно), новая сохраняется,
        `previous_session_id` — id старой (вызывающий достаёт её название для строки «перенесено
        с …»).
      - `"new"` — в слоте не было ни одной отметки этого делегата вовсе.

    `aiosqlite.IntegrityError` на финальном INSERT (тот же приём, что у `reissue_checkin_token`
    выше) — редкая гонка двух волонтёров, отмечающих ОДНОГО делегата на РАЗНЫЕ сессии слота
    практически одновременно: проигравший перечитывает уже вставленную конкурентом строку и
    отвечает `"duplicate"` за НЕЁ, а не падает и не дублирует запись.

    Ревью TOCTOU (критично): read-delete-insert выше — критическая секция, а не последовательность
    независимых запросов. `UNIQUE(telegram_id, point)` защищает только ОДНУ точку, а не слот
    целиком — если два волонтёра ОДНОВРЕМЕННО отмечают ОДНОГО делегата на ДВУХ разных сессиях
    ОДНОГО слота, каждое соединение делает свой SELECT «других отметок слота нет» ДО того, как
    сосед закоммитил свой DELETE+INSERT: без явной блокировки sqlite3/aiosqlite открывает
    транзакцию лениво — только перед первым DML (INSERT/UPDATE/DELETE), не перед SELECT, — то
    есть read идёт в autocommit-режиме без лока, оба видят «слот свободен» и оба доходят до
    INSERT (по РАЗНЫМ `point`, поэтому constraint не срабатывает) -> в слоте остаются ДВЕ
    отметки вместо одной (нарушение D-20). `await db.execute("BEGIN IMMEDIATE")` ниже ставится
    ДО первого SELECT и берёт RESERVED-лок сразу (не отложенно): второе соединение, дошедшее до
    своего `BEGIN IMMEDIATE` раньше, чем первое закоммитило/откатило, ждёт (busy_timeout —
    `DB_BUSY_TIMEOUT_MS` у `_connect()` выше, 5с) и видит уже применённый DELETE+INSERT первого
    ДО своего собственного SELECT."""
    stamp = scanned_at or msk_now().strftime("%Y-%m-%d %H:%M:%S")
    created = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    point = f"session:{session_id}"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT scanned_at FROM checkins WHERE telegram_id = ? AND point = ?",
                (telegram_id, point),
            ) as cursor:
                existing_here = await cursor.fetchone()
            if existing_here is not None:
                await db.rollback()  # ничего не писали -- лок можно снять сразу
                return "duplicate", existing_here["scanned_at"], None

            previous_session_id: int | None = None
            if slot_session_ids:
                other_points = [f"session:{sid}" for sid in slot_session_ids]
                placeholders = ",".join("?" for _ in other_points)
                async with db.execute(
                    f"SELECT point, scanned_at, source, approx_time, by_staff_id, created_at "
                    f"FROM checkins WHERE telegram_id = ? AND point IN ({placeholders})",
                    [telegram_id, *other_points],
                ) as cursor:
                    other_rows = await cursor.fetchall()
                if other_rows:
                    previous_session_id = int(other_rows[0]["point"].split(":", 1)[1])
                    if previous_out is not None:
                        # Идея №32: снимок удаляемой строки — отмена ошибочного переноса
                        # волонтёром (`undo_venue_checkin`) возвращает её на место.
                        previous_out.update(dict(other_rows[0]))
                    await db.execute(
                        f"DELETE FROM checkins WHERE telegram_id = ? AND point IN ({placeholders})",
                        [telegram_id, *other_points],
                    )
            try:
                await db.execute(
                    "INSERT INTO checkins "
                    "(telegram_id, point, scanned_at, source, approx_time, by_staff_id, created_at, day) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (telegram_id, point, stamp, source, 1 if approx else 0, by_staff_id, created,
                     stamp[:10]),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                await db.rollback()
                async with db.execute(
                    "SELECT scanned_at FROM checkins WHERE telegram_id = ? AND point = ?",
                    (telegram_id, point),
                ) as cursor:
                    raced = await cursor.fetchone()
                return "duplicate", (raced["scanned_at"] if raced else stamp), None
        except Exception:
            await db.rollback()
            raise

    status = "moved" if previous_session_id is not None else "new"
    return status, stamp, previous_session_id


# ── Идеи №31/№32 бэклога чек-ина: журнал площадки и снятие ошибочной отметки ─────────────────
#
# Снятие = УДАЛЕНИЕ строки `checkins` + строка журнала `venue_log`, не флаг `revoked`: все
# потребители отметки (счётчики «Пришли N из M», фильтры рассылок «пришёл/не пришёл»,
# `checkin_not_arrived`, отзывы о сессии D-24, статистика по залам) читают `checkins` в момент
# работы — удалённая строка исчезает из всех разом, флаг пришлось бы добавить в каждый запрос.
# История «было — сняли» живёт в журнале.

def _venue_log_row(row) -> dict:
    item = dict(row)
    try:
        item["details"] = json.loads(item.get("details") or "{}")
    except (TypeError, ValueError):
        item["details"] = {}
    return item


async def _venue_log_insert(db, entry: dict) -> int:
    cursor = await db.execute(
        "INSERT INTO venue_log (created_at, action, staff_id, staff_name, telegram_id, city, "
        "point, source, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry.get("created_at") or msk_now().strftime("%Y-%m-%d %H:%M:%S"),
            entry["action"],
            entry.get("staff_id"),
            entry.get("staff_name"),
            entry.get("telegram_id"),
            entry.get("city"),
            entry.get("point"),
            entry.get("source"),
            json.dumps(entry.get("details") or {}, ensure_ascii=False),
        ),
    )
    return cursor.lastrowid


async def venue_log_add(entry: dict) -> int:
    """Одна строка журнала площадки. `entry` — поля таблицы `venue_log` (`action` обязателен,
    `details` — dict, `created_at` по умолчанию — сейчас по Москве). Возвращает id строки."""
    async with _connect() as db:
        log_id = await _venue_log_insert(db, entry)
        await db.commit()
    return log_id


async def venue_log_get(log_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM venue_log WHERE id = ?", (log_id,)) as cursor:
            row = await cursor.fetchone()
    return _venue_log_row(row) if row else None


async def undo_venue_checkin(
    log_id: int, staff_id: int, *, not_before: str, undo_entry: dict,
) -> tuple[str, dict | None]:
    """Отмена волонтёром СВОЕЙ ПОСЛЕДНЕЙ живой отметки (идея №32) — одной транзакцией
    (`BEGIN IMMEDIATE`, тот же приём, что `record_session_checkin`: проверка «последняя ли» и
    удаление не должны разъехаться с параллельным сканом того же волонтёра).

    Проверки — на сервере, фронту не верим: строка журнала `log_id` — отметка (`action =
    'checkin'`) ЭТОГО `staff_id`, ещё не отменена, создана не раньше `not_before` (окно
    отмены считает вызывающий) и это самая свежая отметка волонтёра. Удаляется ровно то, что
    поставил этот скан: строка точки, авто-вход (`details.auto_entry_at`, если вход появился
    этим же сканом) и возвращается строка прошлой сессии слота (`details.previous`, если скан
    был переносом D-20).

    Возвращает `(код, событие)`: `"ok"` | `"not_found"` | `"not_yours"` | `"expired"` |
    `"not_last"` | `"gone"` (строки отметки уже нет — её перенёс/снял кто-то другой)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("SELECT * FROM venue_log WHERE id = ?", (log_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None or row["action"] != "checkin":
                await db.rollback()
                return "not_found", None
            event = _venue_log_row(row)
            if event["staff_id"] != staff_id:
                await db.rollback()
                return "not_yours", event
            created_at = event.get("created_at")
            if event.get("undone_at") or created_at is None or created_at < not_before:
                await db.rollback()
                return "expired", event
            async with db.execute(
                "SELECT MAX(id) FROM venue_log WHERE action = 'checkin' AND staff_id = ?",
                (staff_id,),
            ) as cursor:
                latest = (await cursor.fetchone())[0]
            if latest != log_id:
                await db.rollback()
                return "not_last", event

            details = event["details"]
            tid = event["telegram_id"]
            # Исход решает rowcount самого DELETE, а не данные журнала: строку отметки мог
            # перенести (D-20) или снять кто-то другой — тогда ничего не пишем и откатываем.
            if not details.get("scanned_at") or tid is None:
                await db.rollback()
                return "gone", event
            cur = await db.execute(
                "DELETE FROM checkins WHERE telegram_id = ? AND point = ? AND scanned_at = ?",
                (tid, event["point"], details["scanned_at"]),
            )
            if not cur.rowcount:
                await db.rollback()
                return "gone", event
            if details.get("auto_entry_at"):
                await db.execute(
                    "DELETE FROM checkins WHERE telegram_id = ? AND point = ? AND source = "
                    "'auto_session' AND scanned_at = ?",
                    (tid, CHECKIN_ENTRY_POINT, details["auto_entry_at"]),
                )
            prev = details.get("previous")
            if prev:
                await db.execute(
                    "INSERT OR IGNORE INTO checkins (telegram_id, point, scanned_at, source, "
                    "approx_time, by_staff_id, created_at, day) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (tid, prev["point"], prev["scanned_at"], prev["source"],
                     prev.get("approx_time") or 0, prev.get("by_staff_id"), prev["created_at"],
                     prev["scanned_at"][:10]),
                )
            stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
            await db.execute("UPDATE venue_log SET undone_at = ? WHERE id = ?", (stamp, log_id))
            await _venue_log_insert(db, {
                **undo_entry, "created_at": stamp, "telegram_id": tid, "city": event["city"],
                "point": event["point"], "source": event["source"],
                "details": {
                    "undone_log_id": log_id, "scanned_at": details.get("scanned_at"),
                    "point_label": details.get("point_label"),
                    "previous_label": details.get("previous_label") if prev else None,
                },
            })
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return "ok", event


async def first_entry_scanned_at(telegram_id: int) -> str | None:
    """Время ПЕРВОГО входа делегата за форум (самый ранний из входов по дням) или `None` —
    колонка «Пришёл» таблицы и признак «первый вход за форум»."""
    async with _connect() as db:
        async with db.execute(
            "SELECT MIN(scanned_at) FROM checkins WHERE telegram_id = ? AND point = ?",
            (telegram_id, CHECKIN_ENTRY_POINT),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def first_entry_scanned_at_map() -> dict[int, str]:
    """`first_entry_scanned_at` для ВСЕХ делегатов одним запросом: {telegram_id: время первого
    входа}. Нужен пересборке/синхронизации листа — ячейка «Пришёл» строится из базы, а не «-»,
    и не запросом на строку."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id, MIN(scanned_at) FROM checkins WHERE point = ? GROUP BY telegram_id",
            (CHECKIN_ENTRY_POINT,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(tid): at for tid, at in rows if at}


async def has_entry_on_other_day(telegram_id: int, day: str) -> bool:
    """Был ли у делегата вход в ДРУГОЙ день форума, кроме `day` — `False` значит, что вход дня
    `day` — первый (и единственный) вход за форум."""
    async with _connect() as db:
        async with db.execute(
            "SELECT EXISTS(SELECT 1 FROM checkins WHERE telegram_id = ? AND point = ? AND day != ?)",
            (telegram_id, CHECKIN_ENTRY_POINT, day),
        ) as cursor:
            row = await cursor.fetchone()
    return bool(row and row[0])


async def get_checkin(checkin_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM checkins WHERE id = ?", (checkin_id,)) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def list_checkins_for_user(telegram_id: int) -> list[dict]:
    """Все отметки делегата: вход первым, дальше сессии по времени скана."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM checkins WHERE telegram_id = ? "
            "ORDER BY CASE WHEN point = ? THEN 0 ELSE 1 END, scanned_at",
            (telegram_id, CHECKIN_ENTRY_POINT),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def revoke_checkin(checkin_id: int, log_entry: dict) -> dict | None:
    """Менеджер снимает ОДНУ отметку (идея №32): удаление строки + строка журнала одной
    транзакцией. Остальные отметки делегата не трогаются (снятие входа не снимает сессии).
    `None` — строки уже нет (сняли параллельно / перенос D-20)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("SELECT * FROM checkins WHERE id = ?", (checkin_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return None
            removed = dict(row)
            cur = await db.execute("DELETE FROM checkins WHERE id = ?", (checkin_id,))
            if not cur.rowcount:  # строку удалили между SELECT и DELETE — журнал не пишем
                await db.rollback()
                return None
            details = {
                "scanned_at": removed["scanned_at"],
                "was_source": removed["source"],
                "was_by_staff_id": removed["by_staff_id"],
                **(log_entry.get("details") or {}),
            }
            await _venue_log_insert(db, {
                **log_entry, "telegram_id": removed["telegram_id"], "point": removed["point"],
                "details": details,
            })
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return removed


async def venue_log_page(*, city_scope=None, staff_id: int | None = None, offset: int = 0,
                         limit: int = 10) -> tuple[list[dict], int]:
    """Журнал площадки, новые сверху: `(строки страницы, всего)`. `city_scope` — дескриптор
    `cities.city_scope(...)` по колонке `venue_log.city`; `staff_id` — фильтр по волонтёру."""
    clauses: list[str] = []
    params: list = []
    city_sql, city_params = _city_clause(city_scope, "city")
    if city_sql:
        clauses.append(city_sql)
        params.extend(city_params)
    if staff_id is not None:
        clauses.append("staff_id = ?")
        params.append(staff_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT COUNT(*) FROM venue_log {where}", params) as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute(
            f"SELECT * FROM venue_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ) as cursor:
            rows = await cursor.fetchall()
    return [_venue_log_row(r) for r in rows], total


async def venue_log_staff(*, city_scope=None) -> list[dict]:
    """Кто что-то делал на площадке (для кнопок фильтра): `staff_id`, последнее известное имя,
    число действий — самые активные первыми."""
    city_sql, params = _city_clause(city_scope, "city")
    where = f"AND {city_sql}" if city_sql else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT staff_id, COUNT(*) AS n, "
            "(SELECT v2.staff_name FROM venue_log v2 WHERE v2.staff_id = venue_log.staff_id "
            " AND v2.staff_name IS NOT NULL ORDER BY v2.id DESC LIMIT 1) AS staff_name "
            f"FROM venue_log WHERE staff_id IS NOT NULL {where} "
            "GROUP BY staff_id ORDER BY n DESC, staff_id",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def count_checkins_by_point(point: str, *, city_scope=None, day: str | None = None) -> int:
    """T-12-04 (A2, FORUM-CHECKIN.md): `city_scope` — тот же дескриптор `cities.city_scope(...)`
    и та же `_city_clause`, что у `count_approved_current_season` ниже, — оба числа строки
    «Пришли: N из M» ОБЯЗАНЫ резолвиться одним городским правилом, иначе счётчик молча
    разъедется по разным городам. 03.10 форумы СПб и Тюмени идут одновременно с ещё открытым
    набором в Москве — общий (нескопированный) счётчик путает пришедших одного города с
    одобренными другого; `city_scope=None` (дефолт) — старое нескопированное поведение,
    байт-в-байт (модуль городов выключен или менеджер смотрит «Все города»).

    Вход каждый день: у входа строка на (делегат, день) — считаем ЛЮДЕЙ (DISTINCT), `day`
    («YYYY-MM-DD») — только отметки этого дня форума, `None` — хоть один день."""
    day_frag = " AND c.day = ?" if day else ""
    day_params = [day] if day else []
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    if not city_frag:
        async with _connect() as db:
            async with db.execute(
                f"SELECT COUNT(DISTINCT c.telegram_id) FROM checkins c WHERE c.point = ?{day_frag}",
                [point] + day_params,
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0) if row else 0
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT c.telegram_id) FROM checkins c "
            "JOIN users u ON u.telegram_id = c.telegram_id "
            f"WHERE c.point = ?{day_frag} AND {city_frag}",
            [point] + day_params + city_params,
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0) if row else 0


async def count_approved_current_season(*, city_scope=None) -> int:
    """Одобренные делегаты ТЕКУЩЕГО сезона — тот же признак «не прошлый делегат», что
    `reg_engine.is_past_season_row` (`season IS NULL OR season = event_season`). Знаменатель
    строки «Пришли: N из M одобренных» (handlers/admin_checkin.py)."""
    event_season = (await get_setting("event_season") or "").strip()
    where_parts = ["status = 'approved'"]
    params: list = []
    if event_season:
        where_parts.append("(season IS NULL OR season = ?)")
        params.append(event_season)
    city_frag, city_params = _city_clause(city_scope, "event_city")
    if city_frag:
        where_parts.append(city_frag)
        params.extend(city_params)
    where_sql = " AND ".join(where_parts)
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM users WHERE {where_sql}", params,
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0) if row else 0


# ── Форум-ночь п.3 (D-03, идея №2): рассылка QR накануне форума + утренний повтор ────────────

async def list_approved_users(*, city_scope=None) -> list[dict]:
    """Кандидатный пул для `services.checkin_broadcast`: строки `users` со `status='approved'`
    в границах `city_scope`, БЕЗ фильтра по сезону — сезон (и статус ещё раз) перепроверяет
    `services.checkin.checkin_denial` на КАЖДОЙ строке вызывающим кодом (задание просило
    «через checkin_denial», не отдельную копию его правила SQL-условием), единственный источник
    правды о допуске остаётся один. `city_scope=None` — без ограничения по городу (модуль
    городов выключен)."""
    city_frag, city_params = _city_clause(city_scope, "event_city")
    where = "status = 'approved'"
    params: list = []
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT * FROM users WHERE {where}", params) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def checkin_qr_mark_sent(telegram_id: int, event_city: str | None, sent_at: str) -> bool:
    """Идемпотентная отметка «QR отправлен» — `INSERT OR IGNORE` по `telegram_id` (PRIMARY
    KEY): повторный вызов для уже отправленного делегата ничего не меняет и возвращает
    `False` (звонящий код — `services.checkin_broadcast.send_broadcast` — строит выборку
    получателей ИЗ `checkin_qr_sent_ids`, поэтому второй вызов на того же человека не должен
    случаться в норме; это последний рубеж на гонку двух одновременных отправок одного города).
    `True` — эта строка вставлена именно этим вызовом."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO checkin_qr_sends (telegram_id, event_city, sent_at) "
            "VALUES (?, ?, ?)",
            (telegram_id, event_city, sent_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def checkin_qr_sent_ids(*, city_scope=None, sent_since: str | None = None) -> set[int]:
    """Кому УЖЕ отправлен QR (любой источник — вечерняя джоба/ручная кнопка), в границах
    `city_scope` — по СНИМКУ `checkin_qr_sends.event_city` (город на момент отправки), не по
    текущему `users.event_city`. Вызывающий (`send_broadcast`) вычитает этот набор из
    кандидатного пула — идемпотентность рассылки: повторный запуск/рестарт не шлёт дважды.
    `sent_since` («YYYY-MM-DD HH:MM:SS») — только получившие QR впервые не раньше этого момента
    (утренний повтор не шлёт второй раз тем, кому QR ушёл ручной рассылкой этим же утром)."""
    city_frag, city_params = _city_clause(city_scope, "event_city")
    conds: list[str] = []
    params: list = []
    if city_frag:
        conds.append(city_frag)
        params.extend(city_params)
    if sent_since:
        conds.append("sent_at >= ?")
        params.append(sent_since)
    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM checkin_qr_sends{where}", params
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


async def checkin_qr_unconfirmed_ids(*, city_scope=None) -> set[int]:
    """Кому отправлен QR, но подтверждения «✅ Сохранил» ещё нет — аудитория утреннего
    повтора (`services.checkin_broadcast.send_morning_repeat`), в границах `city_scope`."""
    city_frag, city_params = _city_clause(city_scope, "event_city")
    where = "confirmed_at IS NULL"
    params: list = []
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM checkin_qr_sends WHERE {where}", params
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


async def checkin_qr_confirmed_ids(*, city_scope=None) -> set[int]:
    """Кто уже подтвердил «✅ Сохранил» — единственное, что исключает делегата из аудитории
    утреннего повтора (`services.checkin_broadcast.send_morning_repeat`, находка ревью
    260924: повтор обязан звать ВСЕХ допущенных города, кто ещё не подтвердил, а не только
    тех, у кого уже есть строка `checkin_qr_sends` — иначе одобренный ПОСЛЕ вечерней рассылки
    или потерянный из-за сбоя отправки делегат не получает QR никогда). В отличие от
    `checkin_qr_unconfirmed_ids` (строка есть, `confirmed_at IS NULL`) эта выборка НЕ требует
    существования строки вовсе — вызывающий вычитает результат из полного пула
    `eligible_recipients`, а не пересекает с уже отправленными."""
    city_frag, city_params = _city_clause(city_scope, "event_city")
    where = "confirmed_at IS NOT NULL"
    params: list = []
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM checkin_qr_sends WHERE {where}", params
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


async def checkin_qr_confirm(telegram_id: int, confirmed_at: str) -> bool:
    """Подтверждение «✅ Сохранил, открывается» — идемпотентно: `UPDATE ... WHERE confirmed_at
    IS NULL` пишет метку только на ПЕРВОЕ нажатие (возвращает `True`); повторный тап той же
    кнопки (двойной клик, форвард сообщения) находит `confirmed_at` уже не NULL, ничего не
    меняет, возвращает `False` — вызывающий хендлер отвечает тем же дружелюбным текстом в обоих
    случаях, разница видна только в возвращаемом флаге (для теста), не в ответе делегату.
    `False` тоже, если строки нет вовсе (делегат не получал QR через эту рассылку — например,
    сам открыл «🎟 Мой QR» до первой отправки)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE checkin_qr_sends SET confirmed_at = ? "
            "WHERE telegram_id = ? AND confirmed_at IS NULL",
            (confirmed_at, telegram_id),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def checkin_qr_send_counts(*, city_scope=None) -> tuple[int, int]:
    """`(получили, подтвердили)` — строка «✅ Отметки на форуме» (handlers/admin_checkin.py)."""
    city_frag, city_params = _city_clause(city_scope, "event_city")
    where = f" WHERE {city_frag}" if city_frag else ""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN confirmed_at IS NOT NULL THEN 1 ELSE 0 END) "
            f"FROM checkin_qr_sends{where}",
            city_params,
        ) as cursor:
            row = await cursor.fetchone()
    total = int(row[0] or 0) if row else 0
    confirmed = int(row[1] or 0) if row and row[1] is not None else 0
    return total, confirmed


# ── D-33 (решение владельца 24.09): шпаргалка волонтёра чек-ина за день до форума ────────────

async def checkin_volunteer_guide_mark_sent(
    telegram_id: int, day: str, city: str | None, sent_at: str,
) -> bool:
    """Идемпотентная отметка «шпаргалка отправлена на этот `day` (день форума)» — `INSERT OR
    IGNORE` по `UNIQUE(telegram_id, day)`: повторный вызов (рестарт бота, реконсиляция) для уже
    отправленной пары (человек, день форума) ничего не меняет, возвращает `False`. `True` —
    строка вставлена именно этим вызовом (услуга оказана впервые)."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO checkin_volunteer_guide_sends (telegram_id, day, city, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (telegram_id, day, city, sent_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def checkin_volunteer_guide_unmark(telegram_id: int, day: str) -> None:
    """Снять отметку «шпаргалка отправлена на `day`» — вызывающий застолбил отправку
    (`checkin_volunteer_guide_mark_sent` ДО отправки), а сообщение не дошло: без снятия повтор
    (джоба, повторная выдача права) считал бы, что человек её уже получил."""
    async with _connect() as db:
        await db.execute(
            "DELETE FROM checkin_volunteer_guide_sends WHERE telegram_id = ? AND day = ?",
            (telegram_id, day),
        )
        await db.commit()


async def checkin_volunteer_guide_sent_ids(day: str) -> set[int]:
    """Кому УЖЕ отправлена шпаргалка на этот `day` (день форума) — вызывающий
    (`services.checkin_volunteer_broadcast.send_guide`) вычитает этот набор из держателей
    capability `checkin`, идемпотентность рассылки."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM checkin_volunteer_guide_sends WHERE day = ?", (day,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


# ── Форум-ночь п.6 (D-25, идея №14): шаблон «Не пришёл» + ответы делегата ─────────────────────

# Значения `checkin_not_arrived.response` — сентинелы (не булево), та же причина строки, что у
# CHAT_IN/CHAT_OUT выше: переживают JSON/строковый круговорот там, где он есть, и человеку
# нигде не показываются как код (только как подпись кнопки).
CNA_COMING = "coming"
CNA_CANT = "cant"
CNA_HERE = "here"


async def checkin_not_arrived_pending_ids(*, city_scope=None) -> list[int]:
    """Кандидаты на сегодняшний шаблон «Не пришёл»: approved текущего сезона без отметки
    «Вход» СЕГОДНЯ (вход каждый день: пришедший вчера, но не сегодня — тоже кандидат; то же условие, что ветка `checkin_entry`=`CHECKIN_NO` в `_build_filter_clause`,
    второй копии условия не заводится), МИНУС те, кому шаблон уже уходил СЕГОДНЯ (МСК) — сама
    идемпотентность «повторный тап в тот же день не шлёт дважды»."""
    filters: list[dict] = [{"field": "checkin_entry", "value": CHECKIN_NO, "day": CHECKIN_DAY_TODAY}]
    if city_scope is not None:
        code, exclude = city_scope
        filters.append({"field": "event_city", "value": code, "exclude": list(exclude)})
    candidates = await count_and_list_filtered(filters)
    if not candidates:
        return []
    day = msk_now().strftime("%Y-%m-%d")
    placeholders = ",".join("?" for _ in candidates)
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM checkin_not_arrived WHERE day = ? "
            f"AND telegram_id IN ({placeholders})",
            (day, *candidates),
        ) as cursor:
            already = {row[0] for row in await cursor.fetchall()}
    return [tid for tid in candidates if tid not in already]


async def checkin_not_arrived_mark_sent(telegram_id: int, event_city: str | None, sent_at: str) -> bool:
    """`INSERT OR IGNORE` по `(telegram_id, day)` — идемпотентная отправка на СЕГОДНЯ (`day` —
    календарный день `sent_at`, МСК). `True` — эта строка вставлена именно этим вызовом."""
    day = sent_at[:10]
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO checkin_not_arrived (telegram_id, day, event_city, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (telegram_id, day, event_city, sent_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def checkin_not_arrived_unmark(telegram_id: int, day: str) -> None:
    """Снять отметку «отправлено сегодня» — отправка не удалась по временной причине, и
    повторное «📨 Написать не пришедшим» должно взять этого делегата снова."""
    async with _connect() as db:
        await db.execute(
            "DELETE FROM checkin_not_arrived WHERE telegram_id = ? AND day = ? AND response IS NULL",
            (telegram_id, day),
        )
        await db.commit()


async def record_checkin_not_arrived_response(telegram_id: int, day: str, response: str, responded_at: str) -> bool:
    """Пишет ответ делегата в строку `(telegram_id, day)` — `day` приходит из `callback_data`
    (см. докстринг `handlers/user_actions.py`), не из FSM (переживает рестарт контейнера).
    Повторный тап любой из трёх кнопок на то же сообщение перезаписывает ответ (делегат мог
    ошибиться и поправиться) — не идемпотентно в смысле «первый побеждает», идемпотентно в
    смысле «строка всегда одна на (делегат, день)» (`UNIQUE`). `False` — строки ещё нет (не
    должно случаться: кнопка приходит только в уже отправленном сообщении), не роняем
    вызывающего."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE checkin_not_arrived SET response = ?, responded_at = ? "
            "WHERE telegram_id = ? AND day = ?",
            (response, responded_at, telegram_id, day),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def checkin_not_arrived_summary(*, city_scope=None, day: str | None = None) -> dict:
    """Сводка менеджеру «Едут N · Не смогут M · Уже на месте K» (+ «без ответа») за `day`
    (по умолчанию — сегодня, МСК). `city_scope` — по СНИМКУ `event_city` (город на момент
    отправки), тот же приём, что `checkin_qr_sent_ids`."""
    day = day or msk_now().strftime("%Y-%m-%d")
    city_frag, city_params = _city_clause(city_scope, "event_city")
    where = "day = ?"
    params: list = [day]
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT response, COUNT(*) FROM checkin_not_arrived WHERE {where} GROUP BY response",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    counts = {row[0]: row[1] for row in rows}
    no_response = counts.get(None, 0)
    return {
        "coming": counts.get(CNA_COMING, 0),
        "cant": counts.get(CNA_CANT, 0),
        "here": counts.get(CNA_HERE, 0),
        "no_response": no_response,
        "total": sum(counts.values()),
    }


# ── Форум-ночь п.4: расписание форума в боте (program_halls/program_sessions) ─────────────────
# Бизнес-правила (разбор времени, предупреждение о занятости зала, слоты параллельных сессий,
# копирование между городами) — в аiogram-free `services/program.py`; здесь только сырой CRUD,
# тем же приёмом, что `services/reject_rules.py` поверх `reject_rules`/`auto_reject_log`.

async def create_program_hall(city: str, name: str, capacity: int | None = None) -> int:
    """Новый зал города — `sort_order` авто (следующий после максимального уже существующего
    в этом городе): менеджер не вводит число сортировки руками (CLAUDE.md — кодовые значения
    людям не показываем и вводить не просим)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM program_halls WHERE city = ?", (city,),
        ) as cursor:
            row = await cursor.fetchone()
        next_sort = int(row[0]) + 1 if row and row[0] is not None else 0
        cursor = await db.execute(
            "INSERT INTO program_halls (city, name, capacity, sort_order) VALUES (?, ?, ?, ?)",
            (city, name, capacity, next_sort),
        )
        await db.commit()
        return cursor.lastrowid


async def list_program_halls(city: str) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM program_halls WHERE city = ? ORDER BY sort_order, id", (city,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_program_hall(hall_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM program_halls WHERE id = ?", (hall_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def rename_program_hall(hall_id: int, name: str) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE program_halls SET name = ? WHERE id = ?", (name, hall_id),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def count_program_sessions_for_hall(hall_id: int) -> int:
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM program_sessions WHERE hall_id = ?", (hall_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


async def delete_program_hall(hall_id: int) -> bool:
    """Удаление зала НЕ удаляет его сессии — они остаются в программе, только теряют
    привязку (`hall_id -> NULL`); подтверждение на экране (handlers/admin_program.py) называет
    их число ДО удаления, тем же приёмом, что `arr_delete_confirm`/`afaq_delete_confirm`."""
    async with _connect() as db:
        await db.execute(
            "UPDATE program_sessions SET hall_id = NULL, updated_at = ? WHERE hall_id = ?",
            (msk_now().strftime("%Y-%m-%d %H:%M:%S"), hall_id),
        )
        cursor = await db.execute("DELETE FROM program_halls WHERE id = ?", (hall_id,))
        await db.commit()
        return bool(cursor.rowcount)


async def create_program_session(
    city: str, day: str, start_time: str, end_time: str, title: str, *,
    speaker: str | None = None, hall_id: int | None = None, description: str | None = None,
) -> int:
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO program_sessions "
            "(city, day, start_time, end_time, title, speaker, hall_id, description, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (city, day, start_time, end_time, title, speaker, hall_id, description, stamp, stamp),
        )
        await db.commit()
        return cursor.lastrowid


async def get_program_session(session_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM program_sessions WHERE id = ?", (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


# Поля, которые `update_program_session` умеет частично патчить — тот же приём, что явный allow-
# list колонок у любой другой PATCH-функции в этом файле (не SET из произвольных kwargs).
_PROGRAM_SESSION_PATCH_FIELDS = (
    "day", "start_time", "end_time", "title", "speaker", "hall_id", "description",
)


async def update_program_session(session_id: int, **fields) -> bool:
    """Частичный PATCH — только ключи из `_PROGRAM_SESSION_PATCH_FIELDS`, `updated_at`
    обновляется вместе с ними. Без единого известного поля UPDATE не выполняется вовсе,
    возвращает `False` (как `rename_program_hall` без реального изменения)."""
    keys = [k for k in fields if k in _PROGRAM_SESSION_PATCH_FIELDS]
    if not keys:
        return False
    sets = [f"{key} = ?" for key in keys]
    values = [fields[key] for key in keys]
    sets.append("updated_at = ?")
    values.append(msk_now().strftime("%Y-%m-%d %H:%M:%S"))
    values.append(session_id)
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE program_sessions SET {', '.join(sets)} WHERE id = ?", values,
        )
        await db.commit()
        return bool(cursor.rowcount)


async def delete_program_session(session_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute("DELETE FROM program_sessions WHERE id = ?", (session_id,))
        await db.commit()
        return bool(cursor.rowcount)


async def list_program_sessions_for_city_day(city: str, day: str) -> list[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM program_sessions WHERE city = ? AND day = ? "
            "ORDER BY start_time, end_time, id",
            (city, day),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def list_program_days_for_city(city: str) -> list[str]:
    async with _connect() as db:
        async with db.execute(
            "SELECT DISTINCT day FROM program_sessions WHERE city = ? ORDER BY day", (city,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def has_program_sessions_for_city(city: str) -> bool:
    """Гейт кнопки делегата «🗓 Программа» (keyboards/builders.py::get_main_menu_kb) — дешёвый
    `EXISTS`, а не подсчёт/выборка."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM program_sessions WHERE city = ? LIMIT 1", (city,),
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


async def sessions_overlapping_hall(
    city: str, day: str, hall_id: int, start_time: str, end_time: str, *,
    exclude_id: int | None = None,
) -> list[dict]:
    """Сессии ДРУГОГО занятия ТОГО ЖЕ зала в ТОТ ЖЕ день, чей интервал `[start_time, end_time)`
    пересекается с переданным — предупреждение словами (CLAUDE.md), не запрет: вызывающий
    (`services.program.hall_conflict_warning`) показывает текст и спрашивает подтверждение,
    сохранить разрешено в любом случае."""
    params: list = [city, day, hall_id, end_time, start_time]
    sql = (
        "SELECT * FROM program_sessions WHERE city = ? AND day = ? AND hall_id = ? "
        "AND start_time < ? AND ? < end_time"
    )
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def list_all_program_sessions() -> list[dict]:
    """Все сессии всех городов/дней — только для `services.session_feedback.reconcile_all()`
    (перевзвод джоб отзыва на старте бота, тот же приём, что `reconcile_wave_jobs`); экраны
    менеджера/делегата продолжают читать `list_program_sessions_for_city_day` (скоуп город+день)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM program_sessions") as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Форум-ночь п.9 (идея №15, D-24): «⭐ Отзыв о сессии одним тапом» ────────────────────────

async def is_marked_for_session(telegram_id: int, session_id: int) -> bool:
    """Итоговая отметка слота (D-20: «последний скан слота засчитывается» — `checkins.point`
    хранит РОВНО одну строку на слот благодаря `record_session_checkin`) — единственная
    проверка допуска к оценке: не отмеченный на этой сессии делегат не может её оценить, ни
    получить приглашение (D-24). Точка отметки собрана строкой `f"session:{id}"` НАПРЯМУЮ, не
    через `services.program.point_for_session` — тот модуль импортирует ИЗ `database.db`,
    обратный импорт замкнул бы цикл (тот же довод, что у `record_session_checkin` выше)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM checkins WHERE telegram_id = ? AND point = ? LIMIT 1",
            (telegram_id, f"session:{session_id}"),
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


async def list_marked_telegram_ids_for_session(session_id: int) -> list[int]:
    """Круг получателей приглашения оценить сессию — те же строки, что видит `is_marked_for_
    session` по одному, но списком (джоба рассылки, `services.session_feedback.deliver_
    feedback_prompts`)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM checkins WHERE point = ?", (f"session:{session_id}",),
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]


# Форум-ночь (идея №4 бэклога чек-ина, координация «одна функция чтения»): «✅ Ты отмечен» — обе
# поверхности (кнопка «🎟 Мой QR» бота, `handlers/user_actions.py`; хаб Mini App,
# `miniapp/routers/hub.py`) читают ОДНУ функцию, а не заводят по своей копии SQL — та же
# граница, что уже держат `is_marked_for_session`/`count_checkins_by_point` выше.

async def get_checkin_status(telegram_id: int) -> dict | None:
    """`{"scanned_at": "YYYY-MM-DD HH:MM:SS", "day": "YYYY-MM-DD", "is_today": bool,
    "time_label": "ЧЧ:ММ" | "ЧЧ:ММ (ДД.ММ)", "sessions_count": N}` — время отметки на входе
    (`CHECKIN_ENTRY_POINT`) и число ОТДЕЛЬНЫХ сессий, на которых делегат отмечен (`point LIKE
    'session:%'`, по одной строке на слот — D-20, `record_session_checkin` уже держит эту
    гарантию). `None`, если входа ещё не было — обе поверхности трактуют `None` как «не
    показывать строку вовсе», а не как нулевые факты.

    Вход каждый день: у делегата может быть НЕСКОЛЬКО строк входа (по одной на день форума) —
    берём СЕГОДНЯШНИЙ вход, если он есть, иначе последний по дню (`ORDER BY (day = сегодня)
    DESC, day DESC`, одним запросом, без отдельного «сначала проверить сегодня» похода в БД).
    `time_label` — готовая подпись для `{time}` обеих поверхностей (`handlers/user_actions.py::
    show_my_checkin_qr`, `miniapp/routers/hub.py::_checkin_status_fact`, единая функция чтения,
    второй копии форматирования не заводим): просто «ЧЧ:ММ» для сегодняшнего входа, «
    ЧЧ:ММ (ДД.ММ)» для входа другого дня — без даты делегат мог бы принять вчерашний вход за
    сегодняшний. Дата в скобках, а не «ДД.ММ в ЧЧ:ММ»: шаблон уже говорит «на входе в {time}»,
    и подпись без предлогов одинаково читается в русском и английском тексте."""
    today = msk_now().strftime("%Y-%m-%d")
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT scanned_at, day FROM checkins WHERE telegram_id = ? AND point = ? "
            "ORDER BY (day = ?) DESC, day DESC LIMIT 1",
            (telegram_id, CHECKIN_ENTRY_POINT, today),
        ) as cursor:
            entry = await cursor.fetchone()
        if entry is None:
            return None
        async with db.execute(
            "SELECT COUNT(*) FROM checkins WHERE telegram_id = ? AND point LIKE 'session:%'",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
    scanned_at = entry["scanned_at"] or ""
    day = entry["day"] or ""
    is_today = day == today
    time_part = scanned_at[11:16] or "—"
    if is_today or len(day) != 10:
        time_label = time_part
    else:
        time_label = f"{time_part} ({day[8:10]}.{day[5:7]})"
    return {
        "scanned_at": scanned_at, "day": day, "is_today": is_today,
        "time_label": time_label, "sessions_count": row[0],
    }


async def create_session_feedback_prompt(telegram_id: int, session_id: int, prompted_at: str) -> bool:
    """`INSERT OR IGNORE` — идемпотентность самой РАССЫЛКИ (не только оценки): джоба, тикнувшая
    дважды (перепланирование при правке сессии + старый таймер не снялся, гонка reconcile на
    рестарте), не шлёт делегату второе приглашение — `True` только у ПЕРВОЙ вставки, вызывающий
    шлёт сообщение только тогда."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO session_feedback "
            "(telegram_id, session_id, prompted_at) VALUES (?, ?, ?)",
            (telegram_id, session_id, prompted_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def set_session_feedback_rating(telegram_id: int, session_id: int, rating: int, rated_at: str) -> bool:
    """Повторный тап меняет оценку (правило плана: «одна оценка на делегата на сессию») —
    обычный `UPDATE` по уже существующей строке-приглашению. Строки нет вовсе (делегат каким-то
    образом дотянулся до чужого/устаревшего callback_data без приглашения) -> `False`,
    вызывающий отвечает алертом, не пишет вслепую."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE session_feedback SET rating = ?, rated_at = ? "
            "WHERE telegram_id = ? AND session_id = ?",
            (rating, rated_at, telegram_id, session_id),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def set_session_feedback_comment(telegram_id: int, session_id: int, comment: str, commented_at: str) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE session_feedback SET comment = ?, commented_at = ? "
            "WHERE telegram_id = ? AND session_id = ?",
            (comment, commented_at, telegram_id, session_id),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def get_session_feedback(telegram_id: int, session_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM session_feedback WHERE telegram_id = ? AND session_id = ?",
            (telegram_id, session_id),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def session_feedback_stats(session_id: int) -> dict:
    """`{"avg": float|None, "rating_count": int, "comment_count": int}` для карточки сессии
    (`handlers.admin_program.render_session_card`) — `avg is None`, если оценок ещё нет
    («Пока нет оценок», не «0.0»)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT AVG(rating), COUNT(rating), "
            "SUM(CASE WHEN comment IS NOT NULL AND comment != '' THEN 1 ELSE 0 END) "
            "FROM session_feedback WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
    avg, rating_count, comment_count = row if row else (None, 0, 0)
    return {
        "avg": float(avg) if avg is not None else None,
        "rating_count": int(rating_count or 0),
        "comment_count": int(comment_count or 0),
    }


async def session_feedback_stats_bulk(session_ids: list[int]) -> dict[int, dict]:
    """Та же статистика, что `session_feedback_stats`, для НЕСКОЛЬКИХ сессий одним запросом —
    экран «📊 Оценки сессий» дня (`handlers.session_feedback`) не бьёт БД по сессии в цикле.
    Сессия без единой строки `session_feedback` — просто отсутствует в результате, вызывающий
    подставляет нулевую статистику сам (тот же приём, что `program.sessions_for_city_day`
    подставляет `hall_name=None` для сессий без зала)."""
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    async with _connect() as db:
        async with db.execute(
            f"SELECT session_id, AVG(rating), COUNT(rating), "
            f"SUM(CASE WHEN comment IS NOT NULL AND comment != '' THEN 1 ELSE 0 END) "
            f"FROM session_feedback WHERE session_id IN ({placeholders}) GROUP BY session_id",
            session_ids,
        ) as cursor:
            rows = await cursor.fetchall()
    return {
        r[0]: {
            "avg": float(r[1]) if r[1] is not None else None,
            "rating_count": int(r[2] or 0),
            "comment_count": int(r[3] or 0),
        }
        for r in rows
    }


async def list_session_feedback_comments(session_id: int, *, limit: int = 10, offset: int = 0) -> list[dict]:
    """Комментарии сессии, новые сверху — постранично (экран «💬 Комментарии»)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, rating, comment, commented_at FROM session_feedback "
            "WHERE session_id = ? AND comment IS NOT NULL AND comment != '' "
            "ORDER BY commented_at DESC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Идея №16 бэклога чек-ина: «📊 Отчёт дня форума» вечером ──────────────────────────────────
# Домен (сборка текста, планирование джобы) — `services/forum_day_report.py`; здесь только
# сырой CRUD/агрегаты, тем же приёмом, что `services/session_feedback.py` поверх
# `session_feedback_stats*` выше.

def _forum_day_report_city_key(city: str | None) -> str:
    """`city or "_all"` — сентинел вместо NULL (см. докстринг CREATE TABLE
    `forum_day_report_sends`), используется И на запись, И на чтение — обе стороны обязаны
    читать/писать один и тот же ключ, иначе идемпотентность разъедется по городам."""
    return city or "_all"


async def forum_day_report_sent_days(city: str | None) -> set[str]:
    """Дни форума этого города, за которые АВТОМАТИЧЕСКИЙ отчёт уже уходил — вызывающий
    (`services.forum_day_report.schedule_city_job`) вычитает их из окна дней форума, чтобы
    выбрать следующий ещё не отправленный день. Ручная кнопка «Отчёт дня сейчас» эту таблицу
    не читает и не пишет (см. докстринг таблицы)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT day FROM forum_day_report_sends WHERE city = ?",
            (_forum_day_report_city_key(city),),
        ) as cursor:
            rows = await cursor.fetchall()
    return {r[0] for r in rows}


async def forum_day_report_mark_sent(city: str | None, day: str, sent_at: str) -> bool:
    """`INSERT OR IGNORE` по `(city, day)` — идемпотентная отметка АВТОМАТИЧЕСКОЙ отправки.
    `True` — эта строка вставлена именно этим вызовом."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO forum_day_report_sends (city, day, sent_at) VALUES (?, ?, ?)",
            (_forum_day_report_city_key(city), day, sent_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def count_checkins_by_point_and_day(point: str, day: str, *, city_scope=None) -> int:
    """Тот же приём, что `count_checkins_by_point` выше, но дополнительно скопировано днём
    отметки (`checkins.day` — «YYYY-MM-DD» по Москве, колонка, не вычисление из `scanned_at`,
    см. докстринг `_CHECKINS_DDL`/`record_checkin` — тот же признак, которым `miniapp/routers/
    checkin.py` уже считает «Пришли N из M» на своём экране) — «пришли сегодня N», а не
    «пришли за весь форум N»."""
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    if not city_frag:
        async with _connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM checkins WHERE point = ? AND day = ?",
                (point, day),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0) if row else 0
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM checkins c JOIN users u ON u.telegram_id = c.telegram_id "
            f"WHERE c.point = ? AND c.day = ? AND {city_frag}",
            [point, day] + city_params,
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0) if row else 0


async def checkin_peak_hour_for_city_day(day: str, *, city_scope=None) -> tuple[str, int] | None:
    """`(час "HH", число отметок)` с наибольшим числом отметок на входе (`CHECKIN_ENTRY_POINT`)
    за `day` (колонка `checkins.day`, не вычисление из `scanned_at` — см. докстринг
    `count_checkins_by_point_and_day`) в границах `city_scope` — `None`, если отметок в этот
    день нет вовсе (строка отчёта дня пропускается, а не рисует пустой пик)."""
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    where = "c.point = ? AND c.day = ?"
    params: list = [CHECKIN_ENTRY_POINT, day]
    join = ""
    if city_frag:
        join = "JOIN users u ON u.telegram_id = c.telegram_id "
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT substr(c.scanned_at, 12, 2) AS hh, COUNT(*) AS n FROM checkins c {join}"
            f"WHERE {where} GROUP BY hh ORDER BY n DESC, hh ASC LIMIT 1",
            params,
        ) as cursor:
            row = await cursor.fetchone()
    if row is None or not row[0]:
        return None
    return str(row[0]), int(row[1] or 0)


async def list_checkins_for_city_day(day: str, *, city_scope=None) -> list[dict]:
    """Каждая отметка (вход и сессии) за `day` (колонка `checkins.day` — см. докстринг
    `count_checkins_by_point_and_day`) в границах `city_scope` — источник CSV-выгрузки
    «📥 Выгрузить отметки (CSV)» кнопки отчёта дня."""
    city_frag, city_params = _city_clause(city_scope, "u.event_city")
    where = "c.day = ?"
    params: list = [day]
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT c.telegram_id, u.full_name, u.username, c.point, c.scanned_at, c.source, "
            "c.approx_time, c.by_staff_id FROM checkins c "
            f"JOIN users u ON u.telegram_id = c.telegram_id WHERE {where} "
            "ORDER BY c.scanned_at",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def sos_day_stats(day: str, *, city_scope=None) -> dict:
    """`{"total": N, "resolved": M, "avg_claim_minutes": float|None}` — сколько SOS создано за
    `day`, сколько решено, среднее время до «🙋 Беру» (только среди тех, кого вообще взяли).
    `avg_claim_minutes is None` — либо SOS в этот день не было, либо ни один не был взят."""
    city_frag, city_params = _city_clause(city_scope, "city")
    where = "substr(created_at, 1, 10) = ?"
    params: list = [day]
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END), "
            f"AVG(CASE WHEN claimed_at IS NOT NULL "
            f"THEN (julianday(claimed_at) - julianday(created_at)) * 24 * 60 END) "
            f"FROM sos_reports WHERE {where}",
            params,
        ) as cursor:
            row = await cursor.fetchone()
    total = int(row[0] or 0) if row else 0
    resolved = int(row[1] or 0) if row and row[1] is not None else 0
    avg_claim = float(row[2]) if row and row[2] is not None else None
    return {"total": total, "resolved": resolved, "avg_claim_minutes": avg_claim}


# ── Идея №23 бэклога чек-ина: опрос неявившихся «почему не пришёл» ───────────────────────────

FORUM_NOSHOW_REASON_CHANGED_MIND = "changed_mind"
FORUM_NOSHOW_REASON_STUDY_WORK = "study_work"
FORUM_NOSHOW_REASON_FAR = "far"
FORUM_NOSHOW_REASON_FORGOT = "forgot"
FORUM_NOSHOW_REASON_OTHER = "other"
FORUM_NOSHOW_REASONS: tuple[str, ...] = (
    FORUM_NOSHOW_REASON_CHANGED_MIND, FORUM_NOSHOW_REASON_STUDY_WORK,
    FORUM_NOSHOW_REASON_FAR, FORUM_NOSHOW_REASON_FORGOT, FORUM_NOSHOW_REASON_OTHER,
)


async def forum_noshow_poll_pending_ids(*, city_scope=None) -> list[int]:
    """Кандидаты на опрос: approved текущего сезона (тот же `checkin_entry`=`CHECKIN_NO`
    фильтр, что `checkin_not_arrived_pending_ids` — единая точка правды, второй копии условия
    не заводится) БЕЗ отметки входа НИ В ОДИН день форума, МИНУС те, кому опрос уже уходил В
    ЭТОМ сезоне (`forum_noshow_poll_sent_ids`)."""
    filters: list[dict] = [{"field": "checkin_entry", "value": CHECKIN_NO}]
    if city_scope is not None:
        code, exclude = city_scope
        filters.append({"field": "event_city", "value": code, "exclude": list(exclude)})
    candidates = await count_and_list_filtered(filters)
    if not candidates:
        return []
    season = (await get_setting("event_season") or "").strip()
    already = await forum_noshow_poll_sent_ids(season)
    return [tid for tid in candidates if tid not in already]


async def forum_noshow_poll_sent_ids(season: str) -> set[int]:
    """Кому УЖЕ отправлен опрос в ЭТОМ `season` (снимок сезона на момент отправки) —
    вызывающий (`services.forum_noshow_poll.send_poll`) вычитает этот набор из кандидатов,
    идемпотентность рассылки: повторный тик/перезапуск не шлёт дважды за один сезон."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM forum_noshow_poll WHERE season = ?", (season,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


async def forum_noshow_poll_mark_sent(
    telegram_id: int, city: str | None, season: str, sent_at: str,
) -> bool:
    """`INSERT OR IGNORE` по `(telegram_id, season)` — та же строка потом принимает ОТВЕТ
    (`record_forum_noshow_poll_response`). `True` — эта строка вставлена именно этим вызовом."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO forum_noshow_poll (telegram_id, city, season, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (telegram_id, city, season, sent_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def record_forum_noshow_poll_response(
    telegram_id: int, season: str, reason: str, comment: str | None, answered_at: str,
) -> bool:
    """Повторный тап другой кнопки меняет ответ (правило плана: «повторный тап меняет
    ответ») — обычный `UPDATE` по уже существующей строке-приглашению. Строки нет вовсе
    (делегат дотянулся до чужого/устаревшего callback_data) -> `False`, вызывающий отвечает
    тихо, не пишет вслепую."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE forum_noshow_poll SET reason = ?, comment = ?, answered_at = ? "
            "WHERE telegram_id = ? AND season = ?",
            (reason, comment, answered_at, telegram_id, season),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def forum_noshow_poll_summary(season: str, *, city_scope=None) -> dict:
    """«Ответили N из M: передумал 12, учёба 7…» — строка экрана менеджера
    (`handlers.admin_forum_functions`). `sent` — M (всем, кому опрос уходил), `answered` — N
    (кто нажал хоть одну кнопку), `by_reason` — счётчик по каждой причине (все пять ключей
    всегда присутствуют, даже нулевые — вызывающему не приходится гадать, какие бывают)."""
    city_frag, city_params = _city_clause(city_scope, "city")
    where = "season = ?"
    params: list = [season]
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM forum_noshow_poll WHERE {where}", params,
        ) as cursor:
            sent_row = await cursor.fetchone()
        async with db.execute(
            f"SELECT reason, COUNT(*) FROM forum_noshow_poll WHERE {where} "
            f"AND reason IS NOT NULL GROUP BY reason",
            params,
        ) as cursor:
            reason_rows = await cursor.fetchall()
    counts = {row[0]: row[1] for row in reason_rows}
    answered = sum(counts.values())
    return {
        "sent": int(sent_row[0] or 0) if sent_row else 0,
        "answered": answered,
        "by_reason": {r: counts.get(r, 0) for r in FORUM_NOSHOW_REASONS},
    }


# ── Идея №29 бэклога чек-ина: «Твой Юлид в цифрах» — картинка-итог после форума ─────────────

async def forum_stats_card_sent_ids(season: str) -> set[int]:
    """Кому УЖЕ отправлена карточка в ЭТОМ `season` — вызывающий (`services.forum_stats_card`)
    вычитает этот набор из кандидатов, идемпотентность рассылки: повторный тик/тап не шлёт
    дважды за один сезон."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM forum_stats_card_sends WHERE season = ?", (season,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


async def forum_stats_card_mark_sent(telegram_id: int, city: str | None, season: str, sent_at: str) -> bool:
    """`INSERT OR IGNORE` по `UNIQUE(telegram_id, season)` — `True` эта строка вставлена именно
    этим вызовом (гонка двойного тапа/параллельного вызова видит `False` и не считает
    делегата отправленным дважды)."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO forum_stats_card_sends (telegram_id, city, season, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (telegram_id, city, season, sent_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def forum_stats_card_summary(season: str, *, city_scope=None) -> dict:
    """«Отправлено N» — строка экрана менеджера. `city_scope` — по СНИМКУ
    `forum_stats_card_sends.city` (город на момент отправки), тот же приём, что
    `checkin_qr_sent_ids`."""
    city_frag, city_params = _city_clause(city_scope, "city")
    where = "season = ?"
    params: list = [season]
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM forum_stats_card_sends WHERE {where}", params,
        ) as cursor:
            row = await cursor.fetchone()
    return {"sent": int(row[0] or 0) if row else 0}


# ── Трек «региональные форумы → Москва»: перенос неявившихся ────────────────────────────────

RNM_MOVED = "moved"
RNM_DECLINED = "declined"


async def regional_noshow_move_pending_ids(*, city_scope=None) -> list[int]:
    """Кандидаты на предложение переноса: approved текущего сезона города `city_scope` БЕЗ
    отметки входа НИ В ОДИН день форума (тот же фильтр `checkin_entry`=`CHECKIN_NO`, что
    `forum_noshow_poll_pending_ids` — единая точка правды), МИНУС те, кому предложение уже
    уходило В ЭТОМ сезоне, МИНУС те, кто в опросе неявившихся `forum_noshow_poll` ЭТОГО сезона
    ответил «не интересно» (решение координатора 25.09: «Передумал(а)»/
    `FORUM_NOSHOW_REASON_CHANGED_MIND` И «Не смог(ла) по учёбе/работе»/
    `FORUM_NOSHOW_REASON_STUDY_WORK`) — остальные причины (далеко/забыл/другое) предложение
    получают как обычно."""
    filters: list[dict] = [{"field": "checkin_entry", "value": CHECKIN_NO}]
    if city_scope is not None:
        code, exclude = city_scope
        filters.append({"field": "event_city", "value": code, "exclude": list(exclude)})
    candidates = await count_and_list_filtered(filters)
    if not candidates:
        return []
    season = (await get_setting("event_season") or "").strip()
    already = await regional_noshow_move_sent_ids(season)
    not_interested = await _forum_noshow_poll_not_interested_ids(season)
    return [tid for tid in candidates if tid not in already and tid not in not_interested]


_RNM_NOT_INTERESTED_POLL_REASONS = (
    FORUM_NOSHOW_REASON_CHANGED_MIND, FORUM_NOSHOW_REASON_STUDY_WORK,
)


async def _forum_noshow_poll_not_interested_ids(season: str) -> set[int]:
    """Кто в опросе неявившихся ЭТОГО сезона ответил одной из «не интересно»-причин
    (`_RNM_NOT_INTERESTED_POLL_REASONS`) — решение координатора 25.09."""
    placeholders = ",".join("?" for _ in _RNM_NOT_INTERESTED_POLL_REASONS)
    async with _connect() as db:
        async with db.execute(
            f"SELECT telegram_id FROM forum_noshow_poll WHERE season = ? AND reason IN ({placeholders})",
            (season, *_RNM_NOT_INTERESTED_POLL_REASONS),
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


async def regional_noshow_move_sent_ids(season: str) -> set[int]:
    """Кому УЖЕ отправлено предложение в ЭТОМ `season` — вычитается из кандидатов, та же
    идемпотентность рассылки, что `forum_noshow_poll_sent_ids`."""
    async with _connect() as db:
        async with db.execute(
            "SELECT telegram_id FROM regional_noshow_move WHERE season = ?", (season,),
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(r[0]) for r in rows}


async def regional_noshow_move_mark_sent(
    telegram_id: int, source_city: str | None, season: str, sent_at: str,
) -> bool:
    """`INSERT OR IGNORE` по `(telegram_id, season)` — та же строка потом принимает ОТВЕТ
    (`record_regional_noshow_move_response`). `True` — эта строка вставлена именно этим
    вызовом."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO regional_noshow_move (telegram_id, source_city, season, "
            "sent_at) VALUES (?, ?, ?, ?)",
            (telegram_id, source_city, season, sent_at),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def regional_noshow_move_get(telegram_id: int, season: str) -> dict | None:
    """Текущая строка предложения этого делегата в этом сезоне — `None`, если предложение не
    уходило вовсе (чужой/устаревший callback_data, вызывающий отвечает тихо)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM regional_noshow_move WHERE telegram_id = ? AND season = ?",
            (telegram_id, season),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def record_regional_noshow_move_response(
    telegram_id: int, season: str, response: str, target_city: str | None, responded_at: str,
) -> bool:
    """Повторный тап меняет ответ (та же конвенция, что `record_forum_noshow_poll_response`) —
    обычный `UPDATE` по уже существующей строке-приглашению. Строки нет вовсе -> `False`.
    Используется только для `RNM_DECLINED` (`record_decline`) — сам перенос (`RNM_MOVED`) идёт
    через `regional_noshow_move_claim` ниже (ревью 🟡4: гонка двойного тапа требует атомарного
    `WHERE response IS NULL`, не безусловного `UPDATE`). Уже перенесённого «Нет, спасибо» не
    перетирает: тап «отказаться», проигравший гонку захвату переноса, просто ничего не меняет."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE regional_noshow_move SET response = ?, target_city = ?, responded_at = ? "
            "WHERE telegram_id = ? AND season = ? AND (response IS NULL OR response != ?)",
            (response, target_city, responded_at, telegram_id, season, RNM_MOVED),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def regional_noshow_move_claim(
    telegram_id: int, season: str, target_city: str, responded_at: str,
) -> bool:
    """Атомарный захват строки ПЕРЕД самим переносом (ревью 🟡4, `services.regional_noshow_move.
    apply_move`) — `UPDATE ... WHERE response IS NULL`. `rowcount == 1` означает, что именно
    ЭТОТ вызов выиграл гонку: СУБД сериализует конкурентные `UPDATE` на одну строку, второй
    одновременный тап увидит `response` уже не `NULL` и получит `rowcount == 0`. Сам перенос
    (`services.city_move.move_user_city`) стартует ТОЛЬКО после `True` здесь — не наоборот
    (см. докстринг `apply_move`)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE regional_noshow_move SET response = ?, target_city = ?, responded_at = ? "
            "WHERE telegram_id = ? AND season = ? AND response IS NULL",
            (RNM_MOVED, target_city, responded_at, telegram_id, season),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def regional_noshow_move_release_claim(telegram_id: int, season: str) -> None:
    """Откат захвата (`regional_noshow_move_claim`) — сам перенос технически не удался ПОСЛЕ
    того, как строка уже забрана: возвращаем `response`/`target_city`/`responded_at` в `NULL`,
    чтобы делегат мог повторить тап «Да, перенести». Гард `response = ?` (`RNM_MOVED`) не
    трогает строку, если она уже не в том состоянии, которое сам захват в неё записал."""
    async with _connect() as db:
        await db.execute(
            "UPDATE regional_noshow_move SET response = NULL, target_city = NULL, responded_at = NULL "
            "WHERE telegram_id = ? AND season = ? AND response = ?",
            (telegram_id, season, RNM_MOVED),
        )
        await db.commit()


async def regional_noshow_move_unnotified_moved() -> list[dict]:
    """Строки `response=RNM_MOVED`, ещё не попавшие в сводку менеджеру города назначения
    (`notified_at IS NULL`) — читает ВЕСЬ бот (не по одному городу), группировка по
    `target_city`/`source_city` — забота вызывающего (`services.regional_noshow_move.
    _notify_managers_job`)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, telegram_id, source_city, target_city FROM regional_noshow_move "
            "WHERE response = ? AND notified_at IS NULL",
            (RNM_MOVED,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def regional_noshow_move_mark_notified(ids: list[int], notified_at: str) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    async with _connect() as db:
        await db.execute(
            f"UPDATE regional_noshow_move SET notified_at = ? WHERE id IN ({placeholders})",
            [notified_at, *ids],
        )
        await db.commit()


async def regional_noshow_move_summary(season: str, *, city_scope=None) -> dict:
    """«Предложено N, перенеслись M, отказались K» — строка экрана менеджера
    (`handlers.admin_forum_functions`). `city_scope` фильтрует по `source_city` (регион, откуда
    ушло предложение) — тот же смысл, что `forum_noshow_poll_summary`."""
    city_frag, city_params = _city_clause(city_scope, "source_city")
    where = "season = ?"
    params: list = [season]
    if city_frag:
        where += f" AND {city_frag}"
        params.extend(city_params)
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM regional_noshow_move WHERE {where}", params,
        ) as cursor:
            offered_row = await cursor.fetchone()
        async with db.execute(
            f"SELECT response, COUNT(*) FROM regional_noshow_move WHERE {where} "
            f"AND response IS NOT NULL GROUP BY response",
            params,
        ) as cursor:
            response_rows = await cursor.fetchall()
    counts = {row[0]: row[1] for row in response_rows}
    return {
        "offered": int(offered_row[0] or 0) if offered_row else 0,
        "moved": counts.get(RNM_MOVED, 0),
        "declined": counts.get(RNM_DECLINED, 0),
    }


# ── Идея №20 бэклога чек-ина: бюро находок ────────────────────────────────────────────────

async def create_lost_found_item(
    city: str | None, photo_file_id: str, where_text: str, posted_by: int,
    chat_id: int, message_id: int,
) -> int:
    """`created_at` — снимок `msk_now()` внутри функции (тот же приём, что
    `create_volunteer_invite`), вызывающему передавать его не нужно."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT INTO lost_found (city, photo_file_id, where_text, posted_by, chat_id, "
            "message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                city, photo_file_id, where_text, posted_by, chat_id, message_id,
                msk_now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_lost_found_item(item_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM lost_found WHERE id = ?", (item_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def mark_lost_found_returned(item_id: int, returned_by: int) -> bool:
    """`UPDATE ... WHERE returned_at IS NULL` — идемпотентность кнопки «✅ Нашёлся хозяин»:
    `True` только когда ИМЕННО этот вызов впервые закрыл находку, `False` — записи нет или
    её уже закрыли раньше (повторный тап/гонка двух одновременных тапов)."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE lost_found SET returned_at = ?, returned_by = ? "
            "WHERE id = ? AND returned_at IS NULL",
            (msk_now().strftime("%Y-%m-%d %H:%M:%S"), returned_by, item_id),
        )
        await db.commit()
        return bool(cursor.rowcount)
