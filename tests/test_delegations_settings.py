"""Делегации вузов — ключи реестра настроек модуля.

Одиннадцать ключей в существующих группах `apps` (служебные, отсечка, курсы, тумблер) и `reg`
(три текста делегату): тип/группа/дефолт, типизированное чтение на чистой БД, служебные ключи
скрыты из веб-настроек, у трёх делегатских текстов есть рукописный EN с теми же плейсхолдерами,
бренды в русских дефолтах — кириллицей.
"""
import asyncio
from datetime import datetime

import settings_ops
from config import config
from services.i18n_form_manual import FORM_DEFAULT_EN
from settings_schema import SETTINGS_SCHEMA, _parse_setting, get_setting_typed
from tests._dbtpl import fast_init_db

SERVICE_KEYS = (
    "delegation_form_id", "delegation_q_fullname", "delegation_q_university",
    "delegation_q_course", "delegation_q_email",
)
REG_TEXT_KEYS = (
    "delegation_welcome_text", "delegation_welcome_existing_text", "delegation_game_off_text",
)
ALL_KEYS = SERVICE_KEYS + (
    "delegation_ta_cutoff", "delegation_not_ta_courses", "delegation_game_enabled",
) + REG_TEXT_KEYS


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "settings.db")
    fast_init_db()


def test_all_eleven_keys_registered_with_expected_shape():
    assert len(ALL_KEYS) == 11
    for key in ALL_KEYS:
        assert key in SETTINGS_SCHEMA, key
    for key in SERVICE_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert (entry["type"], entry["group"], entry["default"]) == ("text", "apps", None), key
        assert "служебный" in entry["label"]
    cutoff = SETTINGS_SCHEMA["delegation_ta_cutoff"]
    assert (cutoff["type"], cutoff["group"], cutoff["default"]) == ("date_only", "apps", "23.09.2026")
    assert "ДД.ММ.ГГГГ" in cutoff["prompt"] and "23.09.2026" in cutoff["prompt"]
    courses = SETTINGS_SCHEMA["delegation_not_ta_courses"]
    assert (courses["type"], courses["group"]) == ("list", "apps")
    assert courses["options_from_step"] == "course"
    assert courses["default"] == ["1", "2"]
    game = SETTINGS_SCHEMA["delegation_game_enabled"]
    assert (game["type"], game["group"], game["default"]) == ("enum", "apps", "off")
    assert game["options"] == ["on", "off"]
    for key in REG_TEXT_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert (entry["type"], entry["group"]) == ("text", "reg"), key
        assert entry["default"], key


def test_typed_defaults_on_fresh_db(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(get_setting_typed("delegation_game_enabled")) == "off"
    assert asyncio.run(get_setting_typed("delegation_not_ta_courses")) == ["1", "2"]
    assert asyncio.run(get_setting_typed("delegation_form_id")) is None
    for key in REG_TEXT_KEYS:
        assert asyncio.run(get_setting_typed(key)) == SETTINGS_SCHEMA[key]["default"]


def test_ta_cutoff_default_parses_both_ways():
    # Строка-дефолт разбирается парсером типа date_only в 23.09.2026…
    parsed = _parse_setting("delegation_ta_cutoff", SETTINGS_SCHEMA["delegation_ta_cutoff"]["default"])
    assert isinstance(parsed, datetime)
    assert (parsed.day, parsed.month, parsed.year) == (23, 9, 2026)
    # …а при незаполненном ключе (raw None) ветка date_only отдаёт default КАК ЕСТЬ — строкой.
    # Это зафиксированное поведение `_parse_setting`: потребители обязаны принимать и datetime,
    # и строку «ДД.ММ.ГГГГ» (см. комментарий у ключа в settings_schema.py).
    assert _parse_setting("delegation_ta_cutoff", None) == "23.09.2026"


def test_service_keys_hidden_from_web_settings():
    editable = set(settings_ops.editable_keys())
    for key in SERVICE_KEYS:
        assert key not in editable, key
    # остальные ключи модуля в вебе остаются — у них человеческие типы
    for key in ("delegation_ta_cutoff", "delegation_not_ta_courses", "delegation_game_enabled",
                *REG_TEXT_KEYS):
        assert key in editable, key


def test_reg_texts_have_manual_en_with_same_placeholders():
    for key in REG_TEXT_KEYS:
        ru = SETTINGS_SCHEMA[key]["default"].strip()
        assert ru in FORM_DEFAULT_EN, f"нет рукописного EN для {key}"
        en = FORM_DEFAULT_EN[ru]
        assert ("{university}" in ru) == ("{university}" in en), key
    assert "{university}" in SETTINGS_SCHEMA["delegation_welcome_text"]["default"]
    assert "{university}" in SETTINGS_SCHEMA["delegation_welcome_existing_text"]["default"]


def test_human_texts_use_cyrillic_brands_and_no_internal_ids():
    for key in ALL_KEYS:
        entry = SETTINGS_SCHEMA[key]
        blob = " ".join(str(entry.get(f) or "") for f in ("label", "prompt", "default"))
        assert "YouLead" not in blob and "AIESEC" not in blob, key
        assert "D-" not in blob and "квик" not in blob.lower(), key
