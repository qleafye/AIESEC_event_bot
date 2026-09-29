"""Регистрация на месте (FORUM-CHECKIN.md D-41): правки по ревью 28.09.

- отклонённую заявку сканер называет отклонённой (с причиной, если она записана), обычной кнопки
  одобрения у неё нет — только отдельное «Пропустить вопреки отказу» с подтверждением; флип
  вопреки отказу снимает `rejected_at` и маркеры автоотказа, журнал помечает его отдельно.

pytest-asyncio в проекте нет — async через `asyncio.run()` (конвенция проекта)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from core import cities as cities_mod
from config import config as bot_config
from database import db as bot_db
from services import onsite_reg, venue_log
from services.timeutil import msk_now
from core.settings_schema import SETTINGS_SCHEMA

from tests.test_miniapp_checkin_260924 import (
    BASE,
    _grant_checkin_to_bound_manager,
    _grant_checkin_to_game_manager,
    _insert_user,
    _qr,
    _run,
    _seed_ready,
)
from tests.test_miniapp_routes import BOUND_MANAGER_ID, GAME_MANAGER_ID, _cfg, _client, _hdr

ONSITE = f"{BASE}/onsite"
SEASON = "YL'26"
STAFF_ID = 927001
SCANNER_JS = Path(__file__).resolve().parents[1] / "miniapp" / "static" / "js" / "screens" / "scanner.js"


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


def _exec(sql, *params):
    conn = sqlite3.connect(bot_config.DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _row(uid):
    rows = _rows("SELECT * FROM users WHERE telegram_id = ?", uid)
    return rows[0] if rows else None


def _decisions(uid):
    return _rows("SELECT * FROM application_decisions WHERE telegram_id = ? ORDER BY id", uid)


def _venue(uid):
    return _rows("SELECT * FROM venue_log WHERE telegram_id = ? ORDER BY id", uid)


def _outbox_kinds():
    return [r["kind"] for r in _rows("SELECT kind FROM miniapp_outbox ORDER BY id")]


def _reject_with_reason(uid, reason="Мало опыта"):
    """Ручной отказ менеджера: статус + rejected_at + строка журнала решений с причиной."""
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    _exec("UPDATE users SET status = 'rejected', rejected_at = ?, decision_delivery_status = 'delivered', "
          "decision_delivery_decision = 'rejected', decision_delivery_at = ? WHERE telegram_id = ?",
          stamp, stamp, uid)
    _run(bot_db.record_application_decision(uid, "rejected", reason, 42, stamp, stamp, effects_sent_at=stamp))


def _auto_reject(uid):
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    _exec("UPDATE users SET status = 'rejected', rejected_at = ?, auto_reject_rule_ids = '[7]', "
          "auto_rejected_at = ?, auto_rule_note = '🤖 Автоотказ 20.09 (правило: Возраст)' "
          "WHERE telegram_id = ?", stamp, stamp, uid)


def _door(uid, *, city="spb", bound="spb", **kw):
    user = _run(bot_db.get_user(uid))
    return _run(onsite_reg.approve_at_door(
        user, city=city, staff_id=STAFF_ID, staff_name="Волонтёр", bound=bound, **kw,
    ))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Отклонённая заявка у стойки
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_approve_onsite_does_not_flip_rejected_without_override(tmp_path):
    _seed_ready(tmp_path)
    _run(_insert_user(953001, status="pending", city="spb"))
    _reject_with_reason(953001)
    assert _run(bot_db.approve_onsite(953001, by_staff_id=STAFF_ID, season=SEASON)) is False
    assert _row(953001)["status"] == "rejected"


def test_approve_onsite_override_clears_reject_markers(tmp_path):
    _seed_ready(tmp_path)
    _run(_insert_user(953002, status="pending", city="spb"))
    _auto_reject(953002)
    _exec("UPDATE users SET decision_delivery_status = 'delivered', decision_delivery_decision = 'rejected' "
          "WHERE telegram_id = 953002")
    assert _run(bot_db.approve_onsite(
        953002, by_staff_id=STAFF_ID, season=SEASON, override_reject=True,
    )) is True
    row = _row(953002)
    assert row["status"] == "approved"
    assert row["rejected_at"] is None
    assert row["auto_reject_rule_ids"] is None
    assert row["auto_rejected_at"] is None
    assert row["auto_rule_note"] is None
    assert row["decision_delivery_status"] is None and row["decision_delivery_decision"] is None
    assert row["onsite_by"] == STAFF_ID


def test_approve_at_door_rejected_tells_the_truth_and_changes_nothing(tmp_path):
    _seed_ready(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _run(bot_db.set_setting(cities_mod.per_city_key("onsite_reg_enabled", "spb"), "on"))
    _run(_insert_user(953003, status="pending", city="spb"))
    _reject_with_reason(953003, "Мало опыта")
    before = (_row(953003), _decisions(953003), _venue(953003))
    res = _door(953003)
    assert res["status"] == "rejected"
    assert "отклонена менеджером" in res["reason_text"]
    assert "Мало опыта" in res["reason_text"]
    assert "outbox" not in res
    assert (_row(953003), _decisions(953003), _venue(953003)) == before


def test_approve_at_door_override_is_journaled_under_volunteer(tmp_path):
    _seed_ready(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _run(bot_db.set_setting(cities_mod.per_city_key("onsite_reg_enabled", "spb"), "on"))
    _run(_insert_user(953004, status="pending", city="spb"))
    _reject_with_reason(953004)
    res = _door(953004, override_reject=True)
    assert res["status"] == "new" and res["onsite_approved"] is True
    row = _row(953004)
    assert row["status"] == "approved" and row["rejected_at"] is None
    last = _decisions(953004)[-1]
    assert last["decision"] == "approved" and last["decided_by"] == STAFF_ID
    assert "вопреки отказу" in last["reason"]
    approve_rows = [v for v in _venue(953004) if v["action"] == venue_log.ACTION_ONSITE_APPROVE]
    assert len(approve_rows) == 1
    assert '"override_reject": true' in approve_rows[0]["details"]


def test_rejected_denial_label_exists():
    assert venue_log.DENIAL_LABELS["rejected"]


def test_scan_rejected_says_rejected_and_offers_override_only(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 953010
    _run(_insert_user(uid, status="pending", city="spb"))
    _reject_with_reason(uid, "Мало опыта")
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "denied"
    assert body["reason_text"].startswith("Заявка отклонена менеджером")
    assert "Мало опыта" in body["reason_text"]
    assert body["onsite_approve"] is False
    assert body["onsite_override"] is True
    denied = [v for v in _venue(uid) if v["action"] == venue_log.ACTION_DENIED]
    assert denied and '"reason": "rejected"' in denied[-1]["details"]


def test_scan_pending_has_no_override_flag(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 953011
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["onsite_approve"] is True
    assert body["onsite_override"] is False


def test_search_rejected_row_offers_override(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(_insert_user(953012, full_name="Отказова Анна", status="pending", city="spb"))
    _auto_reject(953012)
    [item] = client.get(f"{BASE}/search?q=Отказова", headers=_hdr(BOUND_MANAGER_ID)).json()["items"]
    assert item["reason_text"].startswith("Заявка отклонена менеджером")
    assert item["onsite_approve"] is False and item["onsite_override"] is True


def test_approve_endpoint_rejected_needs_explicit_override(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 953013
    _run(_insert_user(uid, status="pending", city="spb"))
    _reject_with_reason(uid)
    plain = client.post(f"{ONSITE}/approve", json={"telegram_id": uid}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert plain["status"] == "rejected"
    assert plain["onsite_override"] is True
    assert _row(uid)["status"] == "rejected"
    assert "onsite_approved" not in _outbox_kinds()

    forced = client.post(f"{ONSITE}/approve", json={"telegram_id": uid, "override_reject": True},
                         headers=_hdr(BOUND_MANAGER_ID)).json()
    assert forced["status"] == "new" and forced["onsite_approved"] is True
    assert _row(uid)["status"] == "approved"
    assert "onsite_approved" in _outbox_kinds()


def test_override_texts_in_registry_with_english_defaults(tmp_path):
    from services.i18n_form_manual import FORM_DEFAULT_EN
    staff_group = SETTINGS_SCHEMA["checkin_undo_button_text"]["group"]
    for key in ("onsite_override_button_text", "onsite_override_confirm_text",
                "onsite_rejected_text", "onsite_rejected_reason_text"):
        meta = SETTINGS_SCHEMA[key]
        assert meta["type"] == "text" and meta["group"] == staff_group, key
        assert meta["default"] in FORM_DEFAULT_EN, key
    confirm = SETTINGS_SCHEMA["onsite_override_confirm_text"]["default"]
    assert "{name}" in confirm and "отклон" in confirm.lower()
    assert "{reason}" in SETTINGS_SCHEMA["onsite_rejected_reason_text"]["default"]
    client = _ready(tmp_path)
    _grant_checkin_to_game_manager()
    texts = client.get(f"{BASE}/net-texts", headers=_hdr(GAME_MANAGER_ID)).json()["onsite"]
    assert texts["onsite_override_button_text"] == SETTINGS_SCHEMA["onsite_override_button_text"]["default"]
    assert texts["onsite_override_confirm_text"] == confirm


def _js_function(src: str, name: str) -> str:
    body = src[src.index(f"function {name}("):]
    return body[:body.index("\n  }\n") + 4]


def test_scanner_js_override_button_has_own_confirm_and_flag():
    src = SCANNER_JS.read_text(encoding="utf-8")
    assert "onsite_override_button_text" in src and "onsite_override_confirm_text" in src
    approve = _js_function(src, "approveOnsite")
    assert approve.index("askConfirm(") < approve.index("onsite/approve")
    assert "override_reject" in approve
    assert "onsite_override" in _js_function(src, "showPlaque")
    assert "onsite_override" in _js_function(src, "resultRow")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Признак walk-in не «прилипает»: полная анкета делает строку обычной заявкой
# ══════════════════════════════════════════════════════════════════════════════════════════

def _full_application(uid, *, season=SEASON, city="spb"):
    _run(bot_db.add_user({
        "telegram_id": uid, "username": "masha", "full_name": "Иванова Мария",
        "email": "m@example.com", "phone": "+79991234567", "university": "СПбГУ",
        "event_city": city, "season": season, "participant_type": "full",
        "registration_date": msk_now().strftime("%Y-%m-%d %H:%M:%S"),
    }))


def test_full_application_after_walkin_is_visible_in_moderation_queue(tmp_path):
    _seed_ready(tmp_path)
    _run(bot_db.create_onsite_user(
        953101, username="masha", full_name="Иванова Мария", phone="+79991234567",
        university=None, event_city="spb", season="YL'25",
    ))
    assert 953101 not in {u["telegram_id"] for u in _run(bot_db.get_pending_users(limit=50))}
    count_before = _run(bot_db.get_pending_count())
    _full_application(953101)
    row = _row(953101)
    assert row["onsite_kind"] is None and row["onsite_at"] is None and row["onsite_by"] is None
    assert row["status"] == "pending"
    assert 953101 in {u["telegram_id"] for u in _run(bot_db.get_pending_users(limit=50))}
    assert _run(bot_db.get_pending_count()) == count_before + 1


def test_rejected_walkin_resubmitting_full_form_returns_to_queue(tmp_path):
    _seed_ready(tmp_path)
    _run(bot_db.create_onsite_user(
        953102, username=None, full_name="Петров Пётр", phone="+79990000000",
        university=None, event_city="spb", season=SEASON,
    ))
    _exec("UPDATE users SET status = 'rejected' WHERE telegram_id = 953102")
    _full_application(953102)
    _exec("UPDATE users SET status = 'pending' WHERE telegram_id = 953102")  # как делает финал
    assert _row(953102)["onsite_kind"] is None
    assert 953102 in {u["telegram_id"] for u in _run(bot_db.get_pending_users(limit=50))}
    assert 953102 in _run(bot_db.approve_all_pending())


def test_door_marker_cleared_on_next_season_application(tmp_path):
    _seed_ready(tmp_path)
    _run(_insert_user(953103, status="pending", city="spb", season="YL'25"))
    _exec("UPDATE users SET onsite_kind = 'door', onsite_at = '2025-10-03 10:00:00', onsite_by = 5 "
          "WHERE telegram_id = 953103")
    _full_application(953103)
    row = _row(953103)
    assert row["onsite_kind"] is None and row["onsite_by"] is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Хвост одобрения не теряется: событие outbox — сразу после флипа и повторно при повторе
# ══════════════════════════════════════════════════════════════════════════════════════════

def _onsite_events(uid):
    import json
    return [r for r in _rows("SELECT * FROM miniapp_outbox WHERE kind = 'onsite_approved' ORDER BY id")
            if json.loads(r["payload"]).get("telegram_id") == uid]


def _onsite_on(city="spb"):
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _run(bot_db.set_setting(cities_mod.per_city_key("onsite_reg_enabled", city), "on"))


def test_outbox_event_is_enqueued_before_check_in(tmp_path, monkeypatch):
    import pytest
    _seed_ready(tmp_path)
    _onsite_on()
    _run(_insert_user(953201, status="pending", city="spb"))

    async def _boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(onsite_reg, "record_arrival", _boom)
    with pytest.raises(RuntimeError):
        _door(953201)
    assert _row(953201)["status"] == "approved"
    assert len(_onsite_events(953201)) == 1
    monkeypatch.undo()

    res = _door(953201)  # волонтёр нажал ещё раз
    assert res["status"] == "new"
    assert len(_onsite_events(953201)) == 1  # без дубля


def test_repeat_press_re_enqueues_missing_event(tmp_path, monkeypatch):
    _seed_ready(tmp_path)
    _onsite_on()
    _run(_insert_user(953202, status="pending", city="spb"))
    real = bot_db.enqueue_miniapp_outbox_once

    async def _fail(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bot_db, "enqueue_miniapp_outbox_once", _fail)
    first = _door(953202)
    assert first["status"] == "new" and first["onsite_approved"] is True
    assert _onsite_events(953202) == []
    monkeypatch.setattr(bot_db, "enqueue_miniapp_outbox_once", real)

    second = _door(953202)
    assert second["status"] == "duplicate"
    assert len(_onsite_events(953202)) == 1
    third = _door(953202)
    assert third["status"] == "duplicate"
    assert len(_onsite_events(953202)) == 1


def test_normally_approved_delegate_gets_no_onsite_event(tmp_path):
    _seed_ready(tmp_path)
    _onsite_on()
    _run(_insert_user(953203, status="approved", city="spb"))
    _door(953203)
    assert _onsite_events(953203) == []


def test_endpoint_repeat_press_keeps_single_event(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 953204
    _run(_insert_user(uid, status="pending", city="spb"))
    for _ in range(3):
        client.post(f"{ONSITE}/approve", json={"telegram_id": uid}, headers=_hdr(BOUND_MANAGER_ID))
    assert len(_onsite_events(uid)) == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# Журнал решений — раньше начисления амбассадору; сбой журнала не молчит
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_record_decision_writes_journal_even_if_referral_credit_fails(tmp_path, monkeypatch):
    from datetime import datetime
    from services import applications, referrals
    _seed_ready(tmp_path)
    _run(_insert_user(953301, status="approved", city="spb"))

    async def _boom(*_a, **_kw):
        raise RuntimeError("referrals down")

    monkeypatch.setattr(referrals, "credit_for_approved", _boom)
    _run(applications.record_decision(
        953301, "approved", "Одобрен(а) на месте", STAFF_ID, datetime(2026, 10, 3, 10, 0),
        effects_already_sent=True,
    ))
    assert [d["decision"] for d in _decisions(953301)] == ["approved"]


def test_journal_failure_at_door_is_marked_in_venue_log_and_logged(tmp_path, monkeypatch, caplog):
    import logging
    from services import applications
    _seed_ready(tmp_path)
    _onsite_on()
    _run(_insert_user(953302, status="pending", city="spb"))

    async def _boom(*_a, **_kw):
        raise RuntimeError("journal down")

    monkeypatch.setattr(applications, "record_decision", _boom)
    with caplog.at_level(logging.ERROR, logger="services.onsite_reg"):
        res = _door(953302)
    assert res["status"] == "new"
    assert any(r.levelno >= logging.ERROR and "журнал решений" in r.getMessage() for r in caplog.records)
    [row] = [v for v in _venue(953302) if v["action"] == venue_log.ACTION_ONSITE_APPROVE]
    assert '"decision_journal": "failed"' in row["details"]


def _patch_tail(monkeypatch, alerts):
    from services import reg_finalize, sheets

    async def _noop(*_a, **_kw):
        return None

    async def _alert(text):
        alerts.append(text)

    monkeypatch.setattr(reg_finalize, "write_sheet_row", _noop)
    monkeypatch.setattr(sheets, "update_status_in_sheet", _noop)
    monkeypatch.setattr(sheets, "_send_admin_alert", _alert)


def _fake_bot():
    from unittest.mock import AsyncMock
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    return bot


def test_bot_tail_alerts_admins_when_journal_row_missing(tmp_path, monkeypatch):
    from services import applications
    _seed_ready(tmp_path)
    _onsite_on()
    _run(_insert_user(953303, full_name="Петрова Анна", status="pending", city="spb"))

    async def _boom(*_a, **_kw):
        raise RuntimeError("journal down")

    monkeypatch.setattr(applications, "record_decision", _boom)
    _door(953303)
    monkeypatch.undo()
    alerts = []
    _patch_tail(monkeypatch, alerts)
    _run(onsite_reg.after_onsite_approved(_fake_bot(), 953303))
    assert len(alerts) == 1
    assert "953303" in alerts[0] and "журнал" in alerts[0]


def test_bot_tail_no_alert_when_journal_row_present(tmp_path, monkeypatch):
    _seed_ready(tmp_path)
    _onsite_on()
    _run(_insert_user(953304, status="pending", city="spb"))
    _door(953304)
    alerts = []
    _patch_tail(monkeypatch, alerts)
    _run(onsite_reg.after_onsite_approved(_fake_bot(), 953304))
    assert alerts == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# Отдельное право «Одобрять на месте» (checkin_approve)
# ══════════════════════════════════════════════════════════════════════════════════════════

def _set_role_caps(role, caps):
    _run(bot_db.set_setting(f"role_caps_{role}", caps))


def test_checkin_approve_capability_and_reg_volunteer_role():
    from dashboard import access
    from handlers import admin_caps
    assert "checkin_approve" in admin_caps.ALL_CAPABILITIES
    assert list(access.ALL_CAPABILITIES) == list(admin_caps.ALL_CAPABILITIES)
    assert admin_caps.CAP_LABELS["checkin_approve"]
    assert admin_caps.ROLES["volunteer"]["default_caps"] == ["checkin"]
    assert admin_caps.ROLES["reg_volunteer"]["default_caps"] == ["checkin", "checkin_approve"]
    assert access._ROLE_DEFAULT_CAPS["reg_volunteer"] == ["checkin", "checkin_approve"]
    assert SETTINGS_SCHEMA["role_caps_reg_volunteer"]["default"] == ["checkin", "checkin_approve"]
    assert SETTINGS_SCHEMA["role_reg_volunteer_enabled"]["default"] == "on"


def test_scanner_without_approve_right_gets_no_approve_buttons_and_403(tmp_path):
    client = _ready(tmp_path)
    _set_role_caps("game_manager", "moderate_game;checkin")
    uid = 953401
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid), "city": "spb"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "denied"
    assert body["onsite_approve"] is False and body["onsite_override"] is False
    resp = client.post(f"{ONSITE}/approve", json={"telegram_id": uid, "city": "spb"},
                       headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json()["cap"] == "checkin_approve"
    assert _row(uid)["status"] == "pending"
    points = client.get(f"{BASE}/points?city=spb", headers=_hdr(GAME_MANAGER_ID)).json()
    assert points["onsite_enabled"] is True and points["onsite_can_approve"] is False
    pending = client.get(f"{ONSITE}/pending?city=spb", headers=_hdr(GAME_MANAGER_ID)).json()
    assert pending["can_approve"] is False


def test_scanner_with_approve_right_can_approve(tmp_path):
    client = _ready(tmp_path)
    _set_role_caps("game_manager", "moderate_game;checkin;checkin_approve")
    uid = 953402
    _run(_insert_user(uid, status="pending", city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid), "city": "spb"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["onsite_approve"] is True
    resp = client.post(f"{ONSITE}/approve", json={"telegram_id": uid, "city": "spb"},
                       headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 200 and resp.json()["onsite_approved"] is True


def test_moderate_reg_holder_can_approve_without_explicit_right(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()  # moderate_reg;moderate_receipts;checkin
    points = client.get(f"{BASE}/points", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert points["onsite_can_approve"] is True


def test_scanner_js_hides_approve_without_right():
    src = SCANNER_JS.read_text(encoding="utf-8")
    assert "onsite_can_approve" in src and "can_approve" in _js_function(src, "loadOnsitePending")


# ── Приглашение волонтёров: опция «с одобрением на месте» ─────────────────────────────────

def test_invite_with_approval_grants_reg_volunteer_role(tmp_path):
    from tests.test_roles_phase8 import _fresh_state, dispatch_callback
    from tests.test_volunteer_invite_260924 import ADMIN_ID as INV_ADMIN
    from tests.test_volunteer_invite_260924 import FakeBot, _FakeCommand, _FakeMessage
    from tests.test_volunteer_invite_260924 import _ready as inv_ready
    from handlers import registration as reg
    from handlers.admin_caps import resolve_capabilities

    inv_ready(tmp_path)
    _run(bot_db.set_setting("volunteer_invite_enabled", "on"))
    result, event = dispatch_callback("volinvite_cfg:_all", INV_ADMIN)
    _text, _pm, kb = event.message.answers[-1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "volinvite_new:_all" in cbs and "volinvite_new:_all:appr" in cbs

    state = _fresh_state(INV_ADMIN)
    dispatch_callback("volinvite_new:_all:appr", INV_ADMIN, state=state)
    dispatch_callback("volinv_le:7", INV_ADMIN, state=state)
    dispatch_callback("volinv_re:none", INV_ADMIN, state=state)
    dispatch_callback("volinv_lim:30", INV_ADMIN, state=state)
    [inv] = _run(bot_db.list_volunteer_invites())
    assert inv["role"] == "reg_volunteer"

    uid = 953410
    msg = _FakeMessage(uid, username="regvol", full_name="Волонтёр Регистрации")
    _run(reg.cmd_start(msg, _fresh_state(uid), bot=FakeBot(), command=_FakeCommand(f"vol_{inv['code']}")))
    assert _run(bot_db.get_staff_roles(uid)) == ["reg_volunteer"]
    assert {"checkin", "checkin_approve"} <= _run(resolve_capabilities(uid))

    dispatch_callback(f"volinv_removeuser:{inv['code']}:{uid}", INV_ADMIN)
    assert _run(bot_db.get_staff_roles(uid)) == []


def test_default_invite_stays_scan_only(tmp_path):
    from tests.test_roles_phase8 import _fresh_state, dispatch_callback
    from tests.test_volunteer_invite_260924 import ADMIN_ID as INV_ADMIN
    from tests.test_volunteer_invite_260924 import _ready as inv_ready
    inv_ready(tmp_path)
    _run(bot_db.set_setting("volunteer_invite_enabled", "on"))
    state = _fresh_state(INV_ADMIN)
    dispatch_callback("volinvite_new:_all", INV_ADMIN, state=state)
    dispatch_callback("volinv_le:7", INV_ADMIN, state=state)
    dispatch_callback("volinv_re:none", INV_ADMIN, state=state)
    dispatch_callback("volinv_lim:30", INV_ADMIN, state=state)
    [inv] = _run(bot_db.list_volunteer_invites())
    assert (inv.get("role") or "volunteer") == "volunteer"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Walk-in «не того» города: стойка одобряет его, переводя в свой город
# ══════════════════════════════════════════════════════════════════════════════════════════

def _walkin(uid, *, city="msk", name="Гостев Гость"):
    _run(bot_db.create_onsite_user(
        uid, username="guest", full_name=name, phone="+79991112233",
        university="СПбГУ", event_city=city, season=SEASON,
    ))


def test_bound_volunteer_approves_other_city_walkin_by_moving_it(tmp_path):
    _seed_ready(tmp_path)
    _onsite_on("spb")
    _walkin(953501, city="msk")
    res = _door(953501, city="spb", bound="spb")
    assert res["status"] == "new" and res["onsite_approved"] is True
    row = _row(953501)
    assert row["status"] == "approved" and row["event_city"] == "spb"


def test_bound_volunteer_still_cannot_approve_other_city_regular_applicant(tmp_path):
    _seed_ready(tmp_path)
    _onsite_on("spb")
    _run(_insert_user(953502, status="pending", city="msk"))
    res = _door(953502, city="spb", bound="spb")
    assert res["status"] == "wrong_city"
    assert _row(953502)["status"] == "pending"


def test_search_shows_other_city_walkin_with_move_note(tmp_path):
    client = _ready(tmp_path, enable=("spb", "msk"))
    _grant_checkin_to_bound_manager()  # привязан к spb
    _walkin(953503, city="msk", name="Переездов Гость")
    _run(_insert_user(953504, full_name="Переездов Обычный", status="pending", city="msk"))
    items = client.get(f"{BASE}/search?q=Переездов", headers=_hdr(BOUND_MANAGER_ID)).json()["items"]
    by_id = {it["telegram_id"]: it for it in items}
    assert 953504 not in by_id  # обычная заявка чужого города по-прежнему не видна
    walkin = by_id[953503]
    assert walkin["onsite_approve"] is True
    assert walkin["onsite_move_to"]  # подпись города стойки для подтверждения

    resp = client.post(f"{ONSITE}/approve", json={"telegram_id": 953503}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert resp["status"] == "new"
    assert _row(953503)["event_city"] == "spb"


def test_move_confirm_text_in_registry_and_scanner():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    meta = SETTINGS_SCHEMA["onsite_move_confirm_text"]
    assert "{city}" in meta["default"] and meta["default"] in FORM_DEFAULT_EN
    src = SCANNER_JS.read_text(encoding="utf-8")
    assert "onsite_move_confirm_text" in src
    assert "onsite_move_to" in _js_function(src, "approveOnsite")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Язык короткой анкеты: как у обычного /start
# ══════════════════════════════════════════════════════════════════════════════════════════

def _chat_ready(tmp_path):
    from tests.test_onsite_reg_chat_260927 import _ready as chat_ready
    chat_ready(tmp_path)


class _EnUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "guest"
        self.full_name = "Guest"
        self.language_code = "en"


def test_walkin_asks_language_when_event_is_multilingual_then_speaks_english(tmp_path):
    from handlers import registration as reg
    from services import i18n_form_manual
    from tests.test_onsite_reg_chat_260927 import _Bot, _Cmd, _Msg
    from tests.test_roles_phase8 import _fresh_state

    _chat_ready(tmp_path)
    _run(i18n_form_manual.seed("en"))
    _run(bot_db.set_setting("delegate_lang_enabled", "on"))
    _run(bot_db.set_setting("delegate_lang_ask_on_start", "on"))
    _run(bot_db.set_setting("onsite_reg_enabled", "on"))
    uid = 953601
    state = _fresh_state(uid)
    msg = _Msg(uid)
    msg.from_user = _EnUser(uid)
    _run(reg.cmd_start(msg, state, bot=_Bot(), command=_Cmd("walkin")))
    assert any("Choose the form language" in t for t in msg.texts())
    assert _run(state.get_state()) is None  # анкета ещё не начата
    assert _run(state.get_data()).get("_deeplink_resume_args") == "walkin"

    # Тап «English» (handlers/reg_lang.py::lang_pick_choose): язык записан, /start повторён
    # с теми же аргументами ссылки.
    _run(bot_db.set_user_lang(uid, "en"))
    again = _Msg(uid)
    again.from_user = _EnUser(uid)
    _run(reg.cmd_start(again, state, bot=_Bot(), command=_Cmd("walkin")))
    from services.i18n_form_manual import FORM_DEFAULT_EN
    intro_en = FORM_DEFAULT_EN[SETTINGS_SCHEMA["onsite_reg_intro_text"]["default"]]
    assert any(intro_en in t for t in again.texts()), again.texts()


def test_walkin_single_language_event_goes_straight_to_form(tmp_path):
    from handlers import registration as reg
    from tests.test_onsite_reg_chat_260927 import _Bot, _Cmd, _Msg
    from tests.test_roles_phase8 import _fresh_state

    _chat_ready(tmp_path)
    _run(bot_db.set_setting("onsite_reg_enabled", "on"))
    uid = 953602
    state = _fresh_state(uid)
    msg = _Msg(uid)
    msg.from_user = _EnUser(uid)
    _run(reg.cmd_start(msg, state, bot=_Bot(), command=_Cmd("walkin")))
    assert any(SETTINGS_SCHEMA["onsite_reg_intro_text"]["default"] in t for t in msg.texts())


def test_approval_message_uses_person_language(tmp_path, monkeypatch):
    from services import i18n_form_manual
    from services.i18n_form_manual import FORM_DEFAULT_EN
    _seed_ready(tmp_path)
    _run(i18n_form_manual.seed("en"))
    _onsite_on()
    _run(bot_db.set_setting("delegate_lang_enabled", "on"))
    _run(bot_db.set_setting("delegate_lang_ask_on_start", "on"))
    _run(bot_db.set_user_lang(953603, "en"))  # выбрал до анкеты — лежит в reg_started
    _walkin(953603, city="spb")
    assert _row(953603)["lang"] == "en"  # перенесено в users при создании строки
    _door(953603)
    alerts = []
    _patch_tail(monkeypatch, alerts)
    bot = _fake_bot()
    _run(onsite_reg.after_onsite_approved(bot, 953603))
    sent = bot.send_message.call_args.args[1]
    assert sent == FORM_DEFAULT_EN[SETTINGS_SCHEMA["onsite_reg_approved_text"]["default"]]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Телефон: только свой контакт, не пустой и не короткий номер
# ══════════════════════════════════════════════════════════════════════════════════════════

def _phone_step(uid, contact):
    from handlers import onsite_reg as onsite_handlers
    from tests.test_onsite_reg_chat_260927 import _Msg
    from tests.test_roles_phase8 import _fresh_state
    state = _fresh_state(uid)
    _run(state.set_state(onsite_handlers.OnsiteReg.phone))
    _run(state.update_data(onsite_city="spb", onsite_name="Иванова Мария"))
    msg = _Msg(uid, contact=contact)
    _run(onsite_handlers.onsite_phone_contact(msg, state))
    return msg, state


def test_foreign_contact_card_is_rejected(tmp_path):
    from handlers import onsite_reg as onsite_handlers
    from tests.test_onsite_reg_chat_260927 import _Contact
    _chat_ready(tmp_path)
    msg, state = _phone_step(953701, _Contact("79991234567", user_id=111))
    assert _run(state.get_state()) == onsite_handlers.OnsiteReg.phone.state
    assert (_run(state.get_data())).get("onsite_phone") is None
    assert SETTINGS_SCHEMA["onsite_reg_foreign_contact_text"]["default"] in msg.texts()[0]


def test_empty_or_short_contact_number_is_rejected(tmp_path):
    from handlers import onsite_reg as onsite_handlers
    from tests.test_onsite_reg_chat_260927 import _Contact
    _chat_ready(tmp_path)
    for uid, phone in ((953702, ""), (953703, "12345")):
        msg, state = _phone_step(uid, _Contact(phone, user_id=uid))
        assert _run(state.get_state()) == onsite_handlers.OnsiteReg.phone.state
        assert msg.texts()[0] == SETTINGS_SCHEMA["onsite_reg_bad_phone_text"]["default"]


def test_own_contact_is_accepted(tmp_path):
    from handlers import onsite_reg as onsite_handlers
    from tests.test_onsite_reg_chat_260927 import _Contact
    _chat_ready(tmp_path)
    msg, state = _phone_step(953704, _Contact("79991234567", user_id=953704))
    assert _run(state.get_state()) == onsite_handlers.OnsiteReg.university.state
    assert _run(state.get_data())["onsite_phone"] == "+79991234567"


def test_foreign_contact_text_has_english_default():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    meta = SETTINGS_SCHEMA["onsite_reg_foreign_contact_text"]
    assert meta["group"] == "reg" and meta["default"] in FORM_DEFAULT_EN


# ══════════════════════════════════════════════════════════════════════════════════════════
# Счётчики «на модерации» = очередь: walk-in без решения — отдельно, «ждут на стойке»
# ══════════════════════════════════════════════════════════════════════════════════════════

def _queue_fixture(tmp_path):
    _seed_ready(tmp_path)
    _exec("DELETE FROM users")
    _run(_insert_user(953801, status="pending", city="spb"))
    _walkin(953802, city="spb")
    _walkin(953803, city="spb")
    _exec("UPDATE users SET registration_date = ? WHERE telegram_id IN (953801, 953802, 953803)",
          msk_now().strftime("%Y-%m-%d %H:%M:%S"))


def test_bot_counters_match_the_queue(tmp_path):
    _queue_fixture(tmp_path)
    assert _run(bot_db.get_pending_count()) == 1
    [row] = [r for r in _run(bot_db.get_city_counts()) if r[0] == "spb"]
    assert row[2] == 1  # pending
    counts = _run(bot_db.count_applications())
    assert counts["pending"] == 1
    page = _run(bot_db.list_applications_page(status="pending"))
    rows = page["rows"] if isinstance(page, dict) else page
    assert [r["telegram_id"] for r in rows] == [953801]


def test_daily_digest_counts_walkins_separately(tmp_path):
    from services.daily_digest import build_digest_text
    _queue_fixture(tmp_path)
    stats = _run(bot_db.daily_digest_stats(msk_now().strftime("%Y-%m-%d")))
    assert stats["apps_pending"] == 1
    assert stats["apps_walkin_pending"] == 2
    text = build_digest_text(stats, {}, day_label="сегодня")
    assert "ждут 1" in text and "ждут на стойке 2" in text


def test_dashboard_pending_excludes_walkins(tmp_path):
    from dashboard import db as dash_db
    from dashboard.queries import Scope, funnel, kpi_row, status_totals
    _queue_fixture(tmp_path)
    with dash_db.read_conn(bot_config.DB_PATH) as conn:
        kpi = kpi_row(conn, Scope())
        assert kpi["pending"] == 1 and kpi["pending_walkin"] == 2
        assert dict(funnel(conn, Scope()))["На модерации"] == 1
        assert status_totals(conn, Scope())["pending"] == 1


def test_dashboard_pending_on_old_schema_without_onsite_column(tmp_path):
    from dashboard import db as dash_db
    from dashboard.queries import Scope, kpi_row
    _queue_fixture(tmp_path)
    _exec("ALTER TABLE users DROP COLUMN onsite_kind")
    with dash_db.read_conn(bot_config.DB_PATH) as conn:
        kpi = kpi_row(conn, Scope())
    assert kpi["pending"] == 3 and kpi["pending_walkin"] == 0


# ══════════════════════════════════════════════════════════════════════════════════════════
# Тумблер регистрации на месте — строго по городу
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_toggle_without_city_asks_to_pick_city_when_cities_on(tmp_path):
    from handlers import admin_onsite_reg as aor
    from tests.test_onsite_reg_chat_260927 import _AdminCb, _Cities
    _chat_ready(tmp_path)
    with _Cities():
        cb = _AdminCb("onsitereg_toggle:_all")
        _run(aor.onsitereg_toggle_go(cb))
        assert cb.alerts and "город" in (cb.alerts[-1][0] or "").lower()
        assert _run(bot_db.get_setting("onsite_reg_enabled")) in (None, "off")
        qr = _AdminCb("onsitereg_qr:_all")
        _run(aor.onsitereg_qr_send(qr, object()))
        assert not qr.message.photos
        assert "город" in (qr.alerts[-1][0] or "").lower()


def test_bound_manager_cannot_toggle_without_city(tmp_path):
    from handlers.admin_checkin import _CITY_FORBIDDEN_ALERT
    from tests.test_onsite_reg_chat_260927 import MANAGER_ID, ADMIN_ID as CHAT_ADMIN, _Cities
    from tests.test_roles_phase8 import dispatch_callback
    _chat_ready(tmp_path)
    with _Cities():
        _run(bot_db.add_staff(MANAGER_ID, "reg_manager", CHAT_ADMIN))
        _run(bot_db.set_staff_city(MANAGER_ID, "msk"))
        _result, event = dispatch_callback("onsitereg_toggle:_all", MANAGER_ID)
        assert event.answers and event.answers[0][0] == _CITY_FORBIDDEN_ALERT
    assert _run(bot_db.get_setting("onsite_reg_enabled")) in (None, "off")


def test_global_key_does_not_enable_cities_without_own_value(tmp_path):
    from tests.test_onsite_reg_chat_260927 import _Cities
    _chat_ready(tmp_path)
    with _Cities():
        _run(bot_db.set_setting("onsite_reg_enabled", "on"))
        assert _run(onsite_reg.onsite_enabled("spb")) is False
        _run(bot_db.set_setting(cities_mod.per_city_key("onsite_reg_enabled", "spb"), "on"))
        assert _run(onsite_reg.onsite_enabled("spb")) is True
        assert _run(onsite_reg.onsite_enabled(None)) is False
        assert _run(onsite_reg.walkin_link("yl_bot", None)) is None


def test_single_city_bot_uses_global_toggle(tmp_path):
    _chat_ready(tmp_path)
    _run(bot_db.set_setting("onsite_reg_enabled", "on"))
    assert _run(onsite_reg.onsite_enabled(None)) is True
    assert _run(onsite_reg.walkin_link("yl_bot", None)) == "https://t.me/yl_bot?start=walkin"


# ══════════════════════════════════════════════════════════════════════════════════════════
# «Ждут на стойке»: убрать из списка, постранично и поиск; частота запусков анкеты
# ══════════════════════════════════════════════════════════════════════════════════════════

def _today(hhmmss="10:00:00"):
    return f"{msk_now().strftime('%Y-%m-%d')} {hhmmss}"


def test_remove_pending_walkin_deletes_row_and_logs_without_pii(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    _walkin(953901, city="spb", name="Лишний Гость")
    resp = client.post(f"{ONSITE}/remove", json={"telegram_id": 953901}, headers=_hdr(BOUND_MANAGER_ID))
    assert resp.status_code == 200 and resp.json()["status"] == "removed"
    assert _row(953901) is None
    rows = [r for r in _rows("SELECT * FROM venue_log WHERE action = ?", venue_log.ACTION_ONSITE_REMOVE)]
    assert len(rows) == 1
    assert rows[0]["telegram_id"] is None and rows[0]["staff_id"] == BOUND_MANAGER_ID
    assert "Лишний" not in (rows[0]["details"] or "")
    assert venue_log.ACTION_LABELS[venue_log.ACTION_ONSITE_REMOVE]


def test_remove_refuses_regular_or_decided_rows(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(_insert_user(953902, status="pending", city="spb"))
    _walkin(953903, city="spb")
    _exec("UPDATE users SET status = 'approved' WHERE telegram_id = 953903")
    for uid in (953902, 953903, 959999):
        body = client.post(f"{ONSITE}/remove", json={"telegram_id": uid}, headers=_hdr(BOUND_MANAGER_ID)).json()
        assert body["status"] == "not_removable", uid
    assert _row(953902) is not None and _row(953903) is not None


def test_remove_needs_approve_right_and_own_city(tmp_path):
    client = _ready(tmp_path, enable=("spb", "msk"))
    _set_role_caps("game_manager", "moderate_game;checkin")
    _walkin(953904, city="spb")
    resp = client.post(f"{ONSITE}/remove", json={"telegram_id": 953904}, headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    _grant_checkin_to_bound_manager()  # spb
    _walkin(953905, city="msk")
    body = client.post(f"{ONSITE}/remove", json={"telegram_id": 953905}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "not_removable"
    assert _row(953904) is not None and _row(953905) is not None


def test_pending_list_is_paginated_with_total_and_search(tmp_path):
    client = _ready(tmp_path)
    _grant_checkin_to_bound_manager()
    for i in range(25):
        uid = 954000 + i
        _walkin(uid, city="spb", name=f"Гость{i:02d} Иван")
        _exec("UPDATE users SET registration_date = ? WHERE telegram_id = ?", _today(f"10:{i:02d}:00"), uid)
    _walkin(954100, city="spb", name="Ёлкина Мария")
    _exec("UPDATE users SET registration_date = ? WHERE telegram_id = 954100", _today("09:00:00"))

    first = client.get(f"{ONSITE}/pending", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert first["total"] == 26 and len(first["items"]) == 20 and first["next_offset"] == 20
    second = client.get(f"{ONSITE}/pending?offset=20", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert len(second["items"]) == 6 and second["next_offset"] is None
    found = client.get(f"{ONSITE}/pending?q=елкина", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert [it["telegram_id"] for it in found["items"]] == [954100] and found["total"] == 1


def test_scanner_js_has_remove_paging_and_search():
    src = SCANNER_JS.read_text(encoding="utf-8")
    assert "onsite/remove" in src and "onsite_remove_confirm_text" in src
    loader = _js_function(src, "loadOnsitePending")
    assert "offset=" in loader and "q=" in loader
    remove = _js_function(src, "removeWalkin")
    assert remove.index("askConfirm(") < remove.index("onsite/remove")


def test_remove_texts_in_registry():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    for key in ("onsite_remove_button_text", "onsite_remove_confirm_text", "onsite_reg_rate_limited_text"):
        assert SETTINGS_SCHEMA[key]["default"] in FORM_DEFAULT_EN, key
    assert "{name}" in SETTINGS_SCHEMA["onsite_remove_confirm_text"]["default"]


def test_walkin_starts_are_rate_limited_per_user(tmp_path):
    from handlers import onsite_reg as onsite_handlers
    from tests.test_onsite_reg_chat_260927 import _Msg
    from tests.test_roles_phase8 import _fresh_state
    _chat_ready(tmp_path)
    _run(bot_db.set_setting("onsite_reg_enabled", "on"))
    onsite_handlers._start_times.clear()
    uid = 954200
    for _ in range(onsite_handlers._START_LIMIT):
        msg = _Msg(uid)
        assert _run(onsite_handlers.start_walkin(msg, _fresh_state(uid), None)) is True
        assert SETTINGS_SCHEMA["onsite_reg_intro_text"]["default"] in msg.texts()[0]
    msg = _Msg(uid)
    state = _fresh_state(uid)
    assert _run(onsite_handlers.start_walkin(msg, state, None)) is True
    assert msg.texts()[0] == SETTINGS_SCHEMA["onsite_reg_rate_limited_text"]["default"]
    assert _run(state.get_state()) is None
    other = _Msg(954201)
    _run(onsite_handlers.start_walkin(other, _fresh_state(954201), None))
    assert SETTINGS_SCHEMA["onsite_reg_intro_text"]["default"] in other.texts()[0]
    onsite_handlers._start_times.clear()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Пост рейтинга в чат: двойное нажатие и повтор той же недели не дают второго поста
# ══════════════════════════════════════════════════════════════════════════════════════════

class _SlowBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        import asyncio
        await asyncio.sleep(0.05)
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": 1})()


def _rating_ready(tmp_path):
    from tests import test_chat_rating_post_260927 as t
    path = t._ready(tmp_path)
    t._rules_mode(path)
    t._seed_comments(path)
    return t


def test_double_tap_publish_posts_once(tmp_path):
    import asyncio
    from handlers import admin_chat_rating_post as post
    t = _rating_ready(tmp_path)
    post._publishing.clear()
    post._published_messages.clear()
    bot = _SlowBot()
    shared = t._Message()
    first, second = t._Callback("chpost:go:spb"), t._Callback("chpost:go:spb")
    first.message = second.message = shared
    shared.message_id = 77

    async def both():
        await asyncio.gather(post.chpost_publish_go(first, bot, now=t.NOW),
                             post.chpost_publish_go(second, bot, now=t.NOW))

    _run(both())
    assert len(bot.sent) == 1
    assert "Опубликовано" in shared.edited
    busy = [a for cb in (first, second) for a in cb.answers if a[0] and "Публикую" in a[0]]
    assert busy  # второй тап получил «уже публикую»

    replay = t._Callback("chpost:go:spb")
    replay.message = shared  # тот же экран подтверждения, запоздавший колбэк
    _run(post.chpost_publish_go(replay, bot, now=t.NOW))
    assert len(bot.sent) == 1


def test_scheduled_post_skips_week_already_posted(tmp_path, monkeypatch):
    from datetime import timedelta
    from handlers import admin_chat_rating_post as post
    from services import chat_rating_post as crp
    from services import scheduler as sched
    t = _rating_ready(tmp_path)
    post._publishing.clear()
    post._published_messages.clear()
    _run(bot_db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on"))
    bot = t._Bot()
    monkeypatch.setattr(sched, "_bot", bot)
    now = {"v": t.NOW}
    monkeypatch.setattr(crp, "msk_now", lambda: now["v"])

    _run(crp.run_job("spb"))
    assert len(bot.sent) == 1
    # время поста перенесли на тот же день позже — джоба сработала ещё раз
    _run(crp.run_job("spb"))
    assert len(bot.sent) == 1

    now["v"] = t.NOW + timedelta(days=7)  # следующая неделя — публикуется
    _run(crp.run_job("spb"))
    assert len(bot.sent) == 2


def test_manual_publish_counts_as_posted_week(tmp_path, monkeypatch):
    from handlers import admin_chat_rating_post as post
    from services import chat_rating_post as crp
    from services import scheduler as sched
    t = _rating_ready(tmp_path)
    post._publishing.clear()
    post._published_messages.clear()
    _run(bot_db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on"))
    bot = t._Bot()
    _run(post.chpost_publish_go(t._Callback("chpost:go:spb"), bot, now=t.NOW))
    assert len(bot.sent) == 1
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(crp, "msk_now", lambda: t.NOW)
    _run(crp.run_job("spb"))
    assert len(bot.sent) == 1


def test_failed_scheduled_post_is_retried_same_week(tmp_path, monkeypatch):
    from services import chat_rating_post as crp
    from services import scheduler as sched
    t = _rating_ready(tmp_path)
    _run(bot_db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on"))
    monkeypatch.setattr(crp, "msk_now", lambda: t.NOW)
    monkeypatch.setattr(sched, "_bot", t._Bot(fail=RuntimeError("Forbidden")))
    _run(crp.run_job("spb"))
    ok_bot = t._Bot()
    monkeypatch.setattr(sched, "_bot", ok_bot)
    _run(crp.run_job("spb"))
    assert len(ok_bot.sent) == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# Согласие не записалось — анкета не продолжается
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_consent_write_failure_stops_with_human_text(tmp_path, monkeypatch):
    from handlers import onsite_reg as onsite_handlers
    from tests.test_onsite_reg_chat_260927 import _Cb
    from tests.test_roles_phase8 import _fresh_state
    _chat_ready(tmp_path)

    async def _boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(onsite_handlers, "record_user_consent", _boom)
    uid = 954301
    state = _fresh_state(uid)
    _run(state.set_state(onsite_handlers.OnsiteReg.consent))
    cb = _Cb(uid, "onsite_consent")
    _run(onsite_handlers.onsite_consent(cb, state))
    assert _run(state.get_state()) == onsite_handlers.OnsiteReg.consent.state
    assert cb.alerts and cb.alerts[-1][1] is True
    assert cb.alerts[-1][0] == SETTINGS_SCHEMA["onsite_reg_consent_failed_text"]["default"]
    assert cb.message.texts() == []  # вопроса об имени нет
    assert _row(uid) is None


def test_consent_failed_text_has_english_default():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    meta = SETTINGS_SCHEMA["onsite_reg_consent_failed_text"]
    assert meta["group"] == "reg" and meta["default"] in FORM_DEFAULT_EN


# ══════════════════════════════════════════════════════════════════════════════════════════
# Тексты волонтёру — из реестра (D-34), с английским дефолтом
# ══════════════════════════════════════════════════════════════════════════════════════════

VOLUNTEER_TEXTS = ("onsite_wrong_city_text", "onsite_approved_scanner_text",
                   "onsite_link_error_text", "onsite_link_no_city_text")


def test_volunteer_texts_in_registry_with_english_defaults():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    staff_group = SETTINGS_SCHEMA["checkin_undo_button_text"]["group"]
    for key in VOLUNTEER_TEXTS:
        meta = SETTINGS_SCHEMA[key]
        assert meta["type"] == "text" and meta["group"] == staff_group, key
        assert meta["default"] in FORM_DEFAULT_EN, key
    assert "{city}" in SETTINGS_SCHEMA["onsite_wrong_city_text"]["default"]
    src = (Path(__file__).resolve().parents[1] / "miniapp" / "routers" / "checkin.py").read_text(encoding="utf-8")
    assert "_ONSITE_APPROVED_TEXT" not in src and "_LINK_NO_CITY_TEXT" not in src


def test_scanner_uses_registry_texts(tmp_path):
    client = _ready(tmp_path, enable=("spb", "msk"))
    _grant_checkin_to_bound_manager()
    _run(bot_db.set_setting("onsite_approved_scanner_text", "Пропущен у стойки"))
    _run(bot_db.set_setting("onsite_wrong_city_text", "Чужой город: {city}"))
    _run(_insert_user(954401, status="pending", city="spb"))
    ok = client.post(f"{ONSITE}/approve", json={"telegram_id": 954401}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert ok["reason_text"] == "Пропущен у стойки"
    _run(_insert_user(954402, status="pending", city="msk"))
    wrong = client.post(f"{ONSITE}/approve", json={"telegram_id": 954402}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert wrong["status"] == "wrong_city" and wrong["reason_text"].startswith("Чужой город: ")

    _run(bot_db.set_setting("onsite_link_no_city_text", "Сначала город"))
    from tests.test_miniapp_routes import ADMIN_ID as MINI_ADMIN
    body = client.get(f"{ONSITE}/link", headers=_hdr(MINI_ADMIN)).json()
    assert body["status"] == "error" and body["reason_text"] == "Сначала город"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Сезон не задан — одобрение у стойки не стирает сезон строки; вуз не длиннее потолка
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_approve_onsite_without_event_season_keeps_row_season(tmp_path):
    _seed_ready(tmp_path)
    _run(_insert_user(954501, status="pending", city="spb", season="YL'25"))
    assert _run(bot_db.approve_onsite(954501, by_staff_id=STAFF_ID, season=None)) is True
    row = _row(954501)
    assert row["status"] == "approved"
    assert row["season"] == "YL'25"
    assert row["prev_season"] is None


def test_approve_at_door_with_empty_event_season_keeps_season(tmp_path):
    _seed_ready(tmp_path)
    _onsite_on()
    _run(bot_db.set_setting("event_season", ""))
    _run(_insert_user(954502, status="pending", city="spb", season="YL'25"))
    res = _door(954502)
    assert res["status"] == "new"
    assert _row(954502)["season"] == "YL'25"


def test_university_answer_is_capped_with_hint(tmp_path):
    from handlers import onsite_reg as onsite_handlers
    from services.i18n_form_manual import FORM_DEFAULT_EN
    from tests.test_onsite_reg_chat_260927 import _Msg
    from tests.test_roles_phase8 import _fresh_state
    _chat_ready(tmp_path)
    uid = 954601
    state = _fresh_state(uid)
    _run(state.set_state(onsite_handlers.OnsiteReg.university))
    _run(state.update_data(onsite_city="spb", onsite_name="Иванова Мария", onsite_phone="+79991234567"))
    msg = _Msg(uid, "В" * (onsite_handlers._MAX_UNIVERSITY_LEN + 1))
    _run(onsite_handlers.onsite_university(msg, state))
    assert _run(state.get_state()) == onsite_handlers.OnsiteReg.university.state
    assert msg.texts()[0] == SETTINGS_SCHEMA["onsite_reg_university_too_long_text"]["default"]
    assert _row(uid) is None
    assert onsite_handlers._MAX_UNIVERSITY_LEN == 200
    assert SETTINGS_SCHEMA["onsite_reg_university_too_long_text"]["default"] in FORM_DEFAULT_EN

    ok = _Msg(uid, "В" * onsite_handlers._MAX_UNIVERSITY_LEN)
    _run(onsite_handlers.onsite_university(ok, state))
    assert _row(uid)["university"] == "В" * onsite_handlers._MAX_UNIVERSITY_LEN


def test_outbox_event_is_enqueued_after_decision_journal(tmp_path, monkeypatch):
    """Бот может разобрать событие сразу после постановки — к этому моменту строка журнала
    решений уже должна быть, иначе проверка «журнал не записан» подняла бы ложную тревогу."""
    _seed_ready(tmp_path)
    _onsite_on()
    _run(_insert_user(954701, status="pending", city="spb"))
    seen = []
    real = onsite_reg.ensure_onsite_outbox

    async def _spy(user):
        seen.append(len(_decisions(954701)))
        await real(user)

    monkeypatch.setattr(onsite_reg, "ensure_onsite_outbox", _spy)
    _door(954701)
    assert seen and seen[0] == 1
    assert len(_onsite_events(954701)) == 1
