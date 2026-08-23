"""Phase 19 (08, D-06) — экран «⚙️ Настройки → 🎨 Оформление» Mini App.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async
хелпер гоняется через asyncio.run(), config.DB_PATH указывает на файл в tmp_path.

Task 1: экран handlers/admin_miniapp.py — рендер, тумблеры miniapp_enabled/staff_only,
восемь чекбоксов разделов (каждый переключает только свой ключ), валидация HEX-акцента,
загрузка/снятие логотипа, права в ADMIN_CAPS, регресс «бот для людей» (сырой код ключа не
попадает в текст/подписи кнопок).
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers import admin_miniapp
from handlers.states import MiniAppTheme
from handlers.admin_caps import ADMIN_CAPS, required_capability


ADMIN_ID = 900920


def _admin_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_miniapp_settings.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    config.DASHBOARD_PUBLIC_URL = "https://yl26.example.com"


SECTION_KEYS_EXPECTED = [
    "miniapp_section_tasks",
    "miniapp_section_coins",
    "miniapp_section_leaderboard",
    "miniapp_section_profile",
    "miniapp_section_review",
    "miniapp_section_admin_tasks",
    "miniapp_section_stats",
    "miniapp_section_settings",
]


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID, photo=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.photo = photo
        self.answers_sent = []
        self.answer_markups = []
        self.text_edited = None
        self.edit_markup = None
        self.edit_calls = 0

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


async def _read_sections():
    return {key: await get_setting_typed(key) for key in SECTION_KEYS_EXPECTED}


# ── реестр ────────────────────────────────────────────────────────────────────────────────

def test_exactly_eight_miniapp_section_keys():
    keys = [k for k in SETTINGS_SCHEMA if k.startswith("miniapp_section_")]
    assert sorted(keys) == sorted(SECTION_KEYS_EXPECTED)


def test_miniapp_theme_keys_default(tmp_path):
    _admin_ready(tmp_path)

    async def _read():
        return (
            await get_setting_typed("miniapp_enabled"),
            await get_setting_typed("miniapp_staff_only"),
            await get_setting_typed("miniapp_accent"),
            await get_setting_typed("miniapp_logo"),
        )

    enabled, staff_only, accent, logo = asyncio.run(_read())
    assert enabled == "off"
    assert staff_only == "off"
    assert accent == "#037EF3"
    assert logo is None


# ── экран: рендер + чекбоксы ─────────────────────────────────────────────────────────────

def test_screen_shows_toggles_and_eight_section_checkboxes_in_order(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_miniapp.build_miniapp_settings_keyboard())
    data = _flat_callback_data(kb)
    assert "miniapp_toggle_enabled" in data
    assert "miniapp_toggle_staff_only" in data
    section_buttons = [d for d in data if d.startswith("miniapp_section:")]
    expected_order = [f"miniapp_section:{k[len('miniapp_section_'):]}" for k in SECTION_KEYS_EXPECTED]
    assert section_buttons == expected_order
    assert "miniapp_edit_accent" in data
    assert "miniapp_edit_logo" in data
    assert "admin_settings" in data
    # логотип не задан -> кнопки «убрать» нет
    assert "miniapp_remove_logo" not in data


def test_screen_shows_remove_logo_button_only_when_logo_set(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("miniapp_logo", "AgAC999"))
    kb = asyncio.run(admin_miniapp.build_miniapp_settings_keyboard())
    assert "miniapp_remove_logo" in _flat_callback_data(kb)


def test_open_miniapp_settings_handler(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("admin_miniapp_settings")
    asyncio.run(admin_miniapp.open_miniapp_settings(callback, _new_state()))
    assert callback.message.edit_calls == 1
    assert "Оформление" in callback.message.text_edited


def test_open_miniapp_settings_clears_stale_fsm_state(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("admin_miniapp_settings")
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.accent))
    asyncio.run(admin_miniapp.open_miniapp_settings(callback, state))
    assert asyncio.run(state.get_state()) is None


def test_toggle_enabled_flips_only_its_own_key(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_toggle_enabled")
    asyncio.run(admin_miniapp.toggle_miniapp_enabled(callback))
    assert asyncio.run(get_setting_typed("miniapp_enabled")) == "on"
    assert asyncio.run(get_setting_typed("miniapp_staff_only")) == "off"
    assert callback.message.edit_calls == 1
    assert callback.answers


def test_toggle_staff_only_flips_only_its_own_key(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_toggle_staff_only")
    asyncio.run(admin_miniapp.toggle_miniapp_staff_only(callback))
    assert asyncio.run(get_setting_typed("miniapp_staff_only")) == "on"
    assert asyncio.run(get_setting_typed("miniapp_enabled")) == "off"


def test_section_toggle_flips_only_its_own_key(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_section:coins")
    asyncio.run(admin_miniapp.toggle_miniapp_section(callback))

    values = asyncio.run(_read_sections())
    assert values["miniapp_section_coins"] == "off"
    for key in SECTION_KEYS_EXPECTED:
        if key == "miniapp_section_coins":
            continue
        assert values[key] == "on", key
    assert callback.message.edit_calls == 1
    assert callback.answers


def test_section_toggle_unknown_suffix_does_not_write(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_section:unknown_suffix")
    asyncio.run(admin_miniapp.toggle_miniapp_section(callback))
    assert callback.message.edit_calls == 0
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"
    values = asyncio.run(_read_sections())
    assert all(v == "on" for v in values.values())


def test_screen_text_and_labels_have_no_raw_keys(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_miniapp.render_miniapp_settings_text())
    assert "miniapp_" not in text

    kb = asyncio.run(admin_miniapp.build_miniapp_settings_keyboard())
    for row in kb.inline_keyboard:
        for btn in row:
            assert "miniapp_" not in btn.text


def test_section_label_comes_from_registry(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    monkeypatch.setitem(SETTINGS_SCHEMA, "miniapp_section_tasks", {
        **SETTINGS_SCHEMA["miniapp_section_tasks"], "label": "🧪 Совсем другая подпись",
    })
    text = asyncio.run(admin_miniapp.render_miniapp_settings_text())
    assert "🧪 Совсем другая подпись" in text
    kb = asyncio.run(admin_miniapp.build_miniapp_settings_keyboard())
    assert any("🧪 Совсем другая подпись" in t for t in _flat_texts(kb))


def test_empty_dashboard_public_url_shows_warning(tmp_path):
    _admin_ready(tmp_path)
    config.DASHBOARD_PUBLIC_URL = ""
    text = asyncio.run(admin_miniapp.render_miniapp_settings_text())
    assert "не задан" in text


# ── акцент: правка и валидация ──────────────────────────────────────────────────────────

def test_edit_accent_start_sets_state(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    callback = FakeCallback("miniapp_edit_accent")
    asyncio.run(admin_miniapp.miniapp_edit_accent_start(callback, state))
    assert asyncio.run(state.get_state()) == MiniAppTheme.accent
    assert callback.message.edit_calls == 1


def test_accent_valid_hex_is_saved(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.accent))
    message = FakeMessage(text="#00FF11")
    asyncio.run(admin_miniapp.miniapp_accent_step(message, state))
    assert asyncio.run(get_setting_typed("miniapp_accent")) == "#00FF11"
    assert asyncio.run(state.get_state()) is None
    assert any("#00FF11" in t for t in message.answers_sent)


def test_accent_invalid_without_hash_is_rejected(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.accent))
    message = FakeMessage(text="037EF3")
    asyncio.run(admin_miniapp.miniapp_accent_step(message, state))
    assert asyncio.run(get_setting_typed("miniapp_accent")) == "#037EF3"  # default, unchanged
    assert asyncio.run(state.get_state()) == MiniAppTheme.accent
    assert "решётки" in message.answers_sent[-1]


def test_accent_invalid_bad_hex_chars_is_rejected(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.accent))
    message = FakeMessage(text="#GGGGGG")
    asyncio.run(admin_miniapp.miniapp_accent_step(message, state))
    assert asyncio.run(get_setting_typed("miniapp_accent")) == "#037EF3"
    assert asyncio.run(state.get_state()) == MiniAppTheme.accent


def test_accent_invalid_too_short_is_rejected(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.accent))
    message = FakeMessage(text="#03")
    asyncio.run(admin_miniapp.miniapp_accent_step(message, state))
    assert asyncio.run(get_setting_typed("miniapp_accent")) == "#037EF3"
    assert asyncio.run(state.get_state()) == MiniAppTheme.accent


# ── логотип: загрузка / снятие ──────────────────────────────────────────────────────────

def test_edit_logo_start_sets_state(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    callback = FakeCallback("miniapp_edit_logo")
    asyncio.run(admin_miniapp.miniapp_edit_logo_start(callback, state))
    assert asyncio.run(state.get_state()) == MiniAppTheme.logo


def test_logo_photo_step_saves_file_id(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.logo))
    message = FakeMessage(photo=[FakePhotoSize("small"), FakePhotoSize("big")])
    asyncio.run(admin_miniapp.miniapp_logo_step(message, state))
    assert asyncio.run(get_setting_typed("miniapp_logo")) == "big"
    assert asyncio.run(state.get_state()) is None


def test_logo_step_invalid_content_reprompts_without_writing(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.logo))
    message = FakeMessage(text="не фото")
    asyncio.run(admin_miniapp.miniapp_logo_step_invalid(message))
    assert asyncio.run(get_setting_typed("miniapp_logo")) is None
    assert "фото" in message.answers_sent[-1]


def test_remove_logo_clears_key(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("miniapp_logo", "AgAC999"))
    callback = FakeCallback("miniapp_remove_logo")
    asyncio.run(admin_miniapp.miniapp_remove_logo(callback))
    assert asyncio.run(get_setting_typed("miniapp_logo")) is None
    assert callback.message.edit_calls == 1


def test_cancel_edit_clears_state_and_rerenders(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.accent))
    callback = FakeCallback("miniapp_cancel_edit")
    asyncio.run(admin_miniapp.miniapp_cancel_edit(callback, state))
    assert asyncio.run(state.get_state()) is None
    assert callback.message.edit_calls == 1


# ── ADMIN_CAPS ────────────────────────────────────────────────────────────────────────────

def test_all_new_callbacks_registered_under_settings():
    expected = {
        "admin_miniapp_settings": "settings",
        "miniapp_toggle_enabled": "settings",
        "miniapp_toggle_staff_only": "settings",
        "miniapp_section:*": "settings",
        "miniapp_edit_accent": "settings",
        "miniapp_edit_logo": "settings",
        "miniapp_remove_logo": "settings",
        "miniapp_cancel_edit": "settings",
        "state:MiniAppTheme:*": "settings",
    }
    for key, cap in expected.items():
        assert ADMIN_CAPS.get(key) == cap, key

    assert required_capability(callback_data="admin_miniapp_settings") == "settings"
    assert required_capability(callback_data="miniapp_section:tasks") == "settings"
    assert required_capability(raw_state="MiniAppTheme:accent") == "settings"
    assert required_capability(raw_state="MiniAppTheme:logo") == "settings"
