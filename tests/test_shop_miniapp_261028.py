"""Витрина магазина баллов в Mini App (`miniapp/routers/shop.py`): гейты как у монет, статусы
товаров, бронь тем же `shop_db.reserve`, повтор по request_id, отмена и QR своей брони, раздел
«shop» в /app/api/me и фото товара через прокси файлов.

Харнесс — `tests/test_miniapp_routes.py` (как `tests/test_miniapp_program_260924.py`)."""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db
from database import shop_db
from miniapp import telegram_api
from miniapp.routers import shop as shop_router

from tests.test_miniapp_routes import _cfg, _client, _hdr, _seed, _set, _use_tmp_db

DELEGATE, OTHER, PENDING = 261028501, 261028502, 261028503
UNIVERSITY = "Тестовый университет"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_shop.db")
    _seed(
        users=[(DELEGATE, "approved"), (OTHER, "approved"), (PENDING, "pending")],
        settings={"miniapp_enabled": "on", "event_name": "Юлид", "registration_mode": "full"},
    )
    _run(bot_db.add_coins(DELEGATE, 100, reason="задания", changed_by=1, source="manual"))
    _run(bot_db.add_coins(OTHER, 500, reason="задания", changed_by=1, source="manual"))
    shop_router._RECENT.clear()
    return _client(_cfg(db_path))


def _open(reserve=False):
    _run(shop_db.set_open(True))
    _run(shop_db.set_reserve(reserve))


def _items():
    a = _run(shop_db.add_item("Шоппер", 50, 3, None))
    b = _run(shop_db.add_item("Худи", 150, None, None))
    c = _run(shop_db.add_item("Стикер", 10, 1, None))
    d = _run(shop_db.add_item("Кофе", 10, None, 1))
    _run(shop_db.sell(c, OTHER, 1))  # остаток 0
    _run(shop_db.sell(d, DELEGATE, 1))  # лимит исчерпан, баланс 90
    _run(bot_db.add_coins(DELEGATE, 10, reason="добавка", changed_by=1, source="manual"))  # снова 100
    return a, b, c, d


def _get(client, uid=DELEGATE):
    return client.get("/app/api/shop", headers=_hdr(uid))


def _reserve(client, item_id, rid="req-00000001", uid=DELEGATE):
    return client.post("/app/api/shop/reserve", headers=_hdr(uid), json={"item_id": item_id, "request_id": rid})


# ── гейты ──────────────────────────────────────────────────────────────────────────────

def test_gates_like_coins(client):
    _open()
    assert client.get("/app/api/shop").status_code == 401
    resp = _get(client, PENDING)
    assert resp.status_code == 403 and resp.json()["reason"] == "delegate_gate"
    _set("miniapp_section_coins", "off")
    resp = _get(client)
    assert resp.status_code == 403 and resp.json()["reason"] == "section_off"


def test_delegation_without_game_is_refused(client):
    _open()

    async def mark():
        async with bot_db._connect() as conn:
            await conn.execute("UPDATE users SET delegation = ? WHERE telegram_id = ?", (UNIVERSITY, DELEGATE))
            await conn.commit()

    _run(mark())
    resp = _get(client)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "game_gate" and resp.json()["code"] == "game_off_for_delegation"


# ── витрина ────────────────────────────────────────────────────────────────────────────

def test_closed_404_and_empty(client):
    resp = _get(client)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "shop_closed" and resp.json()["text"]
    _open()
    body = _get(client).json()
    assert body["items"] == [] and body["empty_text"]


def test_statuses_and_take_flags(client):
    a, b, c, d = _items()
    _run(shop_db.set_item_photo(a, "PHOTO_A"))
    _open(reserve=False)
    body = _get(client).json()
    assert body["balance"] == 100 and "100" in body["balance_text"]
    by_id = {i["id"]: i for i in body["items"]}
    assert [by_id[x]["status"] for x in (a, b, c, d)] == ["ok", "short", "sold_out", "limit"]
    assert "50" in by_id[b]["note"]
    assert by_id[a]["note"] is None
    assert not any(i["can_take"] for i in body["items"])
    assert by_id[a]["photo_file_id"] == "PHOTO_A"
    assert by_id[b]["stock_left"] is None and by_id[b]["stock_text"] is None
    assert by_id[a]["stock_text"] and "3" in by_id[a]["stock_text"]
    assert body["reserve_on"] is False and body["texts"]["reserve_off"]

    _run(shop_db.set_reserve(True))
    body = _get(client).json()
    by_id = {i["id"]: i for i in body["items"]}
    assert [x for x in (a, b, c, d) if by_id[x]["can_take"]] == [a]
    assert "Останется 50" in by_id[a]["confirm_text"]
    assert by_id[b]["confirm_text"] is None


# ── бронь ──────────────────────────────────────────────────────────────────────────────

def test_reserve_off_409(client):
    a, *_ = _items()
    _open(reserve=False)
    resp = _reserve(client, a)
    assert resp.status_code == 409 and resp.json()["reason"] == "reserve_off" and resp.json()["text"]


def test_reserve_and_reservations_listed(client):
    a, b, *_ = _items()
    _open(reserve=True)
    resp = _reserve(client, a)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["balance"] == 50 and body["text"]
    code = body["reservation"]["code"]
    assert code.startswith("Ю-") and len(code) == 6
    mine = _get(client).json()["reservations"]
    assert [r["code"] for r in mine] == [code]
    assert code in mine[0]["code_text"] and "Шоппер" in mine[0]["cancel_confirm_text"]


