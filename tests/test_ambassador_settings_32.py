"""Phase 32 Plan 02 (D-03/D-04/D-05/D-18/D-19/D-21/D-29/D-35): реестр амбассадорского слоя —
числа, тумблеры и делегатские/менеджерские тексты волн.

Проверяет:
- дефолты новых числовых/enum ключей безопасны для живых событий (0/0/3/"on"/"off");
- новые ключи группы `game` видны на экране «🎮 Геймификация» (`_GAME_FIELD_ORDER`) — КРОМЕ
  `wave_rating_show_names`, который редактируется отдельной кнопкой-тумблером (CLAUDE.md:
  выбор из готового набора — кнопкой, а не вводом кода "on"/"off"; см. докстринг ключа в
  settings_schema.py и `handlers.admin_settings.toggle_wave_rating_show_names`);
- `dashboard_block_ambassadors` НЕ попал ни в `_GAME_FIELD_ORDER`, ни куда-либо ещё в
  handlers/admin_settings.py — свой экран у группы `dashboard` (handlers/admin_dashboard.py,
  план 32-09);
- делегатские тексты переводятся машинным переводом, `wave_end_manager_text` — нет;
- в дефолтах нет латиницы брендов («AIESEC»/«YouLead»).
"""
from __future__ import annotations

import asyncio

import services.i18n_sources as i18n_sources
from config import config
from database import db
from handlers.admin_settings import _GAME_FIELD_ORDER
from settings_schema import SETTINGS_SCHEMA, get_setting_typed


