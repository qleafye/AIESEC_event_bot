"""Ночь 09.10: чистый /start менеджера (роль в админке) не запускает анкету делегата и не
пишет reg_started; кнопка «Всё-таки заполнить анкету» запускает обычный путь."""
import asyncio

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_manager_start as rms
from handlers.states import Registration
from tests._dbtpl import fast_init_db
from tests.test_reg_resume_draft import (
    FakeCommand,
    _FakeCallback,
    _KBCapturingMessage,
    _callback_datas,
    _new_state,
)

MGR = 810000001
ADMIN = 810000002
STRANGER = 810000003
HINT = "Вы в команде бота: админка открывается командой /admin."


def _ready(tmp_path, monkeypatch, name="mgr_start.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("event_season", "YL 26/2")
        await db.add_staff(MGR, "reg_manager", ADMIN)

    asyncio.run(go())


def _start(uid, args=None, state=None):
    async def go():
        st = state or _new_state(uid)
        msg = _KBCapturingMessage(uid, "u")
        await reg.cmd_start(msg, st, bot=object(), command=FakeCommand(args))
        return msg, st, await db.get_reg_started_by_id(uid)
    return asyncio.run(go())


def test_staff_role_gets_hint_without_reg_started(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    msg, _st, started = _start(MGR)
    assert [t for (t, _rm, _p) in msg.sent] == [HINT]
    assert _callback_datas(msg.sent[0][1]) == ["mgr_fill_form"]
    assert msg.sent[0][1].inline_keyboard[0][0].text == "📝 Всё-таки заполнить анкету"
    assert started is None


def test_config_admin_gets_hint(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    msg, _st, started = _start(ADMIN)
    assert msg.sent[0][0] == HINT
    assert started is None


def test_stranger_gets_ordinary_registration(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    msg, _st, started = _start(STRANGER)
    assert HINT not in [t for (t, _rm, _p) in msg.sent]
    assert started is not None


def test_manager_with_payload_goes_ordinary_path(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    msg, _st, started = _start(MGR, args="src_inst")
    assert HINT not in [t for (t, _rm, _p) in msg.sent]
    assert started is not None


def test_manager_with_draft_or_reg_started_not_interrupted(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)

    async def seed():
        await db.upsert_reg_draft(
            MGR, kind="new", participant_type="full", event_city=None, step="age",
            patch={}, source="bot",
        )
    asyncio.run(seed())
    msg, _st, _s = _start(MGR)
    assert HINT not in [t for (t, _rm, _p) in msg.sent]

    _ready(tmp_path, monkeypatch, name="mgr_start2.db")
    asyncio.run(db.mark_reg_started(MGR, "u"))
    msg, _st, _s = _start(MGR)
    assert HINT not in [t for (t, _rm, _p) in msg.sent]


def test_manager_with_fsm_state_not_interrupted(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    st = _new_state(MGR)
    asyncio.run(st.set_state(Registration.full_name))
    msg, _st, _s = _start(MGR, state=st)
    assert HINT not in [t for (t, _rm, _p) in msg.sent]


def test_manager_with_users_row_sees_ordinary_menu(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)

    async def seed():
        await db.add_user({
            "telegram_id": MGR, "username": "@m", "full_name": "Менеджер Тестовый",
            "event_city": None, "season": "YL 26/2", "participant_type": "full",
            "registration_date": "2026-09-18 12:00:00",
        })
    asyncio.run(seed())
    msg, _st, _s = _start(MGR)
    assert HINT not in [t for (t, _rm, _p) in msg.sent]


def test_fill_button_starts_registration_and_writes_reg_started(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)

    async def go():
        st = _new_state(MGR)
        cb = _FakeCallback("mgr_fill_form", MGR, "u")
        await rms.manager_fill_form(cb, st)
        return await db.get_reg_started_by_id(MGR), await st.get_state()

    started, state_name = asyncio.run(go())
    assert started is not None
    assert state_name is not None


def test_fill_button_denied_for_stranger(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)

    async def go():
        st = _new_state(STRANGER)
        cb = _FakeCallback("mgr_fill_form", STRANGER, "u")
        await rms.manager_fill_form(cb, st)
        return await db.get_reg_started_by_id(STRANGER)

    assert asyncio.run(go()) is None
