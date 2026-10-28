"""Карточка делегата на стойке магазина: баланс и кнопки товаров.

Карточка брони (`reservation_card`) — тоже одна на бот (выдача по коду) и Mini App (скан QR брони).

Одна сборка на два входа: поиск по нику в боте (`handlers/game/admin_shop.py`) и скан QR в
сканере Mini App на точке «🛍 Магазин» (`miniapp/routers/checkin_shop.py` шлёт её волонтёру в
чат). Кнопки — обычные `shp_p:<tid>:<item>` бота, дальше подтверждение и выдача в чате.
Без aiogram: Mini App отдаёт клавиатуру в Bot API как есть."""
from __future__ import annotations

import html

from database import shop_db
from database.db import get_balance, get_user
from services.infra.ru_plural import points_word


async def shop_title() -> str:
    """Название магазина: «<событие>-шоп» или «Магазин баллов». Общее для бота, QR брони и Mini App."""
    from domain.settings.schema import get_setting_typed

    event = (await get_setting_typed("event_name") or "").strip()
    return f"{event}-шоп" if event else "Магазин баллов"


def who(user: dict | None, tid: int) -> str:
    if not user:
        return f"id {tid}"
    name = html.escape(user.get("full_name") or "—")
    nick = (user.get("username") or "").lstrip("@")
    return name + (f" · @{html.escape(nick)}" if nick else "")


async def card(tid: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """`(текст HTML, ряды кнопок [(подпись, callback_data)])`."""
    user = await get_user(tid)
    balance = await get_balance(tid)
    lines = [f"👤 <b>{who(user, tid)}</b>", f"На счету: <b>{balance}</b> {points_word(balance)}", ""]
    rows: list[list[tuple[str, str]]] = []
    reservations = await shop_db.list_user_reservations(tid)
    for r in reservations:
        rows.append([(f"🎫 Выдать бронь {r['pickup_code']}: {r['title']}"[:60], f"shp_rc:{r['id']}")])
    if reservations:
        lines += [f"Брони ждут выдачи: {len(reservations)} — нажмите «🎫», чтобы выдать.", ""]
    for item in await shop_db.list_items(active_only=True):
        blocked = (
            (item["stock_left"] is not None and item["stock_left"] <= 0)
            or item["price"] > balance
            or (item["per_person"] and await shop_db.count_bought(tid, item["id"]) >= item["per_person"])
        )
        rows.append([(f"{'✖ ' if blocked else ''}{item['title']} · {item['price']}"[:60], f"shp_p:{tid}:{item['id']}")])
    lines.append("Что выдаём? ✖ — сейчас нельзя: нажмите, бот скажет почему.")
    rows.append([("🔎 Другой делегат", "shp_sell"), ("◀️ В магазин", "shp_home")])
    return "\n".join(lines), rows


def reservation_card(sale: dict) -> tuple[str, list[list[tuple[str, str]]]]:
    """Карточка действующей брони для стойки: кто, что, код; «✅ Выдать бронь» / «✖ Отменить бронь».
    `sale` — строка `shop_db.find_by_code`/`find_by_token` (с full_name/username)."""
    when = (sale.get("sold_at") or "")[5:16].replace("-", ".")
    price = sale["price"]
    text = "\n".join([
        f"🎫 Бронь <b>{html.escape(sale.get('pickup_code') or '')}</b>",
        f"👤 {who(sale, sale['telegram_id'])}",
        f"«{html.escape(sale['title'])}» — {price} {points_word(price)} уже списаны" + (f" ({when})" if when else ""),
    ])
    rows = [
        [("✅ Выдать бронь", f"shp_rgo:{sale['id']}")],
        [("✖ Отменить бронь", f"shp_rcan:{sale['id']}")],
        [("◀️ В магазин", "shp_home")],
    ]
    return text, rows


def reply_markup(rows: list[list[tuple[str, str]]]) -> dict:
    """Клавиатура в виде JSON Bot API — для отправки из Mini App без aiogram."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
