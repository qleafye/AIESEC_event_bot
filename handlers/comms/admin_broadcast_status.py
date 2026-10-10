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


async def _status_parts(ids: list[int], filters: list[dict] | None, callback_data: str) -> tuple[str, str, list]:
    """(строка «Получатели: …», предупреждение, ряды кнопок). Строка есть всегда, когда есть
    получатели: после «Только одобренные» менеджер видит, что условие сработало."""
    if not ids:
        return "", "", []
    groups = await split_ids_by_app_status(ids)
    counts = {key: len(groups[key]) for key, _ in _PARTS}
    line = "Получатели: " + " · ".join(f"{label} {counts[key]}" for key, label in _PARTS if counts[key]) + "\n"
    not_approved = counts[PENDING] + counts[NOT_SUBMITTED]
    if _has_status(filters) or not (counts[REJECTED] or not_approved):
        return line, "", []
    found = []
    if counts[REJECTED]:
        found.append(f"отклонённые ({counts[REJECTED]})")
    if not_approved:
        found.append(f"не одобренные ({not_approved})")
    warning = f"⚠️ В рассылке есть {' и '.join(found)}. Обычно пишут только одобренным.\n"
    if not counts[APPROVED]:
        return line, warning, []
    return line, warning, [[InlineKeyboardButton(text=f"{_ONLY_APPROVED} ({counts[APPROVED]})", callback_data=callback_data)]]


async def status_block(ids: list[int], filters: list[dict] | None, callback_data: str) -> tuple[str, list]:
    """(текст, ряды кнопок) для экрана «Под фильтр попадает N»."""
    line, warning, rows = await _status_parts(ids, filters, callback_data)
    return (line + warning + "\n" if line else ""), rows


async def staff_missing_note(missing: set[int]) -> str:
    """Кто из команды (админы и менеджеры) не получит рассылку «Всем» и почему: без анкеты
    делегата, анкета не одобрена или отсеян другим условием (например, сезоном)."""
    groups = await split_ids_by_app_status(sorted(missing))
    no_form = len(groups[NOT_SUBMITTED])
    if no_form == len(missing):
        return (f"⚠️ {no_form} из команды (админы и менеджеры) не зарегистрированы как делегаты — "
                "рассылку они не получат.\n\n")
    parts = [(no_form, "без анкеты делегата"),
             (len(groups[PENDING]) + len(groups[REJECTED]), "анкета не одобрена"),
             (len(groups[APPROVED]), "не прошли условия рассылки")]
    reasons = "; ".join(f"{label} — {n}" for n, label in parts if n)
    return f"⚠️ {len(missing)} из команды (админы и менеджеры) рассылку не получат: {reasons}.\n\n"


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
    line, warning, status_rows = await _status_parts(users_ids, filters, "bcstatus_only")
    # сезон — строкой сразу под «Получатели: …», чтобы число не читалось как чужое
    text = line + season_text.strip() + ("\n" if season_text else "") + warning
    return (text + "\n" if text else ""), status_rows + season_rows


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
