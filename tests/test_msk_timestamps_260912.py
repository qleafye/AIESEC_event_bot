"""Квик 260912-mcj: семья меток времени «сейчас» бота — с UTC-контейнера на московское naive.

`services/timeutil.py::msk_now()` — единственный источник этой семьи (карточка заявки, профиль
Mini App, лист, дашборд, фильтр рассылки по дате). `services.timeutil.process_clock_is_utc()` —
гейт одноразовой миграции старых строк (`database.db._migrate_local_timestamps_to_msk`),
вызывается из `init_db` НА ОДНОМ соединении с финальным commit — см. докстринг функции.

Три сторожа в этом файле:
1. `msk_now()`/`process_clock_is_utc()` — чистые unit-тесты хелперов.
2. AST-сторож — ни одного голого `datetime.now()` (без аргументов) в коде бота и Mini App,
   кроме leaf-модулей `services/timeutil.py`/`miniapp/timeutil.py`. AST, а не grep: докстринги
   и комментарии этих же файлов упоминают строку `datetime.now()` как текст, и grep-гейт
   самовалидировался бы на этом тексте.
3. Миграция — сдвиг/не-сдвиг/идемпотентность/isoformat/кривые значения.

pytest-asyncio недоступен в этом окружении — как и в `tests/test_timezone_fix_260816.py`,
каждый async-хелпер гонится через `asyncio.run()`, `config.DB_PATH` указывает на файл в
`tmp_path`.
"""
import ast
import asyncio
import glob
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from config import config
from database import db
import services.timeutil as timeutil_mod
from services.timeutil import msk_now, process_clock_is_utc

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _fresh_db(tmp_path, name: str) -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    asyncio.run(db.init_db())
    return path


async def _run_conn(db_path: str, fn):
    config.DB_PATH = db_path
    async with db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        return await fn(conn)


# ── 1. msk_now() / process_clock_is_utc() — чистые unit-тесты ────────────────────────────

def test_msk_now_is_naive_and_matches_moscow_wall_clock():
    value = msk_now()
    assert value.tzinfo is None
    oracle = datetime.now(MOSCOW_TZ).replace(tzinfo=None)
    assert abs((value - oracle).total_seconds()) <= 5


def test_msk_now_differs_from_utcnow_by_exactly_180_minutes():
    # Москва не переходит на летнее время (постоянный UTC+3) — разница со стабильным
    # datetime.utcnow() всегда ровно 180 минут, без сезонного дрейфа.
    delta_minutes = (msk_now() - datetime.utcnow()).total_seconds() / 60.0
    assert abs(delta_minutes - 180) <= 0.1


def test_process_clock_is_utc_true_when_now_and_utcnow_agree():
    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 9, 12, 12, 0, 0)
            return base if tz is None else base.replace(tzinfo=tz)

        @classmethod
        def utcnow(cls):
            return datetime(2026, 9, 12, 12, 0, 0)

    orig = timeutil_mod.datetime
    timeutil_mod.datetime = _FakeDatetime
    try:
        assert process_clock_is_utc() is True
    finally:
        timeutil_mod.datetime = orig


def test_process_clock_is_utc_false_when_now_is_moscow_and_utcnow_is_utc():
    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 9, 12, 15, 0, 0)  # UTC+3 относительно utcnow ниже
            return base if tz is None else base.replace(tzinfo=tz)

        @classmethod
        def utcnow(cls):
            return datetime(2026, 9, 12, 12, 0, 0)

    orig = timeutil_mod.datetime
    timeutil_mod.datetime = _FakeDatetime
    try:
        assert process_clock_is_utc() is False
    finally:
        timeutil_mod.datetime = orig


# ── 2. AST-сторож: ни одного голого datetime.now() в коде бота/Mini App ──────────────────

_ALLOWED_BARE_DATETIME_NOW = {"services/timeutil.py", "miniapp/timeutil.py"}


def _bare_datetime_now_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "now"
            and isinstance(func.value, ast.Name)
            and func.value.id == "datetime"
            and not node.args
            and not node.keywords
        ):
            hits.append(node.lineno)
    return hits


