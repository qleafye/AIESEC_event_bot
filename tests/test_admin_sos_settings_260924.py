"""Ревью 24.09 (аудит ключей после 8c0d8af): экран «⚙️ Тексты и тайминги» —
`handlers/admin_sos.py::render_sos_settings_screen` + хендлеры
`asos_settings*/asos_set_delay*/asos_delay_custom*/asos_settings_edit*`. Пять ключей реестра
(`sos_delivery_failed_text`, `sos_recent_followup_text`, `sos_fallback_contact_text`,
`sos_reopen_window_minutes`, `sos_claimed_remind_minutes`) жили ТОЛЬКО в Mini App (на проде
выключен) — этот экран впервые делает их доступными в самом боте.

pytest-asyncio недоступна — async через `asyncio.run()`. Fake-объекты — та же форма, что
`tests/test_admin_program_260924.py`/`tests/test_sos_260924.py`. БД — `tmp_path`, шаблон
`fast_init_db`."""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from core import cities
from config import config
from database import db
from handlers import admin_sos
from handlers.admin_caps import role_caps_key
from handlers.states import EditSetting
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 900924201
BOUND_MSK_ID = 900924202
BOUND_SPB_ID = 900924203


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_admin_sos_settings.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


async def _setup_bound_staff():
    await db.set_setting(role_caps_key("reg_manager"), "settings")
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


async def _enable_cities_module():
    await db.set_setting("event_city_enabled", "on")


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text=None, user_id=SUPERADMIN_ID):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers_sent = []
        self.text_edited = None
        self.edit_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

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


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Экран: дефолты, кнопка на «🆘 SOS»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_screen_has_settings_button(tmp_path):
    _ready(tmp_path)
    text, kb = _run(admin_sos.render_sos_screen(SUPERADMIN_ID))
    assert "asos_settings" in _cbs(kb)


def test_settings_screen_shows_defaults_module_off(tmp_path):
    _ready(tmp_path)
    text, kb = _run(admin_sos.render_sos_settings_screen(SUPERADMIN_ID))
    assert "10 мин" in text  # окно повторного открытия
    assert "20 мин" in text  # напоминание взявшему
    assert "не задан" in text
    assert "общие" in text  # модуль городов выключен
    cbs = _cbs(kb)
    assert "asos_set_delay:reopen:15" in cbs
    assert "asos_set_delay:claimed:5" in cbs
    assert "asos_delay_custom:reopen" in cbs
    assert "asos_delay_custom:claimed" in cbs
    assert "asos_settings_edit:contact" in cbs
    assert "asos_settings_edit:failed" in cbs
    assert "asos_settings_edit:followup" in cbs


def test_settings_screen_shows_collecting_timeout_preset_buttons(tmp_path):
    """D-31 («SOS без категорий»): третий тайминг — «сколько ждать дозапись» (режим
    «дописываю SOS») + два новых текста («Готово»/сессия истекла)."""
    _ready(tmp_path)
    text, kb = _run(admin_sos.render_sos_settings_screen(SUPERADMIN_ID))
    assert "30 мин" in text  # сколько ждать дозапись
    cbs = _cbs(kb)
    assert "asos_set_delay:collecting:15" in cbs
    assert "asos_delay_custom:collecting" in cbs
    assert "asos_settings_edit:done" in cbs
    assert "asos_settings_edit:expired" in cbs


