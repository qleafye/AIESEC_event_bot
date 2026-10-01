"""Чип «🚪 Вход · N» в сканере Mini App считает вошедших сегодня по городу волонтёра — так же,
как строка «Пришли N из M» сверху. 03.10 форумы СПб и Тюмени идут одновременно: волонтёр
Тюмени не должен видеть на чипе сумму двух городов."""
from __future__ import annotations

from datetime import datetime

from database import db as bot_db
from tests.test_miniapp_checkin_260924 import (
    BASE,
    _freeze_now,
    _grant_checkin_to_bound_manager,
    _grant_checkin_to_game_manager,
    _insert_user,
    _run,
    client_with,
)
from tests.test_miniapp_routes import BOUND_MANAGER_ID, GAME_MANAGER_ID, _hdr


def _entry_count(body: dict) -> int:
    return next(p for p in body["points"] if p["point"] == "entry")["count"]


def _seed_two_cities(monkeypatch):
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 30))
    for uid, city in ((951001, "spb"), (951002, "tyumen"), (951003, "tyumen")):
        _run(_insert_user(uid, city=city))
        _run(bot_db.record_checkin(uid, "entry", source="miniapp", scanned_at="2026-10-03 10:00:00"))


def test_bound_volunteer_sees_own_city_entry_count(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()  # привязан к spb
    _seed_two_cities(monkeypatch)
    body = client.get(f"{BASE}/points", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["city"] == "spb"
    assert _entry_count(body) == 1


def test_unbound_manager_with_city_choice_sees_that_city(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _seed_two_cities(monkeypatch)
    body = client.get(f"{BASE}/points?city=tyumen", headers=_hdr(GAME_MANAGER_ID)).json()
    assert _entry_count(body) == 2
    body_all = client.get(f"{BASE}/points", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body_all["city"] is None
    assert _entry_count(body_all) == 3
