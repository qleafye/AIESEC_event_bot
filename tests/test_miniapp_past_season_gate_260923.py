"""Квик 260923-en2 (QUICK-260923-EN2): решение владельца 23.09 «закрой разделы до новой
анкеты» — делегат прошлого сезона (`users.season` задана и != `event_season`) получает 403
`{reason: delegate_gate, kind: past_season}` на всех делегатских ручках Mini App, а не только
скрытие пунктов на клиенте. Харнесс — `tests/test_miniapp_routes.py`, фикстуры по образцу
`tests/test_miniapp_delegate.py::test_me_form_status_returning_for_past_season_row`.

Задача 2 (фронт) добавляет source-guard тесты по app.js в этот же файл — см. нижнюю секцию.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api

from tests.test_miniapp_applications import REG_MANAGER_ID
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    GAME_MANAGER_ID,
    PENDING_ID,
    REJECTED_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
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


def _set_season(telegram_id: int, season: str | None):
    _run(_sql("UPDATE users SET season = ? WHERE telegram_id = ?", (season, telegram_id)))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_past_season_gate.db")
    _standard_seed()
    return _client(_cfg(db_path))


# ── delegate_denial: прошлый сезон закрыт на всех делегатских ручках ────────────────────────

GAME_ROUTES = (
    "/app/api/coins/balance",
    "/app/api/leaderboard",
    "/app/api/tasks",
    "/app/api/hub",
)


@pytest.mark.parametrize("route", GAME_ROUTES)
def test_past_season_delegate_gets_403_on_game_routes(client, route):
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'25")
    resp = client.get(route, headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"reason": "delegate_gate", "kind": "past_season"}


@pytest.mark.parametrize("status,tid", [("pending", PENDING_ID), ("rejected", REJECTED_ID)])
def test_past_season_pending_and_rejected_also_get_past_season_kind(client, status, tid):
    """Прошлый сезон одинаково закрыт независимо от статуса (approved/pending/rejected) —
    не подменяется обычным kind "pending"/"rejected"."""
    _set("event_season", "YL'26")
    _set_season(tid, "YL'25")
    resp = client.get("/app/api/coins/balance", headers=_hdr(tid))
    assert resp.status_code == 403
    assert resp.json()["kind"] == "past_season"


def test_current_season_delegate_not_affected(client):
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'26")
    resp = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text


def test_empty_season_or_event_season_keeps_previous_behavior(client):
    # season не задан вовсе (стандартный сид _standard_seed не пишет season) -> approved проходит.
    _set("event_season", "YL'26")
    resp = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text

    # event_season не настроен (пустая строка в реестре) -> approved с season тоже проходит.
    _set("event_season", "")
    _set_season(DELEGATE_ID, "YL'25")
    resp = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text


def test_staff_with_past_season_row_still_uses_manager_route(client):
    """Сотрудник (caps не пуст) со своей строкой users из прошлого сезона — менеджерская ручка
    (за require_cap) не должна интересоваться его season, менеджерское поведение не меняется."""
    _set("event_season", "YL'26")
    _seed(staff=[(REG_MANAGER_ID, "reg_manager", None)], users=[(REG_MANAGER_ID, "approved")])
    _set_season(REG_MANAGER_ID, "YL'25")
    resp = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200, resp.text


def test_me_is_delegate_false_for_past_season(client):
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'25")
    resp = client.get("/app/api/me", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_delegate"] is False
    assert body["form_status"] == "returning"


def test_hub_status_still_200_for_past_season(client):
    """`/app/api/hub/status` стоит за `form_gate`, не `delegate_gate` — возвращенцу отвечает
    200 status "returning" (уже реализовано в квике 260922-wrg)."""
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'25")
    resp = client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "returning"


def test_after_new_season_submission_ordinary_status_gate_applies(client):
    """После «подачи новой анкеты» (season обновлён на текущий) доступ снова определяется
    обычным статусом: pending закрыт как pending, approved открыт."""
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'25")
    # Эмуляция подачи новой анкеты: season = текущему сезону, статус pending.
    _run(_sql(
        "UPDATE users SET season = ?, status = ? WHERE telegram_id = ?",
        ("YL'26", "pending", DELEGATE_ID),
    ))
    resp = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["kind"] == "pending"

    _run(_sql("UPDATE users SET status = ? WHERE telegram_id = ?", ("approved", DELEGATE_ID)))
    resp = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text


# ── upload_actor: возвращенец с живым черновиком грузит резюме ──────────────────────────────

class _FakeBotApi:
    def handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"document": {"file_id": "BQACresume"}}})


@pytest.fixture
def bot_api(monkeypatch):
    fake = _FakeBotApi()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


def test_upload_actor_past_season_with_draft_passes(client, bot_api):
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'25")
    _run(bot_db.upsert_reg_draft(DELEGATE_ID, kind="new", source="miniapp"))
    resp = client.post(
        "/app/api/uploads?target=resume",
        files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200, resp.text


def test_upload_actor_past_season_without_draft_gets_403(client):
    _set("event_season", "YL'26")
    _set_season(DELEGATE_ID, "YL'25")
    resp = client.post(
        "/app/api/uploads", content=b"x" * 10, headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": "past_season"}


# ── Задача 2: фронтовая ветка past_season в app.js — source-guard ───────────────────────────

APP_JS = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "js" / "app.js"


def _app_js_text() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_app_js_has_past_season_branch():
    assert "past_season" in _app_js_text()


def test_app_js_past_season_links_to_form_hash():
    text = _app_js_text()
    idx = text.find("past_season")
    assert idx != -1
    # В окрестности ветки past_season должна быть ссылка на форму анкеты.
    window = text[idx: idx + 2500]
    assert "#/form" in window


def test_app_js_past_season_calls_hub_status():
    text = _app_js_text()
    idx = text.find("past_season")
    assert idx != -1
    window = text[idx: idx + 2500]
    assert "/hub/status" in window


def test_app_js_past_season_kind_code_not_rendered_as_detail():
    """Кодовое имя kind не должно показываться человеку как деталь-строка (правило CLAUDE.md
    «бот для людей») — старый приём `(${payload.kind})` не должен захватывать "past_season"."""
    text = _app_js_text()
    # Явная проверка: в обработчике no-access ветка past_season не должна идти через общий
    # detail-путь `(${payload.kind})`, а через отдельный вызов.
    assert 'showPastSeasonState' in text
