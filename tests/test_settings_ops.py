"""Phase 22 План 01 (WEB-SET-01/04, D-12): снимок поведения переносимых функций —
написан ДО переноса `_apply_event_type_preset`/`_SHEET_TAB_WRITE_MODE`/`HTML_SETTINGS`/
`_tab_confirm_text`/... из `handlers/admin_settings.py` в корневой aiogram-free `settings_ops.py`.

Пока `settings_ops.py` не создан (задача 2 плана), импорт на уровне модуля падает
`ModuleNotFoundError` — это и есть Wave 0 RED-снимок.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — асинхронные
проверки гонятся через asyncio.run(), config.DB_PATH указывает на tmp_path.
"""
import asyncio

import pytest

import settings_ops
from settings_schema import SETTINGS_SCHEMA
from tests.test_miniapp_labels_drift import _loaded_aiogram


ADMIN_ID = 900022


def _use_tmp_db(tmp_path):
    from config import config
    config.DB_PATH = str(tmp_path / "test_settings_ops.db")


# ── модуль aiogram-free (сторож T-22-06 / D-12) ──────────────────────────────────────────

def test_settings_ops_module_does_not_load_aiogram():
    loaded = _loaded_aiogram("import settings_ops")
    assert loaded == [], f"settings_ops потянул aiogram: {loaded}"


# ── перенос, а не копия: HTML_SETTINGS — тот же объект, что видит бот ────────────────────

def test_html_settings_is_same_object_as_bot():
    from handlers import admin_settings
    assert admin_settings.HTML_SETTINGS is settings_ops.HTML_SETTINGS


# ── SHEET_TAB_WRITE_MODE — ровно шесть ключей с теми же режимами ────────────────────────

def test_sheet_tab_write_mode_has_exactly_six_keys_with_expected_modes():
    assert settings_ops.SHEET_TAB_WRITE_MODE == {
        "main_sheet_tab": "rewrite",
        "incomplete_sheet_tab": "rewrite",
        "game_matrix_tab": "rewrite",
        "game_history_tab": "rewrite",
        "short_sheet_tab": "append",
        "party_sheet_tab": "append",
    }
    assert "preselect_tab" not in settings_ops.SHEET_TAB_WRITE_MODE
    assert not any(k.startswith("city_tab_suffix__") for k in settings_ops.SHEET_TAB_WRITE_MODE)


# ── apply_event_type_preset ───────────────────────────────────────────────────────────────

def test_apply_event_type_preset_conference_turns_payment_and_consent_on(tmp_path):
    from database import db
    from database.db import get_setting

    async def _run():
        _use_tmp_db(tmp_path)
        await db.init_db()
        await settings_ops.apply_event_type_preset("conference")
        assert await get_setting("payment_enabled") == "on"
        assert await get_setting("consent_enabled") == "on"

    asyncio.run(_run())


def test_apply_event_type_preset_forum_turns_payment_and_consent_off(tmp_path):
    from database import db
    from database.db import get_setting

    async def _run():
        _use_tmp_db(tmp_path)
        await db.init_db()
        await settings_ops.apply_event_type_preset("forum")
        assert await get_setting("payment_enabled") == "off"
        assert await get_setting("consent_enabled") == "off"

    asyncio.run(_run())


def test_apply_event_type_preset_custom_does_not_change_values(tmp_path):
    from database import db
    from database.db import get_setting, set_setting

    async def _run():
        _use_tmp_db(tmp_path)
        await db.init_db()
        await set_setting("payment_enabled", "on")
        await set_setting("consent_enabled", "off")
        await settings_ops.apply_event_type_preset("custom")
        assert await get_setting("payment_enabled") == "on"
        assert await get_setting("consent_enabled") == "off"

    asyncio.run(_run())


# ── tab_confirm_text_html — эталонные строки символ в символ (снимок текущего бота) ──────

def test_tab_confirm_text_html_append_key_mentions_dopisyvat():
    text = settings_ops.tab_confirm_text_html("short_sheet_tab", "Краткая", 5)
    assert "дописывать" in text
    assert "перезаписывать" not in text


def test_tab_confirm_text_html_rewrite_key_mentions_perezapisyvat():
    text = settings_ops.tab_confirm_text_html("game_matrix_tab", "Гейма", 5)
    assert "перезаписывать" in text
    assert "дописывать" not in text


def test_tab_confirm_text_html_main_sheet_tab_mentions_peresobrat():
    text = settings_ops.tab_confirm_text_html("main_sheet_tab", "Регистрации", 5)
    assert "перезаписывать" in text
    assert "Пересобрать таблицу" in text


# ── plain_text — снятие HTML-тегов + разэкранирование сущностей ─────────────────────────

def test_plain_text_strips_tags_and_unescapes_entities():
    html = '<b>Вкладка «А»</b> &amp; строки'
    assert settings_ops.plain_text(html) == 'Вкладка «А» & строки'


# ── base_setting_key ──────────────────────────────────────────────────────────────────────

