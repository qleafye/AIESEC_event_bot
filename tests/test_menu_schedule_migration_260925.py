"""Одноразовая миграция `menu_schedule` -> `menu_program` (database.db.
`_migrate_menu_schedule_into_program`, PRAGMA user_version 2).

После слияния двух кнопок программы (D-29) у делегатов стенда и прода пропала кнопка программы
вообще: `menu_program` когда-то выключили (старая статичная «📅 Программа форума»), а включённый
`menu_schedule` (дефолт, строки нет) после слияния никто не читает.

БД — шаблон `fast_init_db`, затем гейт откатывается на `PRAGMA user_version = 1` (состояние БД
до этой миграции) и настоящий `init_db()` прогоняет её.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from keyboards.builders import get_main_menu_kb
from tests._dbtpl import fast_init_db

DELEGATE_ID = 260925101
PROGRAM_LABEL = "📅 Программа форума"


def _run(coro):
    return asyncio.run(coro)


async def _set_user_version(version: int) -> None:
    async with db._connect() as conn:
        await conn.execute(f"PRAGMA user_version = {version}")
        await conn.commit()


async def _user_version() -> int:
    async with db._connect() as conn:
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
    return row[0]


async def _all_settings() -> dict[str, str]:
    async with db._connect() as conn:
        async with conn.execute("SELECT key, value FROM bot_settings") as cur:
            return {k: v for k, v in await cur.fetchall()}


def _pre_migration_db(tmp_path, settings: dict[str, str]) -> None:
    config.DB_PATH = str(tmp_path / "menu_schedule_migration.db")
    fast_init_db()
    for key, value in settings.items():
        _run(db.set_setting(key, value))
    _run(_set_user_version(db._MSK_MIGRATION_USER_VERSION))


def _menu_labels() -> list[str]:
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    return [btn.text for row in kb.keyboard for btn in row]


def test_program_off_schedule_default_gets_button_back(tmp_path):
    _pre_migration_db(tmp_path, {"menu_program": "off"})
    _run(db.add_user({"telegram_id": DELEGATE_ID, "full_name": "Д", "registration_date": "2026-09-25"}))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    assert PROGRAM_LABEL not in _menu_labels()  # сама поломка со стенда

    _run(db.init_db())

    assert _run(db.get_setting("menu_program")) == "on"
    assert PROGRAM_LABEL in _menu_labels()
    assert _run(_user_version()) >= db._MENU_SCHEDULE_MIGRATION_USER_VERSION


def test_schedule_explicit_on_turns_program_on_and_orphan_is_deleted(tmp_path):
    _pre_migration_db(tmp_path, {"menu_program": "off", "menu_schedule": "on"})
    _run(db.init_db())
    settings = _run(_all_settings())
    assert settings["menu_program"] == "on"
    assert "menu_schedule" not in settings


def test_schedule_explicit_off_leaves_program_alone(tmp_path):
    _pre_migration_db(tmp_path, {"menu_program": "off", "menu_schedule": "off"})
    _run(db.init_db())
    settings = _run(_all_settings())
    assert settings["menu_program"] == "off"
    assert "menu_schedule" not in settings


def test_schedule_off_program_default_stays_unwritten(tmp_path):
    _pre_migration_db(tmp_path, {"menu_schedule": "off"})
    _run(db.init_db())
    settings = _run(_all_settings())
    assert "menu_program" not in settings
    assert "menu_schedule" not in settings


def test_per_city_variants(tmp_path):
    sep = db._CITY_OVERRIDE_SEP
    _pre_migration_db(tmp_path, {
        "menu_program": "off",
        "menu_schedule": "off",                 # глобально расписание выключено
        f"menu_schedule{sep}msk": "on",         # но у Москвы явно включено
        f"menu_program{sep}spb": "off",         # у СПб программа выключена, расписание — по глобальному off
        f"menu_program{sep}ekb": "off",
        f"menu_schedule{sep}ekb": "on",
    })
    _run(db.init_db())
    settings = _run(_all_settings())
    assert settings["menu_program"] == "off"
    assert settings[f"menu_program{sep}msk"] == "on"
    assert settings[f"menu_program{sep}spb"] == "off"
    assert settings[f"menu_program{sep}ekb"] == "on"
    assert not [k for k in settings if k.startswith("menu_schedule")]


def test_per_city_schedule_off_keeps_old_effective_program(tmp_path):
    """Глобально программа выключена, расписание по дефолту включено -> глобальная программа
    включается; у города с явно выключенным расписанием делегат должен видеть ТО ЖЕ, что до
    миграции (off), поэтому город получает свой вариант off."""
    sep = db._CITY_OVERRIDE_SEP
    _pre_migration_db(tmp_path, {"menu_program": "off", f"menu_schedule{sep}spb": "off"})
    _run(db.init_db())
    settings = _run(_all_settings())
    assert settings["menu_program"] == "on"
    assert settings[f"menu_program{sep}spb"] == "off"


def test_second_init_is_noop(tmp_path):
    _pre_migration_db(tmp_path, {"menu_program": "off"})
    _run(db.init_db())
    # менеджер выключил кнопку уже после миграции — повторный старт не должен её включить
    _run(db.set_setting("menu_program", "off"))
    _run(db.set_setting("menu_schedule", "on"))
    before = _run(_all_settings())
    _run(db.init_db())
    assert _run(_all_settings()) == before
    assert _run(_user_version()) >= db._MENU_SCHEDULE_MIGRATION_USER_VERSION


def test_fresh_db_writes_nothing(tmp_path):
    config.DB_PATH = str(tmp_path / "fresh.db")
    _run(db.init_db())
    settings = _run(_all_settings())
    assert "menu_program" not in settings
    assert _run(_user_version()) >= db._MENU_SCHEDULE_MIGRATION_USER_VERSION
