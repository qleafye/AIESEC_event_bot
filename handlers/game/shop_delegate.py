"""Витрина магазина баллов у делегата: кнопка на экране «🪙 Мои баллы», экран витрины и бронь.

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров читает их построчно).

Делегат видит баланс, товары с ценами и остатками, сколько не хватает, и свои покупки. Пока
менеджер не включил «Бронь делегатом», выдаёт и списывает только стойка
(handlers/game/admin_shop.py). С включённой бронью у доступных товаров есть «Взять»: баллы
списываются сразу (`shop_db.reserve`), делегат получает код выдачи «Ю-NNNN» и QR, находит их в
«🎫 Мои брони» и может отменить бронь до выдачи. «🎫 Мои брони» видны, пока есть действующая
бронь, даже если витрину скрыли — бронь, за которую уже списаны баллы, не теряется.

«🖼 Фото товаров» (`shopv_ph`) присылает фото товаров альбомом по кнопке, а не автоматически:
`shopv` — цель «◀️ Назад» со всех экранов брони, автоматический альбом слал бы фото при каждом
возврате. После альбома витрина приходит заново новым сообщением — под альбомом, у старой
снимаются кнопки.

Кнопка витрины появляется, только пока менеджер открыл витрину и на ней есть товары. Колбэки
`shopv*` не админские — в ADMIN_CAPS их нет. Двойной тап «✅ Да, списать» даёт одну бронь:
одноразовый номер в FSM-данных забирается под замком (тот же приём, что `shop_go` у стойки).
Тексты делегату — ключи реестра (группа «🎮 Геймификация») с переводом. Импорт — хвостом
handlers/user_actions.py, до фолбэка."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets

from aiogram import F, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from database import shop_db
from database.db import get_balance
from domain.settings.schema import get_setting_typed
from handlers.i18n import reg_i18n
from handlers.user_actions import router
from services.infra.ru_plural import agree_placeholder, points_word

logger = logging.getLogger(__name__)

_take_lock = asyncio.Lock()


async def _title() -> str:
    from services.game.shop_card import shop_title

    return await shop_title()


async def _t(key: str, lang: str, tr_map: dict, *, escape: bool = False, **values) -> str:
    """Текст ключа реестра на языке делегата с подстановкой; «{price} баллов» — по числу.
    `escape` — текст без HTML-разметки, уходящий в сообщение с parse_mode=HTML."""
    text = reg_i18n.tr_text(await get_setting_typed(key) or "", lang, tr_map) or ""
    if escape:
        text = html.escape(text, quote=False)
    if "price" in values:
        text = agree_placeholder(text, "price", values["price"])
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=data)


async def shop_button() -> InlineKeyboardButton | None:
    if not await shop_db.is_open() or not await shop_db.list_items(active_only=True):
        return None
    return InlineKeyboardButton(text=f"🛍 {await _title()}", callback_data="shopv")


async def _my_button(telegram_id: int | None, lang: str, tr_map: dict) -> InlineKeyboardButton | None:
    if telegram_id is None or not await shop_db.list_user_reservations(telegram_id):
        return None
    return _btn(await _t("shop_my_reservations_button_text", lang, tr_map), "shopv_my")


async def with_shop_button(
    kb: InlineKeyboardMarkup, telegram_id: int | None = None, lang: str = "ru", tr_map: dict | None = None,
) -> InlineKeyboardMarkup:
    """Экран «🪙 Мои баллы» (handlers/user_actions.py::_balance_screen): кнопка витрины первой
    строкой, под ней «🎫 Мои брони», если у делегата есть действующая бронь."""
    rows = [[b] for b in (await shop_button(), await _my_button(telegram_id, lang, tr_map or {})) if b is not None]
    kb.inline_keyboard[:0] = rows
    return kb


async def _reserve_live() -> bool:
    return await shop_db.is_open() and await shop_db.is_reserve_on()


async def _can_take(item: dict, telegram_id: int, balance: int) -> bool:
    if item["stock_left"] is not None and item["stock_left"] <= 0:
        return False
    if item["price"] > balance:
        return False
    return not (item["per_person"] and await shop_db.count_bought(telegram_id, item["id"]) >= item["per_person"])


async def showcase_text(telegram_id: int, lang: str = "ru", tr_map: dict | None = None) -> str:
    balance = await get_balance(telegram_id)
    lines = [f"🛍 <b>{html.escape(await _title())}</b>", f"На счету: <b>{balance}</b> {points_word(balance)}", ""]
    for item in await shop_db.list_items(active_only=True):
        line = f"• {html.escape(item['title'])} — {item['price']}"
        if item["stock_left"] is not None and item["stock_left"] <= 0:
            line += " · закончился"
        else:
            if item["stock_left"] is not None:
                line += f" · осталось {item['stock_left']}"
            if item["price"] > balance:
                line += f" · не хватает {item['price'] - balance}"
            if item["per_person"]:
                line += f" · не больше {item['per_person']} в одни руки"
        lines.append(line)
    mine = await shop_db.list_user_sales(telegram_id)
    if mine:
        lines += ["", "<b>Ты уже получил(а)</b>"] + [f"✓ {html.escape(s['title'])}" for s in mine]
    if await _reserve_live():
        lines += ["", await _t("shop_reserve_hint_text", lang, tr_map or {}, escape=True)]
    else:
        lines += ["", "Подходи к стойке магазина на форуме и покажи свой QR из «🎟 Мой QR» — баллы спишут на месте. Нет QR — назови свой @ник."]
    return "\n".join(lines)


async def showcase_kb(telegram_id: int, lang: str = "ru", tr_map: dict | None = None) -> InlineKeyboardMarkup:
    tr_map = tr_map or {}
    rows: list[list[InlineKeyboardButton]] = []
    if await _reserve_live():
        balance = await get_balance(telegram_id)
        for item in await shop_db.list_items(active_only=True):
            if await _can_take(item, telegram_id, balance):
                label = await _t("shop_take_button_text", lang, tr_map, title=item["title"], price=item["price"])
                rows.append([_btn(label, f"shopv_take:{item['id']}")])
    my = await _my_button(telegram_id, lang, tr_map)
    if my is not None:
        rows.append([my])
    if await _photo_items():
        rows.append([_btn(await _t("shop_photos_button_text", lang, tr_map), "shopv_ph")])
    rows.append([_btn(reg_i18n.tr_text("◀️ Назад", lang, tr_map), "gbal_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _photo_items() -> list[dict]:
    """Активные товары с фото, пока витрина открыта (порядок — как на витрине)."""
    if not await shop_db.is_open():
        return []
    return [i for i in await shop_db.list_items(active_only=True) if i.get("photo_file_id")]


async def showcase_screen(telegram_id: int, lang: str = "ru", tr_map: dict | None = None):
    """Экран витрины `(text, kb)` — общий для `shopv` и повторной витрины после альбома."""
    return await showcase_text(telegram_id, lang, tr_map), await showcase_kb(telegram_id, lang, tr_map)


async def _show(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


def _back(label: str) -> list[InlineKeyboardButton]:
    return [_btn(label, "shopv")]


async def _send_qr(callback: types.CallbackQuery, sale: dict) -> None:
    from services.game import shop_qr

    code = sale.get("code") or sale.get("pickup_code") or ""
    try:
        png = await shop_qr.build_reservation_qr(sale)
        await callback.message.answer_photo(BufferedInputFile(png, filename="reservation.png"), caption=code)
    except Exception as e:  # код уже в тексте — без картинки бронь не теряется
        logger.info(f"shop_delegate: QR брони {code} не ушёл: {type(e).__name__}")


def _resync() -> None:
    from handlers.game.admin_shop import _resync as resync

    resync()


async def _own_active(sale_id: int, telegram_id: int) -> dict | None:
    sale = await shop_db.get_sale(sale_id)
    if (sale is None or int(sale["telegram_id"]) != telegram_id
            or sale["status"] != shop_db.STATUS_RESERVED or sale["undone_at"]):
        return None
    return sale


@router.callback_query(F.data == "shopv")
async def shop_showcase(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    text, kb = await showcase_screen(callback.from_user.id, lang, tr_map)
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "shopv_ph")
async def shop_photos(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    uid = callback.from_user.id
    items = await _photo_items()
    if not items:  # витрину скрыли или фото убрали, пока экран висел
        await shop_showcase(callback)
        return
    template = reg_i18n.tr_text(await get_setting_typed("shop_photo_caption_text") or "", lang, tr_map) or ""
    media = []
    for item in items:
        caption = agree_placeholder(template, "price", item["price"])
        caption = caption.replace("{price}", str(item["price"])).replace("{title}", item["title"])
        media.append(InputMediaPhoto(media=item["photo_file_id"], caption=caption[:1024], parse_mode=None))
    try:
        for start in range(0, len(media), 10):
            await callback.bot.send_media_group(callback.message.chat.id, media[start:start + 10])
    except TelegramAPIError as e:
        logger.info(f"shop_delegate: альбом витрины не ушёл: {type(e).__name__}")
        await callback.answer(await _t("shop_photos_failed_text", lang, tr_map), show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    text, kb = await showcase_screen(uid, lang, tr_map)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("shopv_take:"))
async def shop_take(callback: types.CallbackQuery, state: FSMContext):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    uid = callback.from_user.id
    item = await shop_db.get_item(int(callback.data.split(":", 1)[1]))
    balance = await get_balance(uid)
    if item is None or not await _reserve_live() or not await _can_take(item, uid, balance):
        await callback.answer(reg_i18n.tr_text("Этого товара уже нет на витрине.", lang, tr_map), show_alert=True)
        return
    nonce = secrets.token_hex(4)
    await state.update_data(shopv_nonce=nonce, shopv_item=item["id"])
    text = await _t("shop_take_confirm_text", lang, tr_map, escape=True,
                    title=html.escape(item["title"]), price=item["price"], left=balance - item["price"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn(await _t("shop_take_confirm_button_text", lang, tr_map), f"shopv_go:{nonce}")],
        _back(reg_i18n.tr_text("◀️ Назад", lang, tr_map)),
    ])
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("shopv_go:"))
async def shop_take_go(callback: types.CallbackQuery, state: FSMContext):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    uid = callback.from_user.id
    nonce = callback.data.split(":", 1)[1]
    async with _take_lock:
        data = await state.get_data()
        if not nonce or data.get("shopv_nonce") != nonce:
            await callback.answer(await _t("shop_reservation_gone_text", lang, tr_map), show_alert=True)
            return
        await state.update_data(shopv_nonce=None)
        try:
            sale = await shop_db.reserve(int(data["shopv_item"]), uid)
        except shop_db.SaleError as e:
            await callback.answer(reg_i18n.tr_text(e.human, lang, tr_map), show_alert=True)
            return
    text = await _t("shop_reserved_text", lang, tr_map, title=html.escape(sale["title"]), code=sale["code"],
                    price=sale["price"], balance=sale["balance"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn(await _t("shop_my_reservations_button_text", lang, tr_map), "shopv_my")],
        _back(reg_i18n.tr_text("◀️ Назад", lang, tr_map)),
    ])
    await _show(callback, text, kb)
    await callback.answer()
    await _send_qr(callback, sale)
    _resync()


@router.callback_query(F.data == "shopv_my")
async def shop_my(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    mine = await shop_db.list_user_reservations(callback.from_user.id)
    lines = [await _t("shop_my_reservations_header_text", lang, tr_map), ""]
    rows: list[list[InlineKeyboardButton]] = []
    if not mine:
        lines.append(await _t("shop_my_reservations_empty_text", lang, tr_map, escape=True))
    for s in mine:
        lines.append(f"• {html.escape(s['title'])} — <b>{s['pickup_code']}</b>")
        rows.append([
            _btn(await _t("shop_reservation_qr_button_text", lang, tr_map, code=s["pickup_code"]), f"shopv_qr:{s['id']}"),
            _btn(await _t("shop_reservation_cancel_button_text", lang, tr_map, code=s["pickup_code"]), f"shopv_cancel:{s['id']}"),
        ])
    rows.append(_back(reg_i18n.tr_text("◀️ Назад", lang, tr_map)))
    await _show(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("shopv_qr:"))
async def shop_my_qr(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    sale = await _own_active(int(callback.data.split(":", 1)[1]), callback.from_user.id)
    if sale is None:
        await callback.answer(await _t("shop_reservation_gone_text", lang, tr_map), show_alert=True)
        return
    await callback.answer()
    await _send_qr(callback, sale)


@router.callback_query(F.data.startswith("shopv_cancel:"))
async def shop_cancel(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    sale = await _own_active(int(callback.data.split(":", 1)[1]), callback.from_user.id)
    if sale is None:
        await callback.answer(await _t("shop_reservation_gone_text", lang, tr_map), show_alert=True)
        return
    text = await _t("shop_cancel_confirm_text", lang, tr_map, escape=True, title=html.escape(sale["title"]),
                    code=sale["pickup_code"], price=sale["price"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _btn(await _t("shop_cancel_confirm_button_text", lang, tr_map), f"shopv_cgo:{sale['id']}"),
        _btn(await _t("shop_cancel_keep_button_text", lang, tr_map), "shopv_my"),
    ]])
    await _show(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("shopv_cgo:"))
async def shop_cancel_go(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    uid = callback.from_user.id
    sale = await shop_db.cancel_reservation(int(callback.data.split(":", 1)[1]), by=uid, owner_id=uid)
    if sale is None:
        await callback.answer(await _t("shop_reservation_gone_text", lang, tr_map), show_alert=True)
        return
    text = await _t("shop_cancelled_text", lang, tr_map, escape=True, title=html.escape(sale["title"]),
                    price=sale["price"], balance=sale["balance"])
    await _show(callback, text, InlineKeyboardMarkup(inline_keyboard=[_back(reg_i18n.tr_text("◀️ Назад", lang, tr_map))]))
    await callback.answer()
    _resync()
