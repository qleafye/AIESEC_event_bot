"""Точка «🛍 Магазин» в сканере Mini App: скан QR делегата на стойке магазина баллов.

Отметку не ставит. Находит делегата по токену из QR (тот же пропуск, что на входе) и шлёт
волонтёру в чат с ботом карточку: баланс и кнопки товаров (`services.game.shop_card`). Дальше
выдача идёт в чате, как при поиске по нику (`handlers/game/admin_shop.py`). В сканере — плашка с
именем, балансом и подсказкой открыть чат.

Точку видит только тот, у кого есть право магазина (`moderate_game`) и на витрине есть товары;
сам сканер, как и раньше, требует `checkin` — волонтёру шопа выдают обе роли. Модуль зовёт
`miniapp/routers/checkin.py` (`/points`, `/scan`, `/manual`)."""
from __future__ import annotations

import logging

from database import shop_db
from database.db import get_balance
from miniapp import telegram_api
from services.forum.checkin import resolve_token
from services.game import shop_card
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
    if user is None:
        resolved = await resolve_token(token, point=SHOP_POINT, source="miniapp")
        user = (resolved or {}).get("user") if (resolved or {}).get("kind") == "delegate" else None
    if not user:
        return {"status": "not_found", "reason_text": "Не нашёл делегата по этому QR. Найдите его в боте по @нику: /shop → «🛒 Выдать товар»."}
    tid = int(user["telegram_id"])
    text, rows = await shop_card.card(tid)
    balance = await get_balance(tid)
    base = {"full_name": user.get("full_name"), "city": user.get("event_city"), "telegram_id": tid}
    try:
        await telegram_api.send_message(cfg, staff_id, text, reply_markup=shop_card.reply_markup(rows), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"checkin_shop: карточка в чат не ушла: {type(e).__name__}")
        return {**base, "status": "denied",
                "reason_text": f"На счету {balance}. Карточку в чат отправить не получилось — найдите делегата в боте: /shop."}
    return {**base, "status": "shop",
            "reason_text": f"На счету {balance} {points_word(balance)}. Карточка с товарами — в чате с ботом."}
