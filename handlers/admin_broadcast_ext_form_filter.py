"""Фильтр рассылки «📝 Внешняя форма»: выбор формы кнопками и режима «заполнил / не заполнил».

Не входит в generic-пикер `handlers/admin_broadcasts.py::_show_value_picker`: значение здесь —
конкретная форма (`form_id` едет внутри записи фильтра), SQL-ветка — `database/db.py`
(`ext_form`). Форма шва — как у `admin_broadcast_session_filter.py`: `from handlers.admin
import router`, декораторы в одну строку, импорт — хвостом `admin_broadcasts.py`."""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.ext_forms_db import list_forms
from handlers.admin import router
from handlers.states import Broadcast

_MODES = {"filled": "заполнил", "not_filled": "не заполнил"}
_CANCEL_ROW = [[InlineKeyboardButton(text="← Назад", callback_data="extff_cancel")]]


def _title(form: dict) -> str:
    return form.get("title") or f"Форма №{form['id']}"


async def _find_form(form_id: int) -> dict | None:
    return next((f for f in await list_forms() if f["id"] == form_id), None)


@router.callback_query(F.data == "extff_start", Broadcast.filter_field)
async def extff_start(callback: types.CallbackQuery, state: FSMContext):
    forms = await list_forms()
    if not forms:
        await callback.answer("Ни одна форма ещё не подключена.", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton(text=f"📝 {_title(f)}", callback_data=f"extff_form:{f['id']}")]
        for f in forms
    ]
    await callback.answer()
    await callback.message.edit_text(
        "📝 Какая форма? Затем выберите, кому писать — заполнившим или нет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons + _CANCEL_ROW),
    )


@router.callback_query(F.data.startswith("extff_form:"), Broadcast.filter_field)
async def extff_form(callback: types.CallbackQuery, state: FSMContext):
    try:
        form_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    form = await _find_form(form_id)
    if form is None:
        await callback.answer("Эта форма уже удалена — выберите другую.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Заполнил", callback_data=f"extff_pick:{form_id}:filled")],
        [InlineKeyboardButton(text="❌ Не заполнил", callback_data=f"extff_pick:{form_id}:not_filled")],
    ] + _CANCEL_ROW)
    await callback.answer()
    await callback.message.edit_text(f"📝 «{_title(form)}» — кому писать?", reply_markup=kb)


@router.callback_query(F.data.startswith("extff_pick:"), Broadcast.filter_field)
async def extff_pick(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    try:
        form_id = int(parts[1])
        mode = parts[2]
    except (IndexError, ValueError):
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    if mode not in _MODES:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    form = await _find_form(form_id)
    if form is None:
        await callback.answer("Эта форма уже удалена — выберите другую.", show_alert=True)
        return
    data = await state.get_data()
    filters = data.get("filters", [])
    filters.append({
        "field": "ext_form", "value": mode, "form_id": form_id,
        "label": f"{_title(form)}: {_MODES[mode]}",
    })
    await state.update_data(filters=filters)
    await callback.answer()

    from handlers.admin_broadcasts import _render_filter_menu  # ленивый шов

    await _render_filter_menu(callback.message, filters, edit=True)


@router.callback_query(F.data == "extff_cancel", Broadcast.filter_field)
async def extff_cancel(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer()

    from handlers.admin_broadcasts import _render_filter_menu  # ленивый шов

    await _render_filter_menu(callback.message, data.get("filters", []), edit=True)
