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
from domain.settings.ops import PREVIEW_SAMPLES
from handlers.access.admin_caps import required_capability
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
        await ph.phchk_save(_Cb("phchk_save", msg), state)
        held = await db.get_setting(KEY)
        st = await state.get_state()
        screen = msg.sent[-1]
        await ph.txtprev_save(_Cb("txtprev_save", msg), state)
        return held, st, screen, await db.get_setting(KEY), await state.get_state(), msg

    held, st, screen, saved, done, msg = _run(go())
    assert held is None
    assert st == EditSetting.waiting_for_text_confirm.state
    assert "Как прочитает делегат" in screen
    assert saved == "Вы записаны, ждём вас"
    assert done is None
    assert not any(s.startswith("Так увидит делегат:") for s in msg.sent)


def test_bot_unknown_fix_and_keep(tmp_path):
    _ready(tmp_path)

    async def go(button):
        await db.delete_setting(KEY)
        state = _state()
        await _start(state)
        msg = _Msg("Оплатите до {dedline}")
        await admin_settings.settings_edit_value(msg, state)
        first = msg.sent[0]
        btns = _buttons(msg.markups[0])
        await getattr(ph, button)(_Cb(button, msg), state)
        held = await db.get_setting(KEY)
        await ph.txtprev_save(_Cb("txtprev_save", msg), state)
        return first, btns, held, await db.get_setting(KEY)

    first, btns, held, saved = _run(go("phchk_fix"))
    assert "Не знаю {dedline} — может, {deadline}?" in first
    assert btns == ["phchk_fix", "phchk_save"]
    assert held is None
    assert saved == "Оплатите до {deadline}"
    _, _, held_keep, kept = _run(go("phchk_save"))
    assert held_keep is None
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
    fresh = "Ждём вас, оплата до {deadline}"

    async def go():
        state = _state()
        await _start(state)
        msg = _Msg(fresh)
        await admin_settings.settings_edit_value(msg, state)
        held = await db.get_setting(KEY)
        st = await state.get_state()
        screen = msg.sent[-1]
        await ph.txtprev_drop(_Cb("txtprev_drop", msg), state)
        kept = await db.get_setting(KEY)
        await admin_settings.settings_edit_value(msg, state)
        await ph.txtprev_save(_Cb("txtprev_save", msg), state)
        return msg, held, st, screen, kept, await db.get_setting(KEY)

    msg, held, st, screen, kept, saved = _run(go())
    assert held is None and kept is None
    assert st == EditSetting.waiting_for_text_confirm.state
    assert "Было:" in screen and "Как прочитает делегат:" in screen
    assert PREVIEW_SAMPLES["deadline"] in screen
    assert "{deadline}" not in screen
    assert not any("Сохранить всё равно" in s for s in msg.sent)
    assert saved == fresh
    assert not any(s.startswith("Так увидит делегат:") for s in msg.sent)


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
        msg = _Msg("Другой текст")
        await state.update_data(ph_ack="Подтверждённый текст без подстановок")
        await admin_settings.settings_edit_value(msg, state)
        shown = msg.sent[-1]
        ack = (await state.get_data()).get("ph_ack")
        held = await db.get_setting(KEY)
        await ph.txtprev_drop(_Cb("txtprev_drop", msg), state)
        later = _Msg("Совсем другой день, до {deadline}")
        await admin_settings.settings_edit_value(later, state)
        return held, ack, shown, later.sent[-1], await db.get_setting(KEY)

    held, ack, shown, later, saved = _run(go())
    assert ack is None and held is None and saved is None
    assert "Подтверждённый текст без подстановок" in shown
    assert "Совсем другой день" in later and PREVIEW_SAMPLES["deadline"] in later
    assert "Подтверждённый текст без подстановок" not in later


def test_dash_waits_and_int_saves_now(tmp_path):
    _ready(tmp_path)

    async def go():
        state = _state()
        await db.set_setting(KEY, "Свой текст")
        await _start(state)
        msg = _Msg("-")
        await admin_settings.settings_edit_value(msg, state)
        held = await db.get_setting(KEY)
        screen = msg.sent[-1]
        await ph.txtprev_wait(_Msg("ещё текст"), state)
        still = await db.get_setting(KEY)
        waited = await state.get_state()
        await ph.txtprev_save(_Cb("txtprev_save", msg), state)
        cleared = await db.get_setting(KEY)

        numbers = _state()
        await _start(numbers, "nudge_after_minutes")
        immediate = _Msg("90")
        await admin_settings.settings_edit_value(immediate, numbers)
        return held, screen, still, waited, cleared, await db.get_setting("nudge_after_minutes"), immediate.sent

    held, screen, still, waited, cleared, minutes, sent = _run(go())
    assert held == "Свой текст" and still == "Свой текст"
    assert waited == EditSetting.waiting_for_text_confirm.state
    assert "Как прочитает делегат:" in screen and PREVIEW_SAMPLES["deadline"] in screen
    assert cleared is None
    assert minutes == "90"
    assert not any("Как прочитает делегат" in s for s in sent)


def test_text_preview_callbacks_are_settings():
    assert required_capability(callback_data="txtprev_save") == "settings"
    assert required_capability(callback_data="txtprev_drop") == "settings"
    assert required_capability(raw_state="EditSetting:waiting_for_text_confirm") == "settings"
