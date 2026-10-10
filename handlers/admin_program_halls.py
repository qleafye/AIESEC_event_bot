"""Форум-ночь п.4 (расписание форума в боте) — вынесенный хвост `handlers/admin_program.py`
(потолок размера модуля, `tests/test_module_size_convention_260816.py`, не архитектурная
граница): экран «🏛 Залы» (список/переименование/удаление) и «📋 Скопировать программу из
города…». Форма шва та же — своего `Router()` нет, `from handlers.admin import router`, каждый
декоратор в одну строку; импортирован ХВОСТОМ `handlers/admin_program.py`, поэтому здесь можно
безопасно, на уровне модуля, читать имена оттуда (`admin_program` к этому моменту уже полностью
определён — тот же приём, что `handlers/applications/admin_reject_cond.py` поверх `admin_reject_rules.py`).
Право — `settings`, тот же префиксный ключ `prog_*` (handlers/admin_caps.py)."""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import city_codes, city_label
from database.db import (
    count_program_sessions_for_hall,
    delete_program_hall,
    get_program_hall,
    list_program_days_for_city,
    list_program_halls,
    list_program_sessions_for_city_day,
)
from handlers.admin import router
from handlers.admin_program import (
    _CITY_FORBIDDEN_ALERT,
    _city_allowed,
    _sessions_word,
    _short,
    render_day_screen,
)
from handlers.states import ProgramHallName, ProgramSessionField
from keyboards.builders import get_cancel_kb
from services.program import copy_program_day, day_label, hall_conflict_warning


async def render_halls_screen(code: str) -> tuple[str, InlineKeyboardMarkup]:
    halls = await list_program_halls(code)
    label = await city_label(code)
    lines = [f"🏛 <b>Залы</b> — {html_module.escape(label)}"]
    buttons: list[list[InlineKeyboardButton]] = []
    if halls:
        lines.append("")
        for h in halls:
            cap = f" · вместимость {h['capacity']}" if h.get("capacity") else ""
            lines.append(f"• {html_module.escape(h['name'])}{cap}")
            buttons.append([
                InlineKeyboardButton(text=f"✏ {_short(h['name'], 30)}", callback_data=f"prog_hallrename:{h['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"prog_halldel:{h['id']}"),
            ])
    else:
        lines.append("")
        lines.append("Залов пока нет.")
    buttons.append([InlineKeyboardButton(text="➕ Новый зал", callback_data=f"prog_hallcreate:{code}")])
    buttons.append([InlineKeyboardButton(text="← К программе", callback_data=f"prog_city:{code}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_halls:"))
async def prog_halls_open(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await render_halls_screen(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_hallcreate:"))
async def prog_hallcreate_start(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.set_data({"ph_mode": "create", "ph_city": code})
    await state.set_state(ProgramHallName.value)
    await callback.message.answer("Название нового зала:", reply_markup=get_cancel_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("prog_hallrename:"))
async def prog_hallrename_start(callback: types.CallbackQuery, state: FSMContext):
    hall_id = int(callback.data.split(":", 1)[1])
    hall = await get_program_hall(hall_id)
    if hall is None:
        await callback.answer("Зал не найден.", show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, hall["city"]):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.set_data({"ph_mode": "rename", "ph_hall_id": hall_id})
    await state.set_state(ProgramHallName.value)
    await callback.message.answer(
        f"Новое имя зала «{html_module.escape(hall['name'])}»:", parse_mode="HTML", reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_halldel:"))
async def prog_halldel_confirm(callback: types.CallbackQuery):
    hall_id = int(callback.data.split(":", 1)[1])
    hall = await get_program_hall(hall_id)
    if hall is None:
        await callback.answer("Зал уже удалён.", show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, hall["city"]):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    count = await count_program_sessions_for_hall(hall_id)
    lines = [f"🗑 <b>Удалить зал «{html_module.escape(hall['name'])}» навсегда?</b>", ""]
    if count:
        lines.append(
            f"В нём сейчас {count} {_sessions_word(count)} — они останутся в программе, но "
            "потеряют привязку к залу."
        )
    else:
        lines.append("Сессий в этом зале нет.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"prog_halldelgo:{hall_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"prog_halls:{hall['city']}")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_halldelgo:"))
async def prog_halldel_go(callback: types.CallbackQuery):
    hall_id = int(callback.data.split(":", 1)[1])
    hall = await get_program_hall(hall_id)
    if hall is None:
        await callback.answer("Зал уже удалён.", show_alert=True)
        return
    code = hall["city"]
    await delete_program_hall(hall_id)
    text, kb = await render_halls_screen(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Зал удалён.")


# ── Скопировать программу дня из другого города ────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_copy:"))
async def prog_copy_pick_source(callback: types.CallbackQuery):
    to_code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, to_code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    buttons = []
    for code in city_codes():
        if code == to_code or not await _city_allowed(callback.from_user.id, code):
            continue
        buttons.append([InlineKeyboardButton(
            text=await city_label(code), callback_data=f"prog_copysrc:{to_code}:{code}",
        )])
    if not buttons:
        await callback.answer("Нет доступных городов, кроме этого.", show_alert=True)
        return
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"prog_city:{to_code}")])
    text = "📋 <b>Скопировать программу из города</b>\n\nИз какого города копировать?"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("prog_copysrc:"))
async def prog_copy_pick_day(callback: types.CallbackQuery):
    _, to_code, from_code = callback.data.split(":", 2)
    days = await list_program_days_for_city(from_code)
    if not days:
        await callback.answer("В этом городе программа ещё пуста — копировать нечего.", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton(text=f"📅 {day_label(d)}", callback_data=f"prog_copyday:{to_code}:{from_code}:{d}")]
        for d in days
    ]
    buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"prog_city:{to_code}")])
    from_label = await city_label(from_code)
    text = f"📋 <b>Скопировать программу из города «{html_module.escape(from_label)}»</b>\n\nКакой день скопировать?"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("prog_copyday:"))
