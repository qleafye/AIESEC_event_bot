"""Экран одной фичи: три кучи, свёрнутое редкое, без строки «наполовину»."""
import asyncio

from config import config
from domain.settings.schema import (
    PILE_DELEGATE,
    PILE_SERVICE,
    PILE_WORK,
    SETTINGS_SCHEMA,
    arrange_feature_keys,
    feature_status_lines,
    is_wiring_key,
    setting_pile,
    setting_rare,
    switch_phrase,
)
from handlers.settings import admin_settings
from tests._dbtpl import fast_init_db

ADMIN_ID = 900054


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_feature_54.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def test_pile_follows_type_and_explicit_override():
    assert setting_pile("payment_enabled") == PILE_WORK
    assert setting_pile("start_text") == PILE_DELEGATE
    assert setting_pile("google_sheet_id") == PILE_SERVICE
    assert is_wiring_key("google_sheet_id")
    sheet_text = next(
        key for key, meta in SETTINGS_SCHEMA.items()
        if meta.get("group") == "sheets" and meta.get("type") == "text"
        and not is_wiring_key(key)
    )
    assert setting_pile(sheet_text) == PILE_SERVICE
    assert not is_wiring_key(sheet_text)
    assert setting_pile("incomplete_sync_hours") == PILE_WORK
    assert setting_rare("nudge_text") is True
    error_key = next(
        key for key, meta in SETTINGS_SCHEMA.items()
        if "ошибк" in str(meta.get("label") or "").lower() and "rare" not in meta
    )
    assert setting_rare(error_key) is True


def test_status_lines_are_binary_and_name_the_empty_text():
    assert feature_status_lines(True, "да", []) == ["Подключено: да", "Включено: да"]
    missing = feature_status_lines(False, "нет", ["💬 Приветствие"])
    assert missing == ["Подключено: нет", "Включено: нет", "Не настроено: 💬 Приветствие"]
    assert "наполовину" not in "\n".join(missing)
    assert switch_phrase("registration_mode", None) == "⚡ Краткая"
    assert "short" not in switch_phrase("registration_mode", "short")
    assert "full" not in switch_phrase("registration_mode", "full")
    assert switch_phrase("payment_enabled", None) == "нет"
    assert switch_phrase(None, None) == "да"


def test_arrange_keeps_input_order_and_folds_rare():
    arranged = arrange_feature_keys(["start_text", "nudge_text", "payment_deadline", "google_sheet_id"])
    by_pile = {pile["token"]: pile["keys"] for pile in arranged["piles"]}
    assert by_pile[PILE_DELEGATE] == ["start_text"]
    assert by_pile[PILE_WORK] == ["payment_deadline"]
    assert by_pile[PILE_SERVICE] == ["google_sheet_id"]
    assert arranged["rare_keys"] == ["nudge_text"]
    assert [pile["label"] for pile in arranged["piles"]] == [
        "Как работает", "Что видит делегат", "Служебное",
    ]


def test_reg_screen_has_three_piles_and_hides_error_copy(tmp_path):
    _ready(tmp_path)
    text = asyncio.run(admin_settings.render_settings_group_text("reg"))
    rare = asyncio.run(admin_settings.render_settings_group_text("reg", rare_only=True))
    kb = asyncio.run(admin_settings.build_settings_group_keyboard("reg"))
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]

    for title in ("Подключено:", "Включено:", "Как работает", "Что видит делегат", "Служебное"):
        assert title in text
    assert "наполовину" not in text
    assert "наполовину" not in rare
    assert "registration_mode" not in text
    assert "SETTINGS_SCHEMA" not in text
    assert "наполовину" not in "\n".join(b.text or "" for row in kb.inline_keyboard for b in row)

    rare_key = next(
        key for key in admin_settings._settings_group_keys("reg") if setting_rare(key)
    )
    label = SETTINGS_SCHEMA[rare_key]["label"]
    assert f"{label}:" not in text
    assert f"{label}:" in rare
    assert "settings_group:reg:rare" in flat
    for row in kb.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data.startswith("settings_edit:"):
                assert button.callback_data.split(":", 1)[1] not in button.text


def test_empty_delegate_text_is_named_on_the_event_screen(tmp_path):
    _ready(tmp_path)
    text = asyncio.run(admin_settings.render_settings_group_text("event"))
    missing = []
    for key in admin_settings._settings_group_keys("event"):
        if setting_pile(key) != PILE_DELEGATE:
            continue
        meta = SETTINGS_SCHEMA[key]
        if meta.get("type") != "text" or meta.get("default") not in (None, ""):
            continue
        missing.append(meta["label"])
    assert missing
    line = next(ln for ln in text.splitlines() if ln.startswith("Не настроено:"))
    assert missing[0] in line
    assert "наполовину" not in line


def test_half_on_stays_off_this_screen():
    import inspect

    import handlers.settings.admin_feature_screen as feature_screen

    src = inspect.getsource(feature_screen)
    assert "delegations_off_alert" not in src
    assert "admin_readiness" not in src
    assert "admin_ext_forms" not in src
