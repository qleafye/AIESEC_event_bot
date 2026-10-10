"""Регистрация на месте в сканере Mini App (FORUM-CHECKIN.md D-41, D-26, D-36, D-34, D-39):
ручки `/app/api/checkin/onsite/*` и onsite-флаги в ответах `/scan`, `/manual`, `/search`,
`/points`, `/net-texts` (`miniapp/routers/checkin.py`). Харнесс — tests/test_miniapp_checkin_260924.py."""
from __future__ import annotations
from tests._paths import REPO_ROOT

import base64
import json
import sqlite3
from pathlib import Path

import cities as cities_mod
from config import config as bot_config
from database import db as bot_db
from miniapp import outbox as outbox_mod
from services import venue_log
from domain.settings.schema import SETTINGS_SCHEMA

from tests.test_miniapp_checkin_260924 import (
    BASE,
    _grant_checkin_to_bound_manager,
    _insert_user,
    _qr,
    _run,
    _seed_ready,
)
from tests.test_miniapp_routes import (
    ADMIN_ID,
    BOUND_MANAGER_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
)

ONSITE = f"{BASE}/onsite"


def _grant_checkin_to_game_manager():
    """Волонтёр стойки регистрации: отметка входа И право «Одобрять на месте» (ревью 28.09 —
    одной `checkin` для одобрения мало)."""
    _run(bot_db.set_setting("role_caps_game_manager", "moderate_game;checkin;checkin_approve"))


def _ready(tmp_path, *, enable=("spb",)):
    db_path = _seed_ready(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "on"))
    for code in enable:
        _run(bot_db.set_setting(cities_mod.per_city_key("onsite_reg_enabled", code), "on"))
    return _client(_cfg(db_path))


