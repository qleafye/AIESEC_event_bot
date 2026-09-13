"""Phase 30 (30-05, задача 3, A2-07): расширение `GET /app/api/hub/status` под полноэкранный
статус заявки — три состояния (на проверке / одобрена / отклонена), плита-ссылка на хабе,
`#/status` в ROUTES. Харнесс — тот же приём, что `tests/test_miniapp_hub_status_260904.py`
(`TestClient` + `make_init_data`, фикстуры из `tests/test_miniapp_routes.py`).

T-30-11/T-30-12 (threat register): причина отказа строго СВОЯ (по `p.telegram_id` из
`form_gate`, параметра «чей статус» у ручки нет) и без имени менеджера ни в одном поле ответа.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    REJECTED_ID,
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


def _seed_rejection(tid: int, reason: str | None):
    _run(_sql(
        "INSERT INTO application_decisions "
        "(telegram_id, decision, reason, decided_by, decided_at, effects_due_at, effects_sent_at) "
        "VALUES (?, 'rejected', ?, 999, '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
        "'2026-01-01 00:00:00')",
        (tid, reason),
    ))


def _set_rejected_at(tid: int, when: str):
    _run(_sql("UPDATE users SET rejected_at = ? WHERE telegram_id = ?", (when, tid)))


def _set_payment(tid: int, *, option: str | None, due: str | None):
    _run(_sql(
        "UPDATE users SET payment_option = ?, payment_due = ? WHERE telegram_id = ?",
        (option, due, tid),
    ))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_status_screen.db")
    _standard_seed()
    return _client(_cfg(db_path))


def _enable_status_screen():
    _set("reg_form_status_screen", "on")


# ── Тумблер выключен (дефолт) — сегодняшнее поведение, ни одного нового поля ────────────────

def test_toggle_off_publishes_no_new_fields_for_any_state(client):
    for tid in (PENDING_ID, REJECTED_ID, DELEGATE_ID):
        body = client.get("/app/api/hub/status", headers=_hdr(tid)).json()
        assert body["status_screen_enabled"] is False
        assert body["badge"] is None
        assert body["title"] is None
        assert body["tile_text"] is None
        assert body["next_steps"] == []
        assert body["payment"] is None


def test_toggle_off_approved_gets_empty_stub_as_before(client):
    body = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID)).json()
    assert body["status"] == "approved"
    assert body["heading"] is None
    assert body["body"] is None


# ── Тумблер включён: три состояния несут полный контракт экрана статуса ────────────────────

def test_pending_with_toggle_on_gets_badge_title_and_three_next_steps(client):
    _enable_status_screen()
    body = client.get("/app/api/hub/status", headers=_hdr(PENDING_ID)).json()
    assert body["status_screen_enabled"] is True
    assert body["badge"]
    assert body["title"]
    assert len(body["next_steps"]) == 3
    for step in body["next_steps"]:
        assert step["title"]
    assert body["tile_text"]


def test_approved_without_payment_option_has_no_payment_card(client):
    _enable_status_screen()
    body = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID)).json()
    assert body["status"] == "approved"
    assert body["status_screen_enabled"] is True
    assert body["badge"]
    assert body["title"]
    assert body["payment"] is None
    assert body["pay_button_text"] is None
    assert len(body["next_steps"]) == 3


def test_approved_with_payment_enabled_and_matching_option_has_amount_and_due_date(client):
    _enable_status_screen()
    _set("payment_enabled", "on")
    _set("payment_options", "Полная форма|3500")
    _set_payment(DELEGATE_ID, option="Полная форма", due="15.10.2026 12:00")
    body = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID)).json()
    assert body["payment"]["amount"] == 3500
    assert body["payment"]["due_date"] == "15.10.2026"
    assert "15.10.2026" in body["tile_text"]
    assert body["pay_button_text"]


def test_approved_title_and_body_substitute_name_date_and_place(client):
    _enable_status_screen()
    _run(_sql("UPDATE users SET full_name = ? WHERE telegram_id = ?", ("Мария", DELEGATE_ID)))
    _set("event_date", "22-24.08.2026")
    _set("event_place_name", "Санкт-Петербург")
    body = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID)).json()
    assert "Мария" in body["title"]
    assert "{имя}" not in body["title"]
    assert "22-24.08.2026" in body["screen_body"]
    assert "Санкт-Петербург" in body["screen_body"]
    assert "{дата}" not in body["screen_body"]
    assert "{город}" not in body["screen_body"]


def test_rejected_with_toggle_on_has_reason_and_date_without_manager_name(client):
    _enable_status_screen()
    _seed_rejection(REJECTED_ID, "Анкета заполнена не полностью")
    _set_rejected_at(REJECTED_ID, "2026-09-12 14:30:00")
    body = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID)).json()
    assert body["reason_text"] == "Анкета заполнена не полностью"
    assert body["reason_date"] == "12 сентября"
    assert body["resubmit_button_text"]
    assert body["saved_answers_label"]
    # T-30-12: причина отказа — дословно из поля менеджера, БЕЗ имени менеджера ни в одном поле.
    for value in body.values():
        if isinstance(value, str):
            assert "Менеджер" not in value


def test_rejected_reason_is_strictly_own_telegram_id(client):
    """T-30-11: параметра «чей статус» у ручки нет — причина ЧУЖОГО отказа физически
    недостижима (запрос идёт строго по telegram_id из init-данных заголовка)."""
    _enable_status_screen()
    _seed_rejection(REJECTED_ID, "Причина первого делегата")
    body_rejected = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID)).json()
    body_pending = client.get("/app/api/hub/status", headers=_hdr(PENDING_ID)).json()
    assert body_rejected["reason_text"] == "Причина первого делегата"
    assert body_pending["reason_text"] is None


def test_rejected_without_decision_row_has_no_reason_or_date(client):
    _enable_status_screen()
    body = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID)).json()
    assert body["reason_text"] is None
    assert body["reason_date"] == ""


# ── #/status зарегистрирован маршрутом ──────────────────────────────────────────────────────

def test_status_screen_module_is_registered_in_routes():
    from tests.test_miniapp_frontend import _routes_from_app_js

    routes = _routes_from_app_js()
    assert routes.get("#/status") == "screens/status.js"


def test_status_screen_module_exists_and_exports_render():
    from tests.test_miniapp_frontend import MINIAPP_STATIC

    status_js = MINIAPP_STATIC / "js" / "screens" / "status.js"
    text = status_js.read_text(encoding="utf-8")
    assert "export async function render(root, params, ctx)" in text


# ── Плита на хабе не рисуется при выключенном тумблере (структурный сторож hub.js) ──────────

def test_hub_js_gates_status_tile_click_behavior_on_status_screen_enabled():
    from tests.test_miniapp_frontend import MINIAPP_STATIC, _js_without_comments

    text = _js_without_comments(MINIAPP_STATIC / "js" / "screens" / "hub.js")
    assert "status.status_screen_enabled" in text
    assert "navigate(\"#/status\")" in text
