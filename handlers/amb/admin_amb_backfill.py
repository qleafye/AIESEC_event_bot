"""«🤝 Амбассадоры → 💰 Баллы и приватность» -> «🔁 Начислить за прошлых приглашённых»: догон баллов
амбассадорам за приглашённых, одобренных до запуска амбассадорского слоя. Предпросмотр показывает,
кому и сколько начислится, затем отдельная кнопка «Начислить».

Логика — `services.game.referrals.backfill_approved`:
кандидаты только текущего сезона, начисление идёт через идемпотентную точку журнала зачётов, так что
повторное нажатие никого не задвоит. Баллы догона идут только в общий зачёт, ни в одну волну.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/amb/admin_amb_points.py`. Право — `moderate_game`, как у соседних кнопок экрана.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from services.game import referrals

logger = logging.getLogger(__name__)

_LIST_LIMIT = 10
_lock = asyncio.Lock()


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="← К баллам и приватности", callback_data="admin_amb_points")]


async def _edit(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


_NOT_GLOBAL = (
    "Начисление касается амбассадоров всех городов сразу, поэтому оно доступно только при выбранном "
    "режиме «Все города». Переключите город в админке и откройте экран заново."
)


async def _global_only(callback: types.CallbackQuery) -> bool:
    """Начисление идёт по амбассадорам всех городов сразу — только админ в режиме «Все города»
    (тот же гейт, что у «🧹 Сбросить статусы»). Иначе городской модератор с правом геймификации
    начислил бы баллы чужим городам."""
    from handlers.amb.admin_amb_reset import _is_global

    if await _is_global(callback.from_user.id):
        return True
    await callback.answer(_NOT_GLOBAL, show_alert=True)
    return False


async def _referral_coins_off() -> bool:
    """Баллы за приглашённого — 0 у всех, кого догон реально читает.

    Ключ городской: общий ноль не гасит догон, если у города своё положительное число.
    Выключенный модуль городов и пустой список городов читают только общее значение —
    то же правило, что у начисления.
    """
    from domain.cities import city_codes, cities_module_on, get_setting_typed_for_city, is_per_city

    key = "ambassador_referral_coins"
    base = int(await get_setting_typed_for_city(key, None) or 0)
    if not is_per_city(key) or not await cities_module_on():
        return base <= 0
    codes = city_codes()
    if not codes:
        return base <= 0
    for code in codes:
        if int(await get_setting_typed_for_city(key, code) or 0) > 0:
            return False
    return True


async def _show_preview(callback: types.CallbackQuery) -> None:
    if await _referral_coins_off():
        await _edit(
            callback,
            "<b>🔁 Начислить за прошлых приглашённых</b>\n\n"
            "Баллы за приглашённого сейчас выключены (0), поэтому начислять нечего. Задайте число "
            "баллов кнопкой «💰 Баллов за приглашённого» и вернитесь сюда.",
            InlineKeyboardMarkup(inline_keyboard=[_back_row()]),
        )
        return
    summary = await referrals.backfill_approved(dry_run=True)
    if not summary["candidates"]:
        if summary["skipped_zero"]:
            await _edit(
                callback,
                "<b>🔁 Начислить за прошлых приглашённых</b>\n\n"
                "Баллы за приглашённого у этих амбассадоров сейчас 0, поэтому начислять нечего. "
                "Задайте число для их города и вернитесь сюда.",
                InlineKeyboardMarkup(inline_keyboard=[_back_row()]),
            )
            return
        await _edit(
            callback,
            "<b>🔁 Начислить за прошлых приглашённых</b>\n\n"
            "Начислять нечего: за всех одобренных приглашённых текущего сезона баллы уже начислены.",
            InlineKeyboardMarkup(inline_keyboard=[_back_row()]),
        )
        return
    lines = [
        "<b>🔁 Начислить за прошлых приглашённых</b>",
        "Находит одобренных приглашённых этого сезона, за которых амбассадору ещё не начислены "
        "баллы (например, одобренных до запуска программы).",
        "",
        f"Начислится: {summary['coins']} баллов. Амбассадоров: {summary['ambassadors']}, "
        f"приглашённых: {summary['credited']}.",
        "",
    ]
    for entry in summary["breakdown"][:_LIST_LIMIT]:
        lines.append(
            f"• {html.escape(entry['referrer_name'])}: приглашённых {entry['invitees']}, "
            f"баллов {entry['coins']}"
        )
    if len(summary["breakdown"]) > _LIST_LIMIT:
        lines.append(f"…и ещё {len(summary['breakdown']) - _LIST_LIMIT}")
    if summary["skipped_zero"]:
        lines.append(
            f"Не попадут: {summary['skipped_zero']} приглашённых — "
            "баллы за приглашённого у их амбассадора сейчас 0."
        )
    lines += [
        "",
        "Баллы нельзя отозвать кнопкой, как и обычное начисление за одобрение (убрать можно только "
        "исключением за накрутку). Они идут в общий зачёт, в рейтинг волны не попадают. "
        "Повторное нажатие никого не задвоит.",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Начислить",
            callback_data=f"ambpt_fill_go:{summary['coins']}:{summary['credited']}",
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_amb_points")],
    ])
    await _edit(callback, "\n".join(lines), kb)


@router.callback_query(F.data == "ambpt_fill")
async def amb_backfill_preview(callback: types.CallbackQuery):
    if not await _global_only(callback):
        return
    await _show_preview(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("ambpt_fill_go"))
async def amb_backfill_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await callback.answer("Кнопка устарела — откройте экран заново.", show_alert=True)
        return
    if not await _global_only(callback):
        return
    if _lock.locked():
        await callback.answer("Начисление уже идёт — дождитесь итога.", show_alert=True)
        return
    async with _lock:
        seen = await referrals.backfill_approved(dry_run=True)
        if (str(seen["coins"]), str(seen["credited"])) != (parts[1], parts[2]):
            await callback.answer("Список изменился, проверьте ещё раз.", show_alert=True)
            await _show_preview(callback)
            return
        await callback.answer()
        try:
            summary = await referrals.backfill_approved(dry_run=False)
        except Exception:
            logger.exception("amb_backfill: начисление оборвалось by=%s", callback.from_user.id)
            await callback.message.answer(
                "Не получилось начислить баллы до конца. Часть начислений могла успеть записаться. "
                "Откройте «🔁 Начислить за прошлых приглашённых» заново и нажмите «✅ Начислить» — "
                "повторное нажатие никого не задвоит. Если снова не выйдет — напишите @qleafye."
            )
            return
    logger.info(
        "amb_backfill: by=%s начислено=%s баллов=%s амбассадорам=%s",
        callback.from_user.id, summary["credited"], summary["coins"], summary["ambassadors"],
    )
    if summary["credited"]:
        text = (
            f"Готово. Начислено баллов: {summary['coins']}. Амбассадоров: {summary['ambassadors']}, "
            f"приглашённых: {summary['credited']}."
        )
    elif summary["skipped_zero"]:
        text = (
            "Готово. Новых начислений нет: у этих амбассадоров баллы за приглашённого сейчас 0."
        )
    else:
        text = "Готово. Новых начислений нет: за всех приглашённых баллы уже начислены."
    from handlers.amb.admin_amb_points import render_points_screen
    screen, kb = await render_points_screen()
    await callback.message.answer(text)
    await callback.message.answer(screen, parse_mode="HTML", reply_markup=kb)


__all__ = ["amb_backfill_preview", "amb_backfill_go"]
