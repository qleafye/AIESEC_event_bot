"""Регистрация на месте (FORUM-CHECKIN.md D-41): правки по ревью 28.09.

- отклонённую заявку сканер называет отклонённой (с причиной, если она записана), обычной кнопки
  одобрения у неё нет — только отдельное «Пропустить вопреки отказу» с подтверждением; флип
  вопреки отказу снимает `rejected_at` и маркеры автоотказа, журнал помечает его отдельно.

pytest-asyncio в проекте нет — async через `asyncio.run()` (конвенция проекта)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import cities as cities_mod
from config import config as bot_config
from database import db as bot_db
from services import onsite_reg, venue_log
from services.timeutil import msk_now
from settings_schema import SETTINGS_SCHEMA

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
