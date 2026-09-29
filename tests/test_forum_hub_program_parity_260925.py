"""Хаб «🎪 Форум: функции» ⇔ меню делегата: строка программы в хабе обязана говорить ровно то,
что делегат видит в меню. Раньше хаб смотрел только на «сессии заведены» и писал «✅ Вкл», когда
кнопка `menu_program` была выключена и делегат её не видел.

Комбинации: тумблер menu_program вкл/выкл × контент (сессии) есть/нет."""
from __future__ import annotations

import asyncio

import pytest

from core.cities import default_city_code
from config import config
from database import db
from handlers import admin_forum_functions as aff
from keyboards.builders import get_main_menu_kb
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 900925301
DELEGATE_ID = 900925302
PROGRAM_LABEL = "📅 Программа форума"
HUB_LINE = "📅 Кнопка «Программа» у делегата: "


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("toggle", ["on", "off"])
@pytest.mark.parametrize("has_sessions", [True, False])
def test_hub_program_status_matches_delegate_menu(tmp_path, toggle, has_sessions):
    config.DB_PATH = str(tmp_path / "hub_program_parity.db")
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    code = default_city_code()
    _run(db.set_setting("menu_program", toggle))
    if has_sessions:
        _run(db.create_program_session(code, "2026-10-30", "10:00", "11:00", "Открытие"))
    _run(db.add_user({"telegram_id": DELEGATE_ID, "full_name": "Д", "registration_date": "2026-09-25"}))

    kb = _run(get_main_menu_kb(DELEGATE_ID))
    delegate_sees = PROGRAM_LABEL in [btn.text for row in kb.keyboard for btn in row]

    text, _ = _run(aff._render_hub(SUPERADMIN_ID, code))
    line = next(ln for ln in text.splitlines() if ln.startswith(HUB_LINE))
    hub_says_on = line.startswith(HUB_LINE + "✅")

    assert hub_says_on == delegate_sees
    assert delegate_sees == (toggle == "on" and has_sessions)
    if toggle == "off":
        assert "выключена" in line
    elif not has_sessions:
        assert "нет ни фото, ни сессий" in line
