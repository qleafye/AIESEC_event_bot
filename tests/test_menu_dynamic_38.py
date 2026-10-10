"""Динамические подписи кнопок меню (запись на сессии, тест): чтение, фильтр, сторож ввода."""
import asyncio

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

import domain.cities as cities
from config import config
from database import db
from handlers.settings import admin_settings
from handlers.access.admin_caps import required_capability
from keyboards.menu_dynamic import (
    DynamicMenuText,
    all_dynamic_captions,
    caption_for,
    is_dynamic_menu_text,
)
from tests._dbtpl import fast_init_db
from tests.test_settings_menu_button_guard_260916 import (
    ADMIN_ID,
    _FakeFSMState,
    _FakeSettingsMessage,
)

DEFAULT = "📅 Запись на сессии"
CUSTOM = "📅 Выбор сессий"
KEY = "session_enroll_menu_label"
MENU = "menu_session_enroll"


@pytest.fixture
def ready(tmp_path):
    config.DB_PATH = str(tmp_path / "menu_dynamic.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _city_per_city_on():
    await db.set_setting("event_city_enabled", "on")


def _first_city() -> str:
    return cities.city_codes()[0]


class _Msg:
    def __init__(self, text):
        self.text = text


def test_caption_default_and_custom(ready):
    async def go():
        await _city_per_city_on()
        code = _first_city()
        other = cities.city_codes()[1]
        assert await caption_for(MENU, code) == DEFAULT
        await db.set_setting(cities.per_city_key(KEY, code), CUSTOM)
        return await caption_for(MENU, code), await caption_for(MENU, other)

    try:
        mine, theirs = asyncio.run(go())
    except IndexError:
        pytest.skip("в реестре меньше двух городов")
    assert mine == CUSTOM
    assert theirs == DEFAULT


def test_caption_en_never_empty(ready):
    caption = asyncio.run(caption_for(MENU, None, "en"))
    assert caption


def test_dynamic_filter_matches(ready):
    async def go():
        await _city_per_city_on()
        # делегат без города видит подписи города по умолчанию — как в get_main_menu_kb
        await db.set_setting(cities.per_city_key(KEY, cities.default_city_code()), CUSTOM)
        f = DynamicMenuText(MENU)
        return (
            await f(_Msg(CUSTOM)),
            await f(_Msg(DEFAULT)),
            await f(_Msg("что-то чужое")),
            await DynamicMenuText("menu_quiz")(_Msg(CUSTOM)),
        )

    custom, default, foreign, other_menu = asyncio.run(go())
    assert custom and default
    assert not foreign
    assert not other_menu


def test_is_dynamic_menu_text(ready):
    async def go():
        await db.set_setting(KEY, CUSTOM)
        return await is_dynamic_menu_text(CUSTOM), await is_dynamic_menu_text("нет такой"), \
            await all_dynamic_captions(MENU)

    yes, no, captions = asyncio.run(go())
    assert yes and not no
    assert {DEFAULT, CUSTOM} <= captions


def test_settings_guard_skips_dynamic_caption(ready):
    message = _FakeSettingsMessage(text=CUSTOM)
    state = _FakeFSMState({"setting_key": "event_place_address"})

    async def go():
        await db.set_setting(KEY, CUSTOM)
        raised = False
        try:
            await admin_settings.settings_edit_value(message, state)
        except SkipHandler:
            raised = True
        return raised, await db.get_setting("event_place_address")

    raised, saved = asyncio.run(go())
    assert raised and saved is None


def test_new_states_have_caps():
    for raw in ("ProgramTrackEdit:name", "ProgramCompetencyEdit:name", "ProgramEnrollLimit:value",
                "QuizEdit:value", "QuizImport:waiting_file"):
        assert required_capability(raw_state=raw) == "settings", raw
    assert required_capability(callback_data="enrf_start:in") == "broadcast"
    assert required_capability(callback_data="bcseason_all") == "broadcast"
    assert required_capability(callback_data="phchk_save") == "settings"
    assert required_capability(raw_state="EditSetting:waiting_for_placeholder_confirm") == "settings"
