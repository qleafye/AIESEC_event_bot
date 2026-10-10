"""Пустой `university_options` больше не подставляет питерские ВУЗы: это свободный ввод."""
from __future__ import annotations

import asyncio

import domain.regform.engine as reg_engine
from config import config
from database import db
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _fresh(tmp_path, settings: dict[str, str]) -> None:
    config.DB_PATH = str(tmp_path / "uni.db")
    fast_init_db()
    for key, value in settings.items():
        _run(db.set_setting(key, value))


def test_config_has_no_spb_universities():
    assert not hasattr(config, "UNIVERSITIES")


def test_empty_list_in_list_mode_is_free_text(tmp_path):
    _fresh(tmp_path, {"reg_university_mode": "list"})
    assert _run(reg_engine.options("university")) == []


def test_manager_list_still_used(tmp_path):
    _fresh(tmp_path, {"reg_university_mode": "list", "university_options": "МГУ\nВШЭ"})
    assert _run(reg_engine.options("university")) == ["МГУ", "ВШЭ"]
