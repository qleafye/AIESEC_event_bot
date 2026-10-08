"""Рассылка и сезон: по умолчанию сообщение уходит только текущему сезону, прошлые включаются
явным действием.

На проде в базе лежат делегаты прошлого сезона; «Всем» без оговорок зовёт и их. Здесь:
кнопки «👥 Включить прошлые сезоны» / «🎯 Только текущий сезон» в мастере фильтров и кнопка
«🎯 Только текущий сезон» на подтверждениях «Всем», «Не завершили регистрацию» и отложенной
рассылки «всем». Отложенная рассылка сохраняет условие сезона в `filter_spec`, поэтому
аудитория пересчитывается в момент отправки.

Форма шва — как `admin_broadcast_session_filter.py`: `router` из `handlers.admin`, ленивый
импорт `handlers.admin_broadcasts` (он сам импортирует этот модуль хвостом)."""
from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton

from database.db import SEASON_CURRENT, get_all_users_ids, split_ids_by_season
from handlers.admin import router
from handlers.states import Broadcast
from services.broadcast_scope import current_season_only, season_default_filter

_ONLY_CURRENT = "🎯 Только текущий сезон"


def _has_season(filters: list[dict]) -> bool:
    return any(f.get("field") == "season" and f.get("value") == SEASON_CURRENT for f in filters)


async def season_menu_row(filters: list[dict]) -> list[InlineKeyboardButton] | None:
    """Ряд меню фильтров: убрать условие сезона или вернуть его (если есть что возвращать)."""
    if _has_season(filters):
        return [InlineKeyboardButton(text="👥 Включить прошлые сезоны", callback_data="bcseason_all")]
    if await season_default_filter() is not None:
        return [InlineKeyboardButton(text=_ONLY_CURRENT, callback_data="bcseason_cur")]
    return None


async def menu_extra_rows(filters: list[dict]) -> list[list[InlineKeyboardButton]]:
    """Все дополнительные ряды меню фильтров: сезон + запись на сессии/тест."""
    from handlers.admin_broadcast_enroll_filter import enroll_menu_rows

    rows = []
    season_row = await season_menu_row(filters)
    if season_row:
        rows.append(season_row)
    return rows + await enroll_menu_rows()


async def _redraw_filter_menu(callback: types.CallbackQuery, filters: list[dict]) -> None:
    from handlers.admin_broadcasts import _render_filter_menu  # ленивый шов — см. докстринг

    await callback.answer()
    await _render_filter_menu(callback.message, filters, edit=True)


@router.callback_query(F.data == "bcseason_all", Broadcast.filter_field)
async def bcseason_all(callback: types.CallbackQuery, state: FSMContext):
    filters = [f for f in (await state.get_data()).get("filters", [])
               if not (f.get("field") == "season" and f.get("value") == SEASON_CURRENT)]
    await state.update_data(filters=filters)
    await _redraw_filter_menu(callback, filters)


@router.callback_query(F.data == "bcseason_cur", Broadcast.filter_field)
async def bcseason_cur(callback: types.CallbackQuery, state: FSMContext):
    filters = list((await state.get_data()).get("filters", []))
    cond = await season_default_filter()
    if cond is not None and not _has_season(filters):
        filters.insert(0, cond)
    await state.update_data(filters=filters)
    await _redraw_filter_menu(callback, filters)


async def season_confirm_extra(state: FSMContext, users_ids: list[int] | None) -> tuple[str, list]:
    """(строка, ряды кнопок) для подтверждения мгновенной рассылки «Всем»/«Не завершили».
    Рассылка по фильтру сюда не попадает: там сезон — осознанное условие менеджера."""
    if not users_ids or (await state.get_data()).get("filters") is not None:
        return "", []
    _current, past = await split_ids_by_season(users_ids)
    if not past:
        return "", []
    return (f"из них прошлого сезона: {len(past)}\n\n",
            [[InlineKeyboardButton(text=_ONLY_CURRENT, callback_data="bcseason_only")]])


async def schedule_season_extra(state: FSMContext) -> tuple[str, list]:
    """То же для отложенной рассылки «всем» (условие фильтров не задавалось)."""
    if (await state.get_data()).get("filters") is not None:
        return "", []
    _current, past = await split_ids_by_season(await get_all_users_ids())
    if not past:
        return "", []
    return (f"Из них прошлого сезона: {len(past)}\n\n",
            [[InlineKeyboardButton(text=_ONLY_CURRENT, callback_data="bcseason_sched")]])


@router.callback_query(F.data == "bcseason_only", Broadcast.confirm)
async def bcseason_only(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    from handlers.admin_broadcasts import _send_confirm_prompt  # ленивый шов — см. докстринг

    users_ids = await current_season_only((await state.get_data()).get("bc_users", []))
    await state.update_data(bc_users=users_ids)
    await callback.answer("Остался только текущий сезон")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_confirm_prompt(bot, callback.message.chat.id, state, len(users_ids), users_ids)


@router.callback_query(F.data == "bcseason_sched", Broadcast.schedule_confirm)
async def bcseason_sched(callback: types.CallbackQuery, state: FSMContext):
    from handlers.admin_broadcasts import _send_schedule_confirm_prompt  # ленивый шов

    cond = await season_default_filter() or {
        "field": "season", "value": SEASON_CURRENT, "label": "Текущий сезон",
    }
    await state.update_data(filters=[cond])
    await callback.answer("Получатели пересчитаются в момент отправки")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_schedule_confirm_prompt(callback.message, state)
