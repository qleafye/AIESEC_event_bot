"""Точка «🛍 Магазин» в сканере Mini App: видна только с правом магазина и при товарах на
витрине; скан QR не ставит отметку, а шлёт волонтёру в чат карточку делегата с кнопками товаров."""
from __future__ import annotations

from database import shop_db
from database import db as bot_db
from miniapp import telegram_api

from tests.test_miniapp_checkin_260924 import (
    BASE, BOUND_MANAGER_ID, GAME_MANAGER_ID, _grant_checkin_to_bound_manager, _grant_checkin_to_game_manager,
    _hdr, _insert_user, _qr, _run, client_with,
)


def _points(client, who):
    return [p["point"] for p in client.get(f"{BASE}/points", headers=_hdr(who)).json()["points"]]


def test_shop_point_needs_game_right_and_items(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _grant_checkin_to_bound_manager()
    assert "shop" not in _points(client, GAME_MANAGER_ID)  # товаров нет
    _run(shop_db.add_item("Шоппер", 150, 10, None))
    assert "shop" in _points(client, GAME_MANAGER_ID)
    assert "shop" not in _points(client, BOUND_MANAGER_ID)  # нет права магазина


def test_shop_scan_sends_card_and_marks_nothing(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950901
    _run(_insert_user(uid, full_name="Петров Пётр", city="spb"))
    _run(bot_db.add_coins(uid, 200, reason="задания", changed_by=1, source="manual"))
    item = _run(shop_db.add_item("Шоппер", 150, 10, None))
    sent = []

    async def fake_send(cfg, chat_id, text, reply_markup=None, parse_mode=None):
        sent.append((chat_id, text, reply_markup, parse_mode))
        return {}

    monkeypatch.setattr(telegram_api, "send_message", fake_send)
    r = client.post(f"{BASE}/scan", json={"payload": _qr(uid, full_name="Петров Пётр"), "point": "shop"},
                    headers=_hdr(GAME_MANAGER_ID))
    body = r.json()
    assert body["status"] == "shop" and "200" in body["reason_text"]
    chat_id, text, markup, mode = sent[0]
    assert chat_id == GAME_MANAGER_ID and mode == "HTML" and "Петров Пётр" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == f"shp_p:{uid}:{item}"
    assert _run(bot_db.list_checkins_for_user(uid)) == []


def test_shop_scan_without_game_right_refused(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()
    uid = 950902
    _run(_insert_user(uid, city="spb"))
    _run(shop_db.add_item("Шоппер", 150, 10, None))
    r = client.post(f"{BASE}/scan", json={"payload": _qr(uid), "point": "shop"}, headers=_hdr(BOUND_MANAGER_ID))
    assert r.json()["status"] == "denied"