def test_reserve_refusal_same_text_as_bot(client):
    a, b, *_ = _items()
    _open(reserve=True)
    resp = _reserve(client, b)
    assert resp.status_code == 409 and resp.json()["reason"] == "sale_error"
    try:
        _run(shop_db.reserve(b, DELEGATE))
    except shop_db.SaleError as e:
        assert resp.json()["text"] == e.human
    else:
        raise AssertionError("должно было отказать")


def test_reserve_bad_body_400(client):
    a, *_ = _items()
    _open(reserve=True)
    h = _hdr(DELEGATE)
    assert client.post("/app/api/shop/reserve", headers=h, json={"item_id": a}).status_code == 400
    assert client.post("/app/api/shop/reserve", headers=h,
                       json={"item_id": str(a), "request_id": "req-00000001"}).status_code == 400
    assert client.post("/app/api/shop/reserve", headers=h,
                       json={"item_id": a, "request_id": "short"}).status_code == 400


def test_same_request_id_reserves_once(client):
    a, *_ = _items()
    _open(reserve=True)
    first = _reserve(client, a, rid="tap-aaaaaaaa").json()
    again = _reserve(client, a, rid="tap-aaaaaaaa").json()
    assert again["reservation"]["id"] == first["reservation"]["id"]
    assert _run(bot_db.get_balance(DELEGATE)) == 50
    second = _reserve(client, a, rid="tap-bbbbbbbb")
    assert second.status_code == 200 and second.json()["reservation"]["id"] != first["reservation"]["id"]
    assert _run(bot_db.get_balance(DELEGATE)) == 0


def test_cancel_own_once_and_foreign_untouched(client):
    a, *_ = _items()
    _open(reserve=True)
    mine = _reserve(client, a).json()["reservation"]
    foreign = _reserve(client, a, rid="other-0000001", uid=OTHER).json()["reservation"]
    url = f"/app/api/shop/reservations/{mine['id']}/cancel"
    resp = client.post(url, headers=_hdr(DELEGATE))
    assert resp.status_code == 200 and resp.json()["balance"] == 100 and "Шоппер" in resp.json()["text"]
    again = client.post(url, headers=_hdr(DELEGATE))
    assert again.status_code == 409 and again.json()["reason"] == "gone"
    resp = client.post(f"/app/api/shop/reservations/{foreign['id']}/cancel", headers=_hdr(DELEGATE))
    assert resp.status_code == 409
    assert [r["id"] for r in _run(shop_db.list_user_reservations(OTHER))] == [foreign["id"]]


def test_qr_own_only(client):
    a, *_ = _items()
    _open(reserve=True)
    mine = _reserve(client, a).json()["reservation"]
    foreign = _reserve(client, a, rid="other-0000001", uid=OTHER).json()["reservation"]
    resp = client.get(f"/app/api/shop/reservations/{mine['id']}/qr", headers=_hdr(DELEGATE))
    assert resp.status_code == 200
    assert resp.json()["qr"].startswith("data:image/png;base64,") and resp.json()["code"] == mine["code"]
    resp = client.get(f"/app/api/shop/reservations/{foreign['id']}/qr", headers=_hdr(DELEGATE))
    assert resp.status_code == 404 and resp.json()["reason"] == "gone"
    client.post(f"/app/api/shop/reservations/{mine['id']}/cancel", headers=_hdr(DELEGATE))
    assert client.get(f"/app/api/shop/reservations/{mine['id']}/qr", headers=_hdr(DELEGATE)).status_code == 404


# ── /app/api/me ────────────────────────────────────────────────────────────────────────

def test_me_shop_section(client):
    def sections():
        body = client.get("/app/api/me", headers=_hdr(DELEGATE)).json()
        return body["sections"], body["section_labels"]

    s, labels = sections()
    assert s["shop"] is False and labels["shop"] == "🛍 Магазин"
    _open()
    assert sections()[0]["shop"] is False  # открыта, но товаров нет
    _run(shop_db.add_item("Шоппер", 50, None, None))
    assert sections()[0]["shop"] is True
    _set("miniapp_section_coins", "off")
    assert sections()[0]["shop"] is False


def test_me_shop_section_off_for_delegation_without_game(client):
    _open()
    _run(shop_db.add_item("Шоппер", 50, None, None))

    async def mark():
        async with bot_db._connect() as conn:
            await conn.execute("UPDATE users SET delegation = ? WHERE telegram_id = ?", (UNIVERSITY, DELEGATE))
            await conn.commit()

    _run(mark())
    assert client.get("/app/api/me", headers=_hdr(DELEGATE)).json()["sections"]["shop"] is False


# ── фото товара через прокси файлов ────────────────────────────────────────────────────

class _Stream:
    content_type = "image/jpeg"
    content_length = 3

    async def chunks(self):
        yield b"jpg"


def test_item_photo_via_file_proxy(client, monkeypatch):
    async def get_file(cfg, file_id):
        return {"file_path": "photos/file_1.jpg"}

    async def download_file(cfg, file_path):
        return _Stream()

    monkeypatch.setattr(telegram_api, "get_file", get_file)
    monkeypatch.setattr(telegram_api, "download_file", download_file)
    a = _run(shop_db.add_item("Шоппер", 50, None, None))
    _run(shop_db.set_item_photo(a, "AgACAgIAAxkBAAIShopPhoto01"))
    resp = client.get("/app/api/file/AgACAgIAAxkBAAIShopPhoto01", headers=_hdr(DELEGATE))
    assert resp.status_code == 200 and resp.content == b"jpg"
    assert client.get("/app/api/file/AgACAgIAAxkBAAIShopPhoto01").status_code == 401
    _run(shop_db.update_item(a, active=0))
    assert client.get("/app/api/file/AgACAgIAAxkBAAIShopPhoto01", headers=_hdr(DELEGATE)).status_code == 403
