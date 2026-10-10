"""Форум-ночь п.6 (D-25, идея №14): мастер «Были на сессии …» / «Не были на сессии …» —
выбор КОНКРЕТНОЙ сессии программы кнопками (город → день → сессия) для фильтра рассылки
`checkin_session` (`database/db.py`).

Не входит в generic-пикер `handlers/admin_broadcasts.py::_show_value_picker` — там значение
всегда одно из ДВУХ сентинелов (has/none, in/out, …), здесь значение — конкретная сессия
программы, у которой есть собственный трёхшаговый выбор. Форма шва — эталон
`handlers/admin_program.py`: своего `Router()` нет, `from handlers.admin import router`,
каждый декоратор — в одну строку (инвариант cap-теста `tests/test_roles_phase8.py`). Импорт —
хвостом `handlers/admin_broadcasts.py` (тот же приём, что `admin_program_halls` в хвосте
`admin_program.py`).

Состояние мастера живёт в FSM (`Broadcast.filter_field`, тот же state, что весь остальной
мастер фильтра): `cksf_mode` (attended/not_attended, ставится один раз на «старте»),
`cksf_city` (код города, ставится на шаге города). День едет в `callback_data` дня И сразу
разбирается в список сессий — отдельно в FSM не хранится."""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, default_city_code, enabled_cities
from database.db import SESSION_ATTENDED, SESSION_NOT_ATTENDED
from handlers.admin import router
from handlers.states import Broadcast
from services.program import day_label, sessions_for_city_day, session_point_label

_MODES = (SESSION_ATTENDED, SESSION_NOT_ATTENDED)

_CANCEL_ROW = [[InlineKeyboardButton(text="← Назад", callback_data="cksf_cancel")]]


async def _render_city_step(mode: str) -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"cksf_city:{c['code']}")]
        for c in await enabled_cities()
    ]
    buttons += _CANCEL_ROW
    verb = "были" if mode == SESSION_ATTENDED else "не были"
    return f"🎤 Сессия какого города — делегаты «{verb}»?", InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_day_step(code: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """`None` — в этом городе программа ещё не заведена (нет ни одной сессии) — вызывающий
    остаётся на предыдущем экране с алертом, а не рисует пустой список дней."""
    from database.db import list_program_days_for_city

    days = await list_program_days_for_city(code)
    if not days:
        return None
    label = await city_label(code)
    buttons = [
        [InlineKeyboardButton(text=f"📅 {day_label(day)}", callback_data=f"cksf_day:{day}")]
        for day in days
    ]
    buttons += _CANCEL_ROW
    return f"🎤 Программа — {label}. Выберите день:", InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_session_step(code: str, day: str) -> tuple[str, InlineKeyboardMarkup]:
    sessions = await sessions_for_city_day(code, day)
    buttons = [
        [InlineKeyboardButton(text=session_point_label(s), callback_data=f"cksf_pick:{s['id']}")]
        for s in sessions
    ]
    buttons += _CANCEL_ROW
    return f"🎤 {day_label(day)} — выберите сессию:", InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("cksf_start:"), Broadcast.filter_field)
async def cksf_start(callback: types.CallbackQuery, state: FSMContext):
    mode = callback.data.split(":", 1)[1]
    if mode not in _MODES:
        await callback.answer("Некорректный режим.", show_alert=True)
        return
    await state.update_data(cksf_mode=mode, cksf_city=None)
    await callback.answer()
    if await cities_module_on():
        text, kb = await _render_city_step(mode)
        await callback.message.edit_text(text, reply_markup=kb)
        return
    code = default_city_code()
    await state.update_data(cksf_city=code)
    day_screen = await _render_day_step(code)
    if day_screen is None:
        await callback.answer("Программа ещё не заведена — фильтровать не по чему.", show_alert=True)
        return
    text, kb = day_screen
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cksf_city:"), Broadcast.filter_field)
async def cksf_city_pick(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    data = await state.get_data()
    mode = data.get("cksf_mode")
    if mode not in _MODES:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        return
    day_screen = await _render_day_step(code)
    if day_screen is None:
        await callback.answer(
            "В этом городе программа ещё не заведена — выберите другой город.", show_alert=True,
        )
        return
    await state.update_data(cksf_city=code)
    text, kb = day_screen
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cksf_day:"), Broadcast.filter_field)
async def cksf_day_pick(callback: types.CallbackQuery, state: FSMContext):
    day = callback.data.split(":", 1)[1]
    data = await state.get_data()
    code = data.get("cksf_city")
    if data.get("cksf_mode") not in _MODES or not code:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        return
    text, kb = await _render_session_step(code, day)
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cksf_pick:"), Broadcast.filter_field)
async def cksf_session_pick(callback: types.CallbackQuery, state: FSMContext):
    try:
        session_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    data = await state.get_data()
    mode = data.get("cksf_mode")
    code = data.get("cksf_city")
    if mode not in _MODES or not code:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        return
    from database.db import get_program_session

    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Эта сессия уже удалена — выберите другую.", show_alert=True)
        return
    filters = data.get("filters", [])
    verb = "были" if mode == SESSION_ATTENDED else "не были"
    label = f"{verb} на «{session['title']}» ({day_label(session['day'])})"
    filters.append({"field": "checkin_session", "value": mode, "session_id": session_id, "label": label})
    await state.update_data(
        filters=filters, cksf_mode=None, cksf_city=None,
    )
    await callback.answer()

    from handlers.admin_broadcasts import _render_filter_menu  # ленивый шов — см. докстринг модуля

    await _render_filter_menu(callback.message, filters, edit=True)


@router.callback_query(F.data == "cksf_cancel", Broadcast.filter_field)
async def cksf_cancel(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(cksf_mode=None, cksf_city=None)
    await callback.answer()

    from handlers.admin_broadcasts import _render_filter_menu  # ленивый шов — см. докстринг модуля

    await _render_filter_menu(callback.message, data.get("filters", []), edit=True)
