"""Enum-настройки в общем редакторе — кнопками с человеческими подписями, а не «напишите
forum/conference»."""
import asyncio
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from config import config
from handlers import admin_settings, admin_settings_enum
from handlers.admin_caps import required_capability
from handlers.states import EditSetting
from tests._dbtpl import fast_init_db

ADMIN = 900261009


def _run(coro):
    return asyncio.run(coro)


def test_event_type_screen_has_option_buttons(tmp_path):
    config.DB_PATH = str(tmp_path / "enum.db")
    fast_init_db()
    text, kb = _run(admin_settings._settings_edit_screen("event_type", None))
    labels = [row[0].text for row in kb.inline_keyboard]
    assert labels[:4] == ["Форум", "Конференция", "Вручную", "Форум СкиллАп"]
    assert kb.inline_keyboard[1][0].callback_data == "settings_enum_pick:1"
    assert "кнопкой" in text and "conference" not in text


def test_text_key_keeps_text_input(tmp_path):
    config.DB_PATH = str(tmp_path / "enum2.db")
    fast_init_db()
    text, kb = _run(admin_settings._settings_edit_screen("start_text", None))
    assert not any(b.callback_data.startswith("settings_enum_pick") for row in kb.inline_keyboard for b in row)
    assert "Пришлите новое значение" in text


def test_pick_routes_value_through_text_path(monkeypatch):
    seen = {}

    async def _fake_edit_value(message, state):
        seen["text"] = message.text
        seen["from"] = message.from_user.id

    monkeypatch.setattr(admin_settings, "settings_edit_value", _fake_edit_value)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
    _run(state.set_state(EditSetting.waiting_for_value))
    _run(state.update_data(setting_key="event_type"))
    bot_msg = Message(message_id=5, date=datetime.now(), chat=Chat(id=ADMIN, type="private"),
                      from_user=User(id=1, is_bot=True, first_name="Бот"), text="экран")
    answers = []

    class _CB:
        data = "settings_enum_pick:1"
        from_user = User(id=ADMIN, is_bot=False, first_name="Админ")
        message = bot_msg
        bot = None

        async def answer(self, *a, **k):
            answers.append(a)

    _run(admin_settings_enum.settings_enum_pick(_CB(), state))
    assert seen == {"text": "conference", "from": ADMIN}
    assert required_capability(callback_data="settings_enum_pick:1") == "settings"


# ── Шапка на одном городе: те же кнопки и подписи, запись — в свою область ─────────────────

CITY_ADMIN = 900261010


def _city_ready(tmp_path, name):
    import cities
    from database import db
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [CITY_ADMIN]
    _run(db.set_setting("event_city_enabled", "on"))
    _run(cities.set_admin_city(CITY_ADMIN, "spb"))


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def __call__(self, method, request_timeout=None):
        self.sent.append(method)


class _ScreenMsg:
    def __init__(self):
        self.text, self.markup = None, None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text, self.markup = text, reply_markup


def _cb(data, message=None, bot=None):
    answers = []

    class _CB:
        from_user = User(id=CITY_ADMIN, is_bot=False, first_name="Админ")

        async def answer(self, *a, **k):
            answers.append(a)

    cb = _CB()
    cb.data, cb.message, cb.bot, cb.answers = data, message or _ScreenMsg(), bot, answers
    return cb


def _bot_msg():
    return Message(message_id=7, date=datetime.now(), chat=Chat(id=CITY_ADMIN, type="private"),
                   from_user=User(id=1, is_bot=True, first_name="Бот"), text="экран")


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CITY_ADMIN, user_id=CITY_ADMIN))


def _pick(state, key, value, bot):
    idx = admin_settings_enum.enum_options(key).index(value)
    _run(admin_settings_enum.settings_enum_pick(_cb(f"settings_enum_pick:{idx}", _bot_msg(), bot), state))


def test_city_header_common_enum_has_buttons_and_label(tmp_path):
    """Общий enum-ключ при городе в шапке — кнопки и подпись, не «Сейчас задано: skillup»."""
    from database import db
    _city_ready(tmp_path, "c1.db")
    _run(db.set_setting("event_type", "skillup"))
    text, kb = _run(admin_settings._settings_edit_screen("event_type", "spb"))
    assert "<b>Форум СкиллАп</b>" in text and "<b>skillup</b>" not in text
    assert "Общая настройка" in text and "Пришлите новое значение" not in text
    assert [r[0].callback_data for r in kb.inline_keyboard][:4] == [f"settings_enum_pick:{i}" for i in range(4)]
    # on/off без option_labels — тоже словами.
    _run(db.set_setting("payment_enabled", "on"))
    text, kb = _run(admin_settings._settings_edit_screen("payment_enabled", "spb"))
    assert "<b>Включено</b>" in text
    assert [r[0].text for r in kb.inline_keyboard][:2] == ["✅ Включено", "Выключено"]


