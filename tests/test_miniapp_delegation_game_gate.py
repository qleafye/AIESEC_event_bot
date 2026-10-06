"""Гейм для делегаций вузов в Mini App: задания, монеты и сдачи закрыты для делегата, пока
менеджер не включил `delegation_game_enabled`; хаб и профиль остаются открытыми."""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

REGULAR_ID = 900120
UNIVERSITY = "Тестовый университет"


async def _set_delegation(telegram_id: int, value):
    async with bot_db._connect() as conn:
        await conn.execute(
            "UPDATE users SET delegation = ? WHERE telegram_id = ?", (value, telegram_id)
        )
        await conn.commit()


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_game_gate.db")
    _standard_seed()
    _seed(users=[(REGULAR_ID, "approved")])
    _set("miniapp_section_tasks", "on")
    _set("miniapp_section_coins", "on")
    asyncio.run(_set_delegation(DELEGATE_ID, UNIVERSITY))
    return _client(_cfg(db_path))


def _game_calls(client, uid):
    h = _hdr(uid)
    return {
        "tasks": client.get("/app/api/tasks", headers=h),
        "balance": client.get("/app/api/coins/balance", headers=h),
        "leaderboard": client.get("/app/api/leaderboard", headers=h),
        "submit": client.post("/app/api/submissions", headers=h, json={"task_id": 1, "parts": []}),
    }


def test_delegate_game_off_gets_403_with_code(client):
    for name, resp in _game_calls(client, DELEGATE_ID).items():
        assert resp.status_code == 403, name
        detail = resp.json()
        assert detail["reason"] == "game_gate", name
        assert detail["code"] == "game_off_for_delegation", name
        assert detail["message"], name


def test_delegate_game_explicit_off_also_403(client):
    _set("delegation_game_enabled", "off")
    assert client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID)).status_code == 403


def test_delegate_game_on_is_open(client):
    _set("delegation_game_enabled", "on")
    calls = _game_calls(client, DELEGATE_ID)
    for name in ("tasks", "balance", "leaderboard"):
        assert calls[name].status_code == 200, name
    assert calls["submit"].status_code != 403  # гейт пройден (дальше — 404 «нет задания»)


def test_non_delegate_unaffected_with_toggle_off(client):
    calls = _game_calls(client, REGULAR_ID)
    for name in ("tasks", "balance", "leaderboard"):
        assert calls[name].status_code == 200, name
    assert calls["submit"].status_code != 403


def test_blank_delegation_counts_as_non_delegate(client):
    asyncio.run(_set_delegation(DELEGATE_ID, "  "))
    assert client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID)).status_code == 200


def test_hub_and_profile_stay_open_for_delegate(client):
    h = _hdr(DELEGATE_ID)
    assert client.get("/app/api/hub", headers=h).status_code == 200
    assert client.get("/app/api/profile", headers=h).status_code == 200


def test_delegate_gate_refusal_wins_over_game_gate(client):
    # Незарегистрированного/pending делегат-гейт отсекает раньше, код гейма не подменяет причину.
    from tests.test_miniapp_routes import PENDING_ID
    asyncio.run(_set_delegation(PENDING_ID, UNIVERSITY))
    resp = client.get("/app/api/tasks", headers=_hdr(PENDING_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "delegate_gate"


def test_hub_hides_game_facts_for_delegate_when_game_off(client):
    h = _hdr(DELEGATE_ID)
    body = client.get("/app/api/hub", headers=h).json()
    assert body["tasks_fact"] is None and body["rank_unit"] is None
    _set("delegation_game_enabled", "on")
    body = client.get("/app/api/hub", headers=h).json()
    assert body["tasks_fact"] is not None and body["rank_unit"] is not None
    regular = client.get("/app/api/hub", headers=_hdr(REGULAR_ID)).json()
    assert regular["tasks_fact"] is not None


def test_me_drops_game_sections_for_delegate_when_game_off(client):
    sections = client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()["sections"]
    assert not any(sections[s] for s in ("tasks", "coins", "leaderboard"))
    _set("delegation_game_enabled", "on")
    sections = client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()["sections"]
    assert sections["tasks"] and sections["coins"]
