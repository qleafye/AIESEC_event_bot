"""Точка «🛍 Магазин» в сканере Mini App: скан QR делегата на стойке магазина баллов.

Отметку не ставит. Находит делегата по токену из QR (тот же пропуск, что на входе) и шлёт
волонтёру в чат с ботом карточку: баланс и кнопки товаров (`services.game.shop_card`). Дальше
выдача идёт в чате, как при поиске по нику (`handlers/game/admin_shop.py`). В сканере — плашка с
именем, балансом и подсказкой открыть чат.

Точку видит только тот, у кого есть право магазина (`moderate_game`) и на витрине есть товары;
сам сканер, как и раньше, требует `checkin` — волонтёру шопа выдают обе роли. QR брони (токен
`shopres-…`, `services.game.shop_qr`) распознаётся до поиска делегата: волонтёру в чат уходит
карточка брони с «✅ Выдать бронь». Модуль зовёт
`miniapp/routers/checkin.py` (`/points`, `/scan`, `/manual`)."""
from __future__ import annotations

import logging

from database import shop_db
from database.db import get_balance
from miniapp import telegram_api
from services.forum.checkin import resolve_token
from services.game import shop_card, shop_qr
from services.infra.ru_plural import points_word

logger = logging.getLogger(__name__)

SHOP_POINT = "shop"
SHOP_CAP = "moderate_game"


async def point_entry(caps) -> dict | None:
    if SHOP_CAP not in caps or not await shop_db.list_items(active_only=True):
        return None
    return {"point": SHOP_POINT, "label": "🛍 Магазин", "live": None, "count": None, "capacity": None}


async def scan(cfg, staff_id: int, caps, *, token: str | None = None, user: dict | None = None) -> dict:
    """Плашка сканера; карточка уходит в чат `staff_id`."""
    if SHOP_CAP not in caps:
        return {"status": "denied", "reason_text": "Нет права магазина: нужна роль «🎮 Менеджер геймификации»."}
    res_token = shop_qr.pickup_token_from(token) if user is None else None
    if res_token is not None:
        sale = await shop_db.find_by_token(res_token)
        if sale is None:
            return {"status": "not_found", "reason_text": "Не нашёл бронь по этому QR — спросите у делегата код и введите его в боте: /shop → «🎫 Выдать бронь по коду»."}
        tid = int(sale["telegram_id"])
        base = {"full_name": sale.get("full_name"), "city": sale.get("event_city"), "telegram_id": tid}
        if sale["undone_at"]:
            return {**base, "status": "denied", "reason_text": "Эта бронь отменена — баллы вернули делегату."}
        if sale["status"] != shop_db.STATUS_RESERVED:
            when = (sale.get("issued_at") or "")[5:16].replace("-", ".")
            return {**base, "status": "denied", "reason_text": f"Эта бронь уже выдана {when}." if when else "Эта бронь уже выдана."}
        text, rows = shop_card.reservation_card(sale)
        ok_text = f"Бронь {sale['pickup_code']}: «{sale['title']}». Карточка с «✅ Выдать бронь» — в чате с ботом."
        fail_text = f"Бронь {sale['pickup_code']}. Карточку в чат отправить не получилось — введите код в боте: /shop → «🎫 Выдать бронь по коду»."
    else:
        if user is None:
            resolved = await resolve_token(token, point=SHOP_POINT, source="miniapp")
            user = (resolved or {}).get("user") if (resolved or {}).get("kind") == "delegate" else None
        if not user:
            return {"status": "not_found", "reason_text": "Не нашёл делегата по этому QR. Найдите его в боте по @нику: /shop → «🛒 Выдать товар»."}
        tid = int(user["telegram_id"])
        text, rows = await shop_card.card(tid)
        balance = await get_balance(tid)
        base = {"full_name": user.get("full_name"), "city": user.get("event_city"), "telegram_id": tid}
        ok_text = f"На счету {balance} {points_word(balance)}. Карточка с товарами — в чате с ботом."
        fail_text = f"На счету {balance}. Карточку в чат отправить не получилось — найдите делегата в боте: /shop."
    try:
        await telegram_api.send_message(cfg, staff_id, text, reply_markup=shop_card.reply_markup(rows), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"checkin_shop: карточка в чат не ушла: {type(e).__name__}")
        return {**base, "status": "denied", "reason_text": fail_text}
    return {**base, "status": "shop", "reason_text": ok_text}
