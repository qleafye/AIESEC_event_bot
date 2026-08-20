"""Quick 260820-rms: команда в поле значения настройки + напоминание про «весь список».

Прод 20.08: в bot_settings оказались source_options='/start' и approve_text='/start' —
менеджер отправил команду, находясь в правке настройки. Админский роутер подключён первым
(main.py), поэтому до cmd_start сообщение не доходит, а хендлер значения принимал любой текст.
Делегаты получили вопрос «Откуда узнал(-а)» с единственной кнопкой «/start».

Второй сюжет: списочные настройки заменяются целиком, а выглядят как обычный текст —
source_options схлопывался в один пункт уже дважды (17.08 и 20.08). Экран правки обязан
показывать список по пунктам и предупреждать про замену.

pytest-asyncio недоступен — async через asyncio.run(), config.DB_PATH -> tmp. Фейки
скопированы по конвенции сьюта (tests/test_settings_int_validation_260819.py).
"""
import asyncio

from config import config
from database import db
from handlers import admin_settings
from handlers.settings_validation import is_command_like

ADMIN_ID = 900802


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_command_guard.db")
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
    _ready(tmp_path)
    message = _FakeSettingsMessage(text=text)
    state = _FakeFSMState({"setting_key": key})

    async def go():
        if preset is not None:
            await db.set_setting(key, preset)
        await admin_settings.settings_edit_value(message, state)
        return await db.get_setting(key)

    saved = asyncio.run(go())
    return message, state, saved


LIST_KEY = "source_options"
TEXT_KEY = "reg_complete_text"

SOURCE_LIST = "Соцсети Юлид\nСоцсети АЙСЕК\nДругое"


# ── чистое правило ───────────────────────────────────────────────────────────────────────────

def test_is_command_like_catches_plain_commands():
    for value in ("/start", "/cancel", "  /start  ", "/start@YouLead_bot", "/admin_menu"):
        assert is_command_like(value), value


def test_is_command_like_leaves_real_values_alone():
    for value in (
        "/start — так мы называем первый экран",
        "Соцсети Юлид",
        "https://t.me/youlead",
        "-",
        "",
        None,
        "/",
    ):
        assert not is_command_like(value), value


# ── хендлер ──────────────────────────────────────────────────────────────────────────────────

def test_command_is_not_saved_as_value(tmp_path):
    _message, _state, saved = _edit(tmp_path, LIST_KEY, "/start")
    assert saved is None, "команда записана в настройку"


def test_command_does_not_wipe_existing_value(tmp_path):
    """Главный ущерб прода: список источников был затёрт одной командой."""
    _message, _state, saved = _edit(tmp_path, LIST_KEY, "/start", preset=SOURCE_LIST)
    assert saved == SOURCE_LIST


def test_command_keeps_state_and_explains(tmp_path):
    message, state, _saved = _edit(tmp_path, LIST_KEY, "/start")
    assert asyncio.run(state.get_state()) == "EditSetting:waiting_for_value"
    assert message.answers, "менеджеру должно прийти объяснение"
    hint = message.answers[-1]
    assert "команда" in hint.lower()
    assert "«-»" in hint


def test_text_starting_with_slash_is_still_saved(tmp_path):
    """Отбиваем ровно одиночную команду, а не всё со слэшем."""
    value = "/start — так мы называем первый экран"
    _message, _state, saved = _edit(tmp_path, TEXT_KEY, value)
    assert saved == value


def test_normal_value_unaffected(tmp_path):
    _message, _state, saved = _edit(tmp_path, LIST_KEY, SOURCE_LIST)
    assert saved == SOURCE_LIST


def test_reset_sentinel_still_works(tmp_path):
    _message, _state, saved = _edit(tmp_path, LIST_KEY, "-", preset=SOURCE_LIST)
    assert saved is None


# ── экран правки списка ──────────────────────────────────────────────────────────────────────

def _screen(tmp_path, key, preset=None):
    _ready(tmp_path)

    async def go():
        if preset is not None:
            await db.set_setting(key, preset)
        return await admin_settings._settings_edit_screen(key, None)

    text, _kb = asyncio.run(go())
    return text


def test_list_screen_warns_about_full_replacement(tmp_path):
    text = _screen(tmp_path, LIST_KEY, preset=SOURCE_LIST)
    assert "ВЕСЬ список" in text
    assert "заменит" in text


def test_list_screen_shows_items_one_per_line(tmp_path):
    text = _screen(tmp_path, LIST_KEY, preset=SOURCE_LIST)
    assert "• Соцсети Юлид" in text
    assert "• Другое" in text
    assert "(3)" in text, "менеджеру полезно видеть, сколько пунктов сейчас"


def test_text_screen_keeps_its_own_hint(tmp_path):
    text = _screen(tmp_path, TEXT_KEY, preset="Заявка принята")
    assert "ВЕСЬ список" not in text
    assert "Сейчас задано" in text
