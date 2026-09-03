"""Phase 19 (08, D-06) + Phase 19.1 (07, D-20) — экран «⚙️ Настройки → 🎨 Оформление» Mini App.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async
хелпер гоняется через asyncio.run(), config.DB_PATH указывает на файл в tmp_path.

Task 1 (19-08): экран handlers/admin_miniapp.py — рендер, тумблеры miniapp_enabled/staff_only,
восемь чекбоксов разделов (каждый переключает только свой ключ), права в ADMIN_CAPS, регресс
«бот для людей» (сырой код ключа не попадает в текст/подписи кнопок).

Задачи 1-2 (19.1-07): второй шов handlers/admin_miniapp_theme.py — пресеты BlueBook/YouLead/
Своя (применение пишет все ручки разом, «Своя (на базе X)» вычисляется сравнением, не флагом),
сброс с подтверждением, ручки кастома D-04 (три цвета с контрастом, шрифт, тон, лого/обложка/
паттерн/стикеры/иконка монеты).
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers import admin_miniapp
from handlers import admin_miniapp_theme
from handlers.states import MiniAppTheme
from handlers.admin_caps import ADMIN_CAPS, required_capability
import web_theme


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
    "miniapp_section_form",
    "miniapp_section_review",
    "miniapp_section_applications",
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
        self.photo_calls = []
        self.text_edited = None
        self.edit_markup = None
        self.edit_calls = 0

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.photo_calls.append((photo, caption, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup
        self.edit_calls += 1


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.menu_button_calls = []

    async def set_chat_menu_button(self, menu_button=None):
        if self.fail:
            raise RuntimeError("Telegram unreachable")
        self.menu_button_calls.append(menu_button)


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None, bot=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []
        self.bot = bot if bot is not None else FakeBot()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


async def _read_sections():
    return {key: await get_setting_typed(key) for key in SECTION_KEYS_EXPECTED}


# ── реестр ────────────────────────────────────────────────────────────────────────────────

def test_exactly_ten_miniapp_section_keys():
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


# ── экран 1 (admin_miniapp.py): рендер + чекбоксы ──────────────────────────────────────────

def test_screen_shows_toggles_eight_sections_and_theme_entry(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_miniapp.build_miniapp_settings_keyboard())
    data = _flat_callback_data(kb)
    assert "miniapp_toggle_enabled" in data
    assert "miniapp_toggle_staff_only" in data
    section_buttons = [d for d in data if d.startswith("miniapp_section:")]
    expected_order = [f"miniapp_section:{k[len('miniapp_section_'):]}" for k in SECTION_KEYS_EXPECTED]
    assert section_buttons == expected_order
    assert "miniapp_theme_open" in data
    # Phase 20 (20-03): «Назад» ведёт в раздел-владелец экрана («🔧 Управление»).
    assert "admin_sec:manage" in data
    # правка акцента/лого больше не живёт на этом экране (перенесена во второй шов)
    assert "miniapp_edit_accent" not in data
    assert "miniapp_edit_logo" not in data


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
    asyncio.run(state.set_state(MiniAppTheme.color))
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


def test_toggle_enabled_syncs_chat_menu_button_to_webapp(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_toggle_enabled")
    asyncio.run(admin_miniapp.toggle_miniapp_enabled(callback))
    assert len(callback.bot.menu_button_calls) == 1
    from aiogram.types import MenuButtonWebApp
    assert isinstance(callback.bot.menu_button_calls[0], MenuButtonWebApp)
    assert callback.bot.menu_button_calls[0].web_app.url.endswith("/app")


def test_toggle_enabled_off_syncs_chat_menu_button_to_default(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("miniapp_enabled", "on"))
    callback = FakeCallback("miniapp_toggle_enabled")
    asyncio.run(admin_miniapp.toggle_miniapp_enabled(callback))
    from aiogram.types import MenuButtonDefault
    assert isinstance(callback.bot.menu_button_calls[0], MenuButtonDefault)


def test_toggle_enabled_fail_soft_when_telegram_unreachable(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_toggle_enabled", bot=FakeBot(fail=True))
    asyncio.run(admin_miniapp.toggle_miniapp_enabled(callback))
    # setting still flips even though the chat menu button call failed
    assert asyncio.run(get_setting_typed("miniapp_enabled")) == "on"
    assert callback.answers
    assert "следующем запуске" in callback.answers[0][0]


def test_sync_chat_menu_button_direct_on_and_off(tmp_path):
    _admin_ready(tmp_path)
    bot = FakeBot()
    asyncio.run(db.set_setting("miniapp_enabled", "on"))
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    from aiogram.types import MenuButtonWebApp
    assert isinstance(bot.menu_button_calls[-1], MenuButtonWebApp)

    asyncio.run(db.set_setting("miniapp_enabled", "off"))
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    from aiogram.types import MenuButtonDefault
    assert isinstance(bot.menu_button_calls[-1], MenuButtonDefault)


def test_sync_chat_menu_button_empty_url_forces_default(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("miniapp_enabled", "on"))
    config.DASHBOARD_PUBLIC_URL = ""
    bot = FakeBot()
    asyncio.run(admin_miniapp.sync_chat_menu_button(bot))
    from aiogram.types import MenuButtonDefault
    assert isinstance(bot.menu_button_calls[-1], MenuButtonDefault)


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


# ── ADMIN_CAPS: экран 1 ──────────────────────────────────────────────────────────────────

def test_all_new_callbacks_registered_under_settings():
    expected = {
        "admin_miniapp_settings": "settings",
        "miniapp_toggle_enabled": "settings",
        "miniapp_toggle_staff_only": "settings",
        "miniapp_section:*": "settings",
        "miniapp_theme_open": "settings",
        "state:MiniAppTheme:*": "settings",
    }
    for key, cap in expected.items():
        assert ADMIN_CAPS.get(key) == cap, key

    assert required_capability(callback_data="admin_miniapp_settings") == "settings"
    assert required_capability(callback_data="miniapp_section:tasks") == "settings"
    assert required_capability(raw_state="MiniAppTheme:color") == "settings"
    assert required_capability(raw_state="MiniAppTheme:logo") == "settings"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Phase 19.1 (07, D-20) — второй шов: пресеты + ручки кастома (handlers/admin_miniapp_theme.py)
# ═══════════════════════════════════════════════════════════════════════════════════════════

# ── пресеты: рендер, выбор кнопкой, превью с подтверждением ────────────────────────────────

def test_theme_screen_defaults_to_bluebook_not_custom(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_miniapp_theme.render_miniapp_theme_text())
    assert "Пресет: АЙСЕК — классика" in text
    assert "Своя" not in text.split("\n")[2]  # заголовочная строка не «Своя»
    kb = asyncio.run(admin_miniapp_theme.build_miniapp_theme_keyboard())
    data = _flat_callback_data(kb)
    assert "miniapp_preset:bluebook" in data
    assert "miniapp_preset:youlead" in data
    # Quick 260904-183: РилТолк — третий реальный пресет (бренд-материалы сняты 04.09.2026).
    assert "miniapp_preset:realtalk" in data
    assert "miniapp_theme_noop" in data  # кнопка «Своя»
    # не «Своя» -> кнопки сброса нет
    assert "miniapp_theme_reset" not in data


def test_open_miniapp_theme_clears_stale_state_and_renders(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.color))
    callback = FakeCallback("miniapp_theme_open")
    asyncio.run(admin_miniapp_theme.open_miniapp_theme(callback, state))
    assert asyncio.run(state.get_state()) is None
    assert callback.message.edit_calls == 1
    assert "Пресеты и ручки" in callback.message.text_edited


def test_preset_pick_sends_text_fallback_when_preview_missing(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    monkeypatch.setattr(admin_miniapp_theme, "PREVIEW_DIR", tmp_path / "no-such-dir")
    callback = FakeCallback("miniapp_preset:youlead")
    asyncio.run(admin_miniapp_theme.miniapp_preset_pick(callback))
    assert callback.message.photo_calls == []
    assert callback.message.answers_sent  # fail-soft: тот же вопрос текстом
    text = callback.message.answers_sent[-1]
    assert "ЮЛид" in text


def test_preset_pick_unknown_name_rejected(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_preset:doesnotexist")
    asyncio.run(admin_miniapp_theme.miniapp_preset_pick(callback))
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"


def test_preset_pick_sends_photo_when_preview_exists(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    preview_dir = tmp_path / "previews"
    preview_dir.mkdir()
    (preview_dir / "bluebook.png").write_bytes(b"\x89PNG fake bytes")
    monkeypatch.setattr(admin_miniapp_theme, "PREVIEW_DIR", preview_dir)
    callback = FakeCallback("miniapp_preset:bluebook")
    asyncio.run(admin_miniapp_theme.miniapp_preset_pick(callback))
    assert len(callback.message.photo_calls) == 1
    assert callback.message.answers_sent == []


def test_preset_apply_writes_all_handles_at_once(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_preset_apply:youlead")
    asyncio.run(admin_miniapp_theme.miniapp_preset_apply(callback))

    async def _read_all():
        return {h: await get_setting_typed(k) for h, k in web_theme.THEME_KEYS.items()}

    values = asyncio.run(_read_all())
    expected = dict(web_theme.PRESETS["youlead"])
    expected["preset"] = "youlead"
    assert values == expected
    assert callback.answers and "ЮЛид" in callback.answers[0][0]
    # применение шлёт свежий экран отдельным сообщением
    assert callback.message.answers_sent


def test_preset_apply_unknown_name_rejected(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_preset_apply:doesnotexist")
    asyncio.run(admin_miniapp_theme.miniapp_preset_apply(callback))
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"
    assert asyncio.run(get_setting_typed("miniapp_theme_preset")) == "bluebook"


def test_preset_cancel_writes_nothing(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_preset_cancel")
    asyncio.run(admin_miniapp_theme.miniapp_preset_cancel(callback))
    assert asyncio.run(get_setting_typed("miniapp_theme_preset")) == "bluebook"
    assert callback.answers


def test_theme_noop_just_answers(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_theme_noop")
    asyncio.run(admin_miniapp_theme.miniapp_theme_noop(callback))
    assert callback.answers
    assert callback.message.edit_calls == 0


# ── «Своя (на базе X)» и сброс ──────────────────────────────────────────────────────────────

def test_editing_one_handle_flips_header_to_custom(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting(web_theme.THEME_KEYS["accent"], "#123456"))
    text = asyncio.run(admin_miniapp_theme.render_miniapp_theme_text())
    assert "Своя (на базе АЙСЕК — классика)" in text
    kb = asyncio.run(admin_miniapp_theme.build_miniapp_theme_keyboard())
    data = _flat_callback_data(kb)
    assert "miniapp_theme_reset" in data


def test_reset_not_offered_when_not_custom(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_theme_reset")
    asyncio.run(admin_miniapp_theme.miniapp_theme_reset_start(callback))
    assert callback.answers and callback.answers[0][1] is True  # show_alert
    assert callback.message.edit_calls == 0


def test_reset_confirmation_lists_consequences(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting(web_theme.THEME_KEYS["accent"], "#123456"))
    callback = FakeCallback("miniapp_theme_reset")
    asyncio.run(admin_miniapp_theme.miniapp_theme_reset_start(callback))
    assert callback.message.edit_calls == 1
    text = callback.message.text_edited
    assert "цвета" in text.lower()
    assert "лого" in text.lower() and "не тронется" in text.lower()


def test_reset_go_restores_preset_defaults_but_not_assets(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting(web_theme.THEME_KEYS["accent"], "#123456"))
    asyncio.run(db.set_setting("miniapp_logo", "AgAC999"))  # ассет -- не часть пресета
    callback = FakeCallback("miniapp_theme_reset_go")
    asyncio.run(admin_miniapp_theme.miniapp_theme_reset_go(callback))
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["accent"])) == web_theme.PRESETS["bluebook"]["accent"]
    assert asyncio.run(get_setting_typed("miniapp_logo")) == "AgAC999"
    assert callback.message.edit_calls == 1


# ── D-04, 1: три цвета — валидация, сохранение, контраст словами ───────────────────────────

def test_color_edit_start_sets_state_with_handle(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    callback = FakeCallback("miniapp_theme_color:secondary")
    asyncio.run(admin_miniapp_theme.miniapp_theme_color_start(callback, state))
    assert asyncio.run(state.get_state()) == MiniAppTheme.color
    assert asyncio.run(state.get_data()) == {"miniapp_theme_color_handle": "secondary"}
    assert callback.message.edit_calls == 1


def test_color_edit_unknown_handle_rejected(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    callback = FakeCallback("miniapp_theme_color:foo")
    asyncio.run(admin_miniapp_theme.miniapp_theme_color_start(callback, state))
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"
    assert asyncio.run(state.get_state()) is None


def test_color_step_valid_hex_is_saved_with_good_contrast_note(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.color))
    asyncio.run(state.update_data(miniapp_theme_color_handle="bg"))
    message = FakeMessage(text="#FFFFFF")
    asyncio.run(admin_miniapp_theme.miniapp_theme_color_step(message, state))
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["bg"])) == "#FFFFFF"
    assert asyncio.run(state.get_state()) is None
    assert "читается" in message.answers_sent[0]


def test_color_step_bad_contrast_explains_without_blocking_save(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.color))
    asyncio.run(state.update_data(miniapp_theme_color_handle="bg"))
    message = FakeMessage(text="#1D1D1D")  # тёмный фон -- чёрный текст (_TEXT_INK) не читается
    asyncio.run(admin_miniapp_theme.miniapp_theme_color_step(message, state))
    # сохранено несмотря на плохой контраст -- D-04: предупреждает, но не блокирует
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["bg"])) == "#1D1D1D"
    assert "Слишком бледно" in message.answers_sent[0]
    assert "контраст" in message.answers_sent[0]


def test_color_step_invalid_hex_is_rejected_and_state_kept(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.color))
    asyncio.run(state.update_data(miniapp_theme_color_handle="accent"))
    message = FakeMessage(text="037EF3")
    asyncio.run(admin_miniapp_theme.miniapp_theme_color_step(message, state))
    assert asyncio.run(get_setting_typed("miniapp_accent")) == "#037EF3"  # default, unchanged
    assert asyncio.run(state.get_state()) == MiniAppTheme.color
    assert "решётки" in message.answers_sent[-1]


def test_color_step_accent_checked_against_bg(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting(web_theme.THEME_KEYS["bg"], "#000000"))
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.color))
    asyncio.run(state.update_data(miniapp_theme_color_handle="accent"))
    message = FakeMessage(text="#FFFFFF")
    asyncio.run(admin_miniapp_theme.miniapp_theme_color_step(message, state))
    assert "читается" in message.answers_sent[0]


# ── D-04, 2: шрифт заголовков — закрытый список ─────────────────────────────────────────────

def test_font_pick_valid_writes_and_marks_active(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_theme_font:lato")
    asyncio.run(admin_miniapp_theme.miniapp_theme_font_pick(callback))
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["heading_font"])) == "lato"
    kb = asyncio.run(admin_miniapp_theme.build_miniapp_theme_keyboard())
    texts = _flat_texts(kb)
    assert any(t.startswith("✅ ") and "Lato" in t for t in texts)


def test_font_pick_unknown_key_rejected(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_theme_font:comic_sans")
    asyncio.run(admin_miniapp_theme.miniapp_theme_font_pick(callback))
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"


# ── D-04, 3/6: тумблеры ──────────────────────────────────────────────────────────────────

def test_toggle_playful_flips_only_its_own_key(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_theme_toggle_playful")
    asyncio.run(admin_miniapp_theme.miniapp_theme_toggle_playful(callback))
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["playful_tone"])) == "on"
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["pattern_enabled"])) == "off"


def test_toggle_pattern_flips_only_its_own_key(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_theme_toggle_pattern")
    asyncio.run(admin_miniapp_theme.miniapp_theme_toggle_pattern(callback))
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["pattern_enabled"])) == "on"
    assert asyncio.run(get_setting_typed(web_theme.THEME_KEYS["playful_tone"])) == "off"


# ── D-04, 4/5/7/8: девять фото-ручек (общий механизм) ────────────────────────────────────

def test_photo_slot_start_sets_matching_state(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    callback = FakeCallback("miniapp_theme_photo:sticker_top1")
    asyncio.run(admin_miniapp_theme.miniapp_theme_photo_start(callback, state))
    assert asyncio.run(state.get_state()) == MiniAppTheme.sticker_top1
    assert callback.message.edit_calls == 1
    assert "топ-1" in callback.message.text_edited.lower()


def test_photo_slot_start_unknown_slot_rejected(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    callback = FakeCallback("miniapp_theme_photo:unknown")
    asyncio.run(admin_miniapp_theme.miniapp_theme_photo_start(callback, state))
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"
    assert asyncio.run(state.get_state()) is None


def test_photo_slot_step_saves_file_id_for_correct_key(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.cover_dark))
    message = FakeMessage(photo=[FakePhotoSize("small"), FakePhotoSize("big")])
    asyncio.run(admin_miniapp_theme.miniapp_theme_photo_step(message, state))
    assert asyncio.run(get_setting_typed("miniapp_cover_dark")) == "big"
    assert asyncio.run(state.get_state()) is None
    # соседние слоты не тронуты
    assert asyncio.run(get_setting_typed("miniapp_cover")) is None


def test_photo_slot_step_invalid_content_reprompts_without_writing(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.sticker_error))
    message = FakeMessage(text="документ, не фото")
    asyncio.run(admin_miniapp_theme.miniapp_theme_photo_step_invalid(message))
    assert asyncio.run(get_setting_typed("miniapp_sticker_error")) is None
    assert "фото" in message.answers_sent[-1]


def test_photo_slot_remove_clears_key(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("miniapp_sticker_success", "AgAC777"))
    callback = FakeCallback("miniapp_theme_remove_photo:sticker_success")
    asyncio.run(admin_miniapp_theme.miniapp_theme_remove_photo(callback))
    assert asyncio.run(get_setting_typed("miniapp_sticker_success")) is None
    assert callback.message.edit_calls == 1


def test_photo_slot_remove_unknown_slot_rejected(tmp_path):
    _admin_ready(tmp_path)
    callback = FakeCallback("miniapp_theme_remove_photo:unknown")
    asyncio.run(admin_miniapp_theme.miniapp_theme_remove_photo(callback))
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"


def test_asset_slot_buttons_show_remove_only_when_set(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_miniapp_theme.build_miniapp_theme_keyboard())
    data = _flat_callback_data(kb)
    assert "miniapp_theme_photo:logo" in data
    assert "miniapp_theme_remove_photo:logo" not in data  # ничего не загружено

    asyncio.run(db.set_setting("miniapp_logo", "AgAC999"))
    kb = asyncio.run(admin_miniapp_theme.build_miniapp_theme_keyboard())
    data = _flat_callback_data(kb)
    assert "miniapp_theme_remove_photo:logo" in data


# ── отмена ────────────────────────────────────────────────────────────────────────────────

def test_cancel_edit_clears_state_and_rerenders_theme_screen(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(MiniAppTheme.color))
    callback = FakeCallback("miniapp_theme_cancel_edit")
    asyncio.run(admin_miniapp_theme.miniapp_theme_cancel_edit(callback, state))
    assert asyncio.run(state.get_state()) is None
    assert callback.message.edit_calls == 1
    assert "Пресеты и ручки" in callback.message.text_edited


# ── «бот для людей»: нет сырых ключей нигде в тексте/подписях второго шва ──────────────────

def test_theme_screen_text_and_labels_have_no_raw_keys(tmp_path):
    _admin_ready(tmp_path)
    text = asyncio.run(admin_miniapp_theme.render_miniapp_theme_text())
    assert "miniapp_" not in text
    assert "raleway_italic" not in text  # код начертания -- только человеческая подпись
    assert "bluebook" not in text  # код пресета (строчными) -- только "АЙСЕК — классика"

    kb = asyncio.run(admin_miniapp_theme.build_miniapp_theme_keyboard())
    for row in kb.inline_keyboard:
        for btn in row:
            assert "miniapp_" not in btn.text
            assert "raleway_italic" not in btn.text
            assert "bluebook" not in btn.text


# ── ADMIN_CAPS: второй шов ───────────────────────────────────────────────────────────────

def test_theme_seam_callbacks_registered_under_settings():
    expected = {
        "miniapp_theme_open": "settings",
        "miniapp_theme_noop": "settings",
        "miniapp_theme_cancel_edit": "settings",
        "miniapp_preset:*": "settings",
        "miniapp_preset_apply:*": "settings",
        "miniapp_preset_cancel": "settings",
        "miniapp_theme_reset": "settings",
        "miniapp_theme_reset_go": "settings",
        "miniapp_theme_color:*": "settings",
        "miniapp_theme_font:*": "settings",
        "miniapp_theme_toggle_playful": "settings",
        "miniapp_theme_toggle_pattern": "settings",
        "miniapp_theme_photo:*": "settings",
        "miniapp_theme_remove_photo:*": "settings",
        "state:MiniAppTheme:*": "settings",
    }
    for key, cap in expected.items():
        assert ADMIN_CAPS.get(key) == cap, key

    assert required_capability(callback_data="miniapp_preset:bluebook") == "settings"
    assert required_capability(callback_data="miniapp_theme_photo:sticker_top1") == "settings"
    assert required_capability(callback_data="miniapp_theme_remove_photo:cover") == "settings"
    assert required_capability(raw_state="MiniAppTheme:sticker_empty") == "settings"

    # старые ключи не оставлены мёртвыми записями в реестре прав
    for stale_key in ("miniapp_edit_accent", "miniapp_edit_logo", "miniapp_remove_logo", "miniapp_cancel_edit"):
        assert stale_key not in ADMIN_CAPS
