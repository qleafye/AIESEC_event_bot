"""Подписи кнопок главного меню делегата — настройки `menu_<x>_label` (per_city).

Проверяем: реестр (дефолт = прежний литерал, подсказка с примером), клавиатуру делегата
(своя подпись, пустое = дефолт, конференция), фильтр `MenuButton` (узнаёт кнопку по любой
актуальной подписи: настроенной общей/городской, дефолту, английской, старой), проверку ввода
(чужая подпись не сохраняется) и вход в правку из чата («🔘 Кнопки меню» → «✏️ Подписи»).
"""
import asyncio

import pytest

import cities
from config import config
from database import db
from i18n_ui_en import MENU_EN
from keyboards import menu_dynamic
from keyboards.builders import MENU_BUTTONS, get_main_menu_kb
from keyboards.menu_dynamic import (
    CONFERENCE_MENU_LABELS,
    MENU_LABEL_KEYS,
    MenuButton,
    caption_for,
    menu_key_for_text,
)
from settings_schema import SETTINGS_SCHEMA
from settings_validation import validate_setting_value
from tests._dbtpl import fast_init_db

ADMIN_ID = 961009


@pytest.fixture
def ready(tmp_path):
    config.DB_PATH = str(tmp_path / "menu_labels.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


class _Msg:
    def __init__(self, text):
        self.text = text


def _captions(markup) -> list[str]:
    return [b.text for row in markup.keyboard for b in row]


# ── реестр ────────────────────────────────────────────────────────────────────────────────

def test_every_menu_button_has_label_setting():
    for key, _text in MENU_BUTTONS:
        assert key in MENU_LABEL_KEYS, key
    assert "menu_payment" in MENU_LABEL_KEYS


def test_menu_buttons_literals_match_registry_defaults():
    for key, text in MENU_BUTTONS:
        assert SETTINGS_SCHEMA[MENU_LABEL_KEYS[key]]["default"] == text, key
    assert SETTINGS_SCHEMA["menu_payment_label"]["default"] == "💳 Оплата"


def test_label_keys_are_human_per_city_texts():
    for label_key in MENU_LABEL_KEYS.values():
        entry = SETTINGS_SCHEMA[label_key]
        assert entry["type"] == "text", label_key
        assert entry["per_city"] is True, label_key
        # делегатский текст в корпусе перевода; запись на сессии и тест — свои группы старше
        assert entry["group"] in ("menu_labels", "event"), label_key
        assert "Пример:" in entry["prompt"], label_key
        assert "menu_" not in entry["label"], label_key  # человеку — подпись, не код


# ── клавиатура делегата ─────────────────────────────────────────────────────────────────

def test_menu_uses_custom_label_and_empty_means_default(ready):
    async def go():
        before = _captions(await get_main_menu_kb())
        await db.set_setting("menu_speakers_label", "🗣 Кто выступает")
        custom = _captions(await get_main_menu_kb())
        await db.set_setting("menu_speakers_label", "   ")
        blank = _captions(await get_main_menu_kb())
        return before, custom, blank

    before, custom, blank = asyncio.run(go())
    assert "🗣 Спикеры" in before
    assert "🗣 Кто выступает" in custom and "🗣 Спикеры" not in custom
    assert "🗣 Спикеры" in blank


def test_conference_default_and_custom(ready):
    async def go():
        await db.set_setting("event_type", "conference")
        conf_default = await caption_for("menu_info", None, conference=True)
        await db.set_setting("menu_info_label", "ℹ️ О съезде")
        conf_custom = await caption_for("menu_info", None, conference=True)
        forum_custom = await caption_for("menu_info", None)
        return conf_default, conf_custom, forum_custom

    conf_default, conf_custom, forum_custom = asyncio.run(go())
    assert conf_default == CONFERENCE_MENU_LABELS["menu_info"]
    assert conf_custom == "ℹ️ О съезде"
    assert forum_custom == "ℹ️ О съезде"


def test_conference_menu_keeps_conference_caption(ready):
    async def go():
        await db.set_setting("event_type", "conference")
        return _captions(await get_main_menu_kb())

    captions = asyncio.run(go())
    assert "ℹ️ О конференции" in captions
    assert "ℹ️ Информация о форуме" not in captions


def test_english_default_is_handwritten(ready):
    caption = asyncio.run(_en_default())
    assert caption == MENU_EN["🪙 Мои монеты"]


async def _en_default():
    return await caption_for("menu_coins", None, "en", tr_map={})


def test_per_city_label(ready):
    codes = cities.city_codes()
    if len(codes) < 2:
        pytest.skip("в реестре меньше двух городов")
    mine, other = codes[0], codes[1]

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(cities.per_city_key("menu_contacts_label", mine), "📞 Оргкомитет")
        return await caption_for("menu_contacts", mine), await caption_for("menu_contacts", other)

    assert asyncio.run(go()) == ("📞 Оргкомитет", "📞 Контакты")


# ── узнавание кнопки ────────────────────────────────────────────────────────────────────

def test_filter_matches_every_actual_caption(ready):
    codes = cities.city_codes()

    async def go():
        await db.set_setting("menu_coins_label", "🪙 Мои баллы")
        if codes:
            await db.set_setting(cities.per_city_key("menu_coins_label", codes[0]), "🪙 Копилка")
        f = MenuButton("menu_coins")
        return {
            "custom": await f(_Msg("🪙 Мои баллы")),
            "city": await f(_Msg("🪙 Копилка")) if codes else True,
            "default": await f(_Msg("🪙 Мои монеты")),
            "en": await f(_Msg(MENU_EN["🪙 Мои монеты"])),
            "foreign": await f(_Msg("просто текст")),
            "other_button": await f(_Msg("🎯 Задания")),
            "other_filter": await MenuButton("menu_game_tasks")(_Msg("🪙 Мои баллы")),
        }

    r = asyncio.run(go())
    assert r["custom"] and r["city"] and r["default"] and r["en"]
    assert not r["foreign"] and not r["other_button"] and not r["other_filter"]


def test_filter_matches_legacy_and_conference_captions(ready):
    async def go():
        f = MenuButton("menu_program")
        return (await f(_Msg("🗓 Программа")), await f(_Msg("📅 Программа конференции")),
                await f(_Msg(MENU_EN["📅 Программа конференции"])))

    assert all(asyncio.run(go()))


def test_one_settings_read_per_update(ready, monkeypatch):
    calls = []
    real = menu_dynamic._configured_text_map

    async def counting():
        calls.append(1)
        return await real()

    monkeypatch.setattr(menu_dynamic, "_configured_text_map", counting)

    async def go():
        msg = _Msg("не кнопка")
        for key in MENU_LABEL_KEYS:
            assert not await MenuButton(key)(msg)
        # статичная подпись — без чтения настроек вовсе
        assert await menu_key_for_text("🪙 Мои монеты", _Msg("🪙 Мои монеты")) == "menu_coins"

    asyncio.run(go())
    assert len(calls) == 1


def test_unknown_menu_key_rejected():
    with pytest.raises(KeyError):
        MenuButton("menu_nope")


# ── проверка ввода ──────────────────────────────────────────────────────────────────────

def test_label_cannot_take_other_buttons_caption():
    value, error = validate_setting_value("menu_coins_label", "🎯 Задания")
    assert value is None and "другой кнопки" in error
    value, error = validate_setting_value("menu_coins_label", "🪙 Мои монеты")
    assert error is None
    value, error = validate_setting_value("menu_info_label", "ℹ️ О конференции")
    assert error is None  # своя конференционная подпись — не чужая


# ── правка из чата ──────────────────────────────────────────────────────────────────────

def test_new_labels_have_own_group_in_translation_corpus():
    from services import i18n_sources

    assert "menu_labels" in i18n_sources.DELEGATE_GROUPS
    keys = i18n_sources.delegate_registry_keys()
    for menu_key, label_key in MENU_LABEL_KEYS.items():
        assert label_key in keys, label_key
        if menu_key not in ("menu_session_enroll", "menu_quiz"):
            assert SETTINGS_SCHEMA[label_key]["group"] == "menu_labels", label_key


def test_labels_group_in_chat_settings():
    from handlers import admin_settings
    from handlers.admin_sections import SECTIONS, section_of

    keys = admin_settings._settings_group_keys("menu_labels")
    assert keys == list(MENU_LABEL_KEYS.values())
    assert section_of("settings_group:menu_labels") == "event"
    event_rows = next(rows for tok, _l, rows in SECTIONS if tok == "event")
    assert ("group", "menu_labels") in event_rows
    for key in keys:
        assert admin_settings._group_of_setting_key(key) == "menu_labels"


def test_menu_buttons_screen_links_to_labels(ready):
    from handlers import admin_reg_config

    kb = asyncio.run(admin_reg_config.build_menu_keyboard(ADMIN_ID))
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "settings_group:menu_labels" in datas


def test_menu_buttons_screen_shows_custom_caption(ready):
    from handlers import admin_reg_config

    async def go():
        await db.set_setting("menu_faq_label", "❓ Ответы")
        return await admin_reg_config.render_menu_text(ADMIN_ID)

    text = asyncio.run(go())
    assert "❓ Ответы" in text
