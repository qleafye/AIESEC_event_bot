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
    DELEGATE_STATE_GROUPS,
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
    assert caption == MENU_EN["🪙 Мои баллы"]


async def _en_default():
    return await caption_for("menu_coins", None, "en", en_map={})


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
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("menu_coins_label", "🪙 Мой счёт")
        if codes:  # делегат без города — город по умолчанию, как в get_main_menu_kb
            await db.set_setting(cities.per_city_key("menu_coins_label", cities.default_city_code()), "🪙 Копилка")
        f = MenuButton("menu_coins")
        return {
            "custom": await f(_Msg("🪙 Мой счёт")),
            "city": await f(_Msg("🪙 Копилка")) if codes else True,
            "default": await f(_Msg("🪙 Мои баллы")),
            "en": await f(_Msg(MENU_EN["🪙 Мои баллы"])),
            # прежний дефолт с закэшированных клавиатур (до 10.10 — «монеты»)
            "legacy": await f(_Msg("🪙 Мои монеты")),
            "legacy_en": await f(_Msg("🪙 My coins")),
            "foreign": await f(_Msg("просто текст")),
            "other_button": await f(_Msg("🎯 Задания")),
            "other_filter": await MenuButton("menu_game_tasks")(_Msg("🪙 Мои баллы")),
        }

    r = asyncio.run(go())
    assert r["custom"] and r["city"] and r["default"] and r["en"]
    assert r["legacy"] and r["legacy_en"]
    assert not r["foreign"] and not r["other_button"] and not r["other_filter"]


def test_filter_matches_legacy_and_conference_captions(ready):
    async def go():
        f = MenuButton("menu_program")
        return (await f(_Msg("🗓 Программа")), await f(_Msg("📅 Программа конференции")),
                await f(_Msg(MENU_EN["📅 Программа конференции"])))

    assert all(asyncio.run(go()))


def test_unknown_menu_key_rejected():
    with pytest.raises(KeyError):
        MenuButton("menu_nope")


# ── проверка ввода ──────────────────────────────────────────────────────────────────────

def test_label_cannot_take_other_buttons_caption():
    value, error = validate_setting_value("menu_coins_label", "🎯 Задания")
    assert value is None and "уже у кнопки «🎯 Задания»" in error
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


# ── ревью: город делегата, общий кэш, состояния, прежние подписи ──────────────────────────

class _User:
    def __init__(self, uid):
        self.id = uid


class _DelegateMsg:
    def __init__(self, text, uid=555):
        self.text = text
        self.from_user = _User(uid)


def _two_cities():
    codes = cities.city_codes()
    if len(codes) < 2:
        pytest.skip("в реестре меньше двух городов")
    return codes[0], codes[1]


def _as_delegate(monkeypatch, city=None, lang=None):
    async def fake_get_user(_uid):
        return {"event_city": city, "lang": lang}
    monkeypatch.setattr(menu_dynamic, "get_user", fake_get_user)


def test_same_custom_label_on_two_buttons_is_refused(ready):
    """Своя подпись другой кнопки — общая или любого города — занята (бот и приложение)."""
    from settings_ops import cross_setting_error
    a, _b = _two_cities()

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("menu_info_label", "🧭 Гид")
        await db.set_setting(cities.per_city_key("menu_faq_label", a), "❓ Ответы")
        return (
            await cross_setting_error("menu_program_label", "🧭 Гид"),
            await cross_setting_error(cities.per_city_key("menu_program_label", a), "🧭 Гид"),
            await cross_setting_error("menu_coins_label", "❓ Ответы"),
            await cross_setting_error(cities.per_city_key("menu_info_label", a), "🧭 Гид"),
        )

    global_dup, city_dup, cross_city_dup, own = asyncio.run(go())
    assert "уже у кнопки «ℹ️ Информация о форуме»" in global_dup
    assert city_dup and cross_city_dup
    assert own is None  # та же кнопка в другом городе — не конфликт


def test_label_resolved_by_delegate_city(ready, monkeypatch):
    """Город A «Гид» = информация, город B «Гид» = программа: делегат B попадает в программу."""
    a, b = _two_cities()

    async def setup():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(cities.per_city_key("menu_info_label", a), "🧭 Гид")
        await db.set_setting(cities.per_city_key("menu_program_label", b), "🧭 Гид")

    asyncio.run(setup())

    def tap(city, key):
        _as_delegate(monkeypatch, city)
        return asyncio.run(MenuButton(key)(_DelegateMsg("🧭 Гид")))

    assert tap(b, "menu_program") and not tap(b, "menu_info")
    assert tap(a, "menu_info") and not tap(a, "menu_program")