def _db_ready(tmp_path, name="test_ambassador_settings_32.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── Числа и тумблеры (Task 1) ───────────────────────────────────────────────────────────────

_NUMERIC_DEFAULTS = {
    "ambassador_referral_coins": 0,
    "game_late_penalty_percent": 0,
    "wave_prize_places": 3,
    "wave_rating_show_names": "on",
    "dashboard_block_ambassadors": "off",
}


def test_numeric_and_toggle_keys_present_with_safe_defaults():
    for key, expected_default in _NUMERIC_DEFAULTS.items():
        assert key in SETTINGS_SCHEMA, key
        entry = SETTINGS_SCHEMA[key]
        assert isinstance(entry["label"], str) and entry["label"].strip(), key
        assert entry["default"] == expected_default, key


def test_ambassador_referral_coins_and_late_penalty_are_int_type():
    for key in ("ambassador_referral_coins", "game_late_penalty_percent", "wave_prize_places"):
        assert SETTINGS_SCHEMA[key]["type"] == "int", key
        assert isinstance(SETTINGS_SCHEMA[key]["prompt"], str) and SETTINGS_SCHEMA[key]["prompt"].strip(), key


def test_wave_rating_show_names_and_dashboard_block_ambassadors_are_enum_on_off():
    for key in ("wave_rating_show_names", "dashboard_block_ambassadors"):
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "enum", key
        assert entry["options"] == ["on", "off"], key


def test_game_late_penalty_percent_hint_explains_flat_penalty():
    prompt = SETTINGS_SCHEMA["game_late_penalty_percent"]["prompt"]
    assert "не зависит от того, насколько поздно" in prompt


def test_wave_rating_show_names_hint_explains_when_to_turn_off():
    prompt = SETTINGS_SCHEMA["wave_rating_show_names"]["prompt"]
    assert prompt and "выключайте" in prompt.lower()


def test_dashboard_block_ambassadors_not_in_admin_settings_field_order():
    # Тот же приём, что у dashboard_block_game/остальных dashboard_block_* — свой экран
    # (handlers/admin_dashboard.py, план 32-09), НЕ generic settings_edit.
    assert "dashboard_block_ambassadors" not in _GAME_FIELD_ORDER


def test_wave_rating_show_names_deliberately_excluded_from_field_order():
    # Деviация от буквального текста плана (Rule 2/CLAUDE.md): переключатель «одной кнопкой»
    # (D-29) не может идти через общий ввод текста "on"/"off" — правится
    # toggle_wave_rating_show_names, отдельная кнопка на экране «🎮 Геймификация».
    assert "wave_rating_show_names" not in _GAME_FIELD_ORDER


# ── Каждый ключ группы game либо в _GAME_FIELD_ORDER, либо в известном списке исключений ────

_KNOWN_GAME_GROUP_TOGGLE_EXCEPTIONS = {
    "game_submit_notify_mode",  # Quick 260822 — тумблер, отдельная кнопка
    "wave_rating_show_names",  # Phase 32-02 (D-29) — тумблер, отдельная кнопка
}


def test_every_game_group_key_in_field_order_or_known_toggle_exception():
    game_keys = {k for k, v in SETTINGS_SCHEMA.items() if v.get("group") == "game"}
    covered = set(_GAME_FIELD_ORDER) | _KNOWN_GAME_GROUP_TOGGLE_EXCEPTIONS
    missing = game_keys - covered
    assert not missing, f"ключи группы game без места на экране: {missing}"
    phantom = set(_GAME_FIELD_ORDER) - game_keys
    assert not phantom, f"в _GAME_FIELD_ORDER есть ключ, которого нет в схеме: {phantom}"
    # без дублей
    assert len(_GAME_FIELD_ORDER) == len(set(_GAME_FIELD_ORDER))


def test_ambassador_referral_coins_db_roundtrip(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        empty = await get_setting_typed("ambassador_referral_coins")
        assert empty == 0
        await db.set_setting("ambassador_referral_coins", "50")
        after = await get_setting_typed("ambassador_referral_coins")
        assert after == 50
        assert isinstance(after, int)

    asyncio.run(_run())


# ── Тексты волн и граница машинного перевода (Task 2) ───────────────────────────────────────

_DELEGATE_TEXT_KEYS = [
    "wave_start_message_text", "wave_start_button_text", "wave_deadline_reminder_text",
    "wave_results_announce_text", "wave_results_winner_text", "wave_results_prize_text",
    "wave_rating_header_text", "wave_rating_own_line_text", "wave_rating_closed_text",
    "ambassador_block_header_text", "game_task_no_deadline_text", "game_task_penalty_hint_text",
    "ambassador_path_prompt_text", "ambassador_path_label_invite", "ambassador_path_label_content",
    "ambassador_path_label_none", "ambassador_leave_button_text", "ambassador_leave_confirm_text",
    "ambassador_leave_done_text",
]

_MANAGER_TEXT_KEYS = ["wave_end_manager_text"]


def test_all_new_text_keys_have_non_empty_russian_defaults():
    for key in _DELEGATE_TEXT_KEYS + _MANAGER_TEXT_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text", key
        default = entry["default"]
        assert isinstance(default, str) and default.strip(), key


def test_placeholders_named_in_prompt_appear_in_default():
    import re

    for key in _DELEGATE_TEXT_KEYS + _MANAGER_TEXT_KEYS:
        entry = SETTINGS_SCHEMA[key]
        prompt = entry.get("prompt") or ""
        default = entry["default"]
        # плейсхолдеры, названные в подсказке форматом "{name} — ..."
        named = set(re.findall(r"\{(\w+)\}\s*—", prompt))
        present = set(re.findall(r"\{(\w+)\}", default))
        missing = named - present
        assert not missing, f"{key}: подсказка обещает {missing}, в дефолте их нет"


def test_game_task_penalty_hint_text_has_penalized_and_coins_placeholders():
    default = SETTINGS_SCHEMA["game_task_penalty_hint_text"]["default"]
    assert "{penalized}" in default
    assert "{coins}" in default


def test_ambassador_leave_confirm_text_mentions_points_stay_and_current_wave_rating():
    default = SETTINGS_SCHEMA["ambassador_leave_confirm_text"]["default"]
    assert "баллы останутся" in default
    assert "рейтинга текущей волны" in default


def test_no_latin_brand_names_in_new_defaults():
    for key in _DELEGATE_TEXT_KEYS + _MANAGER_TEXT_KEYS:
        default = SETTINGS_SCHEMA[key]["default"]
        assert "AIESEC" not in default, key
        assert "YouLead" not in default, key


def test_wave_end_manager_text_is_admin_only():
    assert "wave_end_manager_text" in i18n_sources._ADMIN_ONLY_GAME_KEYS


def test_delegate_texts_are_not_admin_only():
    for key in _DELEGATE_TEXT_KEYS:
        assert key not in i18n_sources._ADMIN_ONLY_GAME_KEYS, key


def test_is_delegate_dynamic_key_true_for_delegate_wave_texts():
    for key in (
        "wave_start_message_text", "wave_results_announce_text", "game_task_penalty_hint_text",
    ):
        assert i18n_sources.is_delegate_dynamic_key(key), key


def test_is_delegate_dynamic_key_false_for_manager_wave_text():
    assert not i18n_sources.is_delegate_dynamic_key("wave_end_manager_text")


def test_every_game_group_key_is_delegate_or_explicitly_admin_only():
    """Каждый text/list-ключ группы game из схемы либо делегатский (проходит через
    `delegate_registry_keys()`), либо явно в `_ADMIN_ONLY_GAME_KEYS` — без молчаливой дыры."""
    for key, spec in SETTINGS_SCHEMA.items():
        if spec.get("group") != "game" or spec.get("type") not in ("text", "list"):
            continue
        is_delegate = key in i18n_sources.delegate_registry_keys()
        is_admin_only = key in i18n_sources._ADMIN_ONLY_GAME_KEYS
        assert is_delegate or is_admin_only, f"{key}: ни делегатский, ни явно админский"
        assert not (is_delegate and is_admin_only), f"{key}: и делегатский, и админский одновременно"
