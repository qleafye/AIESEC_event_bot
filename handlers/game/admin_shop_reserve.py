"""«🛍 Магазин баллов»: брони делегатов на стойке — тумблер «Бронь делегатом», выдача по коду,
карточка брони, выдача и отмена чужой брони.

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров читает их построчно).

Делегат бронирует сам (handlers/game/shop_delegate.py), баллы списаны сразу; здесь бронь только
выдают («✅ Выдать бронь» — атомарно, второй тап ничего не выдаёт) или отменяют с подтверждением
(баллы и остаток возвращаются). Карточка брони — `services.game.shop_card.reservation_card`, та же,
что приходит волонтёру после скана QR брони в сканере Mini App.

Шов `handlers/game/admin_shop.py`: хендлеры на общем `admin.router`, импорт хвостом admin_shop.
Колбэки — под префиксом `shp_*`, состояние — `ShopAdmin.code`: право `moderate_game` уже
выдаётся записями ADMIN_CAPS магазина, новых записей не нужно."""
from __future__ import annotations

import html
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext

from database import shop_db
from database.db import get_balance
from domain.settings.schema import get_setting_typed
from handlers.game.admin_shop import _edit_or_answer, _kb, _resync, _who, home_text_kb, router, shop_title
from handlers.states import ShopAdmin
from services.game.shop_card import reservation_card
from services.infra.ru_plural import agree_placeholder

logger = logging.getLogger(__name__)

_GONE = "Эта бронь уже выдана или отменена — второй раз не выдаём."
_TO_HOME = [("◀️ В магазин", "shp_home")]


async def _notify(callback: types.CallbackQuery, tid: int, key: str, **values) -> None:
    """Сообщение делегату на его языке; не дошло — не страшно, выдача/отмена уже записаны."""
    try:
        from handlers.i18n.reg_i18n import tr_text
        from services.i18n.i18n import context

        lang, tr_map = await context(tid)
        text = tr_text(await get_setting_typed(key) or "", lang, tr_map) or ""
        if "price" in values:
            text = agree_placeholder(text, "price", values["price"])
        for name, value in values.items():
            text = text.replace("{" + name + "}", str(value))
        await callback.bot.send_message(tid, text, parse_mode=None)
    except Exception as e:
        logger.info(f"admin_shop_reserve: сообщение делегату {tid} не ушло: {type(e).__name__}")


@router.callback_query(F.data == "shp_resv")
async def shop_toggle_reserve(callback: types.CallbackQuery):
    now = not await shop_db.is_reserve_on()
    if now and not await shop_db.list_items(active_only=True):
        await callback.answer("Сначала добавьте хотя бы один товар в «📦 Товары».", show_alert=True)
        return
    await shop_db.set_reserve(now)
    text, kb = await home_text_kb()
    await _edit_or_answer(callback, text, kb)
    if not now:
        await callback.answer("Бронь выключена. Уже взятые брони по-прежнему выдаются и отменяются.")
    elif await shop_db.is_open():
        await callback.answer("Бронь включена — у делегатов на витрине появилось «Взять»")
    else:
        await callback.answer("Бронь включена — делегаты увидят «Взять», когда витрина видна", show_alert=True)


@router.callback_query(F.data == "shp_code")
async def shop_code_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ShopAdmin.code)
    await callback.message.answer(
        "Пришлите код брони с экрана делегата.\n\nПример: <code>Ю-4821</code> или просто <code>4821</code>",
        parse_mode="HTML", reply_markup=_kb([_TO_HOME]),
    )
    await callback.answer()


@router.message(ShopAdmin.code, F.text)
async def shop_code_input(message: types.Message, state: FSMContext):
    sale = await shop_db.find_by_code(message.text)
    if sale is None:
        await message.answer(
            f"Не нашёл действующую бронь с кодом «{html.escape(message.text.strip()[:20])}». Проверьте цифры. "
            "Если бронь уже выдана или отменена — она в «🧾 Последние выдачи».",
            parse_mode="HTML", reply_markup=_kb([_TO_HOME]),
        )
        return
    await state.set_state(None)
    text, rows = reservation_card(sale)
    await message.answer(text, parse_mode="HTML", reply_markup=_kb(rows))


async def _active(sale_id: int) -> dict | None:
    sale = await shop_db.get_sale(sale_id)
    if sale is None or sale["status"] != shop_db.STATUS_RESERVED or sale["undone_at"]:
        return None
    return await shop_db.find_by_code(sale["pickup_code"])


@router.callback_query(F.data.startswith("shp_rc:"))
async def shop_res_card(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    sale = await _active(int(callback.data.split(":", 1)[1]))
    if sale is None:
        await callback.answer(_GONE, show_alert=True)
        return
    text, rows = reservation_card(sale)
    await _edit_or_answer(callback, text, _kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("shp_rgo:"))
async def shop_res_issue(callback: types.CallbackQuery):
    sale = await shop_db.issue_reservation(int(callback.data.split(":", 1)[1]), callback.from_user.id)
    if sale is None:
        await callback.answer(_GONE, show_alert=True)
        return
    tid = int(sale["telegram_id"])
    await _edit_or_answer(
        callback,
        f"✅ Выдано по брони {sale['pickup_code']}: «{html.escape(sale['title'])}» — {_who(sale, tid)}",
        _kb([[("🎫 Следующая бронь", "shp_code")], _TO_HOME]),
    )
    await callback.answer("Выдано")
    await _notify(callback, tid, "shop_issued_notify_text", shop=await shop_title(), code=sale["pickup_code"], title=sale["title"])
    _resync()


@router.callback_query(F.data.startswith("shp_rcan:"))
async def shop_res_cancel(callback: types.CallbackQuery):
    sale = await _active(int(callback.data.split(":", 1)[1]))
    if sale is None:
        await callback.answer("Эта бронь уже выдана или отменена.", show_alert=True)
        return
    from services.infra.ru_plural import points_word

    await _edit_or_answer(
        callback,
        f"Отменить бронь {sale['pickup_code']} — «{html.escape(sale['title'])}», {_who(sale, int(sale['telegram_id']))}?\n\n"
        f"Вернём делегату {sale['price']} {points_word(sale['price'])}, товар вернётся в остаток, код перестанет работать.",
        _kb([[("↩️ Да, отменить бронь", f"shp_rcango:{sale['id']}"), ("Не отменять", f"shp_rc:{sale['id']}")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shp_rcango:"))
async def shop_res_cancel_go(callback: types.CallbackQuery):
    sale = await shop_db.cancel_reservation(int(callback.data.split(":", 1)[1]), by=callback.from_user.id)
    if sale is None:
        await callback.answer("Эта бронь уже выдана или отменена.", show_alert=True)
        return
    from services.infra.ru_plural import points_word

    tid = int(sale["telegram_id"])
    await _edit_or_answer(
        callback,
        f"↩️ Бронь {sale['pickup_code']} «{html.escape(sale['title'])}» отменена, делегату вернули "
        f"{sale['price']} {points_word(sale['price'])}. На счету: {sale['balance']}.",
        _kb([_TO_HOME]),
    )
    await callback.answer("Отменено")
    await _notify(callback, tid, "shop_cancelled_by_staff_notify_text", shop=await shop_title(),
                  code=sale["pickup_code"], title=sale["title"], price=sale["price"], balance=await get_balance(tid))
    _resync()
