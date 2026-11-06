"""QR брони магазина баллов: тот же генератор, что пропуск на вход.

Строка QR — формат пропуска (`services.forum.checkin.build_payload`: метка события · «Бронь» ·
код · токен), только токен с префиксом `shopres-`: сканер Mini App на точке «🛍 Магазин» по нему
отличает бронь от личного QR делегата и ищет строку `shop_sales` по случайному `pickup_token`,
а не по 4 цифрам кода. Картинка — `services.forum.qr_card.render_qr_card`, при любом сбое
оформления — голый QR (бронь важнее красоты). Без aiogram: зовут и бот, и Mini App."""
from __future__ import annotations

import asyncio
import io
import logging

import segno

from domain.settings.schema import get_setting_typed
from services.forum.checkin import build_payload, current_event_tag

logger = logging.getLogger(__name__)

RES_TOKEN_PREFIX = "shopres-"


async def reservation_payload(sale: dict) -> str:
    code = sale.get("code") or sale.get("pickup_code") or ""
    token = sale.get("token") or sale.get("pickup_token") or ""
    return build_payload(await current_event_tag(), "Бронь", code, RES_TOKEN_PREFIX + token)


def pickup_token_from(token: str | None) -> str | None:
    """Токен брони из последнего поля QR; `None` — это не QR брони."""
    if not token or not token.startswith(RES_TOKEN_PREFIX):
        return None
    return token[len(RES_TOKEN_PREFIX):] or None


async def build_reservation_qr(sale: dict) -> bytes:
    """PNG QR брони: карточка (название товара, код, магазин), при сбое — голый QR."""
    payload = await reservation_payload(sale)
    try:
        from services.forum.qr_card import render_qr_card
        from services.game.shop_card import shop_title

        event = (await get_setting_typed("event_name") or "").strip()
        footer = await get_setting_typed("shop_reservation_qr_footer_text") or ""
        code = sale.get("code") or sale.get("pickup_code") or ""
        return await asyncio.to_thread(
            render_qr_card, payload, sale.get("title") or "—", code, await shop_title(), footer,
            (event or "АЙСЕК").upper(),
        )
    except Exception as e:  # бронь важнее оформления
        logger.warning(f"build_reservation_qr: карточка не собралась, шлём голый QR: {type(e).__name__}")
    buf = io.BytesIO()
    segno.make(payload).save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()
