"""Сканер x запись на сессии: подсказка на плашке и правка записи волонтёром."""
from __future__ import annotations

from datetime import datetime

from cities import per_city_key
from database import db as bot_db, session_enroll_db
from services import session_enroll as se
from services.checkin import current_event_tag
from tests._enroll38 import CITY, seed_delegates, seed_msk_program
from tests.test_miniapp_checkin_260924 import (
    BASE, GAME_MANAGER_ID, _freeze_now, _grant_checkin_to_bound_manager,
    _grant_checkin_to_game_manager, _hdr, _qr, _run, client_with,
)
from tests.test_miniapp_routes import BOUND_MANAGER_ID

U = 101


def _setup(tmp_path, monkeypatch, *, enabled=True):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _freeze_now(monkeypatch, datetime(2026, 10, 30, 10, 30))

    async def go():
        ids = await seed_msk_program()
        await seed_delegates()
        await bot_db.set_setting("event_season", "YL 26/2")
        await bot_db.set_setting("event_city_enabled", "on")
        if enabled:
            await bot_db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        return ids
    return client, _run(go())


def _scan(client, point, uid=U):
    tag = _run(current_event_tag())
    return client.post(f"{BASE}/scan", json={"payload": _qr(uid, tag=tag), "point": point},
                       headers=_hdr(GAME_MANAGER_ID)).json()


def test_scan_enrolled_other_gets_hint(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)
    _run(se.enroll(U, ids["A"]))
    body = _scan(client, f"session:{ids['B']}")
    assert body["status"] == "new"
    assert "Сессия A" in body["hint"]
    assert body["enroll"]["action"] == "rebook" and body["enroll"]["session_id"] == ids["B"]


def test_scan_not_enrolled_gets_book(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)
    body = _scan(client, f"session:{ids['B']}")
    assert body["enroll"]["action"] == "book"


def test_scan_enrolled_this_no_enroll(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)
    _run(se.enroll(U, ids["B"]))
    body = _scan(client, f"session:{ids['B']}")
    assert body["status"] == "new" and "enroll" not in body


def test_scan_module_off_no_hint(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch, enabled=False)
    body = _scan(client, f"session:{ids['B']}")
    assert body["status"] == "new" and "enroll" not in body and not body.get("hint")


def test_scan_session_without_track_no_hint(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)
    body = _scan(client, f"session:{ids['P']}")
    assert "enroll" not in body


def test_scan_entry_point_untouched(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)
    body = _scan(client, "entry")
    assert "enroll" not in body


def test_hint_failure_does_not_break_scan(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr("miniapp.routers.checkin_enroll.scan_hint", boom)
    body = _scan(client, f"session:{ids['B']}")
    assert body["status"] == "new" and "enroll" not in body


def test_manual_also_gets_hint(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)
    _run(se.enroll(U, ids["A"]))
    body = client.post(f"{BASE}/manual", json={"telegram_id": U, "point": f"session:{ids['B']}"},
                       headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["enroll"]["action"] == "rebook"


def test_enroll_endpoint_rebook(tmp_path, monkeypatch):
    client, ids = _setup(tmp_path, monkeypatch)
    _run(se.enroll(U, ids["A"]))
    _run(bot_db.update_program_session(ids["B"], enroll_closed=1, enroll_limit=1))
    payload = {"telegram_id": U, "session_id": ids["B"]}
    for _ in range(2):  # повтор идемпотентен
        r = client.post(f"{BASE}/enroll", json=payload, headers=_hdr(GAME_MANAGER_ID))
        assert r.status_code == 200 and r.json()["status"] == "ok"
        assert "Сессия B" in r.json()["message"]
    mine = _run(session_enroll_db.list_user_enrollments(U, CITY))
    assert [s["id"] for s in mine] == [ids["B"]]


def test_enroll_endpoint_requires_checkin_cap(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    r = client.post(f"{BASE}/enroll", json={"telegram_id": U, "session_id": 1},
                    headers=_hdr(GAME_MANAGER_ID))
    assert r.status_code == 403


def test_enroll_endpoint_bound_city(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    ids = _run(seed_msk_program())
    r = client.post(f"{BASE}/enroll", json={"telegram_id": U, "session_id": ids["A"]},
                    headers=_hdr(BOUND_MANAGER_ID))
    assert r.json()["status"] == "wrong_city_point"
