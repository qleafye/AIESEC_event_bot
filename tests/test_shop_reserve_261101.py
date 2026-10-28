"""Бронь в магазине баллов: делегат берёт товар сам (баллы списываются сразу, код «Ю-NNNN» и QR),
отменяет до выдачи; стойка выдаёт по коду или скану QR одной кнопкой без двойной выдачи."""
from __future__ import annotations

import asyncio
import re

import aiosqlite

from config import config
from database import db, shop_db
from services.game import shop_qr

UID, UID2, STAFF = 261101101, 261101102, 1


def _run(coro):
    return asyncio.run(coro)


def _env(tmp_path, *, on: bool = True):
    config.DB_PATH = str(tmp_path / "shop_res.db")
    _run(db.init_db())
    for uid, name, nick in ((UID, "Иванова Анна", "anna_iv"), (UID2, "Петров Пётр", "petrov_p")):
        _run(db.add_user({"telegram_id": uid, "full_name": name, "username": nick, "registration_date": "2026-01-01"}))
    _run(db.add_coins(UID, 200, reason="задания", changed_by=STAFF, source="manual"))
    _run(db.add_coins(UID2, 50, reason="чат", changed_by=STAFF, source="manual"))
    if on:
        _run(shop_db.set_open(True))
        _run(shop_db.set_reserve(True))


def _refused(coro, needle):
    try:
        _run(coro)
    except shop_db.SaleError as e:
        assert needle in e.human, e.human
    else:
        raise AssertionError("должно было отказать")


async def _coins(uid):
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute("SELECT delta, reason FROM coins WHERE user_id = ? ORDER BY id", (uid,)) as cur:
            return await cur.fetchall()


# ── слой данных ───────────────────────────────────────────────────────────────────────

def test_reserve_off_by_default_and_needs_open_shop(tmp_path):
    _env(tmp_path, on=False)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    assert _run(shop_db.is_reserve_on()) is False
    _refused(shop_db.reserve(item, UID), "закрыт")
    _run(shop_db.set_open(True))
    _refused(shop_db.reserve(item, UID), "выключена")
    _run(shop_db.set_reserve(True))
    assert _run(shop_db.is_open()) is True  # тумблер брони не трогает витрину
    _run(shop_db.set_open(False))
    _refused(shop_db.reserve(item, UID), "закрыт")