def test_set_delay_preset_collecting_module_off_writes_global_key(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("asos_set_delay:collecting:15", user_id=SUPERADMIN_ID)
    _run(admin_sos.asos_set_delay(callback))
    assert _run(db.get_setting("sos_collecting_timeout_minutes")) == "15"


def test_text_edit_global_done_saves_through_generic_editor(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    _run(admin_sos.asos_settings_edit_start(_FakeCallback("asos_settings_edit:done"), state))
    msg = _FakeMessage(text="Ок, спасибо!", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting("sos_done_text")) == "Ок, спасибо!"


def test_text_edit_global_expired_saves_through_generic_editor(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    _run(admin_sos.asos_settings_edit_start(_FakeCallback("asos_settings_edit:expired"), state))
    msg = _FakeMessage(text="Время вышло, напиши заново.", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting("sos_collecting_expired_text")) == "Время вышло, напиши заново."


def test_settings_open_via_callback(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("asos_settings", user_id=SUPERADMIN_ID)
    _run(admin_sos.asos_settings_open(callback))
    assert "тайминги" in callback.message.text_edited.lower()


def test_settings_screen_module_on_ambiguous_city_blocks_percity_edit(tmp_path):
    _ready(tmp_path)
    _run(_enable_cities_module())
    _run(cities.set_admin_city(SUPERADMIN_ID, cities.ALL_CITIES))
    text, _kb = _run(admin_sos.render_sos_settings_screen(SUPERADMIN_ID))
    assert "Выберите конкретный город" in text


def test_settings_screen_module_on_specific_city_names_it(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    _run(_enable_cities_module())
    text, _kb = _run(admin_sos.render_sos_settings_screen(BOUND_MSK_ID))
    label = _run(cities.city_label("msk"))
    assert label in text
    assert "не задан" in text  # экстренный контакт msk ещё не задан


# ══════════════════════════════════════════════════════════════════════════════════════════
# Пресеты/«Другое» — тайминги per_city
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_set_delay_preset_reopen_module_off_writes_global_key(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("asos_set_delay:reopen:15", user_id=SUPERADMIN_ID)
    _run(admin_sos.asos_set_delay(callback))
    assert _run(db.get_setting("sos_reopen_window_minutes")) == "15"


def test_set_delay_preset_claimed_module_on_writes_composite_key(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    _run(_enable_cities_module())
    callback = _FakeCallback("asos_set_delay:claimed:30", user_id=BOUND_MSK_ID)
    _run(admin_sos.asos_set_delay(callback))
    assert _run(db.get_setting(cities.per_city_key("sos_claimed_remind_minutes", "msk"))) == "30"
    assert _run(db.get_setting("sos_claimed_remind_minutes")) is None


def test_set_delay_denied_when_module_on_and_all_cities_selected(tmp_path):
    _ready(tmp_path)
    _run(_enable_cities_module())
    _run(cities.set_admin_city(SUPERADMIN_ID, cities.ALL_CITIES))
    callback = _FakeCallback("asos_set_delay:reopen:15", user_id=SUPERADMIN_ID)
    _run(admin_sos.asos_set_delay(callback))
    assert callback.answers[0][1] is True
    assert _run(db.get_setting("sos_reopen_window_minutes")) is None


def test_delay_custom_start_enters_fsm_with_composed_key(tmp_path):
    _ready(tmp_path)
    _run(_setup_bound_staff())
    _run(_enable_cities_module())
    state = _new_state(BOUND_SPB_ID)
    callback = _FakeCallback("asos_delay_custom:claimed", user_id=BOUND_SPB_ID)
    _run(admin_sos.asos_delay_custom_start(callback, state))
    data = _run(state.get_data())
    assert data["setting_key"] == cities.per_city_key("sos_claimed_remind_minutes", "spb")
    assert _run(state.get_state()) == EditSetting.waiting_for_value.state


def test_delay_custom_invalid_input_gives_clear_error(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    _run(admin_sos.asos_delay_custom_start(_FakeCallback("asos_delay_custom:reopen"), state))
    msg = _FakeMessage(text="скоро", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert any("целое число" in a for a in msg.answers_sent)
    assert _run(db.get_setting("sos_reopen_window_minutes")) is None


def test_delay_custom_valid_input_saves(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    _run(admin_sos.asos_delay_custom_start(_FakeCallback("asos_delay_custom:reopen"), state))
    msg = _FakeMessage(text="7", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting("sos_reopen_window_minutes")) == "7"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Тексты — два глобальных, один per_city (опциональный контакт, «-» чистит)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_text_edit_unknown_field_denied(tmp_path):
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("asos_settings_edit:bogus", user_id=SUPERADMIN_ID)
    _run(admin_sos.asos_settings_edit_start(callback, state))
    assert callback.answers[0][1] is True


def test_text_edit_global_failed_saves_through_generic_editor(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    state = _new_state(SUPERADMIN_ID)
    _run(admin_sos.asos_settings_edit_start(_FakeCallback("asos_settings_edit:failed"), state))
    msg = _FakeMessage(text="Подойди к волонтёру в жёлтой футболке.", user_id=SUPERADMIN_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting("sos_delivery_failed_text")) == "Подойди к волонтёру в жёлтой футболке."


def test_text_edit_percity_contact_module_on_writes_composite_key(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    _run(_setup_bound_staff())
    _run(_enable_cities_module())
    state = _new_state(BOUND_MSK_ID)
    _run(admin_sos.asos_settings_edit_start(_FakeCallback("asos_settings_edit:contact"), state))
    msg = _FakeMessage(text="+7 999 000-00-00", user_id=BOUND_MSK_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting(cities.per_city_key("sos_fallback_contact_text", "msk"))) == "+7 999 000-00-00"
    assert _run(db.get_setting("sos_fallback_contact_text")) is None


def test_text_edit_percity_contact_clear_with_dash(tmp_path):
    from handlers import admin_settings
    _ready(tmp_path)
    _run(_setup_bound_staff())
    _run(_enable_cities_module())
    key = cities.per_city_key("sos_fallback_contact_text", "msk")
    _run(db.set_setting(key, "+7 999 000-00-00"))
    state = _new_state(BOUND_MSK_ID)
    _run(admin_sos.asos_settings_edit_start(_FakeCallback("asos_settings_edit:contact"), state))
    msg = _FakeMessage(text="-", user_id=BOUND_MSK_ID)
    _run(admin_settings.settings_edit_value(msg, state))
    assert _run(db.get_setting(key)) is None


def test_text_edit_contact_denied_without_specific_city(tmp_path):
    _ready(tmp_path)
    _run(_enable_cities_module())
    _run(cities.set_admin_city(SUPERADMIN_ID, cities.ALL_CITIES))
    state = _new_state(SUPERADMIN_ID)
    callback = _FakeCallback("asos_settings_edit:contact", user_id=SUPERADMIN_ID)
    _run(admin_sos.asos_settings_edit_start(callback, state))
    assert callback.answers[0][1] is True
    assert _run(state.get_state()) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# 25.09: длина форума (sos_active_days) живёт рядом с датой форума; здесь — только ссылка
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_settings_screen_links_forum_length(tmp_path):
    _ready(tmp_path)
    text, kb = _run(admin_sos.render_sos_settings_screen(SUPERADMIN_ID))
    assert "Кнопка SOS видна все дни форума (2 дн.)" in text
    assert "рядом с датой форума" in text
    assert "settings_edit:sos_active_days" in _cbs(kb)


def test_settings_screen_hides_length_button_without_settings_right(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))
    _run(db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID))
    _text, kb = _run(admin_sos.render_sos_settings_screen(BOUND_MSK_ID))
    assert "settings_edit:sos_active_days" not in _cbs(kb)


def test_forum_length_sits_next_to_forum_date():
    """Подпись по смыслу (длина форума, не «SOS»), подсказка с примерами, место — сразу под
    датой начала форума на экране «🎪 Событие/Медиа»."""
    from handlers.admin_settings import _settings_group_keys
    from core.settings_schema import SETTINGS_SCHEMA
    from core.settings_synonyms import SETTINGS_SYNONYMS

    spec = SETTINGS_SCHEMA["sos_active_days"]
    assert spec["label"] == "🗓 Сколько дней идёт форум"
    assert "1 — однодневный форум (регионы), 2 — Москва 30–31.10" in spec["prompt"]
    assert spec["default"] == 2 and spec["per_city"] is True
    keys = _settings_group_keys("event")
    assert keys[keys.index("forum_date") + 1] == "sos_active_days"
    assert "длина форума" in SETTINGS_SYNONYMS["sos_active_days"]


def test_event_screen_shows_forum_length_default(tmp_path):
    from handlers.admin_settings import render_settings_group_text
    _ready(tmp_path)
    text = _run(render_settings_group_text("event"))
    assert "🗓 Сколько дней идёт форум: <i>по умолчанию</i>" in text
