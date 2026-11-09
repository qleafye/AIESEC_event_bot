"""Три двери одного окна дней форума: SOS, меню дня и фильтр «не пришли»."""
from __future__ import annotations

import asyncio
from datetime import date

from config import config
from database import db
from services.forum.forum_day_menu import _forum_window_dates
from services.forum.forum_days import forum_window
from services.sos import sos_active_window
from tests._dbtpl import fast_init_db


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "forum_window.db")
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _three(city):
    return await forum_window(city), await sos_active_window(city), await _forum_window_dates(city)


def test_three_forum_windows_agree_including_bad_days(tmp_path):
    """На одних настройках окно одно, в том числе когда число дней кривое."""
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "16.10.2026"))

    assert _run(_three(None)) == (
        (date(2026, 10, 16), date(2026, 10, 17)),
    ) * 3  # дефолт длины — 2

    _run(db.set_setting("sos_active_days", "3"))
    assert _run(_three(None)) == ((date(2026, 10, 16), date(2026, 10, 18)),) * 3

    # Реестр нечисло и «не больше нуля» читает как дефолт 2. Три функции не расходятся.
    for raw in ("0", "-4", "нет"):
        _run(db.set_setting("sos_active_days", raw))
        got = _run(_three(None))
        assert got[0] == got[1] == got[2] == (date(2026, 10, 16), date(2026, 10, 17)), raw

    _run(db.set_setting("forum_date", "не дата"))
    assert _run(_three(None)) == (None, None, None)
    _run(db.set_setting("forum_date", ""))
    assert _run(_three(None)) == (None, None, None)

    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    _run(db.set_setting("sos_active_days__city__spb", "1"))
    _run(db.set_setting("forum_date__city__msk", "30.10.2026"))
    _run(db.set_setting("sos_active_days__city__msk", "нет"))
    assert _run(_three("spb")) == ((date(2026, 10, 3), date(2026, 10, 3)),) * 3
    assert _run(_three("msk")) == ((date(2026, 10, 30), date(2026, 10, 31)),) * 3
