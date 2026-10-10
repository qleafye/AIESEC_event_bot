"""Идея №32: «↩️ Отменить» на плашке сканера Mini App — `/app/api/checkin/scan|manual` отдают
`undo` у новой отметки, `/app/api/checkin/undo` снимает только СВОЮ ПОСЛЕДНЮЮ отметку.
Харнесс — тот же, что `tests/test_miniapp_checkin_260924.py`."""
from __future__ import annotations

from database import db as bot_db
from services.forum.checkin import ENTRY_POINT

from tests.test_miniapp_checkin_260924 import (
    BASE,
    _grant_checkin_to_bound_manager,
    _grant_checkin_to_game_manager,
    _insert_user,
    _qr,
    _run,
    client_with,
)
from tests.test_miniapp_routes import ADMIN_ID, BOUND_MANAGER_ID, GAME_MANAGER_ID, _hdr


def _marks(uid):
    return [r["point"] for r in _run(bot_db.list_checkins_for_user(uid))]


def test_scan_new_offers_undo_and_undo_removes_mark(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951001
    _run(_insert_user(uid, city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert body["undo"]["seconds"] == 10
    assert body["undo"]["label"] == "↩️ Отменить"
    assert "log_id" not in body

    r = client.post(f"{BASE}/undo", json={"id": body["undo"]["id"]}, headers=_hdr(GAME_MANAGER_ID))
    assert r.status_code == 200
    assert r.json()["status"] == "undone"
    assert r.json()["reason_text"]
    assert _marks(uid) == []


def test_duplicate_scan_has_no_undo(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951002
    _run(_insert_user(uid, city="spb"))
    client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "duplicate"
    assert "undo" not in body


def test_manual_mark_offers_undo(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951003
    _run(_insert_user(uid, city="spb"))
    body = client.post(f"{BASE}/manual", json={"telegram_id": uid}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new" and body["undo"]["id"]


def test_undo_of_someone_elses_mark_is_refused_on_server(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _grant_checkin_to_bound_manager()
    uid = 951004
    _run(_insert_user(uid, city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    r = client.post(f"{BASE}/undo", json={"id": body["undo"]["id"]}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert r["status"] == "undo_refused"
    assert r["code"] == "not_yours"
    assert "менеджер" in r["reason_text"]
    assert _marks(uid) == [ENTRY_POINT]


def test_undo_of_older_mark_is_refused(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    a, b = 951005, 951006
    _run(_insert_user(a, city="spb"))
    _run(_insert_user(b, city="spb", full_name="Петров Пётр"))
    first = client.post(f"{BASE}/scan", json={"payload": _qr(a)}, headers=_hdr(GAME_MANAGER_ID)).json()
    client.post(f"{BASE}/scan", json={"payload": _qr(b, full_name="Петров Пётр")}, headers=_hdr(GAME_MANAGER_ID))
    r = client.post(f"{BASE}/undo", json={"id": first["undo"]["id"]}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert r["status"] == "undo_refused" and r["code"] == "not_last"
    assert _marks(a) == [ENTRY_POINT]


def test_undo_without_cap_is_403(tmp_path):
    client = client_with(tmp_path)
    r = client.post(f"{BASE}/undo", json={"id": 1}, headers=_hdr(GAME_MANAGER_ID))
    assert r.status_code == 403


def test_scan_journal_keeps_volunteer_name(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951007
    _run(_insert_user(uid, city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    row = _run(bot_db.venue_log_get(body["undo"]["id"]))
    assert row["staff_id"] == GAME_MANAGER_ID
    assert row["source"] == "miniapp"
    assert ADMIN_ID != GAME_MANAGER_ID  # суперадмин тут ни при чём — пишем того, кто сканировал


def test_undo_db_failure_is_human_refusal_not_500(tmp_path, monkeypatch):
    from services.forum import venue_log

    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(venue_log, "undo_last_scan", _boom)
    r = client.post(f"{BASE}/undo", json={"id": 1}, headers=_hdr(GAME_MANAGER_ID))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "undo_refused" and body["code"] == "error"
    assert "менеджера" in body["reason_text"]


def test_undo_of_moved_away_mark_says_it_changed(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 951008
    _run(_insert_user(uid, city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    mark = _run(bot_db.list_checkins_for_user(uid))[0]
    _run(bot_db.revoke_checkin(mark["id"], {"action": "revoke", "staff_id": 1}))
    r = client.post(f"{BASE}/undo", json={"id": body["undo"]["id"]}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert r["status"] == "undo_refused" and r["code"] == "gone"
    assert "уже изменилась" in r["reason_text"]
