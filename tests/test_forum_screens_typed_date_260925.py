"""Экраны настроек форумных функций с НАСТОЯЩЕЙ датой форума.

`get_setting_typed_for_city("forum_date", ...)` отдаёт `datetime`, а не строку: три экрана
(«день форума», «отчёт дня», «опрос неявившихся») вызывали у неё `.strip()` и падали, как
только менеджер задавал дату. Тесты раньше подавали строку моком — здесь только реальный
путь `set_setting` → `get_setting_typed_for_city`, без моков."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers.forum import admin_forum_functions as aff
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "forum_screens_typed_date.db")
    fast_init_db()


def _screens():
    return (aff._forumdaymenu_cfg_text_kb, aff._day_report_cfg_text_kb, aff._noshow_poll_cfg_text_kb)


def test_screens_render_with_real_forum_date(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "03.10.2026"))
    for build in _screens():
        text, kb = _run(build(None))
        assert text and kb is not None, build.__name__


def test_screens_render_without_forum_date(tmp_path):
    _ready(tmp_path)
    for build in _screens():
        text, kb = _run(build(None))
        assert text and kb is not None, build.__name__