def _rows(sql, *params):
    conn = sqlite3.connect(bot_config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def _status(uid):
    return _rows("SELECT status FROM users WHERE telegram_id = ?", uid)[0]["status"]


def _outbox_kinds():
    return [r["kind"] for r in _rows("SELECT kind FROM miniapp_outbox ORDER BY id")]


def _decisions(uid):
    return _rows("SELECT * FROM application_decisions WHERE telegram_id = ?", uid)


def _venue(uid=None):
    if uid is None:
        return _rows("SELECT * FROM venue_log ORDER BY id")
    return _rows("SELECT * FROM venue_log WHERE telegram_id = ? ORDER BY id", uid)


def _walkin(uid, *, city="spb", name="Гостев Гость", day=None):
    _run(bot_db.create_onsite_user(
        uid, username="guest", full_name=name, phone="+79991112233",
        university="СПбГУ", event_city=city, season="YL'26",
    ))
    if day is not None:
        conn = sqlite3.connect(bot_config.DB_PATH)
        conn.execute("UPDATE users SET registration_date = ? WHERE telegram_id = ?", (day, uid))
        conn.commit()
        conn.close()


# ── /onsite/approve ──────────────────────────────────────────────────────────────────────

def test_onsite_kind_registered_in_outbox_contract():
    assert "onsite_approved" in outbox_mod.OUTBOX_KINDS


def test_approve_without_cap_is_403(tmp_path):
    client = _ready(tmp_path)
    _run(_insert_user(951001, status="pending", city="spb"))
    resp = client.post(f"{ONSITE}/approve", json={"telegram_id": 951001}, headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403


def test_approve_pending_door_full_path_then_duplicate(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951002
    _run(_insert_user(uid, full_name="Петрова Анна", status="pending", city="spb"))

    r1 = client.post(f"{ONSITE}/approve", json={"telegram_id": uid, "city": "spb"},
                     headers=_hdr(GAME_MANAGER_ID))
    assert r1.status_code == 200
    body = r1.json()
    assert body["status"] == "new"
    assert body["onsite_approved"] is True
    assert body["full_name"] == "Петрова Анна"
    assert body["city_label"]
    assert "outbox" not in body and "first_entry" not in body
    assert _status(uid) == "approved"
    kinds = _outbox_kinds()
    assert "onsite_approved" in kinds and "checkin_first_entry" in kinds
    assert len(_decisions(uid)) == 1
    assert _decisions(uid)[0]["decided_by"] == GAME_MANAGER_ID
    assert any(v["action"] == venue_log.ACTION_ONSITE_APPROVE and v["staff_id"] == GAME_MANAGER_ID
               for v in _venue(uid))

    r2 = client.post(f"{ONSITE}/approve", json={"telegram_id": uid, "city": "spb"},
                     headers=_hdr(GAME_MANAGER_ID))
    assert r2.json()["status"] == "duplicate"
    assert not r2.json().get("onsite_approved")
    assert len(_decisions(uid)) == 1
    assert _outbox_kinds().count("onsite_approved") == 1


def test_approve_toggle_off_is_onsite_off(tmp_path):
    client = _ready(tmp_path, enable=())
    _grant_checkin_to_game_manager()
    uid = 951003
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{ONSITE}/approve", json={"telegram_id": uid, "city": "spb"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "onsite_off"
    assert body["reason_text"] == SETTINGS_SCHEMA["onsite_off_text"]["default"]
    assert _status(uid) == "pending"
    assert _outbox_kinds() == []


def test_approve_bound_other_city_is_wrong_city_and_logged(tmp_path):
    client = _ready(tmp_path, enable=("spb", "msk"))
    _grant_checkin_to_bound_manager()
    uid = 951004
    _run(_insert_user(uid, status="pending", city="msk"))
    # город из тела игнорируется для привязанного волонтёра (D-26)
    body = client.post(f"{ONSITE}/approve", json={"telegram_id": uid, "city": "msk"},
                       headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "wrong_city"
    assert _status(uid) == "pending"
    denied = [v for v in _venue(uid) if v["action"] == venue_log.ACTION_DENIED]
    assert len(denied) == 1 and denied[0]["staff_id"] == BOUND_MANAGER_ID


def test_approve_unknown_id_is_not_found(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_game_manager()
    body = client.post(f"{ONSITE}/approve", json={"telegram_id": 959999, "city": "spb"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "not_found"


def test_approve_body_takes_single_id_only(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_game_manager()
    resp = client.post(f"{ONSITE}/approve", json={"telegram_id": [1, 2]}, headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 422


# ── /onsite/pending ──────────────────────────────────────────────────────────────────────

def test_pending_lists_todays_walkin_of_own_city_without_phone(tmp_path, monkeypatch):
    client = _ready(tmp_path, enable=("spb", "msk"))
    _grant_checkin_to_bound_manager()
    from services.timeutil import msk_now
    today = msk_now().strftime("%Y-%m-%d")
    _walkin(951010, city="spb", name="Сегодняшний Спб", day=f"{today} 10:05:00")
    _walkin(951011, city="msk", name="Сегодняшний Мск", day=f"{today} 10:06:00")
    _walkin(951012, city="spb", name="Вчерашний Спб", day="2020-01-01 10:00:00")
    _run(_insert_user(951013, status="pending", city="spb"))  # не walk-in

    body = client.get(f"{ONSITE}/pending", headers=_hdr(BOUND_MANAGER_ID)).json()
    ids = [it["telegram_id"] for it in body["items"]]
    assert ids == [951010]
    item = body["items"][0]
    assert "phone" not in item
    assert item["registered_at"] == "10:05"
    assert item["full_name"] == "Сегодняшний Спб"
    assert body["enabled"] is True


def test_pending_toggle_off_is_empty(tmp_path):
    client = _ready(tmp_path, enable=())
    _grant_checkin_to_bound_manager()
    from services.timeutil import msk_now
    _walkin(951014, city="spb", day=msk_now().strftime("%Y-%m-%d 09:00:00"))
    body = client.get(f"{ONSITE}/pending", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body == {"items": [], "enabled": False}


# ── /onsite/link ─────────────────────────────────────────────────────────────────────────

def test_link_returns_url_and_data_uri_qr(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_game_manager()
    body = client.get(f"{ONSITE}/link?city=spb", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["url"] == "https://t.me/YouLead_test_bot?start=walkin_spb"
    assert body["qr"].startswith("data:image/png;base64,")
    assert base64.b64decode(body["qr"].split(",", 1)[1]).startswith(b"\x89PNG")
    assert body["hint"] == SETTINGS_SCHEMA["onsite_register_hint_text"]["default"]


def test_link_toggle_off(tmp_path):
    client = _ready(tmp_path, enable=())
    _grant_checkin_to_game_manager()
    body = client.get(f"{ONSITE}/link?city=spb", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "onsite_off"
    assert "url" not in body


def test_link_bound_city_wins(tmp_path):
    client = _ready(tmp_path, enable=("spb", "msk"))
    _grant_checkin_to_bound_manager()
    body = client.get(f"{ONSITE}/link?city=msk", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["url"].endswith("?start=walkin_spb")


def test_link_without_bot_username_explains(tmp_path):
    db_path = _seed_ready(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _run(bot_db.set_setting(cities_mod.per_city_key("onsite_reg_enabled", "spb"), "on"))
    client = _client(_cfg(db_path, bot_username=""))
    _grant_checkin_to_game_manager()
    body = client.get(f"{ONSITE}/link?city=spb", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "error"
    assert "сканер" in body["reason_text"]


# ── флаги в существующих ответах ─────────────────────────────────────────────────────────

def test_scan_denied_pending_carries_approve_flag(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 951020
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "denied"
    assert body["telegram_id"] == uid
    assert body["onsite_approve"] is True
    assert body["onsite_register"] is False


def test_scan_denied_toggle_off_no_flag(tmp_path):
    client = _ready(tmp_path, enable=())
    _grant_checkin_to_bound_manager()
    uid = 951021
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["onsite_approve"] is False


def test_scan_not_found_carries_register_flag(tmp_path):
    from services.checkin import build_payload
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    payload = build_payload("YL26", "Никто Никтов", "СПб", "no-such-token")
    body = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "not_found"
    assert body["onsite_register"] is True
    assert body["onsite_approve"] is False


def test_scan_unbound_uses_body_city_for_flag(tmp_path):
    client = _ready(tmp_path, enable=("spb",))
    _grant_checkin_to_game_manager()
    uid = 951022
    _run(_insert_user(uid, status="approved", season="YL'25", city="msk"))
    on = client.post(f"{BASE}/scan", json={"payload": _qr(uid), "city": "spb"},
                     headers=_hdr(GAME_MANAGER_ID)).json()
    assert on["status"] == "denied" and on["onsite_approve"] is True
    off = client.post(f"{BASE}/scan", json={"payload": _qr(uid), "city": "msk"},
                      headers=_hdr(GAME_MANAGER_ID)).json()
    assert off["onsite_approve"] is False


def test_manual_denied_carries_flags(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 951023
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{BASE}/manual", json={"telegram_id": uid}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "denied"
    assert body["telegram_id"] == uid
    assert body["onsite_approve"] is True
    nf = client.post(f"{BASE}/manual", json={"telegram_id": 959998}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert nf["status"] == "not_found" and nf["onsite_register"] is True


def test_training_point_has_no_onsite_flags(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 951024
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{BASE}/manual", json={"telegram_id": uid, "point": "training"},
                       headers=_hdr(BOUND_MANAGER_ID)).json()
    assert not body.get("onsite_approve")
    assert not body.get("onsite_register")


def test_search_rows_carry_flags(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(_insert_user(951030, full_name="Смирнова Ольга", status="pending", city="spb"))
    _run(_insert_user(951031, full_name="Смирнова Ирина", status="approved", city="spb"))
    items = client.get(f"{BASE}/search?q=Смирнова", headers=_hdr(BOUND_MANAGER_ID)).json()["items"]
    by_id = {it["telegram_id"]: it for it in items}
    assert by_id[951030]["onsite_approve"] is True
    assert by_id[951030]["onsite_register"] is False
    assert by_id[951031]["onsite_approve"] is False


def test_search_rows_flags_off_when_toggle_off(tmp_path):
    client = _ready(tmp_path, enable=())
    _grant_checkin_to_bound_manager()
    _run(_insert_user(951032, full_name="Смирнова Ольга", status="pending", city="spb"))
    items = client.get(f"{BASE}/search?q=Смирнова", headers=_hdr(BOUND_MANAGER_ID)).json()["items"]
    assert items[0]["onsite_approve"] is False


def test_points_carry_onsite_enabled(tmp_path):
    client = _ready(tmp_path, enable=("spb",))
    _grant_checkin_to_game_manager()
    assert client.get(f"{BASE}/points?city=spb", headers=_hdr(GAME_MANAGER_ID)).json()["onsite_enabled"] is True
    assert client.get(f"{BASE}/points?city=msk", headers=_hdr(GAME_MANAGER_ID)).json()["onsite_enabled"] is False


def test_net_texts_carry_onsite_texts(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_game_manager()
    body = client.get(f"{BASE}/net-texts", headers=_hdr(GAME_MANAGER_ID)).json()
    for key in ("onsite_approve_button_text", "onsite_approve_confirm_text",
                "onsite_register_button_text", "onsite_register_hint_text",
                "onsite_pending_title_text"):
        assert body["onsite"][key] == SETTINGS_SCHEMA[key]["default"]


def test_admin_superuser_unrestricted_link(tmp_path):
    client = _ready(tmp_path, enable=("msk",))
    body = client.get(f"{ONSITE}/link?city=msk", headers=_hdr(ADMIN_ID)).json()
    assert body["url"].endswith("walkin_msk")
    assert json.dumps(body)


# ── экран сканера ────────────────────────────────────────────────────────────────────────

SCANNER_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js"


def _js_function(src: str, name: str) -> str:
    body = src[src.index(f"function {name}("):]
    return body[:body.index("\n  }\n") + 4]


def test_scanner_js_wires_onsite_endpoints_through_confirm():
    src = SCANNER_JS.read_text(encoding="utf-8")
    for needle in ("onsite/approve", "showConfirm", "onsite/pending", "onsite/link",
                   "onsite_approve_button_text", "onsite_register_button_text",
                   "onsite_pending_title_text", "onsite_enabled"):
        assert needle in src, needle
    # одобрение уходит на сервер только после «да» в подтверждении
    approve = _js_function(src, "approveOnsite")
    assert approve.index("askConfirm(") < approve.index("onsite/approve")
    assert "if (!ok) return;" in approve
    assert "showConfirm" in _js_function(src, "askConfirm")
    # QR — data URI из JSON, не отдельный URL картинки (тег img не шлёт initData)
    qr = _js_function(src, "showWalkinQr")
    assert "src: res.qr" in qr
    # в сканере нет массового одобрения: тело — один telegram_id
    assert "telegram_ids" not in src and "approve-all" not in src
    assert "innerHTML" not in src