def test_no_bare_datetime_now_in_bot_and_miniapp_code():
    """AST, не grep: докстринги/комментарии этих же файлов упоминают текст `datetime.now()`
    как историческую справку — текстовый grep-гейт самовалидировался бы на них."""
    patterns = [
        "database/db.py",
        "services/*.py",
        "handlers/*.py",
        "miniapp/*.py",
        "miniapp/routers/*.py",
    ]
    offenders: dict[str, list[int]] = {}
    for pattern in patterns:
        for path_str in glob.glob(pattern):
            norm = path_str.replace("\\", "/")
            if norm in _ALLOWED_BARE_DATETIME_NOW:
                continue
            hits = _bare_datetime_now_lines(Path(path_str))
            if hits:
                offenders[norm] = hits
    assert not offenders, f"голый datetime.now() без аргументов запрещён (квик 260912-mcj): {offenders}"


# ── 3. Миграция старых строк ──────────────────────────────────────────────────────────────

async def _seed_pre_migration_rows(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "INSERT INTO users (telegram_id, registration_date, edited_at, approved_at, paid_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, "2026-09-01 10:00:00", "2026-09-01 10:05:00", "2026-09-01 10:06:00",
         "2026-09-01T10:10:00.123456"),
    )
    await conn.execute(
        "INSERT INTO reg_events (telegram_id, event, ts) VALUES (?, ?, ?)",
        (1, "start", "2026-09-01 10:00:00"),
    )
    await conn.execute(
        "INSERT INTO application_decisions "
        "(telegram_id, decision, decided_by, decided_at, effects_due_at) VALUES (?, ?, ?, ?, ?)",
        (1, "approved", 999, "2026-09-01 10:00:00", "2026-09-01 10:00:05"),
    )
    await conn.execute(
        "INSERT INTO user_consents (user_id, consent_key, accepted_at) VALUES (?, ?, ?)",
        (1, "main", "2026-09-01T10:00:00.500000"),
    )
    await conn.execute(
        "INSERT INTO reg_drafts (telegram_id, kind, updated_by, updated_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, "new", "bot", "2026-09-01 10:00:00", "2026-09-01 09:55:00"),
    )
    await conn.commit()


async def _read_family(conn: aiosqlite.Connection) -> dict:
    async with conn.execute(
        "SELECT registration_date, edited_at, approved_at, paid_at FROM users WHERE telegram_id = ?",
        (1,),
    ) as cur:
        user_row = await cur.fetchone()
    async with conn.execute("SELECT ts FROM reg_events WHERE telegram_id = ?", (1,)) as cur:
        ev_row = await cur.fetchone()
    async with conn.execute(
        "SELECT decided_at, effects_due_at FROM application_decisions WHERE telegram_id = ?", (1,)
    ) as cur:
        dec_row = await cur.fetchone()
    async with conn.execute(
        "SELECT accepted_at FROM user_consents WHERE user_id = ?", (1,)
    ) as cur:
        consent_row = await cur.fetchone()
    async with conn.execute(
        "SELECT updated_at, created_at FROM reg_drafts WHERE telegram_id = ?", (1,)
    ) as cur:
        draft_row = await cur.fetchone()
    async with conn.execute("PRAGMA user_version") as cur:
        version_row = await cur.fetchone()
    return {
        "registration_date": user_row["registration_date"],
        "edited_at": user_row["edited_at"],
        "approved_at": user_row["approved_at"],
        "paid_at": user_row["paid_at"],
        "ts": ev_row["ts"],
        "draft_updated_at": draft_row["updated_at"],
        "draft_created_at": draft_row["created_at"],
        "decided_at": dec_row["decided_at"],
        "effects_due_at": dec_row["effects_due_at"],
        "accepted_at": consent_row["accepted_at"],
        "user_version": version_row[0] if version_row else 0,
    }


def _reset_marker_and_seed(db_path: str) -> None:
    """Пост-фикс (полный прогон 260912): гейт миграции — `PRAGMA user_version`, не строка
    `bot_settings` (та засоряла реестр настроек, см. докстринг `_migrate_local_timestamps_to_msk`).
    Сброс гейта здесь — `PRAGMA user_version = 0`, а не DELETE по таблице."""

    async def _do():
        config.DB_PATH = db_path
        async with db._connect() as conn:
            await conn.execute("PRAGMA user_version = 0")
            await conn.commit()
            await _seed_pre_migration_rows(conn)

    asyncio.run(_do())


