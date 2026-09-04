"""Quick 260904-de4 Task 4 (E5, второй корень) — паритет «пресет в вебе == пресет в боте».

До этой правки `POST /app/api/admin/theme/preview` и `settings_batch` читали ручки оформления
через `get_setting_typed` — незаданная ручка приходила ДЕФОЛТОМ реестра (`miniapp_accent`),
`web_theme.resolve_theme` считал любое валидное значение явной ручкой, и пресет в вебе никогда
не побеждал (превью «РилТолк» отдавало accent `#037EF3` вместо `#7552CC`). Бот при выборе
пресета пишет ВСЕ его ручки (`handlers/admin_miniapp_theme.py::miniapp_preset_apply`) —
`web_theme.preset_handle_writes` даёт вебу тот же приём: сырые чтения + серверная дозапись
ручек пресета после успешной фазы 2 `settings_batch`.

Харнесс — `tests/test_miniapp_routes.py`, образец стиля — `tests/test_miniapp_settings_batch.py`.
"""
from __future__ import annotations

import asyncio

import web_theme
from database import db as bot_db

from tests.test_miniapp_routes import ADMIN_ID, _cfg, _client, _hdr, _seed, _set, _standard_seed, _use_tmp_db

SETTINGS_MANAGER_SPB = 900610


def _run(coro):
    return asyncio.run(coro)


def _raw(key):
    return _run(bot_db.get_setting(key))


def _setup(tmp_path, name="miniapp_theme_preset_260904.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _seed(staff=[(SETTINGS_MANAGER_SPB, "reg_manager", "spb")])
    _set("role_caps_reg_manager", "moderate_reg\nsettings")
    return _client(_cfg(db_path))


def _preview(client, changes, user=ADMIN_ID):
    return client.post(
        "/app/api/admin/theme/preview", json={"changes": changes}, headers=_hdr(user),
    )


def _batch(client, changes, *, base=None, confirm=None, user=ADMIN_ID):
    body = {"changes": [{"key": k, "value": v} for k, v in changes], "base": base or {}, "confirm": confirm or []}
    return client.post("/app/api/admin/settings/batch", json=body, headers=_hdr(user))


# ── preset_handle_writes (чистая функция) ────────────────────────────────────────────────

def test_preset_handle_writes_returns_all_handles_except_preset_key_and_skip():
    writes = web_theme.preset_handle_writes("realtalk")
    assert writes[web_theme.THEME_KEYS["accent"]] == web_theme.PRESETS["realtalk"]["accent"]
    assert web_theme.THEME_KEYS["preset"] not in writes


def test_preset_handle_writes_respects_skip_keys():
    skip = {web_theme.THEME_KEYS["pattern_enabled"]}
    writes = web_theme.preset_handle_writes("youlead", skip_keys=skip)
    assert web_theme.THEME_KEYS["pattern_enabled"] not in writes
    assert web_theme.THEME_KEYS["accent"] in writes


def test_preset_handle_writes_unknown_name_is_empty():
    assert web_theme.preset_handle_writes("no-such-preset") == {}


# ── theme_preview: пресет без ручек побеждает дефолт реестра ────────────────────────────

def test_preview_preset_without_handles_shows_preset_accent_not_registry_default(tmp_path):
    client = _setup(tmp_path)
    resp = _preview(client, [{"key": "miniapp_theme_preset", "value": "realtalk"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["vars"]["light"]["--accent"] == web_theme.PRESETS["realtalk"]["accent"]


def test_preview_matches_what_batch_would_save(tmp_path):
    """Превью обязано отдавать РОВНО то, что даст «Сохранить» — иначе менеджер видит одно,
    получает другое."""
    client = _setup(tmp_path)
    preview = _preview(client, [{"key": "miniapp_theme_preset", "value": "youlead"}]).json()
    resp = _batch(client, [("miniapp_theme_preset", "youlead")])
    assert resp.status_code == 200, resp.text
    assert _raw(web_theme.THEME_KEYS["accent"]) == web_theme.PRESETS["youlead"]["accent"]
    assert preview["vars"]["light"]["--accent"] == web_theme.PRESETS["youlead"]["accent"]


# ── settings_batch: сохранение пресета пишет все его ручки (паритет с ботом) ────────────

def test_batch_preset_alone_writes_all_handles_like_bot_button(tmp_path):
    client = _setup(tmp_path)
    resp = _batch(client, [("miniapp_theme_preset", "realtalk")])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "miniapp_theme_preset" in body["saved"]
    assert web_theme.THEME_KEYS["accent"] in body["saved"]
    assert _raw(web_theme.THEME_KEYS["accent"]) == web_theme.PRESETS["realtalk"]["accent"]
    assert _raw(web_theme.THEME_KEYS["pattern_enabled"]) == "on"


def test_batch_preset_plus_explicit_handle_in_same_batch_keeps_explicit_value(tmp_path):
    """Явная ручка ИЗ ТОГО ЖЕ пакета сильнее пресета — остальные ручки догоняют пресет."""
    client = _setup(tmp_path)
    resp = _batch(client, [
        ("miniapp_theme_preset", "youlead"),
        ("miniapp_theme_pattern_enabled", "off"),
    ])
    assert resp.status_code == 200, resp.text
    assert _raw(web_theme.THEME_KEYS["pattern_enabled"]) == "off"
    assert _raw(web_theme.THEME_KEYS["heading_font"]) == web_theme.PRESETS["youlead"]["heading_font"]


def test_batch_without_preset_key_does_not_touch_theme_handles(tmp_path):
    """Пакет без ключа пресета не должен «самопроизвольно» переписывать ручки оформления."""
    client = _setup(tmp_path)
    resp = _batch(client, [("event_name", "форума RusCo")])
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == ["event_name"]
    assert _raw(web_theme.THEME_KEYS["accent"]) is None


# ── бот: меню темы показывает пресет, а не «Своя», на чистом стенде ─────────────────────

def test_bot_theme_menu_shows_preset_not_custom_on_clean_stand(tmp_path):
    from handlers.admin_miniapp_theme import _current_preset_and_custom

    _use_tmp_db(tmp_path, "miniapp_theme_preset_bot_260904.db")
    preset_name, is_custom = _run(_current_preset_and_custom())
    assert preset_name == web_theme.DEFAULT_PRESET
    assert is_custom is False


def test_bot_theme_menu_reflects_preset_after_web_batch_save(tmp_path):
    from handlers.admin_miniapp_theme import _current_preset_and_custom

    client = _setup(tmp_path)
    _batch(client, [("miniapp_theme_preset", "realtalk")])
    preset_name, is_custom = _run(_current_preset_and_custom())
    assert preset_name == "realtalk"
    assert is_custom is False
