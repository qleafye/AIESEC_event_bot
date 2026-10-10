"""Мастер фильтров рассылки по записи на сессии и тесту: «📅 Записан на сессию…» (город → день →
сессия), «🚫 Не записался ни на одну» (город), «🧭 Не прошёл тест» (город).

Кладёт в `filters` записи `session_enroll` / `quiz` (SQL — `database/db.py`) с человеческой
подписью. Приглашение пройти тест рассылается обычным текстом с deep-link: отдельного
конструктора кнопок нет.

Форма шва — `admin_broadcast_session_filter.py`: `router` из `handlers.admin`, состояние мастера
в FSM (`Broadcast.filter_field`): `enrf_kind` и `enrf_city`; ленивый импорт
`handlers.comms.admin_broadcasts` (он импортирует этот модуль хвостом)."""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, city_scope, default_city_code, enabled_cities
from database.db import QUIZ_NOT_PASSED, SESSION_ENROLL_IN, SESSION_ENROLL_NONE
from database.quiz_db import get_quiz_for_city
from database.session_enroll_db import list_trackable_sessions
from handlers.admin import router
from handlers.states import Broadcast
from services.program import day_label, session_point_label

_KINDS = (SESSION_ENROLL_IN, SESSION_ENROLL_NONE, "quiz")
_BACK_ROW = [[InlineKeyboardButton(text="← Назад", callback_data="enrf_cancel")]]


async def _city_codes() -> list[str]:
    if await cities_module_on():
        return [c["code"] for c in await enabled_cities()]
    return [default_city_code()]


async def _has_content(kind: str, code: str) -> bool:
    if kind == "quiz":
        return await get_quiz_for_city(code) is not None
    return bool(await list_trackable_sessions(code))


async def _cities_with(kind: str) -> list[str]:
    return [c for c in await _city_codes() if await _has_content(kind, c)]


async def enroll_menu_rows() -> list[list[InlineKeyboardButton]]:
    """Ряды кнопок меню фильтров: запись — если есть сессии с треком, тест — если он есть."""
    rows = []
    if await _cities_with(SESSION_ENROLL_IN):
        rows.append([
            InlineKeyboardButton(text="📅 Записан на сессию…", callback_data="enrf_start:in"),
            InlineKeyboardButton(text="🚫 Не записался ни на одну", callback_data="enrf_start:none"),
        ])
    if await _cities_with("quiz"):
        rows.append([InlineKeyboardButton(text="🧭 Не прошёл тест", callback_data="enrf_start:quiz")])
    return rows


async def _redraw(callback: types.CallbackQuery, filters: list[dict]) -> None:
    from handlers.comms.admin_broadcasts import _render_filter_menu  # ленивый шов — см. докстринг

    await _render_filter_menu(callback.message, filters, edit=True)


async def _finish(callback: types.CallbackQuery, state: FSMContext, cond: dict) -> None:
    filters = list((await state.get_data()).get("filters", []))
    filters.append(cond)
    await state.update_data(filters=filters, enrf_kind=None, enrf_city=None)
    await _redraw(callback, filters)


async def _after_city(callback: types.CallbackQuery, state: FSMContext, kind: str, code: str) -> None:
    """Город выбран: для «записан» — к выбору дня, для остальных — условие готово."""
    if kind != SESSION_ENROLL_IN:
        label = await city_label(code)
        scope = city_scope(code)
        exclude = list(scope[1]) if scope else []
        if kind == "quiz":
            cond = {"field": "quiz", "value": QUIZ_NOT_PASSED, "city": code, "exclude": exclude,
                    "label": f"🧭 Не прошёл тест ({label})"}
        else:
            cond = {"field": "session_enroll", "value": SESSION_ENROLL_NONE, "city": code,
                    "exclude": exclude, "label": f"🚫 Не записался ни на одну сессию ({label})"}
        await _finish(callback, state, cond)
        return
    await state.update_data(enrf_city=code)
    days = sorted({s["day"] for s in await list_trackable_sessions(code)})
    buttons = [[InlineKeyboardButton(text=f"📅 {day_label(d)}", callback_data=f"enrf_day:{d}")] for d in days]
    await callback.message.edit_text(
        f"📅 Программа — {await city_label(code)}. Выберите день:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons + _BACK_ROW),
    )


@router.callback_query(F.data.startswith("enrf_start:"), Broadcast.filter_field)
async def enrf_start(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 1)[1]
    if kind not in _KINDS:
        await callback.answer("Некорректный режим.", show_alert=True)
        return
    codes = await _cities_with(kind)
    if not codes:
        await callback.answer("Фильтровать пока не по чему — в городах нет подходящих данных.",
                              show_alert=True)
        return
    await state.update_data(enrf_kind=kind, enrf_city=None)
    await callback.answer()
    if len(codes) == 1:
        await _after_city(callback, state, kind, codes[0])
        return
    buttons = [[InlineKeyboardButton(text=await city_label(c), callback_data=f"enrf_city:{c}")]
               for c in codes]
    await callback.message.edit_text(
        "🏙 Какой город?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons + _BACK_ROW),
    )


@router.callback_query(F.data.startswith("enrf_city:"), Broadcast.filter_field)
async def enrf_city_pick(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    kind = (await state.get_data()).get("enrf_kind")
    if kind not in _KINDS or code not in await _cities_with(kind):
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        return
    await callback.answer()
    await _after_city(callback, state, kind, code)


@router.callback_query(F.data.startswith("enrf_day:"), Broadcast.filter_field)
async def enrf_day_pick(callback: types.CallbackQuery, state: FSMContext):
    day = callback.data.split(":", 1)[1]
    data = await state.get_data()
    code = data.get("enrf_city")
    if data.get("enrf_kind") != SESSION_ENROLL_IN or not code:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        return
    sessions = await list_trackable_sessions(code, day)
    buttons = [[InlineKeyboardButton(text=session_point_label(s), callback_data=f"enrf_pick:{s['id']}")]
               for s in sessions]
    await callback.answer()
    await callback.message.edit_text(
        f"📅 {day_label(day)} — выберите сессию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons + _BACK_ROW),
    )


@router.callback_query(F.data.startswith("enrf_pick:"), Broadcast.filter_field)
async def enrf_session_pick(callback: types.CallbackQuery, state: FSMContext):
    try:
        session_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("enrf_kind") != SESSION_ENROLL_IN or not data.get("enrf_city"):
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        return
    from database.db import get_program_session

    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Эта сессия уже удалена — выберите другую.", show_alert=True)
        return
    await callback.answer()
    await _finish(callback, state, {
        "field": "session_enroll", "value": SESSION_ENROLL_IN, "session_id": session_id,
        "label": f"Записан на «{session['title']}» ({day_label(session['day'])})",
    })


@router.callback_query(F.data == "enrf_cancel", Broadcast.filter_field)
async def enrf_cancel(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(enrf_kind=None, enrf_city=None)
    await callback.answer()
    await _redraw(callback, data.get("filters", []))
