"""Phase 19 Plan 07 Task 2 (WEBAPP-01, T-19-43/44/45): настройки-лайт из Mini App —
`GET/POST /app/api/admin/settings`, закрытый белый список `EDITABLE_KEYS`.

Главное — белый список НЕ пропускает ничего, кроме `miniapp_*`-тумблеров, и переключение
сразу видно боту (`settings_schema.get_setting_typed`, без перезапуска). Харнесс —
`tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio

from database import db as bot_db
from settings_schema import get_setting_typed

from miniapp.routers.settings import EDITABLE_KEYS

from tests.test_miniapp_routes import (
    ADMIN_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


def _setup(tmp_path, name="miniapp_settings_api.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    return _client(_cfg(db_path))


def _list(client, user=ADMIN_ID):
    return client.get("/app/api/admin/settings", headers=_hdr(user))


def _post(client, key, value, user=ADMIN_ID):
    return client.post("/app/api/admin/settings", json={"key": key, "value": value}, headers=_hdr(user))


# ── EDITABLE_KEYS — вычисляется из реестра ───────────────────────────────────────────────

def test_editable_keys_are_only_miniapp_toggles():
    assert "miniapp_enabled" in EDITABLE_KEYS
    assert "miniapp_staff_only" in EDITABLE_KEYS
    assert all(k.startswith("miniapp_section_") for k in EDITABLE_KEYS
               if k not in ("miniapp_enabled", "miniapp_staff_only"))
    # Вне белого списка -- ни оплата, ни роли, ни Sheets, ни произвольный "bot_token".
    for forbidden in ("payment_enabled", "role_caps_reg_manager", "role_caps_game_manager",
                       "consent_enabled", "bot_token", "google_sheet_id"):
        assert forbidden not in EDITABLE_KEYS


# ── GET /app/api/admin/settings ──────────────────────────────────────────────────────────

def test_settings_list_only_whitelist_with_human_labels(tmp_path):
    client = _setup(tmp_path)
    resp = _list(client)
    assert resp.status_code == 200
    items = resp.json()
    keys = {i["key"] for i in items}
    assert keys == set(EDITABLE_KEYS)
    for item in items:
        assert set(item.keys()) == {"key", "label", "value", "group_label"}
        assert item["label"].strip()  # человеческая подпись, не пустая
        assert item["group_label"].strip()
        assert item["value"] in ("on", "off")


def test_settings_list_without_settings_cap_403(tmp_path):
    client = _setup(tmp_path)
    resp = _list(client, GAME_MANAGER_ID)  # moderate_game есть, settings -- нет
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


def test_settings_section_off_blocks_route(tmp_path):
    client = _setup(tmp_path)
    _set("miniapp_section_settings", "off")
    resp = _list(client)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"


# ── POST /app/api/admin/settings ─────────────────────────────────────────────────────────

def test_settings_post_toggles_and_bot_sees_it_immediately(tmp_path):
    client = _setup(tmp_path)
    before = _run(get_setting_typed("miniapp_section_stats"))
    assert before == "on"  # дефолт реестра

    resp = _post(client, "miniapp_section_stats", "off")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    row = next(i for i in body if i["key"] == "miniapp_section_stats")
    assert row["value"] == "off"

    # Бот читает то же самое значение сразу, без перезапуска (D-05 из реестра).
    after = _run(get_setting_typed("miniapp_section_stats"))
    assert after == "off"


def test_settings_post_unknown_key_403_not_editable_and_db_unchanged(tmp_path):
    client = _setup(tmp_path)
    for bad_key in ("bot_token", "payment_enabled", "role_caps_reg_manager", "role_caps_game_manager"):
        resp = _post(client, bad_key, "on")
        assert resp.status_code == 403, bad_key
        assert resp.json()["reason"] == "not_editable"
    # payment_enabled реально есть в реестре -- убеждаемся, что запись не создана вовсе
    # (POST на неё не прошёл, значит и строки в bot_settings быть не должно).
    assert _run(bot_db.get_setting("payment_enabled")) is None
    assert _run(get_setting_typed("payment_enabled")) == "off"  # дефолт реестра, не тронуто


def test_settings_post_bad_value_400(tmp_path):
    client = _setup(tmp_path)
    for bad_value in ("yes", "1", "true", "", "ON"):
        resp = _post(client, "miniapp_section_stats", bad_value)
        assert resp.status_code == 400, bad_value
        assert resp.json()["reason"] == "bad_value" and resp.json()["text"]
    assert _run(get_setting_typed("miniapp_section_stats")) == "on"  # не тронуто


def test_settings_post_without_settings_cap_403(tmp_path):
    client = _setup(tmp_path)
    resp = _post(client, "miniapp_section_stats", "off", GAME_MANAGER_ID)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"
    assert _run(get_setting_typed("miniapp_section_stats")) == "on"


# ── miniapp_enabled умеет выключать сам себя ─────────────────────────────────────────────

def test_disabling_miniapp_enabled_locks_app_with_503(tmp_path):
    client = _setup(tmp_path)
    assert client.get("/app").status_code == 200

    resp = _post(client, "miniapp_enabled", "off")
    assert resp.status_code == 200, resp.text

    locked = client.get("/app")
    assert locked.status_code == 503
    assert client.get("/app/health").status_code == 200  # health переживает тумблер
