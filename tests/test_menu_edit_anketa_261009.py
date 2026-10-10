"""Кнопка главного меню «✏️ Изменить анкету» (владелец 09.10).

Видна делегату с поданной анкетой этого сезона, пока `reg_edit_policy.edit_gate` разрешает
правку; тап ведёт в тот же вход, что `/start edit`.
"""
import asyncio
import sqlite3

from config import config
from database import db
from handlers.reg import menu_edit_anketa as mod
from keyboards import builders
from keyboards.builders import MENU_BUTTONS, MENU_TEXTS, get_main_menu_kb
from tests._dbtpl import fast_init_db

UID = 961001
EDIT_TEXT = dict(MENU_BUTTONS)["menu_edit_anketa"]


def _ready(tmp_path, *, status="pending", registration_date="2026-10-01", season=None):
    config.DB_PATH = str(tmp_path / "test_menu_edit_anketa.db")
    fast_init_db()
    asyncio.run(db.add_user({
        "telegram_id": UID, "full_name": "Делегат", "registration_date": registration_date,
    }))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = ?, season = ? WHERE telegram_id = ?", (status, season, UID))
    conn.commit()
    conn.close()


def _texts(uid=UID):
    kb = asyncio.run(get_main_menu_kb(uid))
    return [b.text for row in kb.keyboard for b in row]


def test_caption_and_english():
    assert EDIT_TEXT == "✏️ Изменить анкету"
    assert MENU_TEXTS["menu_edit_anketa"] == {"✏️ Изменить анкету", "✏️ Edit application"}


def test_visible_to_submitted_delegate(tmp_path):
    _ready(tmp_path)
    assert EDIT_TEXT in _texts()


def test_hidden_when_policy_never(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("reg_edit_policy", "never"))
    assert EDIT_TEXT not in _texts()


def test_until_decision_only_before_approval(tmp_path):
    _ready(tmp_path, status="approved")
    asyncio.run(db.set_setting("reg_edit_policy", "until_decision"))
    assert EDIT_TEXT not in _texts()
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = 'pending' WHERE telegram_id = ?", (UID,))
    conn.commit()
    conn.close()
    assert EDIT_TEXT in _texts()


def test_hidden_for_rejected_unsubmitted_and_past_season(tmp_path):
    _ready(tmp_path, status="rejected")
    assert EDIT_TEXT not in _texts()
    _ready(tmp_path, registration_date="")
    assert EDIT_TEXT not in _texts()
    _ready(tmp_path, season="YL 26/1")
    asyncio.run(db.set_setting("event_season", "YL 26/2"))
    assert EDIT_TEXT not in _texts()
    assert EDIT_TEXT not in _texts(UID + 1)  # строки users нет вовсе


def test_menu_toggle_hides_button(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("menu_edit_anketa", "off"))
    assert EDIT_TEXT not in _texts()


def test_hidden_reason_names_the_setting(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(builders.menu_hidden_reason("menu_edit_anketa", None)) is None
    asyncio.run(db.set_setting("reg_edit_policy", "never"))
    reason = asyncio.run(builders.menu_hidden_reason("menu_edit_anketa", None))
    assert "«✏️ Правка анкеты делегатом»" in reason


def test_tap_goes_into_start_edit(monkeypatch):
    calls = []

    async def fake_cmd_start(message, state, bot, command=None):
        calls.append(command.args)

    class State:
        cleared = False

        async def clear(self):
            State.cleared = True

    from handlers import registration
    monkeypatch.setattr(registration, "cmd_start", fake_cmd_start)
    asyncio.run(mod.menu_edit_anketa(object(), State(), object()))
    assert calls == ["edit"] and State.cleared
