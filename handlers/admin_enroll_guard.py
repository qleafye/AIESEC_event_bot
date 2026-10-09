"""Запись на сессии: предупреждение менеджеру, когда правка сессии затрагивает уже записанных.

Своего `Router()` нет: `from handlers.admin import router`, импортирован хвостом
`handlers/admin_enroll.py`. Две правки, которые молча ломали бы чужие записи:
- время сессии (`confirm_time`) — записанные остаются записанными, но время для них меняется;
- лимит мест ниже числа записанных (`confirm_limit`) — новых записей не будет, текущие остаются.
Если записанных нет, вопроса нет — правка применяется сразу, как раньше.

Callback'и: prog_tmok:{sid} / prog_tmno (время), prog_lmok:{sid}:{limit} / prog_lmno (лимит).
Право — `settings` по префиксу `prog_*`; город всегда из строки БД и сверяется `_city_allowed`."""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import session_enroll_db as edb
from database.db import get_program_session, update_program_session
from handlers.admin import router
from handlers.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed


def _plural(n: int) -> str:
    if n % 100 in (11, 12, 13, 14):
        return "человек"
    return {1: "человек", 2: "человека", 3: "человека", 4: "человека"}.get(n % 10, "человек")


def _kb(yes_text: str, yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=yes_text, callback_data=yes_data)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=no_data)],
    ])


async def confirm_time(target: types.Message, state: FSMContext, session_id: int,
                       start: str, end: str) -> bool:
    """True — спросили подтверждение (правку не применять). False — записанных нет или
    время не меняется."""
    session = await get_program_session(session_id)
    if not session or (session["start_time"], session["end_time"]) == (start, end):
        return False
    count = await edb.count_enrollments(session_id)
    if not count:
        return False
    await state.update_data(pf_pending_start=start, pf_pending_end=end)
    await target.answer(
        f"Записано {count} {_plural(count)} — они останутся записаны, "
        f"но время изменится: {session['start_time']}–{session['end_time']} → {start}–{end}.\n\n"
        "Новое время может пересечься с другими сессиями, на которые они записаны. "
        "Изменить время?",
        reply_markup=_kb("✅ Да, изменить время", f"prog_tmok:{session_id}", "prog_tmno"),
    )
    return True


async def confirm_limit(target: types.Message, session: dict, limit: int | None) -> bool:
    """True — лимит ниже числа записанных, спросили подтверждение."""
    if limit is None:
        return False
    count = await edb.count_enrollments(session["id"])
    if limit >= count:
        return False
    await target.answer(
        f"Записано {count} {_plural(count)}, а лимит {limit}: новых записей не будет, "
        "текущие записи останутся. Поставить такой лимит?",
        reply_markup=_kb("✅ Да, поставить", f"prog_lmok:{session['id']}:{limit}", "prog_lmno"),
    )
    return True


async def _session_for(callback: types.CallbackQuery, session_id: int) -> dict | None:
    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Сессия больше недоступна.", show_alert=True)
        return None
    if not await _city_allowed(callback.from_user.id, session["city"]):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return None
    return session


@router.callback_query(F.data.startswith("prog_tmok:"))
async def prog_tmok(callback: types.CallbackQuery, state: FSMContext):
    try:
        session_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    if await _session_for(callback, session_id) is None:
        return
    data = await state.get_data()
    start, end = data.get("pf_pending_start"), data.get("pf_pending_end")
    await state.clear()
    if start is None or end is None:
        await callback.answer("Действие устарело — откройте карточку заново.", show_alert=True)
        return
    await update_program_session(session_id, start_time=start, end_time=end)
    from handlers.admin_program import _edit_to_card
    await _edit_to_card(callback, session_id)
    await callback.answer("Время обновлено.")


@router.callback_query(F.data == "prog_tmno")
async def prog_tmno(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Время не изменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("prog_lmok:"))
async def prog_lmok(callback: types.CallbackQuery):
    try:
        _, sid, limit = callback.data.split(":")
        session_id, limit = int(sid), int(limit)
    except ValueError:
        await callback.answer()
        return
    if await _session_for(callback, session_id) is None:
        return
    await update_program_session(session_id, enroll_limit=limit)
    from handlers.admin_enroll import render_enroll_card
    text, kb = await render_enroll_card(await get_program_session(session_id))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Лимит сохранён.")


@router.callback_query(F.data == "prog_lmno")
async def prog_lmno(callback: types.CallbackQuery):
    await callback.message.edit_text("Лимит не изменён.")
    await callback.answer()