def test_base_setting_key_strips_per_city_suffix():
    assert settings_ops.base_setting_key("start_text__city__msk") == "start_text"
    assert settings_ops.base_setting_key("start_text") == "start_text"


# ── Task 3: множество ключей веба, карта разделов, опасные ключи (D-01/D-02/D-08/D-13) ────

# (а) editable_keys() не содержит ни одного ключа группы roles и содержит ровно
# len(SETTINGS_SCHEMA) - len(группа roles) ключей — формулой, не литералом (план 22-02 той
# же волны добавляет ~42 ключа группы miniapp).

def test_editable_keys_excludes_roles_group_by_formula():
    roles_keys = [k for k, m in SETTINGS_SCHEMA.items() if m.get("group") == "roles"]
    keys = settings_ops.editable_keys()
    assert not any(k in roles_keys for k in keys)
    assert len(keys) == len(SETTINGS_SCHEMA) - len(roles_keys)
    assert len(keys) == len(set(keys)), "editable_keys() не должен задваивать ключи"


# (б) каждый ключ editable_keys() достижим ровно через одну пару (раздел, группа) по
# SECTION_GROUPS/TOGGLE_SECTION — ни одного потерянного, ни одного задвоенного.

def test_every_editable_key_reachable_exactly_once_via_section_maps():
    reach_count: dict[str, int] = {}

    for _section, _label, groups in settings_ops.SECTION_GROUPS:
        for group in groups:
            for key, meta in SETTINGS_SCHEMA.items():
                if meta.get("group") == group:
                    reach_count[key] = reach_count.get(key, 0) + 1

    for key, section in settings_ops.TOGGLE_SECTION.items():
        assert section in {s for s, _l, _g in settings_ops.SECTION_GROUPS}
        reach_count[key] = reach_count.get(key, 0) + 1

    for key in settings_ops.editable_keys():
        assert reach_count.get(key, 0) == 1, f"{key}: достижим {reach_count.get(key, 0)} раз(а), ожидалось 1"


# (в) все ключи группы toggles распределены TOGGLE_SECTION ровно один раз.

def test_toggle_section_covers_every_toggles_group_key_exactly_once():
    toggle_keys = [k for k, m in SETTINGS_SCHEMA.items() if m.get("group") == "toggles"]
    assert len(settings_ops.TOGGLE_SECTION) == 21
    assert sorted(settings_ops.TOGGLE_SECTION) == sorted(toggle_keys)
    assert len(settings_ops.TOGGLE_SECTION) == len(set(settings_ops.TOGGLE_SECTION))


# (г) DANGEROUS_KEYS содержит все ключи SHEET_TAB_WRITE_MODE и обе пары DANGER_CONFIRM.

def test_dangerous_keys_covers_sheet_tabs_and_miniapp_danger_confirm_pair():
    assert set(settings_ops.SHEET_TAB_WRITE_MODE).issubset(settings_ops.DANGEROUS_KEYS)
    assert "miniapp_enabled" in settings_ops.DANGEROUS_KEYS
    assert "miniapp_staff_only" in settings_ops.DANGEROUS_KEYS
    assert settings_ops.dangerous_confirm_key("miniapp_enabled", "off") == "miniapp_confirm_disable_text"
    assert settings_ops.dangerous_confirm_key("miniapp_staff_only", "on") == "miniapp_confirm_staff_only_text"
    assert settings_ops.dangerous_confirm_key("miniapp_enabled", "on") is None


# (д) item_spec не возвращает ни None-подписи, ни код ключа в label.

def test_item_spec_never_returns_none_label_or_key_code_in_label():
    for key in settings_ops.editable_keys():
        spec = settings_ops.item_spec(key, raw=None, value=None, is_default=True)
        assert spec["label"] is not None
        assert "_" not in spec["label"], f"{key}: label содержит код ключа — {spec['label']!r}"


def test_editable_keys_count_matches_formula():
    roles_count = len([k for k, m in SETTINGS_SCHEMA.items() if m.get("group") == "roles"])
    assert len(settings_ops.editable_keys()) == len(SETTINGS_SCHEMA) - roles_count


# (е) Phase 22 Plan 07 (D-16): SETTINGS_MAIN_SECTIONS — подмножество кодов SECTION_GROUPS,
# непустое и не покрывающее все разделы (иначе второй ряд «Реже» стартового экрана всегда
# пуст — сигнал, что константу забыли поправить вместе со структурой разделов).

def test_settings_main_sections_is_a_proper_nonempty_subset_of_section_groups():
    all_tokens = {token for token, _label, _groups in settings_ops.SECTION_GROUPS}
    assert settings_ops.SETTINGS_MAIN_SECTIONS
    assert settings_ops.SETTINGS_MAIN_SECTIONS.issubset(all_tokens)
    assert settings_ops.SETTINGS_MAIN_SECTIONS != all_tokens


def test_no_key_code_in_any_editable_label():
    for key in settings_ops.editable_keys():
        label = SETTINGS_SCHEMA[settings_ops.base_setting_key(key)]["label"]
        assert "_" not in label