def test_reserve_charges_and_gives_code(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    res = _run(shop_db.reserve(item, UID))
    assert re.fullmatch(r"Ю-\d{4}", res["code"]) and res["token"]
    assert res["balance"] == 50 and _run(db.get_balance(UID)) == 50
    assert _run(shop_db.get_item(item))["stock_left"] == 2
    sale = _run(shop_db.get_sale(res["id"]))
    assert sale["status"] == "reserved" and sale["pickup_code"] == res["code"]
    assert (-150, "Магазин: бронь «Шоппер»") in _run(_coins(UID))


def test_reserve_refuses_like_counter(tmp_path):
    _env(tmp_path)
    pricey = _run(shop_db.add_item("Ужин", 600, None, None))
    one = _run(shop_db.add_item("Стикер", 10, 1, None))
    limited = _run(shop_db.add_item("Кофе", 10, None, 1))
    _refused(shop_db.reserve(pricey, UID), "Не хватает баллов")
    _run(shop_db.reserve(one, UID))
    _refused(shop_db.reserve(one, UID2), "закончился")
    _run(shop_db.reserve(limited, UID))
    _refused(shop_db.reserve(limited, UID), "в одни руки")
    _refused(shop_db.sell(limited, UID, STAFF), "в одни руки")  # бронь считается и на стойке


def test_codes_unique_among_active_and_reused_after_issue(tmp_path, monkeypatch):
    _env(tmp_path)
    item = _run(shop_db.add_item("Стикер", 10, None, None))
    monkeypatch.setattr(shop_db, "_random_code_digits", lambda: 4821)
    first = _run(shop_db.reserve(item, UID))
    second = _run(shop_db.reserve(item, UID))
    assert first["code"] == "Ю-4821" and second["code"] != "Ю-4821"
    assert _run(shop_db.issue_reservation(first["id"], STAFF)) is not None
    third = _run(shop_db.reserve(item, UID))
    assert third["code"] == "Ю-4821"


def test_cancel_returns_once_and_keeps_row(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    res = _run(shop_db.reserve(item, UID))
    assert _run(shop_db.cancel_reservation(res["id"], UID2, owner_id=UID2)) is None  # чужая
    assert _run(db.get_balance(UID)) == 50
    done = _run(shop_db.cancel_reservation(res["id"], UID, owner_id=UID))
    assert done is not None and done["balance"] == 200
    assert _run(shop_db.cancel_reservation(res["id"], UID, owner_id=UID)) is None
    assert _run(db.get_balance(UID)) == 200
    assert _run(shop_db.get_item(item))["stock_left"] == 3
    assert _run(shop_db.get_sale(res["id"]))["undone_at"]
    assert (150, "Магазин: отмена брони «Шоппер»") in _run(_coins(UID))


def test_issued_reservation_cannot_be_cancelled(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    res = _run(shop_db.reserve(item, UID))
    _run(shop_db.issue_reservation(res["id"], STAFF))
    assert _run(shop_db.cancel_reservation(res["id"], STAFF)) is None
    assert _run(db.get_balance(UID)) == 50


def test_issue_once_even_concurrently(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Стикер", 10, None, None))
    res = _run(shop_db.reserve(item, UID))
    first = _run(shop_db.issue_reservation(res["id"], STAFF))
    assert first["status"] == "issued" and first["issued_by"] == STAFF
    assert _run(shop_db.issue_reservation(res["id"], STAFF)) is None
    res2 = _run(shop_db.reserve(item, UID))

    async def both():
        return await asyncio.gather(shop_db.issue_reservation(res2["id"], STAFF), shop_db.issue_reservation(res2["id"], STAFF))

    assert sum(r is not None for r in _run(both())) == 1


def test_find_by_code_and_token(tmp_path, monkeypatch):
    _env(tmp_path)
    item = _run(shop_db.add_item("Стикер", 10, None, None))
    monkeypatch.setattr(shop_db, "_random_code_digits", lambda: 4821)
    res = _run(shop_db.reserve(item, UID))
    for raw in ("Ю-4821", "ю 4821", "Ю4821", "U-4821", "4821"):
        found = _run(shop_db.find_by_code(raw))
        assert found and found["id"] == res["id"] and found["full_name"] == "Иванова Анна", raw
    assert _run(shop_db.find_by_code("48210")) is None
    assert _run(shop_db.find_by_code("привет")) is None
    _run(shop_db.issue_reservation(res["id"], STAFF))
    assert _run(shop_db.find_by_code("4821")) is None
    assert _run(shop_db.find_by_token(res["token"]))["status"] == "issued"
    assert _run(shop_db.find_by_token("nope")) is None


def test_lists_and_stats_split_reserved_and_issued(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Стикер", 10, None, None))
    res = _run(shop_db.reserve(item, UID))
    _run(shop_db.sell(item, UID, STAFF))
    assert [r["id"] for r in _run(shop_db.list_user_reservations(UID))] == [res["id"]]
    assert [s["status"] for s in _run(shop_db.list_user_sales(UID))] == ["issued"]
    stats = _run(shop_db.sales_stats())
    assert stats == {"count": 1, "spent": 20, "reserved": 1}


def test_sell_marks_issued_and_old_rows_migrate(tmp_path):
    config.DB_PATH = str(tmp_path / "old.db")

    async def old_db():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "CREATE TABLE shop_sales (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, "
                "telegram_id INTEGER NOT NULL, title TEXT NOT NULL, price INTEGER NOT NULL, coin_id INTEGER, "
                "sold_by INTEGER NOT NULL, sold_at TEXT NOT NULL, undone_at TEXT, undone_by INTEGER)"
            )
            await conn.execute(
                "INSERT INTO shop_sales (item_id, telegram_id, title, price, sold_by, sold_at) "
                "VALUES (1, 5, 'Шоппер', 10, 7, '2026-10-27 12:00:00')"
            )
            await conn.commit()

    _run(old_db())
    _run(db.init_db())
    _run(db.init_db())  # идемпотентно
    old = _run(shop_db.get_sale(1))
    assert old["status"] == "issued" and old["issued_at"] == "2026-10-27 12:00:00" and old["issued_by"] == 7
    _run(db.add_user({"telegram_id": UID, "full_name": "Иванова Анна", "registration_date": "2026-01-01"}))
    _run(db.add_coins(UID, 100, reason="задания", changed_by=STAFF, source="manual"))
    item = _run(shop_db.add_item("Стикер", 10, None, None))
    sale = _run(shop_db.sell(item, UID, STAFF))
    row = _run(shop_db.get_sale(sale["id"]))
    assert row["status"] == "issued" and row["issued_at"] and row["issued_by"] == STAFF


# ── QR брони ──────────────────────────────────────────────────────────────────────────

def test_qr_payload_and_token():
    sale = {"code": "Ю-4821", "token": "abc", "title": "Шоппер"}

    async def payload():
        from services.forum.checkin import build_payload, current_event_tag

        return await shop_qr.reservation_payload(sale), build_payload(await current_event_tag(), "Бронь", "Ю-4821", "shopres-abc")

    got, want = _run(payload())
    assert got == want
    assert shop_qr.pickup_token_from("shopres-abc") == "abc"
    assert shop_qr.pickup_token_from("xyz") is None and shop_qr.pickup_token_from(None) is None


def test_qr_png_and_fallback(tmp_path, monkeypatch):
    _env(tmp_path)
    sale = {"code": "Ю-4821", "token": "abc", "title": "Шоппер"}
    assert _run(shop_qr.build_reservation_qr(sale)).startswith(b"\x89PNG")
    from services.forum import qr_card

    def boom(*a, **kw):
        raise RuntimeError("шрифт")

    monkeypatch.setattr(qr_card, "render_qr_card", boom)
    assert _run(shop_qr.build_reservation_qr(sale)).startswith(b"\x89PNG")


# ── карточка брони и стойка в Mini App ────────────────────────────────────────────────

def test_reservation_card_and_delegate_card(tmp_path):
    from services.game import shop_card

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    text, rows = _run(shop_card.card(UID))
    assert rows[0][0][1] == f"shp_p:{UID}:{item}"  # без броней — как раньше
    res = _run(shop_db.reserve(item, UID))
    sale = _run(shop_db.find_by_code(res["code"]))
    text, rows = shop_card.reservation_card(sale)
    assert res["code"] in text and "Шоппер" in text and "Иванова Анна" in text
    flat = [b for row in rows for b in row]
    assert ("✅ Выдать бронь", f"shp_rgo:{res['id']}") in flat and ("✖ Отменить бронь", f"shp_rcan:{res['id']}") in flat
    text, rows = _run(shop_card.card(UID))
    assert rows[0][0] == (f"🎫 Выдать бронь {res['code']}: Шоппер", f"shp_rc:{res['id']}")
    assert "Брони ждут выдачи: 1" in text


def _scan_env(tmp_path, monkeypatch):
    from miniapp import telegram_api
    from tests.test_miniapp_checkin_260924 import _grant_checkin_to_game_manager, _insert_user, client_with

    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950911
    _run(_insert_user(uid, full_name="Петров Пётр", city="spb"))
    _run(db.add_coins(uid, 200, reason="задания", changed_by=1, source="manual"))
    item = _run(shop_db.add_item("Шоппер", 150, 10, None))
    _run(shop_db.set_open(True))
    _run(shop_db.set_reserve(True))
    sent = []

    async def fake_send(cfg, chat_id, text, reply_markup=None, parse_mode=None):
        sent.append((chat_id, text, reply_markup, parse_mode))
        return {}

    monkeypatch.setattr(telegram_api, "send_message", fake_send)
    return client, uid, item, sent


def _scan(client, payload):
    from tests.test_miniapp_checkin_260924 import BASE, GAME_MANAGER_ID, _hdr

    return client.post(f"{BASE}/scan", json={"payload": payload, "point": "shop"}, headers=_hdr(GAME_MANAGER_ID)).json()


def test_scan_reservation_qr_sends_card(tmp_path, monkeypatch):
    from tests.test_miniapp_checkin_260924 import GAME_MANAGER_ID

    client, uid, item, sent = _scan_env(tmp_path, monkeypatch)
    res = _run(shop_db.reserve(item, uid))
    payload = _run(shop_qr.reservation_payload(res))
    body = _scan(client, payload)
    assert body["status"] == "shop" and res["code"] in body["reason_text"]
    assert body["full_name"] == "Петров Пётр" and body["telegram_id"] == uid
    chat_id, text, markup, mode = sent[0]
    assert chat_id == GAME_MANAGER_ID and mode == "HTML" and res["code"] in text
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert f"shp_rgo:{res['id']}" in datas
    assert _run(db.list_checkins_for_user(uid)) == []


def test_scan_reservation_qr_issued_cancelled_unknown(tmp_path, monkeypatch):
    client, uid, item, sent = _scan_env(tmp_path, monkeypatch)
    res = _run(shop_db.reserve(item, uid))
    payload = _run(shop_qr.reservation_payload(res))
    _run(shop_db.issue_reservation(res["id"], 1))
    body = _scan(client, payload)
    assert body["status"] == "denied" and "уже выдана" in body["reason_text"]
    _run(db.add_coins(uid, 200, reason="задания", changed_by=1, source="manual"))
    res2 = _run(shop_db.reserve(item, uid))
    _run(shop_db.cancel_reservation(res2["id"], uid))
    body = _scan(client, _run(shop_qr.reservation_payload(res2)))
    assert body["status"] == "denied" and "отменена" in body["reason_text"]
    body = _scan(client, _run(shop_qr.reservation_payload({"code": "Ю-1111", "token": "nope"})))
    assert body["status"] == "not_found" and "Выдать бронь по коду" in body["reason_text"]
    assert sent == []
