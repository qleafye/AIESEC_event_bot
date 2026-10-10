"""Подстановки {…} при сохранении текста: подтверждение в боте, превью, подсказка, Mini App.

pytest-asyncio недоступна — async через `asyncio.run()`; фейки сообщения/FSM — как в
tests/test_admin_session_feedback_settings_260924.py. БД — tmp_path.
"""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.settings.ops as settings_ops
from config import config
from database import db
from handlers.settings import admin_settings, admin_settings_placeholders as ph
from handlers.states import EditSetting
from tests._dbtpl import fast_init_db

ADMIN = 900938501
KEY = "session_enroll_confirmed_text"


def _run(coro):
    return asyncio.run(coro)


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, text=None, uid=ADMIN):
        self.text = text
        self.html_text = text
        self.entities = None
        self.from_user = _User(uid)
        self.sent = []
        self.markups = []

    def model_copy(self, update=None):
        c = _Msg(self.text)
        c.sent, c.markups = self.sent, self.markups
        for k, v in (update or {}).items():
            setattr(c, k, v)
        c.html_text = c.text
        return c

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.sent.append(text)
        self.markups.append(reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.sent.append(text)


class _Cb:
    def __init__(self, data, message):
        self.data = data
        self.from_user = _User(ADMIN)
        self.message = message

    async def answer(self, text=None, show_alert=False):
        pass


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "ph.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]


async def _start(state, key=KEY):
    await state.set_state(EditSetting.waiting_for_value)
    await state.update_data(setting_key=key)


def _buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_bot_missing_asks_confirm(tmp_path):
    _ready(tmp_path)

    async def go():
        state = _state()
        await _start(state)
        msg = _Msg("Вы записаны, ждём вас")
        await admin_settings.settings_edit_value(msg, state)
        return msg, await state.get_state(), await db.get_setting(KEY)

    msg, st, saved = _run(go())
    assert st == EditSetting.waiting_for_placeholder_confirm.state
    assert saved is None
    assert "Пропала {deadline}" in msg.sent[-1]
    assert {"phchk_save", "phchk_retry"} <= set(_buttons(msg.markups[-1]))


def test_bot_confirm_saves(tmp_path):
    _ready(tmp_path)

    async def go():
        state = _state()
        await _start(state)
        msg = _Msg("Вы записаны, ждём вас")
        await admin_settings.settings_edit_value(msg, state)
        cb = _Cb("phchk_save", msg)
        await ph.phchk_save(cb, state)
        return await db.get_setting(KEY), await state.get_state()

    saved, st = _run(go())
    assert saved == "Вы записаны, ждём вас"
    assert st is None


def test_bot_unknown_fix_and_keep(tmp_path):
    _ready(tmp_path)

    async def go(button):
        state = _state()
        await _start(state)
        msg = _Msg("Оплатите до {dedline}")
        await admin_settings.settings_edit_value(msg, state)
        first = msg.sent[0]
        btns = _buttons(msg.markups[0])
        cb = _Cb(button, msg)
        await getattr(ph, button)(cb, state)
        return first, btns, await db.get_setting(KEY)

    first, btns, saved = _run(go("phchk_fix"))
    assert "Не знаю {dedline} — может, {deadline}?" in first
    assert btns == ["phchk_fix", "phchk_save"]
    assert saved == "Оплатите до {deadline}"
    _, _, kept = _run(go("phchk_save"))
    assert kept == "Оплатите до {dedline}"


def test_bot_retry_returns_to_input(tmp_path):
    _ready(tmp_path)

    async def go():
        state = _state()
        await _start(state)
        msg = _Msg("без подстановки")
        await admin_settings.settings_edit_value(msg, state)
        await ph.phchk_retry(_Cb("phchk_retry", msg), state)
        return await state.get_state(), await db.get_setting(KEY)

    st, saved = _run(go())
    assert st == EditSetting.waiting_for_value.state and saved is None


def test_bot_clean_text_unchanged_and_preview(tmp_path):
    _ready(tmp_path)

    async def go():
        state = _state()
        await _start(state)
        msg = _Msg("Ждём вас, оплата до {deadline}")
        await admin_settings.settings_edit_value(msg, state)
        return msg, await db.get_setting(KEY), await state.get_state()

    msg, saved, st = _run(go())
    assert saved == "Ждём вас, оплата до {deadline}"
    assert st is None
    assert not any("Сохранить всё равно" in s for s in msg.sent)
    preview = [s for s in msg.sent if s.startswith("Так увидит делегат:")]
    assert preview and "{deadline}" not in preview[0]


def test_no_preview_without_placeholders(tmp_path):
    _ready(tmp_path)
    msg = _Msg()
    _run(ph.send_preview(msg, "event_name", "Просто текст"))
    assert msg.sent == []


def test_prompt_hint_shown():
    assert "Скобки бот заменит сам" in ph.hint_line(KEY)
    assert ph.hint_line("no_such_key") == ""


def test_miniapp_needs_confirm(tmp_path):
    _ready(tmp_path)
    kw = dict(visible_codes=[], selected_city=None, cities_on=False)

    async def go():
        a = await settings_ops.validate_batch_item(KEY, "Оплатите до {dedline}", **kw)
        b = await settings_ops.validate_batch_item(KEY, "Оплатите до {dedline}", **kw, confirmed=True)
        c = await settings_ops.validate_batch_item(KEY, "Оплатите до {deadline}", **kw)
        return a, b, c

    a, b, c = _run(go())
    assert a.needs_confirm and "Не знаю {dedline} — может, {deadline}?" in a.needs_confirm
    assert b.needs_confirm is None and b.value == "Оплатите до {dedline}"
    assert c.needs_confirm is None


def test_miniapp_help_contains_hint():
    spec = settings_ops.item_spec(KEY, raw=None, value="", is_default=True)
    assert "Скобки бот заменит сам" in (spec["help"] or "")


def test_ack_is_one_shot(tmp_path):
    """Подтверждённый текст расходуется один раз: в FSM он не остаётся и не подменит следующий."""
    _ready(tmp_path)

    async def go():
        state = _state()
        await _start(state)
        await state.update_data(ph_ack="Подтверждённый текст без подстановок")
        await admin_settings.settings_edit_value(_Msg("Другой текст"), state)
        return await db.get_setting(KEY), (await state.get_data()).get("ph_ack")

    saved, ack = _run(go())
    assert ack is None
    assert saved == "Подтверждённый текст без подстановок"