def test_migration_shifts_family_by_3_hours_when_process_clock_is_utc(tmp_path):
    db_path = _fresh_db(tmp_path, "migrate_utc.db")
    _reset_marker_and_seed(db_path)

    orig = db.process_clock_is_utc
    db.process_clock_is_utc = lambda: True
    try:
        config.DB_PATH = db_path
        asyncio.run(db.init_db())
    finally:
        db.process_clock_is_utc = orig

    result = asyncio.run(_run_conn(db_path, _read_family))
    assert result["registration_date"] == "2026-09-01 13:00:00"
    assert result["edited_at"] == "2026-09-01 13:05:00"
    assert result["approved_at"] == "2026-09-01 13:06:00"
    assert result["paid_at"] == "2026-09-01T13:10:00.123456"
    assert result["ts"] == "2026-09-01 13:00:00"
    assert result["decided_at"] == "2026-09-01 13:00:00"
    assert result["effects_due_at"] == "2026-09-01 13:00:05"
    assert result["accepted_at"] == "2026-09-01T13:00:00.500000"
    assert result["draft_updated_at"] == "2026-09-01 13:00:00"
    assert result["draft_created_at"] == "2026-09-01 12:55:00"
    assert result["user_version"] == db._MSK_MIGRATION_USER_VERSION


def test_migration_is_idempotent_on_second_boot(tmp_path):
    db_path = _fresh_db(tmp_path, "migrate_idem.db")
    _reset_marker_and_seed(db_path)

    orig = db.process_clock_is_utc
    db.process_clock_is_utc = lambda: True
    try:
        config.DB_PATH = db_path
        asyncio.run(db.init_db())
        after_first = asyncio.run(_run_conn(db_path, _read_family))
        asyncio.run(db.init_db())  # second boot -- marker already set
        after_second = asyncio.run(_run_conn(db_path, _read_family))
    finally:
        db.process_clock_is_utc = orig

    assert after_first == after_second


def test_migration_shifts_reg_draft_created_at_and_second_boot_is_a_noop(tmp_path):
    """Доработка по решению оркестратора (после исходного исполнения квика): `created_at`
    черновика анкеты пишется тем же `msk_now()`, что и соседние `updated_at`/`submitting_at`
    (которые уже были в списке миграции) — оставлять одну колонку той же строки на 3 часа
    позади остальных бессмысленно, `_draft_is_fresh` (TTL черновика,
    `handlers/registration.py`) читает именно `created_at`. Отдельный, независимый от общего
    `_read_family` кейс — колонка сдвинулась один раз, второй прогон её больше не трогает."""
    db_path = _fresh_db(tmp_path, "migrate_draft_created_at.db")

    async def seed():
        config.DB_PATH = db_path
        async with db._connect() as conn:
            await conn.execute("PRAGMA user_version = 0")
            await conn.execute(
                "INSERT INTO reg_drafts (telegram_id, kind, updated_by, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (42, "new", "bot", "2026-09-01 10:00:00", "2026-09-01 09:50:00"),
            )
            await conn.commit()

    asyncio.run(seed())

    async def read_created_at():
        config.DB_PATH = db_path
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT created_at FROM reg_drafts WHERE telegram_id = ?", (42,)
            ) as cur:
                row = await cur.fetchone()
                return row[0]

    orig = db.process_clock_is_utc
    db.process_clock_is_utc = lambda: True
    try:
        config.DB_PATH = db_path
        asyncio.run(db.init_db())  # first boot -- shifts +3h
        after_first = asyncio.run(read_created_at())
        asyncio.run(db.init_db())  # second boot -- marker already set, no-op
        after_second = asyncio.run(read_created_at())
    finally:
        db.process_clock_is_utc = orig

    assert after_first == "2026-09-01 12:50:00"
    assert after_second == "2026-09-01 12:50:00"


def test_migration_skips_shift_when_process_clock_is_already_moscow(tmp_path):
    db_path = _fresh_db(tmp_path, "migrate_msk.db")
    _reset_marker_and_seed(db_path)

    orig = db.process_clock_is_utc
    db.process_clock_is_utc = lambda: False
    try:
        config.DB_PATH = db_path
        asyncio.run(db.init_db())
    finally:
        db.process_clock_is_utc = orig

    result = asyncio.run(_run_conn(db_path, _read_family))
    # Ничего не сдвинуто -- значения байт-в-байт исходные.
    assert result["registration_date"] == "2026-09-01 10:00:00"
    assert result["edited_at"] == "2026-09-01 10:05:00"
    assert result["approved_at"] == "2026-09-01 10:06:00"
    assert result["paid_at"] == "2026-09-01T10:10:00.123456"
    assert result["ts"] == "2026-09-01 10:00:00"
    assert result["accepted_at"] == "2026-09-01T10:00:00.500000"
    assert result["draft_updated_at"] == "2026-09-01 10:00:00"
    assert result["draft_created_at"] == "2026-09-01 09:55:00"
    # Но маркер стоит -- иначе каждый старт на этой же машине пересчитывал бы часы заново.
    assert result["user_version"] == db._MSK_MIGRATION_USER_VERSION


