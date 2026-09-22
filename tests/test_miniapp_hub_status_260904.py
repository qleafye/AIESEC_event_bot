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


def test_auto_rejected_delegate_sees_reason_and_date_like_manual_reject(client):
    """Phase 31 (31-06, D-22): экран статуса заявки после автоотказа — тот же, что при обычном
    отказе (статус, причина, дата). `hub_status` читает `application_decisions`/`users.
    rejected_at` универсально, без ветвления по `decided_by` — запись с сентинелом
    AUTO_DECIDED_BY уже даёт паритет без единой правки hub.py (Pitfall 1)."""
    from services.reject_journal import AUTO_DECIDED_BY

    _set("reg_form_status_screen", "on")
    _run(_sql(
        "UPDATE users SET rejected_at = '2026-09-20 10:00:00' WHERE telegram_id = ?",
        (REJECTED_ID,),
    ))
    _run(_sql(
        "INSERT INTO application_decisions "
        "(telegram_id, decision, reason, decided_by, decided_at, effects_due_at, effects_sent_at) "
        "VALUES (?, 'rejected', ?, ?, '2026-09-20 10:00:00', '2026-09-20 10:00:00', "
        "'2026-09-20 10:00:00')",
        (REJECTED_ID, "Мест на выбранный курс уже нет — набор на него закрыт.", AUTO_DECIDED_BY),
    ))
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reason_line"] == (
        "Причина: Мест на выбранный курс уже нет — набор на него закрыт."
    )
    assert body["reason_text"] == "Мест на выбранный курс уже нет — набор на него закрыт."
    assert body["reason_date"]  # непусто — как после обычного отказа, не «автоматически»


def test_unregistered_does_not_crash(client):
    # form_gate пропускает незарегистрированного (он тот, у кого ещё нет анкеты) — ручка не
    # должна падать 500, даже если по плану 260904-aup он читается как "approved" (пустая
    # строка users -> user.get("status") пусто -> "approved", ручка не выдумывает "none").
    resp = client.get("/app/api/hub/status", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["heading"] is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Квик 260922-wrg (задача 2, B-2/B-3/A-5): плита возвращенца, gate повторной подачи.
# ══════════════════════════════════════════════════════════════════════════════════════════

def _set_season(telegram_id: int, season: str | None):
    _run(_sql("UPDATE users SET season = ? WHERE telegram_id = ?", (season, telegram_id)))


def test_approved_past_season_gets_returning_plate_not_approved(client):
    """B-1/B-2: approved прошлого сезона — возвращенец, а не «Одобрена»."""
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'25")
    _set("start_text_returning", "Привет снова, {season}!")
    _set("start_returning_cta_text", "🚀 Обновить анкету")
    resp = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["status"] == "returning"
    assert "YL'25" in body["heading"]
    assert body["cta_text"] == "🚀 Обновить анкету"
    assert body["status_screen_enabled"] is False
    assert body["tile_text"] == body["heading"]


def test_rejected_past_season_gets_returning_plate_not_rejected(client):
    """B-1: rejected прошлого сезона — тоже возвращенец, не экран отказа с причиной."""
    _set("event_season", "YL'26")
    _set_season(REJECTED_ID, "YL'25")
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["status"] == "returning"
    assert body["reason_line"] is None


def test_rejected_current_season_keeps_rejected_plate_with_reason(client):
    """B-3: отклонённый ТЕКУЩЕГО сезона (season == event_season) — прежняя плита отказа."""
    _set("event_season", "YL'26")
    _set_season(REJECTED_ID, "YL'26")
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["status"] == "rejected"


def test_rejected_no_season_configured_keeps_rejected_plate(client):
    """event_season не настроен -> is_past_season_row всегда False (fail-soft byte-в-byte)."""
    _set_season(REJECTED_ID, None)
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["status"] == "rejected"


def test_resubmit_deny_removes_cta_for_rejected_current_season(client):
    """A-5: «нельзя» убирает cta_text у отклонённого ТЕКУЩЕГО сезона."""
    _set("event_season", "YL'26")
    _set_season(REJECTED_ID, "YL'26")
    _set("reg_resubmit_after_reject", "deny")
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["cta_text"] is None


def test_resubmit_allow_keeps_cta_for_rejected_current_season(client):
    _set("event_season", "YL'26")
    _set_season(REJECTED_ID, "YL'26")
    _set("reg_resubmit_after_reject", "allow")
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["cta_text"]


def test_resubmit_deny_does_not_touch_returning_past_season_plate(client):
    """Deny не касается возвращенца прошлого сезона — та же плита, с cta."""
    _set("event_season", "YL'26")
    _set_season(REJECTED_ID, "YL'25")
    _set("reg_resubmit_after_reject", "deny")
    _set("start_returning_cta_text", "🚀 Обновить анкету")
    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["status"] == "returning"
    assert body["cta_text"] == "🚀 Обновить анкету"
