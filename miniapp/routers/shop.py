"""Витрина магазина баллов в Mini App: экран «🛍 Магазин» делегата.

  GET  /app/api/shop                              — витрина: баланс, товары со статусом, мои брони
  POST /app/api/shop/reserve {item_id, request_id} — «Взять» (бронь, баллы списываются сразу)
  POST /app/api/shop/reservations/{id}/cancel     — отменить свою бронь (баллы назад)
  GET  /app/api/shop/reservations/{id}/qr         — QR своей брони (data URL)

Гейты — как у монет (`/app/api/coins/balance`): `game_gate` (одобренный делегат, делегация вуза
только при включённой геме) + раздел «Монеты». Закрытая витрина — 404 `shop_closed`, а не 403:
403 без quiet уводит фронт в «Нет доступа», а витрину могли просто скрыть, пока экран открыт.
Отказы брони — 409 с человеческим текстом.

Логика брони — слой `database/shop_db.py` (тот же, что у кнопки «Взять» в чате): `reserve`,
`cancel_reservation(owner_id=…)`, `list_user_reservations`, `SaleError.human`; QR — тот же
`services.game.shop_qr.build_reservation_qr`. Второй копии проверок баланса, остатка и лимита
здесь нет: статус товара на витрине — только подсказка, решает `reserve` в одной транзакции.

Двойной тап «Да, списать»: фронт блокирует кнопку, а сервер помнит последние успешные ответы
по `(telegram_id, request_id)` (10 минут, не больше 1000) — повтор отдаёт ту же бронь."""
from __future__ import annotations

import asyncio
import base64
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from database import shop_db
from database.db import get_balance
from services.i18n import i18n
from services.infra.ru_plural import agree_placeholder

from miniapp.deps import Principal, game_gate, require_section

router = APIRouter()

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_RECENT_TTL = 600.0
_RECENT_MAX = 1000
_RECENT: dict[tuple[int, str], tuple[float, dict]] = {}
_RECENT_LOCK = asyncio.Lock()

# Подстановки, за которыми в тексте стоит слово «баллов» — согласуется с числом.
_NUMERIC = ("price", "balance", "left", "n")


async def shop_section_visible() -> bool:
    """Плитка «🛍 Магазин» в хабе: витрина открыта и на ней есть товары — то же правило, что у
    кнопки витрины в чате (`handlers/game/shop_delegate.py::shop_button`)."""
    return await shop_db.is_open() and bool(await shop_db.list_items(active_only=True))


async def _lang(p: Principal) -> tuple[str, dict]:
    lang, tr_map = await i18n.context(p.telegram_id)
    return (lang if lang in ("ru", "en") else "ru"), tr_map


async def _t(key: str, lang: str, tr_map: dict, **values) -> str:
    """Текст ключа реестра на языке делегата; подстановки — после перевода."""
    text = await i18n.tr_setting(key, lang, tr_map) or ""
    for name in _NUMERIC:
        if name in values:
            text = agree_placeholder(text, name, values[name])
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


async def _closed(lang: str, tr_map: dict) -> HTTPException:
    return HTTPException(404, {"reason": "shop_closed", "text": await _t("miniapp_shop_closed_text", lang, tr_map)})


async def _gone(lang: str, tr_map: dict, status: int = 409) -> HTTPException:
    return HTTPException(status, {"reason": "gone", "text": await _t("shop_reservation_gone_text", lang, tr_map)})


async def _item_out(item: dict, tid: int, balance: int, reserve_on: bool, lang: str, tr_map: dict) -> dict:
    stock_left = item["stock_left"]
    if stock_left is not None and stock_left <= 0:
        status, note = "sold_out", await _t("miniapp_shop_sold_out_text", lang, tr_map)
    elif item["per_person"] and await shop_db.count_bought(tid, item["id"]) >= item["per_person"]:
        status, note = "limit", await _t("miniapp_shop_limit_text", lang, tr_map, n=item["per_person"])
    elif item["price"] > balance:
        status, note = "short", await _t("miniapp_shop_short_text", lang, tr_map, n=item["price"] - balance)
    else:
        status, note = "ok", None
    can_take = reserve_on and status == "ok"
    title = i18n.tr(item["title"], lang, tr_map)
    return {
        "id": item["id"],
        "title": title,
        "price": item["price"],
        "price_text": await _t("miniapp_shop_price_text", lang, tr_map, price=item["price"]),
        "stock_left": stock_left,
        "stock_text": (await _t("miniapp_shop_stock_text", lang, tr_map, n=stock_left)
                       if stock_left is not None and stock_left > 0 else None),
        "photo_file_id": item.get("photo_file_id") or None,
        "status": status,
        "note": note,
        "can_take": can_take,
        "confirm_text": (await _t("shop_take_confirm_text", lang, tr_map, price=item["price"], title=title,
                                  left=balance - item["price"]) if can_take else None),
    }


async def _reservations_out(tid: int, lang: str, tr_map: dict) -> list[dict]:
    out = []
    for sale in await shop_db.list_user_reservations(tid):
        title = i18n.tr(sale["title"], lang, tr_map)
        out.append({
            "id": sale["id"],
            "code": sale["pickup_code"],
            "title": title,
            "price": sale["price"],
            "code_text": await _t("miniapp_shop_code_text", lang, tr_map, code=sale["pickup_code"]),
            "cancel_confirm_text": await _t("shop_cancel_confirm_text", lang, tr_map, title=title,
                                            code=sale["pickup_code"], price=sale["price"]),
        })
    return out


