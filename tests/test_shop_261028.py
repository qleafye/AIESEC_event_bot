"""Магазин баллов: выдача на стойке одной транзакцией, отказы с причиной, отмена, рейтинг,
защита от двойного «✅ Выдать», витрина у делегата."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import config
from database import db, shop_db
from handlers.access.admin_caps import required_capability
from handlers.game import admin_shop as adm
from handlers.game import shop_delegate

UID, UID2, STAFF = 261028101, 261028102, 1


def _run(coro):
    return asyncio.run(coro)


def _env(tmp_path):
    config.DB_PATH = str(tmp_path / "shop.db")
    _run(db.init_db())
    for uid, name, nick in ((UID, "Иванова Анна", "anna_iv"), (UID2, "Петров Пётр", "petrov_p")):
        _run(db.add_user({"telegram_id": uid, "full_name": name, "username": nick, "registration_date": "2026-01-01"}))
    _run(db.add_coins(UID, 200, reason="задания", changed_by=STAFF, source="manual"))
    _run(db.add_coins(UID2, 50, reason="чат", changed_by=STAFF, source="manual"))


def test_sell_deducts_coins_and_stock(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 2, None))
    sale = _run(shop_db.sell(item, UID, STAFF))
    assert sale["balance"] == 50
    assert _run(db.get_balance(UID)) == 50
    assert _run(shop_db.get_item(item))["stock_left"] == 1


def test_sell_refusals_explain_why(tmp_path):
    _env(tmp_path)
    pricey = _run(shop_db.add_item("Ужин", 600, None, None))
    one = _run(shop_db.add_item("Стикер", 10, 1, None))
    limited = _run(shop_db.add_item("Кофе", 10, None, 1))
    for item_id, uid, needle in ((pricey, UID, "Не хватает баллов"),):
        try:
            _run(shop_db.sell(item_id, uid, STAFF))
        except shop_db.SaleError as e:
            assert needle in e.human
        else:
            raise AssertionError("должно было отказать")
    _run(shop_db.sell(one, UID, STAFF))
    try:
        _run(shop_db.sell(one, UID2, STAFF))
        raise AssertionError
    except shop_db.SaleError as e:
        assert "закончился" in e.human
    _run(shop_db.sell(limited, UID, STAFF))
    try:
        _run(shop_db.sell(limited, UID, STAFF))
        raise AssertionError
    except shop_db.SaleError as e:
        assert "в одни руки" in e.human


def test_undo_returns_coins_and_stock_once(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 5, None))
    sale = _run(shop_db.sell(item, UID, STAFF))
    assert _run(shop_db.undo_sale(sale["id"], STAFF)) is not None
    assert _run(shop_db.undo_sale(sale["id"], STAFF)) is None
    assert _run(db.get_balance(UID)) == 200
    assert _run(shop_db.get_item(item))["stock_left"] == 5


def test_set_stock_counts_already_sold(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 10, 5, None))
    _run(shop_db.sell(item, UID, STAFF))
    _run(shop_db.set_stock(item, 3))
    assert _run(shop_db.get_item(item))["stock_left"] == 2
    assert not _run(shop_db.delete_item(item))


def test_leaderboard_desc_with_spent(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, None, None))
    _run(shop_db.sell(item, UID, STAFF))
    top = _run(shop_db.leaderboard(10))
    # равный баланс — по имени: «Иванова» раньше «Петрова»
    assert [(r["telegram_id"], r["balance"], r["spent"]) for r in top] == [(UID, 50, 150), (UID2, 50, 0)]


def test_double_tap_go_sells_once(tmp_path):
    _env(tmp_path)
    item = _run(shop_db.add_item("Стикер", 10, None, None))
    store = {}

    async def get_data():
        return dict(store)

    async def update_data(**kw):
        store.update(kw)

    state = SimpleNamespace(get_data=get_data, update_data=update_data, set_state=AsyncMock())

    def cb(data):
        msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=STAFF), message=msg,
                               answer=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()))

    _run(adm.shop_pick(cb(f"shp_p:{UID}:{item}"), state))
    nonce = store["shp_nonce"]

    async def both():
        await asyncio.gather(adm.shop_go(cb(f"shp_go:{nonce}"), state), adm.shop_go(cb(f"shp_go:{nonce}"), state))

    _run(both())
    assert _run(db.get_balance(UID)) == 190
    assert _run(shop_db.sales_stats())["count"] == 1


def test_showcase_hidden_until_open(tmp_path):
    _env(tmp_path)
    assert _run(shop_delegate.shop_button()) is None
    _run(shop_db.add_item("Шоппер", 150, 3, None))
    _run(shop_db.set_open(True))
    assert _run(shop_delegate.shop_button()).callback_data == "shopv"
    text = _run(shop_delegate.showcase_text(UID2))
    assert "не хватает 100" in text and "осталось 3" in text


def test_admin_new_item_wizard(tmp_path):
    _env(tmp_path)
    store = {"shp_step": 0, "shp_new": {}}

    async def get_data():
        return dict(store)

    async def update_data(**kw):
        store.update(kw)

    state = SimpleNamespace(get_data=get_data, update_data=update_data, set_state=AsyncMock())
    for text in ("Шоппер Юлида", "abc", "150", "-", "1"):
        msg = SimpleNamespace(text=text, answer=AsyncMock())
        _run(adm.shop_new_input(msg, state))
    items = _run(shop_db.list_items())
    assert [(i["title"], i["price"], i["stock_total"], i["per_person"]) for i in items] == [("Шоппер Юлида", 150, None, 1)]


def test_shop_callbacks_need_game_right():
    for data in ("admin_shop", "shp_sell", "shp_go:abcd", "shp_top:0", "shp_undogo:1"):
        assert required_capability(callback_data=data) == "moderate_game"
    assert required_capability(callback_data="shopv") is None
