"""Квик 260916: подпись reply-кнопки меню в поле значения настройки.

Стенд: в event_place_address оказалось «🔄 Пройти регистрацию заново» — подпись кнопки, а не
адрес площадки. Механизм тот же, что у команды «/start» 20.08 (см.
tests/test_settings_command_guard_260820.py): менеджер открыл ввод текстовой настройки (FSM
ждёт текст), под сообщением всё ещё висит главное меню (settings_edit_start шлёт только инлайн,
свою reply-клавиатуру никогда не пересылает) — привычный тап по кнопке уходит сюда как обычный
текст и раньше молча ложился в bot_settings.

Два исхода:
  - подпись из MENU_TEXTS (у неё есть настоящий обработчик в user_actions.router) — значение не
    сохраняется, FSM чистится, хендлер отдаёт событие дальше через SkipHandler (admin.router
    подключён первым в main.py) — тап делает то, что и должен.
  - подпись без обработчика ниже по роутерам (инлайн-кнопка «Пройти регистрацию заново» из
    ADMIN_MISC_BUTTON_TEXTS) — значение не сохраняется, менеджеру объясняется почему.

pytest-asyncio недоступен — async через asyncio.run(), фейки скопированы по конвенции сьюта
(tests/test_settings_command_guard_260820.py).
"""
import asyncio

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from config import config
from database import db
from handlers import admin_settings
from keyboards.builders import MENU_TEXTS, ADMIN_REREG_BUTTON_TEXT

ADMIN_ID = 900916


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_menu_button_guard.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = None


class _FakeSettingsMessage:
    def __init__(self, uid=ADMIN_ID, text=""):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.html_text = text
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


class _FakeFSMState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self._state = "EditSetting:waiting_for_value"

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self._state = state

    async def get_state(self):
        return self._state

    async def clear(self):
        self._data = {}
        self._state = None


def _edit(tmp_path, key, text, preset=None):
    """Same shape as test_settings_command_guard_260820.py::_edit, but the menu-button branch
    raises SkipHandler on purpose (aiogram's own "let the next router handle it" signal) — the
    caller decides whether to expect it."""
    _ready(tmp_path)
    message = _FakeSettingsMessage(text=text)
    state = _FakeFSMState({"setting_key": key})

    async def go():
        if preset is not None:
            await db.set_setting(key, preset)
        raised = False
        try:
            await admin_settings.settings_edit_value(message, state)
        except SkipHandler:
            raised = True
        return raised, await db.get_setting(key)

    raised, saved = asyncio.run(go())
    return message, state, raised, saved


TEXT_KEY = "event_place_address"  # ровно тот ключ, что пострадал на стенде


def _menu_caption(menu_key: str) -> str:
    """One RU caption out of MENU_TEXTS[menu_key] — a frozenset of {ru, en} (or a single
    element when no EN translation exists)."""
    return sorted(MENU_TEXTS[menu_key])[0]


# ── подпись меню с обработчиком дальше по роутерам ──────────────────────────────────────────

def test_menu_button_text_not_saved_as_value(tmp_path):
    value = _menu_caption("menu_coins")
    _message, _state, raised, saved = _edit(tmp_path, TEXT_KEY, value)
    assert raised, "хендлер обязан отдать событие дальше через SkipHandler"
    assert saved is None, "подпись кнопки меню записана в настройку"


def test_menu_button_text_does_not_wipe_existing_value(tmp_path):
    """Главный ущерб стенда: адрес площадки был бы затёрт одним тапом по меню."""
    value = _menu_caption("menu_coins")
    _message, _state, raised, saved = _edit(tmp_path, TEXT_KEY, value, preset="Москва, Тверская 1")
    assert raised
    assert saved == "Москва, Тверская 1"


def test_menu_button_text_clears_fsm_state(tmp_path):
    """FSM обязана очиститься — иначе следующее сообщение админа снова уйдёт в настройку,
    хотя он уже переключился на другой раздел меню."""
    value = _menu_caption("menu_coins")
    _message, state, raised, _saved = _edit(tmp_path, TEXT_KEY, value)
    assert raised
    assert asyncio.run(state.get_state()) is None


def test_menu_button_english_caption_also_guarded(tmp_path):
    """EN-перевод подписи (i18n_ui_en.MENU_EN) — тот же MENU_TEXTS, тот же результат."""
    en_captions = MENU_TEXTS["menu_program"] - {_menu_caption("menu_program")}
    # There should be at most one other member (the EN translation, if it exists and differs).
    if not en_captions:
        pytest.skip("для menu_program нет отдельного EN-перевода в текущей i18n-таблице")
    value = sorted(en_captions)[0]
    _message, _state, raised, saved = _edit(tmp_path, TEXT_KEY, value)
    assert raised
    assert saved is None


def test_every_menu_caption_is_guarded(tmp_path):
    """Список подписей не хардкодится в тесте отдельно — перебираем ровно то, что строит
    клавиатуру (keyboards.builders.MENU_TEXTS), включая payment."""
    _ready(tmp_path)
    for menu_key, captions in MENU_TEXTS.items():
        for caption in captions:
            message = _FakeSettingsMessage(text=caption)
            state = _FakeFSMState({"setting_key": TEXT_KEY})

            async def go():
                try:
                    await admin_settings.settings_edit_value(message, state)
                    return False
                except SkipHandler:
                    return True

            assert asyncio.run(go()), f"{menu_key}: {caption!r} не поймана как подпись меню"
            assert asyncio.run(db.get_setting(TEXT_KEY)) is None, caption


# ── подпись без обработчика дальше по роутерам ──────────────────────────────────────────────

def test_admin_misc_button_text_not_saved(tmp_path):
    _message, _state, raised, saved = _edit(tmp_path, TEXT_KEY, ADMIN_REREG_BUTTON_TEXT)
    assert not raised, "инлайн-подпись без текстового обработчика не должна улетать дальше"
    assert saved is None


def test_admin_misc_button_text_explains_and_keeps_state(tmp_path):
    message, state, raised, _saved = _edit(tmp_path, TEXT_KEY, ADMIN_REREG_BUTTON_TEXT)
    assert not raised
    assert asyncio.run(state.get_state()) == "EditSetting:waiting_for_value"
    assert message.answers, "менеджеру должно прийти объяснение"
    hint = message.answers[-1]
    assert "кнопка меню" in hint.lower()
    assert "📫 Адрес" in hint, "подсказка называет настройку по её человеческой подписи"


def test_admin_misc_button_text_does_not_wipe_existing_value(tmp_path):
    _message, _state, raised, saved = _edit(
        tmp_path, TEXT_KEY, ADMIN_REREG_BUTTON_TEXT, preset="Москва, Тверская 1"
    )
    assert not raised
    assert saved == "Москва, Тверская 1"


# ── обычный текст по-прежнему сохраняется ───────────────────────────────────────────────────

def test_normal_value_still_saved(tmp_path):
    value = "Москва, Краснопресненская наб., 14"
    _message, _state, raised, saved = _edit(tmp_path, TEXT_KEY, value)
    assert not raised
    assert saved == value