def test_migration_survives_curved_and_empty_values(tmp_path):
    db_path = _fresh_db(tmp_path, "migrate_curved.db")

    async def seed_curved():
        config.DB_PATH = db_path
        async with db._connect() as conn:
            await conn.execute("PRAGMA user_version = 0")
            await conn.execute(
                "INSERT INTO users (telegram_id, registration_date, edited_at, approved_at, "
                "paid_at) VALUES (?, ?, ?, ?, ?)",
                (2, "", "мусор", None, "не дата"),
            )
            await conn.commit()

    asyncio.run(seed_curved())

    orig = db.process_clock_is_utc
    db.process_clock_is_utc = lambda: True
    try:
        config.DB_PATH = db_path
        asyncio.run(db.init_db())  # must not raise
    finally:
        db.process_clock_is_utc = orig

    async def read_curved():
        config.DB_PATH = db_path
        async with db._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT registration_date, edited_at, approved_at, paid_at FROM users "
                "WHERE telegram_id = ?",
                (2,),
            ) as cur:
                return await cur.fetchone()

    row = asyncio.run(read_curved())
    assert row["registration_date"] == ""
    assert row["edited_at"] == "мусор"
    assert row["approved_at"] is None
    assert row["paid_at"] == "не дата"


def test_migration_skips_columns_missing_in_old_schema(tmp_path):
    """Колонка семьи, которой ещё нет (старая БД, `_ensure_column` для неё не отработал) —
    пропускается молча, миграция не падает. `_column_exists` фильтрует список до SQL, поэтому
    несуществующая пара (таблица, колонка) в `_MSK_MIGRATION_COLUMNS` не должна ронять прогон."""
    db_path = _fresh_db(tmp_path, "migrate_missing_col.db")
    _reset_marker_and_seed(db_path)

    orig_columns = db._MSK_MIGRATION_COLUMNS
    db._MSK_MIGRATION_COLUMNS = orig_columns + (("users", "does_not_exist_260912"),)
    orig_clock = db.process_clock_is_utc
    db.process_clock_is_utc = lambda: True
    try:
        config.DB_PATH = db_path
        asyncio.run(db.init_db())  # must not raise despite the bogus column
    finally:
        db._MSK_MIGRATION_COLUMNS = orig_columns
        db.process_clock_is_utc = orig_clock

    result = asyncio.run(_run_conn(db_path, _read_family))
    assert result["registration_date"] == "2026-09-01 13:00:00"
    assert result["user_version"] == db._MSK_MIGRATION_USER_VERSION


# ── 4. Дашборд: свой timeutil, tzdata объявлена, ноль импортов бота ──────────────────────

def test_dashboard_requirements_declares_tzdata():
    req = Path("dashboard/requirements.txt").read_text(encoding="utf-8")
    assert any(line.strip() == "tzdata" for line in req.splitlines()), (
        "dashboard/timeutil.py резолвит ZoneInfo('Europe/Moscow') -- в slim-образе системной "
        "базы поясов может не быть, как и у бота"
    )


def test_dashboard_modules_never_import_bot_services_or_database():
    """Тот же класс падения образа, что ловит `test_dashboard_docker.py` для корневых
    модулей (web_theme 31.08, tg_media 10.09) -- `dashboard/Dockerfile` копирует ТОЛЬКО
    `dashboard/` + несколько корневых файлов, `services.timeutil.msk_now` уронил бы контейнер
    на старте `ModuleNotFoundError`."""
    offenders = {}
    for path_str in glob.glob("dashboard/*.py"):
        tree = ast.parse(Path(path_str).read_text(encoding="utf-8"), filename=path_str)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            bad = [n for n in names if n == "services" or n.startswith("services.")
                   or n == "database" or n.startswith("database.")]
            if bad:
                offenders.setdefault(path_str.replace("\\", "/"), []).extend(bad)
    assert not offenders, f"dashboard/*.py не должен импортировать бота: {offenders}"
