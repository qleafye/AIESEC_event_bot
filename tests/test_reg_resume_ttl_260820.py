"""Quick 260820-rms: окно восстановления брошенной анкеты (reg_started).

Прод 20.08: делегат с строкой reg_started от 08.08 (event_city='tyumen') не видел экран
выбора города НИ НА ОДНОМ /start — cmd_start восстанавливал город из строки, а
_should_show_city_fork выходит на непустом городе. Строка живёт до конца регистрации и никем
не чистится (её читают «Незавершённые» и догонялка), поэтому лечение — окно на ЧТЕНИИ, а не
удаление строк.

pytest-asyncio недоступен — async через asyncio.run(), config.DB_PATH -> tmp.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA, _parse_setting

USER_ID = 703402465  # тот самый делегат из разбора


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reg_resume_ttl.db")
    asyncio.run(db.init_db())


def _seed_started(hours_ago: float, city="tyumen", track="full"):
    """Строка reg_started с началом hours_ago часов назад (started_at пишется локальным
    временем процесса — тем же, что и mark_reg_started)."""
    stamp = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    async def go():
        await db.mark_reg_started(USER_ID, "dasha", track, city)
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_started SET started_at = ? WHERE telegram_id = ?", (stamp, USER_ID)
            )
            await conn.commit()

    asyncio.run(go())


# ── город ────────────────────────────────────────────────────────────────────────────────────

def test_fresh_row_city_still_recovered(tmp_path):
    """Тот же вечер, повторный /start посреди анкеты — город подставляется, как и раньше."""
    _ready(tmp_path)
    _seed_started(hours_ago=2)
    assert asyncio.run(db.get_reg_started_city(USER_ID, 24)) == "tyumen"


def test_stale_row_city_not_recovered(tmp_path):
    """Возврат через 12 дней — город НЕ наследуется, значит экран выбора города покажется."""
    _ready(tmp_path)
    _seed_started(hours_ago=12 * 24)
    assert asyncio.run(db.get_reg_started_city(USER_ID, 24)) is None


def test_stale_row_survives_the_read(tmp_path):
    """Окно действует только на чтение: строка на месте — «Незавершённые» и догонялка её ждут."""
    _ready(tmp_path)
    _seed_started(hours_ago=12 * 24)
    asyncio.run(db.get_reg_started_city(USER_ID, 24))

    async def row():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT event_city FROM reg_started WHERE telegram_id = ?", (USER_ID,)
            ) as cur:
                return await cur.fetchone()

    assert asyncio.run(row())[0] == "tyumen"


def test_no_ttl_keeps_previous_behaviour(tmp_path):
    """Без аргумента поведение прежнее — старые вызовы не меняют смысла."""
    _ready(tmp_path)
    _seed_started(hours_ago=12 * 24)
    assert asyncio.run(db.get_reg_started_city(USER_ID)) == "tyumen"
    assert asyncio.run(db.get_reg_started_city(USER_ID, 0)) == "tyumen"


def test_boundary_just_inside_window(tmp_path):
    _ready(tmp_path)
    _seed_started(hours_ago=23.5)
    assert asyncio.run(db.get_reg_started_city(USER_ID, 24)) == "tyumen"


def test_boundary_just_outside_window(tmp_path):
    _ready(tmp_path)
    _seed_started(hours_ago=24.5)
    assert asyncio.run(db.get_reg_started_city(USER_ID, 24)) is None


# ── трек ─────────────────────────────────────────────────────────────────────────────────────

def test_track_follows_the_same_window(tmp_path):
    """Трек живёт в той же строке и протухает по тому же правилу — иначе вернувшийся гость
    вечеринки остался бы в party-анкете навсегда."""
    _ready(tmp_path)
    _seed_started(hours_ago=12 * 24, track="party_overnight")
    assert asyncio.run(db.get_reg_started_track(USER_ID, 24)) is None
    assert asyncio.run(db.get_reg_started_track(USER_ID, 24 * 30)) == "party_overnight"


# ── настройка ────────────────────────────────────────────────────────────────────────────────

def test_ttl_setting_registered_with_sane_default():
    entry = SETTINGS_SCHEMA["reg_resume_ttl_hours"]
    assert entry["type"] == "int"
    assert entry["default"] == 24
    assert entry["group"] == "reg"
    assert entry["prompt"], "менеджеру нужно объяснение, что за часы"


def test_ttl_setting_parses_like_other_int_keys():
    assert _parse_setting("reg_resume_ttl_hours", "2") == 2
    assert _parse_setting("reg_resume_ttl_hours", None) == 24
    assert _parse_setting("reg_resume_ttl_hours", "мусор") == 24