_TEXT_KEYS = {
    "hint": "shop_reserve_hint_text",
    "reserve_off": "miniapp_shop_reserve_off_text",
    "my_title": "miniapp_shop_my_title_text",
    "take": "miniapp_shop_take_button_text",
    "take_yes": "miniapp_shop_take_yes_text",
    "cancel": "miniapp_shop_cancel_text",
    "qr_button": "miniapp_shop_qr_button_text",
    "cancel_button": "miniapp_shop_cancel_button_text",
    "cancel_yes": "miniapp_shop_cancel_yes_text",
    "keep": "miniapp_shop_keep_text",
}


@router.get("/app/api/shop")
async def showcase(p: Principal = Depends(game_gate), _: Principal = Depends(require_section("coins"))) -> dict:
    lang, tr_map = await _lang(p)
    if not await shop_db.is_open():
        raise await _closed(lang, tr_map)
    from services.game.shop_card import shop_title

    tid = p.telegram_id
    balance = await get_balance(tid)
    reserve_on = await shop_db.is_reserve_on()
    items = [await _item_out(i, tid, balance, reserve_on, lang, tr_map)
             for i in await shop_db.list_items(active_only=True)]
    body = {
        "title": i18n.tr(await shop_title(), lang, tr_map),
        "balance": balance,
        "balance_text": await _t("miniapp_shop_balance_text", lang, tr_map, balance=balance),
        "reserve_on": reserve_on,
        "items": items,
        "reservations": await _reservations_out(tid, lang, tr_map),
        "texts": {name: await _t(key, lang, tr_map) for name, key in _TEXT_KEYS.items()},
    }
    if not items:
        body["empty_text"] = await _t("miniapp_shop_empty_text", lang, tr_map)
    return body


def _sweep(now: float) -> None:
    for key in [k for k, (ts, _) in _RECENT.items() if now - ts > _RECENT_TTL]:
        del _RECENT[key]
    while len(_RECENT) >= _RECENT_MAX:
        del _RECENT[min(_RECENT, key=lambda k: _RECENT[k][0])]


def _parse_reserve_body(data) -> tuple[int, str]:
    if not isinstance(data, dict):
        raise HTTPException(400, {"reason": "bad_request"})
    item_id, request_id = data.get("item_id"), data.get("request_id")
    if (not isinstance(item_id, int) or isinstance(item_id, bool) or not isinstance(request_id, str)
            or not _REQUEST_ID_RE.match(request_id)):
        raise HTTPException(400, {"reason": "bad_request"})
    return item_id, request_id


@router.post("/app/api/shop/reserve")
async def reserve(request: Request, p: Principal = Depends(game_gate),
                  _: Principal = Depends(require_section("coins"))) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, {"reason": "bad_request"})
    item_id, request_id = _parse_reserve_body(data)
    lang, tr_map = await _lang(p)
    tid = p.telegram_id
    key = (tid, request_id)
    async with _RECENT_LOCK:
        now = time.monotonic()
        cached = _RECENT.get(key)
        if cached is not None and now - cached[0] <= _RECENT_TTL:
            return cached[1]
        if not await shop_db.is_open():
            raise await _closed(lang, tr_map)
        if not await shop_db.is_reserve_on():
            raise HTTPException(409, {"reason": "reserve_off",
                                      "text": await _t("miniapp_shop_reserve_off_text", lang, tr_map)})
        try:
            sale = await shop_db.reserve(item_id, tid)
        except shop_db.SaleError as e:
            raise HTTPException(409, {"reason": "sale_error", "text": i18n.tr(e.human, lang, tr_map)})
        title = i18n.tr(sale["title"], lang, tr_map)
        body = {
            "reservation": {"id": sale["id"], "code": sale["code"], "title": title, "price": sale["price"]},
            "balance": sale["balance"],
            "text": await _t("miniapp_shop_reserved_text", lang, tr_map, title=title, code=sale["code"]),
        }
        _sweep(now)
        _RECENT[key] = (now, body)
    return body


@router.post("/app/api/shop/reservations/{sale_id}/cancel")
async def cancel(sale_id: int, p: Principal = Depends(game_gate),
                 _: Principal = Depends(require_section("coins"))) -> dict:
    lang, tr_map = await _lang(p)
    tid = p.telegram_id
    sale = await shop_db.cancel_reservation(sale_id, by=tid, owner_id=tid)
    if sale is None:
        raise await _gone(lang, tr_map)
    title = i18n.tr(sale["title"], lang, tr_map)
    return {
        "balance": sale["balance"],
        "text": await _t("shop_cancelled_text", lang, tr_map, title=title, price=sale["price"],
                         balance=sale["balance"]),
    }


@router.get("/app/api/shop/reservations/{sale_id}/qr")
async def reservation_qr(sale_id: int, p: Principal = Depends(game_gate),
                         _: Principal = Depends(require_section("coins"))) -> dict:
    lang, tr_map = await _lang(p)
    sale = next((s for s in await shop_db.list_user_reservations(p.telegram_id) if s["id"] == sale_id), None)
    if sale is None:
        raise await _gone(lang, tr_map, status=404)
    from services.game import shop_qr

    png = await shop_qr.build_reservation_qr(sale)
    return {"code": sale["pickup_code"], "qr": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


__all__ = ["router", "shop_section_visible"]
