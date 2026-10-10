"""Рассылка и статус заявки: кому на самом деле уйдёт сообщение.

На проде менеджер отправила рассылку «🎯 По фильтру → 💬 Чат делегатов → не в чате» без
условия «Статус → Одобрена», и письмо ушло отклонённым и тем, кто анкету не подал. Поэтому
экран «Под фильтр попадает N» и подтверждение рассылки по фильтру или «Всем» показывают
разбивку получателей по статусу заявки. Если в получателях есть не одобренные, а условия по
статусу нет, рядом с «Отправить» появляется предупреждение и кнопка «✅ Только одобренные».

Форма шва — как `admin_broadcast_season.py`: `router` из `handlers.admin`, ленивый импорт
`handlers.comms.admin_broadcasts` (он сам импортирует этот модуль хвостом)."""
from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton

from database.app_status_db import APPROVED, NOT_SUBMITTED, PENDING, REJECTED, split_ids_by_app_status
from handlers.admin import router
from handlers.states import Broadcast

_ONLY_APPROVED = "✅ Только одобренные"
_APPROVED_FILTER = {"field": "status", "value": APPROVED}
_PARTS = (
    (APPROVED, "Одобрены"), (PENDING, "На рассмотрении"),
    (REJECTED, "Отклонены"), (NOT_SUBMITTED, "Не подали анкету"),
)


def _has_status(filters: list[dict] | None) -> bool:
    return any(f.get("field") == "status" for f in filters or [])


async def status_block(ids: list[int], filters: list[dict] | None, callback_data: str) -> tuple[str, list]:
    """(текст, ряды кнопок): разбивка по статусу и, если нужно, предупреждение с кнопкой.
    Все получатели одобрены — пусто."""
    if not ids:
        return "", []
    groups = await split_ids_by_app_status(ids)
    counts = {key: len(groups[key]) for key, _ in _PARTS}
    if counts[APPROVED] == len(ids):
        return "", []  # все одобрены — экран прежний, без шума
    text = "Получатели: " + " · ".join(f"{label} {counts[key]}" for key, label in _PARTS if counts[key]) + "\n"
    not_approved = counts[PENDING] + counts[NOT_SUBMITTED]
    if _has_status(filters) or not (counts[REJECTED] or not_approved):
        return text + "\n", []
    found = []
    if counts[REJECTED]:
        found.append(f"отклонённые ({counts[REJECTED]})")
    if not_approved:
        found.append(f"не одобренные ({not_approved})")
    text += f"⚠️ В рассылке есть {' и '.join(found)}. Обычно пишут только одобренным.\n\n"
    if not counts[APPROVED]:
        return text, []
    return text, [[InlineKeyboardButton(text=f"{_ONLY_APPROVED} ({counts[APPROVED]})", callback_data=callback_data)]]


async def confirm_extra(state: FSMContext, users_ids: list[int] | None) -> tuple[str, list]:
    """Строка и кнопки подтверждения мгновенной рассылки: сезон + статус заявки.
    Статус — только у «Всем» и рассылки по фильтру; файл и «Не завершили регистрацию» —
    списки, где статус известен заранее."""
    from handlers.comms.admin_broadcast_season import season_confirm_extra

    season_text, season_rows = await season_confirm_extra(state, users_ids)
    data = await state.get_data()
    filters = data.get("filters")
    if not users_ids or (data.get("target_type", "all") != "all" and filters is None):
        return season_text, season_rows
    status_text, status_rows = await status_block(users_ids, filters, "bcstatus_only")
    return season_text + status_text, status_rows + season_rows


@router.callback_query(F.data == "bcstatus_only", Broadcast.confirm)
async def bcstatus_only(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    from handlers.comms.admin_broadcasts import _send_confirm_prompt  # ленивый шов — см. докстринг

    data = await state.get_data()
    users_ids = (await split_ids_by_app_status(data.get("bc_users", [])))[APPROVED]
    update = {"bc_users": users_ids}
    if data.get("filters") is not None and not _has_status(data["filters"]):
        update["filters"] = [*data["filters"], dict(_APPROVED_FILTER)]
    await state.update_data(**update)
    await callback.answer("Остались только одобренные")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_confirm_prompt(bot, callback.message.chat.id, state, len(users_ids), users_ids)


@router.callback_query(F.data == "bcstatus_filter", Broadcast.filter_field)
async def bcstatus_filter(callback: types.CallbackQuery, state: FSMContext):
    """Экран «Под фильтр попадает N»: добавить условие «Статус = Одобрена» и пересчитать."""
    from handlers.comms.admin_broadcasts import filter_count  # ленивый шов — см. докстринг

    filters = list((await state.get_data()).get("filters", []))
    if not _has_status(filters):
        filters.append(dict(_APPROVED_FILTER))
    await state.update_data(filters=filters)
    await filter_count(callback, state)
