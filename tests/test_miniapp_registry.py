"""Phase 19 Plan 01 Task 1 (WEBAPP-01, D-06/D-10): реестр ключей `miniapp_*`, кнопка меню
`menu_miniapp` и константы процесса `miniapp/config.py`.

Список из 24 ключей перечислен ЯВНО: добавление 25-го обязано осознанно ломать этот тест
(контракт, на который опираются экраны планов 19-02..19-08).
"""
from __future__ import annotations

from handlers import user_actions
from handlers.admin_settings import SETTINGS_FIELDS, SETTINGS_GROUPS
from settings_schema import SETTINGS_SCHEMA

from miniapp import config as miniapp_config

MINIAPP_KEYS = [
    # тумблеры и оформление
    "miniapp_enabled",
    "miniapp_staff_only",
    "miniapp_accent",
    "miniapp_logo",
    # разделы-чекбоксы
    "miniapp_section_tasks",
    "miniapp_section_coins",
    "miniapp_section_leaderboard",
    "miniapp_section_profile",
    "miniapp_section_review",
    "miniapp_section_admin_tasks",
    "miniapp_section_stats",
    "miniapp_section_settings",
    # тексты
    "miniapp_open_text",
    "miniapp_open_button",
    "miniapp_open_in_bot_text",
    "miniapp_login_button",
    "miniapp_login_hint",
    "miniapp_session_expired_text",
    "miniapp_disabled_text",
    "miniapp_no_access_text",
    "miniapp_upload_too_large_text",
    "miniapp_profile_edit_hint",
    "miniapp_upload_caption_delegate",
    "miniapp_upload_caption_staff",
]

SECTION_KEYS = [k for k in MINIAPP_KEYS if k.startswith("miniapp_section_")]

_ALLOWED_TYPES = {"toggle", "int", "list", "date", "text", "enum", "photo", "file"}


def test_exactly_24_miniapp_keys_and_no_extra():
    assert len(MINIAPP_KEYS) == 24
    present = sorted(k for k in SETTINGS_SCHEMA if k.startswith("miniapp_"))
    assert present == sorted(MINIAPP_KEYS)


def test_every_miniapp_key_has_label_group_and_valid_type():
    for key in MINIAPP_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "miniapp", key
        assert isinstance(entry["label"], str) and entry["label"].strip(), key
        assert entry["type"] in _ALLOWED_TYPES, key
        # Кодовые значения человеку не показываем (CLAUDE.md): подпись без «miniapp_».
        assert "miniapp_" not in entry["label"], key


def test_toggle_defaults():
    assert SETTINGS_SCHEMA["miniapp_enabled"]["default"] == "off"
    assert SETTINGS_SCHEMA["miniapp_staff_only"]["default"] == "off"
    assert len(SECTION_KEYS) == 8
    for key in SECTION_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "enum" and entry["options"] == ["on", "off"], key
        assert entry["default"] == "on", key


def test_accent_and_logo_shape():
    accent = SETTINGS_SCHEMA["miniapp_accent"]
    assert accent["type"] == "text"
    assert accent["default"] == "#037EF3"
    assert "#037EF3" in accent["prompt"]  # пример формата в подсказке
    logo = SETTINGS_SCHEMA["miniapp_logo"]
    assert logo["type"] == "photo" and logo["default"] is None


def test_text_keys_have_human_defaults():
    text_keys = [
        k for k in MINIAPP_KEYS
        if SETTINGS_SCHEMA[k]["type"] == "text" and k != "miniapp_accent"
    ]
    assert len(text_keys) == 12
    for key in text_keys:
        default = SETTINGS_SCHEMA[key]["default"]
        assert isinstance(default, str) and default.strip(), key
    assert SETTINGS_SCHEMA["miniapp_open_button"]["default"] == "📱 Открыть приложение"
    assert SETTINGS_SCHEMA["miniapp_login_button"]["default"] == "Войти через Telegram"
    assert SETTINGS_SCHEMA["miniapp_upload_caption_delegate"]["default"] == "копия сдачи"
    assert SETTINGS_SCHEMA["miniapp_upload_caption_staff"]["default"] == "загружено из приложения"


def test_miniapp_keys_not_in_settings_fields_or_groups():
    """Своя поверхность правки (план 19-08) — иначе ключи всплывут в «📦 Прочие»."""
    field_keys = {k for k, _, _ in SETTINGS_FIELDS}
    group_keys = {k for _, _, keys in SETTINGS_GROUPS for k in keys}
    for key in MINIAPP_KEYS:
        assert key not in field_keys, key
        assert key not in group_keys, key


def test_menu_miniapp_mirrors_menu_game_tasks():
    menu = SETTINGS_SCHEMA["menu_miniapp"]
    ref = SETTINGS_SCHEMA["menu_game_tasks"]
    assert menu["label"] == "📱 Приложение"
    for field in ("type", "group", "options", "default", "per_city", "prompt"):
        assert menu[field] == ref[field], field
    assert set(menu) == set(ref)
    assert menu["group"] == "menu"


def test_menu_miniapp_placed_next_to_menu_game_tasks():
    keys = list(SETTINGS_SCHEMA)
    assert keys.index("menu_miniapp") == keys.index("menu_game_tasks") + 1


def test_limits_match_bot():
    assert miniapp_config.MAX_PARTS == user_actions.MAX_PARTS
    assert miniapp_config.MAX_TEXT_PART == user_actions.MAX_TEXT_PART
    assert miniapp_config.MAX_UPLOAD_BYTES == 20 * 1024 * 1024
    assert miniapp_config.PHOTO_MAX_BYTES == 10 * 1024 * 1024
    assert miniapp_config.INIT_DATA_MAX_AGE == 86400


def test_load_miniapp_config_wraps_dashboard_config():
    cfg = miniapp_config.load_miniapp_config({
        "DASHBOARD_SESSION_SECRET": "s",
        "BOT_TOKEN": "1:x",
        "ADMIN_IDS": "[1, 2]",
    })
    assert isinstance(cfg, miniapp_config.DashboardConfig)
    assert cfg.admin_ids == (1, 2)
    assert cfg.bot_token == "1:x"
