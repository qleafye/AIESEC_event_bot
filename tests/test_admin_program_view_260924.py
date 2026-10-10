"""Пакет C, п.3 (D-29, «одна кнопка программы у делегата»): `handlers/forum/admin_program_view.py`
— циклический тумблер «Таблица/Фото» и его врезка в оба экрана-владельца («🗓 Программа
форума», «🎪 Форум: функции»).

Та же конвенция, что `tests/test_admin_program_260924.py`: Fake-объекты, БД — tmp_path
(`fast_init_db`), `asyncio.run()` (pytest-asyncio недоступна)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers.forum import admin_forum_functions as aff
from handlers.forum import admin_program, admin_program_view
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 900924401


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_admin_program_view.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, user_id=SUPERADMIN_ID):
        self.from_user = _FakeUser(user_id)
        self.text_edited = None
        self.edit_markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=SUPERADMIN_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── program_view_row: статус + подпись кнопки отражают текущее значение ────────────────────

def test_program_view_row_defaults_to_photo_without_sessions(tmp_path):
    _ready(tmp_path)
    status, button = _run(admin_program_view.program_view_row("msk", "program"))
    assert "Фото" in status
    assert "Фото" in button.text
    assert button.callback_data == "prog_view_toggle:msk:program"


def test_program_view_row_reflects_table_when_sessions_exist(tmp_path):
    _ready(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    status, button = _run(admin_program_view.program_view_row("msk", "hub"))
    assert "Таблица" in status
    assert button.callback_data == "prog_view_toggle:msk:hub"


# ── prog_view_toggle_go: цикл + запись + возврат на правильный экран ───────────────────────

def test_toggle_writes_global_key_when_cities_module_off(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("prog_view_toggle:msk:program")
    _run(admin_program_view.prog_view_toggle_go(callback))
    # дефолт без сессий — "photo" (resolve_program_view), цикл переключает на "table".
    assert _run(db.get_setting("program_miniapp_view")) == "table"
    assert callback.message.text_edited is not None  # экран перерисован


def test_toggle_writes_percity_key_when_cities_module_on(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    callback = _FakeCallback("prog_view_toggle:msk:program")
    _run(admin_program_view.prog_view_toggle_go(callback))
    from domain.cities import per_city_key
    assert _run(db.get_setting(per_city_key("program_miniapp_view", "msk"))) == "table"
    assert _run(db.get_setting("program_miniapp_view")) is None  # общий ключ не тронут


def test_toggle_cycles_back_to_table(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("program_miniapp_view", "photo"))
    callback = _FakeCallback("prog_view_toggle:msk:program")
    _run(admin_program_view.prog_view_toggle_go(callback))
    assert _run(db.get_setting("program_miniapp_view")) == "table"


def test_toggle_back_to_program_redraws_program_screen(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("prog_view_toggle:msk:program")
    _run(admin_program_view.prog_view_toggle_go(callback))
    assert "Программа форума" in callback.message.text_edited
    assert "prog_daynew:msk" in _cbs(callback.message.edit_markup)  # это экран admin_program


def test_toggle_back_to_hub_redraws_forum_functions_hub(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("prog_view_toggle:msk:hub")
    _run(admin_program_view.prog_view_toggle_go(callback))
    assert "Форум: функции" in callback.message.text_edited
    assert "forumfn_open:chk:msk" in _cbs(callback.message.edit_markup)  # это хаб admin_forum_functions


# ── Врезка в оба экрана-владельца ────────────────────────────────────────────────────────────

def test_program_screen_shows_view_toggle_button(tmp_path):
    _ready(tmp_path)
    text, kb = _run(admin_program.render_city_program_screen(SUPERADMIN_ID, "msk"))
    assert "prog_view_toggle:msk:program" in _cbs(kb)
    assert "Программа в приложении" in text


def test_forum_functions_hub_shows_view_toggle_button(tmp_path):
    _ready(tmp_path)
    text, kb = _run(aff._render_hub(SUPERADMIN_ID, "msk"))
    assert "prog_view_toggle:msk:hub" in _cbs(kb)
    assert "Программа в приложении" in text