def test_city_header_common_enum_pick_writes_common_key(tmp_path):
    from database import db
    _city_ready(tmp_path, "c2.db")
    state = _state()
    _run(admin_settings.settings_edit_start(_cb("settings_edit:event_type"), state))
    _pick(state, "event_type", "conference", _FakeBot())
    assert _run(db.get_setting("event_type")) == "conference"
    assert _run(db.get_setting("event_type__city__spb")) is None


def test_city_header_skillup_pick_goes_to_type_only_confirm(tmp_path):
    from database import db
    _city_ready(tmp_path, "c3.db")
    state = _state()
    bot = _FakeBot()
    _run(admin_settings.settings_edit_start(_cb("settings_edit:event_type"), state))
    _pick(state, "event_type", "skillup", bot)
    shown = bot.sent[-1]
    assert "не применится" in shown.text
    cbs = [b.callback_data for row in shown.reply_markup.inline_keyboard for b in row]
    assert cbs == ["preset_confirm:skillup:et", "settings_edit:event_type"]
    assert _run(db.get_setting("event_type")) is None

    # «✅ Сменить только тип» → экран «🎭 Тип события» с кнопками и живым FSM: следующий выбор
    # не теряется молча.
    from handlers import admin_reg_config
    cb = _cb("preset_confirm:skillup:et")
    _run(admin_reg_config.preset_confirm(cb, state))
    assert _run(db.get_setting("event_type")) == "skillup"
    assert "<b>Форум СкиллАп</b>" in cb.message.text and "Пришлите новое значение" not in cb.message.text
    assert any(b.callback_data.startswith("settings_enum_pick:") for row in cb.message.markup.inline_keyboard for b in row)
    assert _run(state.get_state()) == EditSetting.waiting_for_value.state
    assert (_run(state.get_data())).get("setting_key") == "event_type"
    _pick(state, "event_type", "forum", bot)
    assert _run(db.get_setting("event_type")) == "forum"


def test_city_own_enum_screen_has_buttons_and_pick_writes_city_key(tmp_path):
    """Per-city enum: «✏️ Изменить для …» — кнопки с подписями; выбор пишет ключ города,
    общий не трогает."""
    from database import db
    _city_ready(tmp_path, "c4.db")
    _run(db.set_setting("reg_edit_policy", "until_decision"))
    text, kb = _run(admin_settings._settings_edit_screen("reg_edit_policy", "spb"))
    assert "только до решения" in text and "until_decision" not in text
    state = _state()
    cb = _cb("settings_edit_city:reg_edit_policy")
    _run(admin_settings.settings_edit_city(cb, state))
    assert "until_decision" not in cb.message.text and "Выберите вариант кнопкой" in cb.message.text
    assert "отправьте «-»" not in cb.message.text
    labels = [r[0].text for r in cb.message.markup.inline_keyboard]
    assert labels[:3] == ["всегда можно", "только до решения", "нельзя"]
    _pick(state, "reg_edit_policy", "never", _FakeBot())
    assert _run(db.get_setting("reg_edit_policy__city__spb")) == "never"
    assert _run(db.get_setting("reg_edit_policy")) == "until_decision"
    # Повторно: экран города показывает своё значение подписью и отмечает его галочкой.
    cb = _cb("settings_edit_city:reg_edit_policy")
    _run(admin_settings.settings_edit_city(cb, _state()))
    assert "<b>нельзя</b>" in cb.message.text
    assert [r[0].text for r in cb.message.markup.inline_keyboard][2] == "✅ нельзя"


def test_city_own_text_key_keeps_text_input(tmp_path):
    _city_ready(tmp_path, "c5.db")
    cb = _cb("settings_edit_city:start_text")
    _run(admin_settings.settings_edit_city(cb, _state()))
    assert not any(b.callback_data.startswith("settings_enum_pick") for row in cb.message.markup.inline_keyboard for b in row)