async def prog_copy_confirm(callback: types.CallbackQuery):
    _, to_code, from_code, day = callback.data.split(":", 3)
    sessions = await list_program_sessions_for_city_day(from_code, day)
    from_label = await city_label(from_code)
    to_label = await city_label(to_code)
    lines = [
        f"📋 <b>Скопировать программу {day_label(day)}</b>", "",
        f"Из города «{html_module.escape(from_label)}» в «{html_module.escape(to_label)}»: "
        f"{len(sessions)} {_sessions_word(len(sessions))} и их залы.", "",
        f"Уже заведённая программа этого дня в «{html_module.escape(to_label)}» не удаляется — "
        "копия ДОБАВИТ сессии рядом.",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Скопировать", callback_data=f"prog_copygo:{to_code}:{from_code}:{day}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"prog_city:{to_code}")],
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_copygo:"))
async def prog_copy_go(callback: types.CallbackQuery):
    _, to_code, from_code, day = callback.data.split(":", 3)
    if not await _city_allowed(callback.from_user.id, to_code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    stats = await copy_program_day(from_code, to_code, day)
    text, kb = await render_day_screen(to_code, day)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(
        f"Скопировано: {stats['sessions_created']} {_sessions_word(stats['sessions_created'])}, "
        f"{stats['halls_created']} новых залов.",
        show_alert=True,
    )


# ── Мастер новой сессии: отмена кнопкой и «ввести время заново» при конфликте зала ──────────
# Живёт здесь, а не в admin_program.py (тот на потолке размера); admin_program зовёт лениво.

def wizard_cancel_kb() -> InlineKeyboardMarkup:
    """Инлайн-«Отмена» на шагах «время»/«название»: reply-клавиатура «Отмена» в Telegram Web
    свёрнута, менеджер её не видит. Набранное «Отмена» по-прежнему работает."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить создание", callback_data="prog_wcancel")],
    ])


def wizard_conflict_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё равно", callback_data="prog_wconfirm_yes")],
        [InlineKeyboardButton(text="🔁 Выбрать другой зал", callback_data="prog_wconfirm_no")],
        [InlineKeyboardButton(text="✏️ Ввести время заново", callback_data="prog_wretime")],
        [InlineKeyboardButton(text="❌ Отменить создание", callback_data="prog_wcancel")],
    ])


@router.callback_query(F.data == "prog_wretime")
async def prog_wretime(callback: types.CallbackQuery, state: FSMContext):
    from handlers.admin_program import _TIME_HINT
    data = await state.get_data()
    if data.get("pmode") != "new" or not data.get("pw_city"):
        await callback.answer("Создание сессии уже закрыто — начните заново.", show_alert=True)
        return
    await state.set_state(ProgramSessionField.time)
    await callback.message.edit_text(
        f"Пришлите время заново. {_TIME_HINT}\nНазвание и зал сохранятся.", reply_markup=wizard_cancel_kb(),
    )
    await callback.answer()


async def wizard_after_retime(message: types.Message, state: FSMContext) -> None:
    """Новое время после конфликта: тот же зал проверяется заново, дальше — спикер."""
    from handlers.admin_program import _wizard_ask_speaker
    data = await state.get_data()
    warning = await hall_conflict_warning(
        data.get("pw_city"), data.get("pw_day"), data.get("pw_hall_id"), data.get("pw_start"), data.get("pw_end"),
    )
    if warning:
        await message.answer(f"⚠️ {warning}\n\nСохранить всё равно?", reply_markup=wizard_conflict_kb())
        return
    await _wizard_ask_speaker(message, state)
