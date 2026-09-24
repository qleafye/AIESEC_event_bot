"""D-29 (FORUM-CHECKIN.md, «Решения владельца 24.09») — плитка делегата «📅 Программа» в
Mini App: `GET /app/api/program` (`miniapp/routers/program.py`).

Харнесс — `tests/test_miniapp_routes.py` (тот же процесс/БД, что у соседних роутеров, тот же
приём, что `tests/test_miniapp_faq_260906.py`)."""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import _cfg, _client, _hdr, _seed, _set, _use_tmp_db

DELEGATE_ID = 900924501


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_program.db")
    _seed(
        users=[(DELEGATE_ID, "approved")],
        settings={"miniapp_enabled": "on", "event_name": "форума YouLead"},
    )
    return _client(_cfg(db_path))


def _set_user_city(tid, city):
    _run(bot_db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}", "event_city": city,
        "registration_date": "2026-01-01 00:00:00",
    }))


# ── auth ──────────────────────────────────────────────────────────────────────────────────

def test_program_no_auth_401(client):
    resp = client.get("/app/api/program")
    assert resp.status_code == 401
    assert resp.json() == {"reason": "no_auth"}


def test_program_unregistered_user_403_delegate_gate(client):
    resp = client.get("/app/api/program", headers=_hdr(900999))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "delegate_gate"


# ── дефолт (нет ни фото, ни сессий) — вид "photo", пустое состояние ─────────────────────────

def test_program_defaults_to_photo_view_with_empty_state(client):
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["view"] == "photo"
    assert body["photo_url"] is None
    assert body["days"] == []
    assert body["empty_text"]  # текст из реестра, не пустая строка


# ── вид "photo" с загруженным фото ──────────────────────────────────────────────────────────

def test_program_photo_view_returns_file_proxy_url(client):
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["view"] == "photo"
    assert body["photo_url"] == "/app/api/file/GLOBAL_FILE_ID"
    assert body["empty_text"] is None


def test_program_photo_is_public_asset_no_auth_needed(client):
    """Тег `<img>` не может послать initData (память проекта) — фото программы должно
    отдаваться СОВСЕМ без принципала, как лого/обложка (`is_public_asset`)."""
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    resp = client.get("/app/api/file/GLOBAL_FILE_ID")
    # 404 — сам файл не существует у тестового бота (getFile не резолвится), но КЛЮЧЕВОЕ —
    # не 401: is_public_asset пропустил запрос без принципала дальше, к попытке скачивания.
    assert resp.status_code != 401


# ── вид "table" — сессии заведены ───────────────────────────────────────────────────────────

def test_program_table_view_when_sessions_exist(client):
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["view"] == "table"
    assert len(body["days"]) == 1
    assert body["days"][0]["day"] == "2026-10-30"
    slot = body["days"][0]["slots"][0]
    assert slot["sessions"][0]["title"] == "Открытие"


def test_program_table_view_groups_parallel_sessions(client):
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Трек А"))
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:30", "11:30", "Трек Б"))
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    slots = resp.json()["days"][0]["slots"]
    assert len(slots) == 1
    assert {s["title"] for s in slots[0]["sessions"]} == {"Трек А", "Трек Б"}


# ── per_city фото/вид — своё городское побеждает общее ──────────────────────────────────────

def test_program_per_city_photo_overrides_global(client):
    from cities import per_city_key
    _set("event_city_enabled", "on")
    _set_user_city(DELEGATE_ID, "msk")
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    _set(per_city_key("program_photo_file_id", "msk"), "MSK_FILE_ID")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.json()["photo_url"] == "/app/api/file/MSK_FILE_ID"


def test_program_per_city_view_overrides_global(client):
    from cities import per_city_key
    _set("event_city_enabled", "on")
    _set_user_city(DELEGATE_ID, "msk")
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _set("program_miniapp_view", "photo")
    _set(per_city_key("program_miniapp_view", "msk"), "table")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.json()["view"] == "table"
