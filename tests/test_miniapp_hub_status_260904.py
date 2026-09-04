"""Quick 260904-aup Task 1 (UAT D3): `GET /app/api/hub/status` — плита «Анкета на проверке /
Заявка отклонена» над плитками хаба. Гейт — `form_gate` (не `delegate_gate`: у pending/rejected
`delegate_gate` отдал бы 403, ровно поэтому им сегодня нечего показать). Харнесс — тот же приём,
что `tests/test_miniapp_delegate.py` (`TestClient` + `make_init_data`, `tests/test_miniapp_routes.py`
несёт общие фикстуры/сиды).
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    REJECTED_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


async def _sql(query: str, params=()):
    async with bot_db._connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


def _set_city(telegram_id: int, city: str):
    _run(_sql("UPDATE users SET event_city = ? WHERE telegram_id = ?", (city, telegram_id)))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_hub_status.db")
    _standard_seed()
    return _client(_cfg(db_path))


def test_pending_delegate_sees_pending_plate_with_days(client):
    resp = client.get("/app/api/hub/status", headers=_hdr(PENDING_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["heading"]
    assert body["cta_text"] is None
    assert body["days"] == 3  # дефолт реестра miniapp_hub_pending_days
    assert "{days}" not in body["body"]
    assert "3" in body["body"]


def test_pending_percity_days_override_wins_over_global(client):
    _set("event_city_enabled", "on")
    _set_city(PENDING_ID, "spb")
    _set("miniapp_hub_pending_days", "5")
    _set("miniapp_hub_pending_days__city__spb", "7")
    resp = client.get("/app/api/hub/status", headers=_hdr(PENDING_ID))
    body = resp.json()
    assert body["days"] == 7
    assert "7" in body["body"]


def test_rejected_delegate_sees_rejected_plate_with_cta(client):
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["heading"]
    assert body["body"]
    assert body["cta_text"]
    assert body["days"] is None


def test_approved_delegate_gets_no_heading(client):
    resp = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["heading"] is None


def _seed_rejection(tid: int, reason: str | None):
    """Сеет причину прямой записью в журнал (не гоняя путь отказа целиком) — тот же приём,
    что `_expire` в `tests/test_miniapp_applications.py`. `effects_sent_at` заполнен — строка
    выглядит как настоящее, уже применённое решение."""
    _run(_sql(
        "INSERT INTO application_decisions "
        "(telegram_id, decision, reason, decided_by, decided_at, effects_due_at, effects_sent_at) "
        "VALUES (?, 'rejected', ?, 999, '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
        "'2026-01-01 00:00:00')",
        (tid, reason),
    ))


def test_rejected_delegate_with_reason_sees_reason_line(client):
    _seed_rejection(REJECTED_ID, "Анкета заполнена не полностью")
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["reason_line"] == "Причина: Анкета заполнена не полностью"
    assert "{reason}" not in body["reason_line"]


def test_rejected_delegate_without_reason_gets_no_reason_line(client):
    # Старый отказ (до quick 260904-liz) или отказ без причины — reason_line = None, никакого
    # «Причина: None» в выдаче.
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["reason_line"] is None


def test_pending_and_approved_have_no_reason_line(client):
    body_pending = client.get("/app/api/hub/status", headers=_hdr(PENDING_ID)).json()
    assert body_pending["reason_line"] is None
    body_approved = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID)).json()
    assert body_approved["reason_line"] is None


def test_unregistered_does_not_crash(client):
    # form_gate пропускает незарегистрированного (он тот, у кого ещё нет анкеты) — ручка не
    # должна падать 500, даже если по плану 260904-aup он читается как "approved" (пустая
    # строка users -> user.get("status") пусто -> "approved", ручка не выдумывает "none").
    resp = client.get("/app/api/hub/status", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["heading"] is None
