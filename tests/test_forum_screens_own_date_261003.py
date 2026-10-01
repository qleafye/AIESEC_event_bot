"""Экраны хаба «🎪 Форум: функции» и программы форума читают дату форума ТОЛЬКО своего города
(`services.reject_rules.forum_date_for`, без отката на общую) — как форумные джобы. Иначе город
без своей даты, у которого задана лишь общая, видел «дата задана» и подсказки дней чужого
форума, а джобы ему при этом ничего не ставили.

async через `asyncio.run()` (конвенция проекта), БД — `tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio

from config import config
from database import db
import handlers.admin_forum_functions as aff
import handlers.admin_program as ap
from tests._dbtpl import fast_init_db

ADMIN_ID = 261003101
_NO_DATE = "«🗓 Дата начала форума» не задана"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "forum_screens_own_date.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("forum_date", "03.10.2026"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))


_SCREENS = (
    aff._forumdaymenu_cfg_text_kb,
    aff._day_report_cfg_text_kb,
    aff._noshow_poll_cfg_text_kb,
    aff._regional_noshow_cfg_text_kb,
)


def test_forum_function_screens_warn_city_with_only_common_date(tmp_path):
    _ready(tmp_path)
    for render in _SCREENS:
        text, _ = _run(render("msk"))
        assert _NO_DATE in text, render.__name__


def test_forum_function_screens_quiet_for_city_with_own_date(tmp_path):
    """Город со своей датой — предупреждения нет (и экран переноса неявившихся больше не
    падает на `.strip()` у даты-объекта)."""
    _ready(tmp_path)
    for render in _SCREENS:
        text, _ = _run(render("spb"))
        assert _NO_DATE not in text, render.__name__


def test_program_screen_suggests_days_only_from_own_date(tmp_path):
    _ready(tmp_path)
    _, kb_msk = _run(ap.render_city_program_screen(ADMIN_ID, "msk"))
    msk_days = [b.callback_data for row in kb_msk.inline_keyboard for b in row
                if (b.callback_data or "").startswith("prog_day:")]
    assert msk_days == []

    _, kb_spb = _run(ap.render_city_program_screen(ADMIN_ID, "spb"))
    spb_days = [b.callback_data for row in kb_spb.inline_keyboard for b in row
                if (b.callback_data or "").startswith("prog_day:")]
    assert spb_days == ["prog_day:spb:2026-10-03", "prog_day:spb:2026-10-04", "prog_day:spb:2026-10-05"]
