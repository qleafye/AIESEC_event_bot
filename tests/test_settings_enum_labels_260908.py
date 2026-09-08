"""UAT 07.09 (T-d6t-05): человеческие подписи вариантов enum-настроек живут в реестре
(`settings_schema.SETTINGS_SCHEMA[key]["option_labels"]`), а не тремя копиями в хендлерах
бота. Сторож формулируется ОТ РЕЕСТРА («каждый enum вне on/off обязан иметь подписи»), не
списком ключей — новый enum без подписей роняет набор сам.

Стиль — `tests/test_settings_multi_260906.py`; харнесс API-части — `tests/test_miniapp_settings_api.py`
/ `tests/test_miniapp_settings_registry.py`.
"""
from __future__ import annotations

import asyncio

import web_theme
from handlers.admin_miniapp_theme import _FONT_LABELS, _PRESET_LABELS
from handlers.admin_settings import _enum_human_label
from settings_schema import SETTINGS_SCHEMA, option_label, option_labels

from tests.test_miniapp_routes import (
    ADMIN_ID,
    _cfg,
    _client,
    _hdr,
    _standard_seed,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


def _is_on_off(options) -> bool:
    opts = options or []
    return len(opts) == 2 and "on" in opts and "off" in opts


def _non_on_off_enum_keys() -> list[str]:
    return [
        key for key, meta in SETTINGS_SCHEMA.items()
        if meta.get("type") == "enum" and not _is_on_off(meta.get("options"))
    ]


# ── Сторож реестра: КАЖДЫЙ enum вне on/off обязан иметь подписи ────────────────────────────

def test_every_non_on_off_enum_has_option_labels():
    keys = _non_on_off_enum_keys()
    assert keys, "в реестре должен быть хотя бы один enum-ключ вне пары on/off"
    for key in keys:
        meta = SETTINGS_SCHEMA[key]
        labels = meta.get("option_labels")
        assert labels, f"{key}: нет option_labels"
        assert set(labels) == set(meta["options"]), f"{key}: подписи не покрывают все options"
        for code, label in labels.items():
            assert label, f"{key}:{code} — пустая подпись"
            assert label != code, f"{key}:{code} — подпись совпадает с кодом"


def test_option_labels_youlead_is_cyrillic_brand():
    assert option_labels("miniapp_theme_preset")["youlead"] == "ЮЛид"


def test_option_label_known_and_unknown_code():
    assert option_label("registration_mode", "short") == "⚡ Краткая"
    assert option_label("registration_mode", "unknown-code") == "unknown-code"


def test_option_labels_empty_for_on_off_and_unknown_key():
    assert option_labels("event_city_enabled") == {}
    assert option_labels("no-such-key") == {}


def test_option_labels_normalizes_per_city_composite_key():
    assert option_labels("registration_mode__city__msk") == option_labels("registration_mode")


# ── API настроек: spec несёт option_labels, options остаётся кодами ────────────────────────

def _setup(tmp_path, name="settings_enum_labels_260908.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    return _client(_cfg(db_path))


def _items(body) -> list[dict]:
    out = []
    for section in body["sections"]:
        out.extend(section["toggles"])
        for group in section["groups"]:
            out.extend(group["items"])
    return out


def _item(body, key) -> dict:
    return next(i for i in _items(body) if i["key"] == key)


def test_theme_preset_spec_has_all_four_option_labels_and_coded_options(tmp_path):
    client = _setup(tmp_path)
    body = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    item = _item(body, "miniapp_theme_preset")
    assert set(item["options"]) == {"bluebook", "youlead", "realtalk", "custom"}
    assert item["option_labels"]["youlead"] == "ЮЛид"
    assert len(item["option_labels"]) == 4


def test_theme_preset_display_is_label_not_code(tmp_path):
    client = _setup(tmp_path)
    from tests.test_miniapp_routes import _set
    _set("miniapp_theme_preset", "youlead")
    body = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    item = _item(body, "miniapp_theme_preset")
    assert item["display"] == "ЮЛид"


def test_on_off_key_has_no_option_labels_and_unchanged_display(tmp_path):
    client = _setup(tmp_path)
    from tests.test_miniapp_routes import _set
    _set("event_city_enabled", "on")
    body = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    item = _item(body, "event_city_enabled")
    assert item["option_labels"] is None
    assert item["display"] == "on"


# ── Бот: три места чтения подписей не разъехались от реестра ───────────────────────────────

def test_preset_labels_match_registry_and_web_theme_presets_exactly():
    """Сторож tests/test_ru_brand_wording_260824.py:88 держит равенство с web_theme.PRESETS —
    здесь проверяем, что значения совпадают со СЛОВОМ В СЛОВО подписью реестра."""
    assert set(_PRESET_LABELS) == set(web_theme.PRESETS)
    for name in web_theme.PRESETS:
        assert _PRESET_LABELS[name] == option_label("miniapp_theme_preset", name)


def test_font_labels_match_registry():
    assert _FONT_LABELS == option_labels("miniapp_theme_heading_font")


def test_enum_human_label_registration_mode_unchanged():
    assert _enum_human_label("registration_mode", "short") == "⚡ Краткая"
    assert _enum_human_label("registration_mode", "full") == "📋 Полная"


def test_enum_human_label_on_off_unchanged_for_any_key():
    assert _enum_human_label("any_key_without_labels", "on") == "✅ Вкл"
    assert _enum_human_label("any_key_without_labels", "off") == "❌ Выкл"
