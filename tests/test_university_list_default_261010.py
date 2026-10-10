"""Пустой `university_options` больше не подставляет питерские ВУЗы: это свободный ввод.
Стеки, где выбор из списка уже включён, получают прежний список явно одной миграцией."""
from __future__ import annotations

import asyncio

import reg_engine
import reg_options
from config import config
from database import db
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _fresh(tmp_path, settings: dict[str, str], user_version: int = 4) -> None:
    config.DB_PATH = str(tmp_path / "uni.db")
    fast_init_db()
    for key, value in settings.items():
        _run(db.set_setting(key, value))

    async def _uv():
        async with db._connect() as conn:
            await conn.execute(f"PRAGMA user_version = {user_version}")
            await conn.commit()

    _run(_uv())


def test_config_has_no_spb_universities():
    assert not hasattr(config, "UNIVERSITIES")


def test_empty_list_in_list_mode_is_free_text(tmp_path):
    _fresh(tmp_path, {"reg_university_mode": "list"}, user_version=5)
    assert _run(reg_engine.options("university")) == []


def test_manager_list_still_used(tmp_path):
    _fresh(tmp_path, {"reg_university_mode": "list", "university_options": "МГУ\nВШЭ"}, user_version=5)
    assert _run(reg_engine.options("university")) == ["МГУ", "ВШЭ"]


def test_migration_freezes_list_for_list_mode_stack(tmp_path):
    _fresh(tmp_path, {"reg_university_mode": "list"})
    _run(db.init_db())
    assert _run(reg_engine.options("university")) == reg_options.LEGACY_SPB_UNIVERSITIES


def test_migration_keeps_manager_list_and_text_mode(tmp_path):
    _fresh(tmp_path, {"reg_university_mode": "list", "university_options": "МГУ"})
    _run(db.init_db())
    assert _run(reg_engine.options("university")) == ["МГУ"]
    _fresh(tmp_path, {"reg_university_mode": "text"})
    _run(db.init_db())
    assert _run(db.get_setting("university_options")) in (None, "")


def test_migration_runs_once(tmp_path):
    _fresh(tmp_path, {"reg_university_mode": "list"})
    _run(db.init_db())
    _run(db.set_setting("university_options", ""))
    _run(db.init_db())
    assert _run(reg_engine.options("university")) == []
