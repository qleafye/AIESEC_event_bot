"""Фото товара магазина: колонка и функции shop_db, кнопки в карточке товара, загрузка фото
и файлом-картинкой, удаление с подтверждением, альбом «🖼 Фото товаров» на витрине делегата."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

from config import config
from database import db, shop_db
from handlers.access.admin_caps import required_capability
from handlers.game import admin_shop, admin_shop_photo, shop_delegate
from handlers.states import ShopAdmin

UID, UID2, STAFF = 261028401, 261028402, 261028409


def _run(coro):
    return asyncio.run(coro)


def _env(tmp_path):
    config.DB_PATH = str(tmp_path / "shop_photo.db")
    _run(db.init_db())
    _run(db.add_user({"telegram_id": UID, "full_name": "Иванова Анна", "username": "anna_iv",
                      "registration_date": "2026-01-01"}))
    _run(db.add_coins(UID, 200, reason="задания", changed_by=STAFF, source="manual"))


def _state(**data):
    store = dict(data)
    st = {"state": "x"}

    async def get_data():
        return dict(store)

    async def update_data(**kw):
        store.update(kw)

    async def set_state(value):
        st["state"] = value

    return store, st, SimpleNamespace(get_data=get_data, update_data=update_data, set_state=set_state)


def _cb(data, uid=STAFF):
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock(), answer_photo=AsyncMock(),
                          edit_reply_markup=AsyncMock(), chat=SimpleNamespace(id=uid))
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), message=msg, answer=AsyncMock(),
                           bot=SimpleNamespace(send_message=AsyncMock(), send_media_group=AsyncMock()))


def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


# ── данные ─────────────────────────────────────────────────────────────────────────────

def test_old_db_gets_photo_column(tmp_path):
    config.DB_PATH = str(tmp_path / "old.db")

    async def old():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "CREATE TABLE shop_items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
                "price INTEGER NOT NULL, stock_total INTEGER, stock_left INTEGER, per_person INTEGER, "
                "active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)"
            )
            await conn.execute("INSERT INTO shop_items (title, price, created_at) VALUES ('Шоппер', 10, '2026-01-01')")
            await conn.commit()

    _run(old())
    _run(db.init_db())
    _run(db.init_db())
    item = _run(shop_db.list_items())[0]
    assert item["title"] == "Шоппер" and item["photo_file_id"] is None


def test_set_and_check_item_photo(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    _run(shop_db.set_item_photo(iid, "PH"))
    assert _run(shop_db.get_item(iid))["photo_file_id"] == "PH"
    assert _run(shop_db.is_item_photo("PH")) is True
    _run(shop_db.update_item(iid, active=0))
    assert _run(shop_db.is_item_photo("PH")) is False
    assert _run(shop_db.is_item_photo("NOPE")) is False
    _run(shop_db.set_item_photo(iid, None))
    assert _run(shop_db.get_item(iid))["photo_file_id"] is None


# ── админка ────────────────────────────────────────────────────────────────────────────

def test_item_card_photo_row(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    text, kb = _run(admin_shop.item_text_kb(_run(shop_db.get_item(iid))))
    assert "Фото: нет" in text
    assert f"shp_ph:{iid}" in _datas(kb) and f"shp_phdel:{iid}" not in _datas(kb)
    assert "🖼 Загрузить фото" in _texts(kb)
    _run(shop_db.set_item_photo(iid, "PH"))
    text, kb = _run(admin_shop.item_text_kb(_run(shop_db.get_item(iid))))
    assert "Фото: ✅ есть" in text
    assert "🖼 Заменить фото" in _texts(kb) and "🗑 Убрать фото" in _texts(kb)
    assert f"shp_phdel:{iid}" in _datas(kb)
    # ряд фото — сразу под «Количество / Лимит»
    rows = [[b.callback_data for b in row] for row in kb.inline_keyboard]
    stock_row = next(i for i, r in enumerate(rows) if f"shp_ed:{iid}:stock" in r)
    assert f"shp_ph:{iid}" in rows[stock_row + 1]


def test_photo_start_sets_state_and_shows_current(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    _run(shop_db.set_item_photo(iid, "OLD"))
    store, st, state = _state()
    cb = _cb(f"shp_ph:{iid}")
    _run(admin_shop_photo.shop_photo_start(cb, state))
    assert st["state"] == ShopAdmin.photo and store["shp_photo_item"] == iid
    assert cb.message.answer_photo.call_args.args[0] == "OLD"
    prompt = cb.message.answer.call_args.args[0]
    assert "Пришлите фото товара" in prompt and "10 МБ" in prompt
    assert _datas(cb.message.answer.call_args.kwargs["reply_markup"]) == [f"shp_it:{iid}"]


def _msg(**kw):
    base = dict(answer=AsyncMock(), answer_photo=AsyncMock(), photo=None, document=None,
                bot=SimpleNamespace(download=AsyncMock()))
    base.update(kw)
    return SimpleNamespace(**base)


def test_photo_message_saved(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    store, st, state = _state(shp_photo_item=iid)
    msg = _msg(photo=[SimpleNamespace(file_id="SMALL"), SimpleNamespace(file_id="BIG")])
    _run(admin_shop_photo.shop_photo_got_photo(msg, state))
    assert _run(shop_db.get_item(iid))["photo_file_id"] == "BIG"
    assert st["state"] is None
    assert "✅ Фото товара «Шоппер» сохранено" in msg.answer_photo.call_args.kwargs["caption"]
    assert "Фото: ✅ есть" in msg.answer.call_args.args[0]


def test_photo_document_resent_as_photo(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    store, st, state = _state(shp_photo_item=iid)
    doc = SimpleNamespace(file_id="DOC", mime_type="image/png", file_size=1024 * 1024)
    sent = SimpleNamespace(photo=[SimpleNamespace(file_id="P1"), SimpleNamespace(file_id="P2")])
    msg = _msg(document=doc, answer_photo=AsyncMock(return_value=sent))
    _run(admin_shop_photo.shop_photo_got_document(msg, state))
    msg.bot.download.assert_awaited_once()
    assert isinstance(msg.answer_photo.call_args.args[0], BufferedInputFile)
    assert _run(shop_db.get_item(iid))["photo_file_id"] == "P2"
    assert st["state"] is None


def test_photo_document_refusals_keep_state(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    store, st, state = _state(shp_photo_item=iid)
    st["state"] = ShopAdmin.photo
    pdf = _msg(document=SimpleNamespace(file_id="D", mime_type="application/pdf", file_size=100))
    _run(admin_shop_photo.shop_photo_got_document(pdf, state))
    assert "Это не картинка" in pdf.answer.call_args.args[0]
    big = _msg(document=SimpleNamespace(file_id="D", mime_type="image/jpeg", file_size=11 * 1024 * 1024))
    _run(admin_shop_photo.shop_photo_got_document(big, state))
    assert "больше 10 МБ" in big.answer.call_args.args[0]
    big.bot.download.assert_not_awaited()
    other = _msg(text="привет")
    _run(admin_shop_photo.shop_photo_other(other, state))
    assert "Это не картинка" in other.answer.call_args.args[0]
    assert st["state"] == ShopAdmin.photo
    assert _run(shop_db.get_item(iid))["photo_file_id"] is None


def test_photo_delete_with_confirm(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    _run(shop_db.set_item_photo(iid, "PH"))
    cb = _cb(f"shp_phdel:{iid}")
    _run(admin_shop_photo.shop_photo_delete(cb))
    text = cb.message.edit_text.call_args.args[0]
    kb = cb.message.edit_text.call_args.kwargs["reply_markup"]
    assert "перестанут видеть" in text
    assert _datas(kb) == [f"shp_phdelgo:{iid}", f"shp_it:{iid}"]
    assert _run(shop_db.get_item(iid))["photo_file_id"] == "PH"
    go = _cb(f"shp_phdelgo:{iid}")
    _run(admin_shop_photo.shop_photo_delete_go(go))
    assert _run(shop_db.get_item(iid))["photo_file_id"] is None
    assert go.answer.call_args.args[0] == "Фото убрано"
    _run(shop_db.delete_item(iid))
    gone = _cb(f"shp_phdelgo:{iid}")
    _run(admin_shop_photo.shop_photo_delete_go(gone))
    assert gone.answer.call_args.args[0] == "Товар уже удалён."


def test_photo_callbacks_rights():
    assert required_capability(callback_data="shp_ph:1") == "moderate_game"
    assert required_capability(callback_data="shp_phdel:1") == "moderate_game"
    assert required_capability(callback_data="shp_phdelgo:1") == "moderate_game"
    assert required_capability(callback_data="shopv_ph") is None


# ── витрина делегата: альбом ───────────────────────────────────────────────────────────

def test_showcase_photo_button_only_with_photos(tmp_path):
    _env(tmp_path)
    iid = _run(shop_db.add_item("Шоппер", 10, None, None))
    _run(shop_db.set_open(True))
    assert "shopv_ph" not in _datas(_run(shop_delegate.showcase_kb(UID)))
    _run(shop_db.set_item_photo(iid, "PH"))
    datas = _datas(_run(shop_delegate.showcase_kb(UID)))
    assert datas[-2:] == ["shopv_ph", "gbal_back"]
    _run(shop_db.set_open(False))
    assert "shopv_ph" not in _datas(_run(shop_delegate.showcase_kb(UID)))


def test_album_sent_in_batches_and_showcase_again(tmp_path):
    _env(tmp_path)
    _run(shop_db.set_open(True))
    for n in range(11):
        iid = _run(shop_db.add_item(f"Товар {n}", 1 if n == 0 else 100 + n, None, None))
        _run(shop_db.set_item_photo(iid, f"PH{n}"))
    cb = _cb("shopv_ph", uid=UID)
    _run(shop_delegate.shop_photos(cb))
    assert cb.bot.send_media_group.await_count == 2
    first_batch = cb.bot.send_media_group.call_args_list[0].args[1]
    assert len(first_batch) == 10
    assert first_batch[0].caption == "Товар 0 — 1 балл"
    cb.message.edit_reply_markup.assert_awaited()
    assert "shopv_ph" in _datas(cb.message.answer.call_args.kwargs["reply_markup"])


def test_album_one_batch_and_failure_alert(tmp_path):
    _env(tmp_path)
    _run(shop_db.set_open(True))
    iid = _run(shop_db.add_item("Шоппер", 150, None, None))
    _run(shop_db.set_item_photo(iid, "PH"))
    cb = _cb("shopv_ph", uid=UID)
    _run(shop_delegate.shop_photos(cb))
    assert cb.bot.send_media_group.await_count == 1
    assert cb.bot.send_media_group.call_args.args[1][0].caption == "Шоппер — 150 баллов"

    bad = _cb("shopv_ph", uid=UID)
    bad.bot.send_media_group = AsyncMock(side_effect=TelegramBadRequest(method=None, message="bad"))
    _run(shop_delegate.shop_photos(bad))
    args, kw = bad.answer.call_args
    assert "Фото сейчас не загрузились" in args[0] and kw.get("show_alert") is True
    bad.message.answer.assert_not_awaited()


def test_album_when_photos_gone_shows_showcase(tmp_path):
    _env(tmp_path)
    _run(shop_db.set_open(True))
    _run(shop_db.add_item("Шоппер", 150, None, None))
    cb = _cb("shopv_ph", uid=UID)
    _run(shop_delegate.shop_photos(cb))
    cb.bot.send_media_group.assert_not_awaited()
    cb.message.edit_text.assert_awaited()
