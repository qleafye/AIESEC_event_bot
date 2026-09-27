"""Решение владельца (Q8): отказы скана/поиска в Mini App пишутся в журнал площадки строкой
«⛔ не пропустил(а)» с кодом причины и тем, кто сканировал. Из данных делегата — только
`telegram_id` (если найден); ФИО из самого QR в журнал не попадает. Запись fail-soft.
Харнесс — тот же, что `tests/test_miniapp_checkin_260924.py`."""
from __future__ import annotations

import json

from database import db as bot_db
from handlers import admin_venue
from services import venue_log
from services.checkin import build_payload

from tests.test_miniapp_checkin_260924 import (
    BASE,
    TAG,
    _grant_checkin_to_bound_manager,
    _grant_checkin_to_game_manager,
    _insert_user,
    _qr,
    _run,
    client_with,
)
from tests.test_miniapp_routes import ADMIN_ID, BOUND_MANAGER_ID, GAME_MANAGER_ID, _hdr


def _denials():
    rows, _total = _run(bot_db.venue_log_page(limit=50))
    return [r for r in rows if r["action"] == venue_log.ACTION_DENIED]


def test_scan_not_approved_is_logged_with_reason_and_staff(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 952001
    _run(_insert_user(uid, full_name="Иванов Иван", status="pending"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "denied"
    [row] = _denials()
    assert row["details"] == {"reason": "not_approved"}
    assert row["staff_id"] == GAME_MANAGER_ID
    assert row["telegram_id"] == uid
    assert row["source"] == "miniapp"
    assert "Иванов" not in json.dumps(row, ensure_ascii=False)


def test_scan_unknown_qr_logs_no_personal_data(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    payload = build_payload(TAG, "Чужой Чужаков", "Тюмень", "no-such-token")
    body = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "not_found"
    [row] = _denials()
    assert row["details"] == {"reason": "no_user"}
    assert row["telegram_id"] is None
    assert "Чужак" not in json.dumps(row, ensure_ascii=False)


def test_scan_foreign_event_is_logged(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 952002
    _run(_insert_user(uid))
    client.post(f"{BASE}/scan", json={"payload": _qr(uid, tag="OTHERFEST")}, headers=_hdr(GAME_MANAGER_ID))
    [row] = _denials()
    assert row["details"] == {"reason": "foreign_event"}
    assert row["telegram_id"] is None


def test_entry_other_city_denial_logged_under_stand_city(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()  # привязан к spb
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 952003
    _run(_insert_user(uid, full_name="Морозов Марк", city="msk"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid, city="Москва")}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "wrong_city"
    [row] = _denials()
    assert row["details"] == {"reason": "wrong_city"}
    assert row["city"] == "spb"  # отказ виден менеджеру города стойки
    assert row["staff_id"] == BOUND_MANAGER_ID


def test_manual_denial_is_logged_as_search(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 952004
    _run(_insert_user(uid, status="rejected"))
    client.post(f"{BASE}/manual", json={"telegram_id": uid}, headers=_hdr(GAME_MANAGER_ID))
    [row] = _denials()
    # Отклонённая заявка — свой код «rejected» (ревью 28.09), не «на рассмотрении».
    assert row["source"] == "manual" and row["details"] == {"reason": "rejected"}


def test_successful_scan_writes_no_denial(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 952005
    _run(_insert_user(uid))
    client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID))
    client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID))  # duplicate
    assert _denials() == []


def test_journal_failure_does_not_break_scan_answer(tmp_path, monkeypatch):
    async def _boom(*_a, **_kw):
        raise RuntimeError("journal down")

    monkeypatch.setattr(venue_log, "log_denial", _boom)
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 952006
    _run(_insert_user(uid, status="pending"))
    resp = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"


def test_journal_screen_shows_denial_reason_and_staff_filter(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 952007
    _run(_insert_user(uid, full_name="Петров Пётр", status="pending"))
    client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID))
    text, _kb = _run(admin_venue.render_log_screen(ADMIN_ID))
    assert "не пропустил(а)" in text and "заявка не одобрена" in text
    assert "not_approved" not in text
    text_f, _kb = _run(admin_venue.render_log_screen(ADMIN_ID, staff_id=GAME_MANAGER_ID))
    assert "заявка не одобрена" in text_f
    text_other, _kb = _run(admin_venue.render_log_screen(ADMIN_ID, staff_id=BOUND_MANAGER_ID))
    assert "заявка не одобрена" not in text_other
    staff = _run(bot_db.venue_log_staff())
    assert [s["staff_id"] for s in staff] == [GAME_MANAGER_ID]


def test_every_denial_code_has_human_label():
    from services.checkin import DENIAL_REASON_TEXT

    for code in (*DENIAL_REASON_TEXT, "wrong_city", "wrong_city_point", "wrong_day", "invalid_point"):
        assert code in venue_log.DENIAL_LABELS, code