def test_label_data_cached_until_write(ready, monkeypatch):
    """Подписи читаются один раз до следующей записи; русскому делегату EN-переводы не грузятся."""
    from services import menu_labels
    loads, en_loads = [], []
    real_keys, real_en = menu_labels._setting_keys, db.fetch_manual_translations

    def counting_keys():
        loads.append(1)
        return real_keys()

    async def counting_en(lang):
        en_loads.append(lang)
        return await real_en(lang)

    monkeypatch.setattr(menu_labels, "_setting_keys", counting_keys)
    monkeypatch.setattr(db, "fetch_manual_translations", counting_en)
    _as_delegate(monkeypatch, lang="ru")
    menu_labels.invalidate()

    async def go():
        for text in ("привет", "Hello there", "🪙 Мои монеты", "ещё текст"):
            await MenuButton("menu_coins")(_DelegateMsg(text))
        first = len(loads)
        await db.set_setting("menu_coins_label", "🪙 Мой счёт")
        hit = await MenuButton("menu_coins")(_DelegateMsg("🪙 Мой счёт"))
        return first, len(loads), hit

    first, after_write, hit = asyncio.run(go())
    assert first == 1 and after_write == 2 and hit
    assert en_loads == []


def test_menu_not_matched_in_delegate_states(ready):
    from handlers.states import DELEGATE_STATE_GROUPS as STATES_GROUPS

    assert DELEGATE_STATE_GROUPS == STATES_GROUPS

    async def go():
        f = MenuButton("menu_speakers")
        return (await f(_Msg("🗣 Спикеры"), raw_state="Question:waiting_for_question"),
                await f(_Msg("🗣 Speakers"), raw_state="Registration:full_name"),
                await f(_Msg("🗣 Спикеры"), raw_state="EditSetting:waiting_for_value"),
                await f(_Msg("🗣 Спикеры"), raw_state=None))

    in_question, in_reg, admin_state, no_state = asyncio.run(go())
    assert not in_question and not in_reg
    assert admin_state and no_state


def test_only_manual_english_translation_matches(ready, monkeypatch):
    from services.i18n import src_hash
    _as_delegate(monkeypatch, lang="en")

    async def go():
        await db.set_setting("menu_coins_label", "🪙 Копилка")
        await db.upsert_translation("en", src_hash("🪙 Копилка"), "🪙 Копилка", "🪙 Piggy", manual=0)
        machine = await MenuButton("menu_coins")(_DelegateMsg("🪙 Piggy", uid=1))
        machine_caption = await caption_for("menu_coins", None, "en")
        await db.upsert_translation("en", src_hash("🪙 Копилка"), "🪙 Копилка", "🪙 Piggy bank", manual=1)
        manual = await MenuButton("menu_coins")(_DelegateMsg("🪙 Piggy bank", uid=2))
        manual_caption = await caption_for("menu_coins", None, "en")
        return machine, machine_caption, manual, manual_caption

    machine, machine_caption, manual, manual_caption = asyncio.run(go())
    assert not machine and machine_caption == "🪙 Копилка"
    assert manual and manual_caption == "🪙 Piggy bank"


def test_taken_label_in_chat_is_explained_not_tapped(ready):
    """Ввод подписи, занятой другой кнопкой, — объяснение, а не срабатывание чужой кнопки."""
    from handlers import admin_settings
    from tests.test_settings_menu_button_guard_260916 import (
        ADMIN_ID as GUARD_ADMIN, _FakeFSMState, _FakeSettingsMessage,
    )
    config.ADMIN_IDS = [GUARD_ADMIN]
    message = _FakeSettingsMessage(text="🎯 Задания")
    state = _FakeFSMState({"setting_key": "menu_coins_label"})

    async def go():
        await admin_settings.settings_edit_value(message, state)  # без SkipHandler
        return await db.get_setting("menu_coins_label")

    assert asyncio.run(go()) is None
    assert any("уже у кнопки «🎯 Задания»" in a for a in message.answers)


def test_old_captions_keep_working_after_rename(ready):
    """Прежние подписи (до HISTORY_DEPTH на ключ) узнаются — старая клавиатура не мертва."""
    from services import menu_labels

    async def rename(value):
        await db.set_setting("menu_coins_label", value)
        await menu_labels.on_setting_written("menu_coins_label")

    async def go():
        for value in ("🪙 Один", "🪙 Два", "🪙 Три", "🪙 Четыре", "🪙 Пять"):
            await rename(value)
        await db.delete_setting("menu_coins_label")
        await menu_labels.on_setting_written("menu_coins_label")
        f = MenuButton("menu_coins")
        return {v: await f(_Msg(v)) for v in ("🪙 Один", "🪙 Два", "🪙 Три", "🪙 Четыре", "🪙 Пять",
                                               "🪙 Мои монеты")}

    seen = asyncio.run(go())
    assert seen["🪙 Пять"] and seen["🪙 Четыре"] and seen["🪙 Три"] and seen["🪙 Два"]
    assert not seen["🪙 Один"]  # глубже HISTORY_DEPTH + текущей
    assert seen["🪙 Мои монеты"]


def test_history_hook_wired_into_setting_writes():
    import inspect
    import settings_audit

    assert "menu_labels.on_setting_written" in inspect.getsource(settings_audit.run_setting_hooks)
