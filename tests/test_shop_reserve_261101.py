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


# ── делегат: «Взять», «Мои брони», отмена ─────────────────────────────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402


def _state():
    store = {}

    async def get_data():
        return dict(store)

    async def update_data(**kw):
        store.update(kw)

    return store, SimpleNamespace(get_data=get_data, update_data=update_data, set_state=AsyncMock())


def _cb(data, uid=UID):
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock(), answer_photo=AsyncMock(), chat=None)
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), message=msg, answer=AsyncMock(),
                           bot=SimpleNamespace(send_message=AsyncMock()))


def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _shown(cb):
    args, kw = cb.message.edit_text.call_args
    return args[0], kw["reply_markup"]


def _alert(cb):
    args, kw = cb.answer.call_args
    return args[0] if args else ""


def test_showcase_without_reserve_is_unchanged(tmp_path):
    from handlers.game import shop_delegate

    _env(tmp_path, on=False)
    _run(shop_db.add_item("Шоппер", 150, 3, None))
    _run(shop_db.set_open(True))
    cb = _cb("shopv")
    _run(shop_delegate.shop_showcase(cb))
    text, kb = _shown(cb)
    assert _datas(kb) == ["gbal_back"] and "покажи свой QR" in text


def test_showcase_take_buttons_only_for_affordable(tmp_path):
    from handlers.game import shop_delegate

    _env(tmp_path)
    ok = _run(shop_db.add_item("Шоппер", 150, 3, None))
    pricey = _run(shop_db.add_item("Ужин", 600, None, None))
    gone = _run(shop_db.add_item("Стикер", 10, 0, None))
    cb = _cb("shopv")
    _run(shop_delegate.shop_showcase(cb))
    text, kb = _shown(cb)
    datas = _datas(kb)
    assert f"shopv_take:{ok}" in datas and f"shopv_take:{pricey}" not in datas and f"shopv_take:{gone}" not in datas
    assert "Нажми «Взять»" in text


def test_take_confirm_and_double_tap_reserves_once(tmp_path):
    from handlers.game import shop_delegate

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    store, state = _state()
    cb = _cb(f"shopv_take:{item}")
    _run(shop_delegate.shop_take(cb, state))
    text, kb = _shown(cb)
    assert "Списать 150 баллов" in text and "Останется 50" in text
    nonce = store["shopv_nonce"]
    assert f"shopv_go:{nonce}" in _datas(kb)
    a, b = _cb(f"shopv_go:{nonce}"), _cb(f"shopv_go:{nonce}")

    async def both():
        await asyncio.gather(shop_delegate.shop_take_go(a, state), shop_delegate.shop_take_go(b, state))

    _run(both())
    assert _run(db.get_balance(UID)) == 50
    assert len(_run(shop_db.list_user_reservations(UID))) == 1
    winner = a if a.message.edit_text.called else b
    text, kb = _shown(winner)
    assert re.search(r"Ю-\d{4}", text) and "shopv_my" in _datas(kb)
    winner.message.answer_photo.assert_called_once()
    loser = b if winner is a else a
    assert "уже выдана или отменена" in _alert(loser)


def test_my_reservations_button_survives_hidden_showcase(tmp_path):
    from aiogram.types import InlineKeyboardMarkup

    from handlers.game import shop_delegate

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    _run(shop_db.reserve(item, UID))
    _run(shop_db.set_open(False))
    kb = _run(shop_delegate.with_shop_button(InlineKeyboardMarkup(inline_keyboard=[]), UID))
    assert _datas(kb) == ["shopv_my"]
    kb = _run(shop_delegate.with_shop_button(InlineKeyboardMarkup(inline_keyboard=[]), UID2))
    assert _datas(kb) == []


def test_my_reservations_screen_and_cancel(tmp_path):
    from handlers.game import shop_delegate

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    res = _run(shop_db.reserve(item, UID))
    cb = _cb("shopv_my")
    _run(shop_delegate.shop_my(cb))
    text, kb = _shown(cb)
    assert res["code"] in text and f"shopv_qr:{res['id']}" in _datas(kb) and f"shopv_cancel:{res['id']}" in _datas(kb)
    cb = _cb(f"shopv_qr:{res['id']}")
    _run(shop_delegate.shop_my_qr(cb))
    cb.message.answer_photo.assert_called_once()
    # чужая бронь — ни QR, ни отмены
    for handler, data in ((shop_delegate.shop_my_qr, "shopv_qr"), (shop_delegate.shop_cancel, "shopv_cancel"),
                          (shop_delegate.shop_cancel_go, "shopv_cgo")):
        cb = _cb(f"{data}:{res['id']}", uid=UID2)
        _run(handler(cb))
        assert "уже выдана или отменена" in _alert(cb)
        assert not cb.message.answer_photo.called and not cb.message.edit_text.called
    assert _run(db.get_balance(UID)) == 50
    cb = _cb(f"shopv_cancel:{res['id']}")
    _run(shop_delegate.shop_cancel(cb))
    text, kb = _shown(cb)
    assert "Вернём 150 баллов" in text and f"shopv_cgo:{res['id']}" in _datas(kb)
    cb = _cb(f"shopv_cgo:{res['id']}")
    _run(shop_delegate.shop_cancel_go(cb))
    text, _ = _shown(cb)
    assert "отменена" in text and _run(db.get_balance(UID)) == 200
    cb = _cb(f"shopv_cgo:{res['id']}")
    _run(shop_delegate.shop_cancel_go(cb))
    assert "уже выдана или отменена" in _alert(cb)


