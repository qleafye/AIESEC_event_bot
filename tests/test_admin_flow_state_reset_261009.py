"""Вход в админку (/admin, «⬅ Панель») снимает любое брошенное админское ожидание ввода
(`handlers.states.clear_admin_flow_state`), но не трогает анкету и прочие ответы делегата."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from handlers import states
from handlers.admin import cmd_admin_help
from handlers.cities.admin_cities import admin_menu_root
from tests._dbtpl import fast_init_db


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=5, user_id=5))


class _Msg:
    def __init__(self):
        self.from_user = SimpleNamespace(id=5)

    async def answer(self, *a, **kw):
        return None

    async def edit_text(self, *a, **kw):
        return None


class _Cb:
    def __init__(self):
        self.from_user = SimpleNamespace(id=5)
        self.message = _Msg()
        self.data = "admin_menu"

    async def answer(self, *a, **kw):
        return None


def _delegate_states():
    from handlers.reg.onsite_reg import OnsiteReg
    from handlers.reg.reg_types_composite import _CompositeChat
    from handlers.reg.reg_types_lookup import _LookupChat

    groups = [states.Registration, _CompositeChat, _LookupChat, OnsiteReg, states.Question,
              states.GameSubmit, states.SosReport, states.SessionFeedbackComment,
              states.ForumNoshowPollOther]
    return groups


def test_whitelist_names_match_real_state_groups():
    names = {g.__full_group_name__ for g in _delegate_states()}
    assert names == states.DELEGATE_STATE_GROUPS


ADMIN_STATES = [
    states.EditSetting.waiting_for_value, states.BotAvatar.photo, states.CoinsTransfer.link,
    states.Broadcast.message, states.Approval.reason,
]


@pytest.mark.parametrize("admin_state", ADMIN_STATES, ids=lambda s: s.state)
def test_admin_and_panel_drop_any_admin_state(tmp_path, admin_state):
    config.DB_PATH = str(tmp_path / "flow.db")
    fast_init_db()

    async def go():
        state = _state()
        await state.set_state(admin_state)
        await state.update_data(pending="x")
        await cmd_admin_help(_Msg(), state)
        assert await state.get_state() is None
        assert await state.get_data() == {}

        await state.set_state(admin_state)
        await admin_menu_root(_Cb(), state)
        assert await state.get_state() is None

    asyncio.run(go())


def test_delegate_states_survive_admin_and_panel(tmp_path):
    config.DB_PATH = str(tmp_path / "flow2.db")
    fast_init_db()

    async def go():
        for group in _delegate_states():
            st = next(iter(group.__states__))
            state = _state()
            await state.set_state(st)
            await state.update_data(answers={"name": "Аня"})
            await cmd_admin_help(_Msg(), state)
            await admin_menu_root(_Cb(), state)
            assert await state.get_state() == st.state, group.__full_group_name__
            assert await state.get_data() == {"answers": {"name": "Аня"}}

    asyncio.run(go())


def test_no_state_is_a_noop():
    async def go():
        state = _state()
        assert await states.clear_admin_flow_state(state) is False
        assert await state.get_state() is None

    asyncio.run(go())