def test_delegate_callbacks_are_not_admin():
    from handlers.access.admin_caps import required_capability

    for data in ("shopv_take:1", "shopv_go:ab", "shopv_my", "shopv_qr:1", "shopv_cancel:1", "shopv_cgo:1"):
        assert required_capability(callback_data=data) is None, data


# ── стойка: тумблер, выдача по коду, отмена чужой брони ──────────────────────────────

def test_reserve_toggle_needs_items(tmp_path):
    from handlers.game import admin_shop as adm
    from handlers.game import admin_shop_reserve as res_adm

    _env(tmp_path, on=False)
    cb = _cb("shp_resv", uid=STAFF)
    _run(res_adm.shop_toggle_reserve(cb))
    assert "Сначала добавьте" in _alert(cb) and _run(shop_db.is_reserve_on()) is False
    _run(shop_db.add_item("Шоппер", 150, 3, None))
    _run(res_adm.shop_toggle_reserve(_cb("shp_resv", uid=STAFF)))
    assert _run(shop_db.is_reserve_on()) is True
    text, kb = _run(adm.home_text_kb())
    assert "Бронь делегатом: ✅ включена" in text
    assert "shp_code" in _datas(kb) and "shp_resv" in _datas(kb)
    _run(res_adm.shop_toggle_reserve(_cb("shp_resv", uid=STAFF)))
    assert _run(shop_db.is_reserve_on()) is False


def test_counter_finds_reservation_by_code(tmp_path, monkeypatch):
    from handlers.game import admin_shop_reserve as res_adm
    from handlers.states import ShopAdmin

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    monkeypatch.setattr(shop_db, "_random_code_digits", lambda: 4821)
    res = _run(shop_db.reserve(item, UID))
    store, state = _state()
    _run(res_adm.shop_code_start(_cb("shp_code", uid=STAFF), state))
    state.set_state.assert_called_with(ShopAdmin.code)
    for raw in ("4821", "Ю-4821"):
        msg = SimpleNamespace(text=raw, answer=AsyncMock())
        _run(res_adm.shop_code_input(msg, state))
        kw = msg.answer.call_args.kwargs
        assert f"shp_rgo:{res['id']}" in _datas(kw["reply_markup"])
    msg = SimpleNamespace(text="1234", answer=AsyncMock())
    _run(res_adm.shop_code_input(msg, state))
    assert "Не нашёл действующую бронь" in msg.answer.call_args.args[0]


def test_counter_issue_once_and_notify_once(tmp_path):
    from handlers.game import admin_shop_reserve as res_adm

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    res = _run(shop_db.reserve(item, UID))
    a, b = _cb(f"shp_rgo:{res['id']}", uid=STAFF), _cb(f"shp_rgo:{res['id']}", uid=STAFF)

    async def both():
        await asyncio.gather(res_adm.shop_res_issue(a), res_adm.shop_res_issue(b))

    _run(both())
    assert _run(shop_db.get_sale(res["id"]))["status"] == "issued"
    assert a.bot.send_message.await_count + b.bot.send_message.await_count == 1
    loser = a if not a.bot.send_message.await_count else b
    assert "второй раз не выдаём" in _alert(loser)
    winner = b if loser is a else a
    assert winner.bot.send_message.call_args.args[0] == UID
    assert res["code"] in winner.bot.send_message.call_args.args[1]


def test_counter_cancels_someone_elses_reservation(tmp_path):
    from handlers.game import admin_shop_reserve as res_adm

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    res = _run(shop_db.reserve(item, UID))
    cb = _cb(f"shp_rcan:{res['id']}", uid=STAFF)
    _run(res_adm.shop_res_cancel(cb))
    text, kb = _shown(cb)
    assert "Вернём делегату 150" in text and f"shp_rcango:{res['id']}" in _datas(kb)
    cb = _cb(f"shp_rcango:{res['id']}", uid=STAFF)
    _run(res_adm.shop_res_cancel_go(cb))
    assert _run(db.get_balance(UID)) == 200
    cb.bot.send_message.assert_awaited_once()
    assert "отменили на стойке" in cb.bot.send_message.call_args.args[1]
    cb = _cb(f"shp_rcango:{res['id']}", uid=STAFF)
    _run(res_adm.shop_res_cancel_go(cb))
    assert "уже выдана или отменена" in _alert(cb)


def test_counter_callbacks_need_game_right():
    from handlers.access.admin_caps import required_capability

    for data in ("shp_resv", "shp_code", "shp_rc:1", "shp_rgo:1", "shp_rcan:1", "shp_rcango:1"):
        assert required_capability(callback_data=data) == "moderate_game", data
    assert required_capability(raw_state="ShopAdmin:code") == "moderate_game"


def test_sales_list_and_csv_mark_reservations(tmp_path):
    from handlers.game import admin_shop as adm

    _env(tmp_path)
    item = _run(shop_db.add_item("Шоппер", 150, 3, None))
    res = _run(shop_db.reserve(item, UID))
    cb = _cb("shp_sales:0", uid=STAFF)
    _run(adm.shop_sales(cb))
    text, _ = _shown(cb)
    assert f"🎫 бронь {res['code']}" in text
    cb = _cb("shp_salescsv", uid=STAFF)
    cb.message.answer_document = AsyncMock()
    _run(adm.shop_sales_csv(cb))
    doc = cb.message.answer_document.call_args.args[0]
    body = doc.data.decode("utf-8-sig")
    assert "Статус;Код" in body.splitlines()[0] and f"бронь;{res['code']}" in body
