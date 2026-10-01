"""Форум-ночь п.4 (расписание форума в боте, FORUM-CHECKIN.md D-18..D-20/D-24) — раздел
«🗓 Программа форума»: залы и сессии, per-city, менеджер заводит их сам, без разработчика и без
импорта из таблицы (решение владельца).

Форма шва — эталон `handlers/admin_reject_rules.py`/`handlers/admin_checkin.py`: своего
`Router()` нет, хендлеры декорируют ОБЩИЙ `handlers.admin.router`, каждый декоратор — в ОДНУ
строку (инвариант cap-теста `tests/test_roles_phase8.py`). Импортирован ХВОСТОМ
`handlers/admin_sections.py` (после `admin_reject_reports`).

Право — `settings` (тот же класс экрана, что «🚫 Правила автоотказа»/«🧮 Правила балла»:
конфигурирование контента события, не действие над конкретной заявкой). Город экрана — из
шапки админки (`handlers.admin_core._admin_city_scope`), тем же трёхветочным приёмом, что
`handlers/admin_checkin.py::_counter_line` (закреплённый город / модуль городов выключен /
«Все города» — список городов на выбор): расписание форума не бывает «общим на все города»,
поэтому третья ветка ведёт на ЭКРАН ВЫБОРА конкретного города, а не схлопывается в
нескопированный запрос, как у чек-ина.

Бизнес-правила (разбор времени/дня, предупреждение о занятости зала, копирование между
городами) — в `services/program.py`; здесь только UI и права. Конфликт зала (CLAUDE.md:
предупреждение словами, но разрешить после подтверждения) — везде РЕАЛЬНЫЙ экран с двумя
кнопками, не всплывающий алерт с автопродолжением: менеджер обязан нажать «Всё равно», а не
просто увидеть текст."""
import html as html_module

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import (
    city_codes,
    city_label,
    cities_module_on,
    default_city_code,
    enabled_cities,
)
from database.db import (
    count_checkins_by_point,
    create_program_hall,
    create_program_session,
    delete_program_session,
    get_program_hall,
    get_program_session,
    list_program_days_for_city,
    list_program_halls,
    list_program_sessions_for_city_day,
    rename_program_hall,
    update_program_session,
)
from handlers.admin import router
from handlers.admin_core import _admin_city_scope
from handlers.states import ProgramDayCustom, ProgramHallName, ProgramSessionField
from keyboards.builders import get_cancel_kb, get_skip_kb
from services.program import (
    day_label,
    format_time_range,
    hall_conflict_warning,
    own_forum_date,
    parse_day_input,
    parse_time_range,
    point_for_session,
    sessions_for_city_day,
    suggested_days_for_city,
)
# Форум-ночь п.9 (идея №15, D-24): «⭐ Отзыв о сессии одним тапом» — джоба переставляется после
# ЛЮБОГО создания/правки сессии, снимается после удаления; статистика — в карточке сессии.
# Планирование/агрегаты живут в services/session_feedback.py, здесь только точки вызова.
from services import session_feedback

_CITY_FORBIDDEN_ALERT = "Этот город правит другой менеджер."
_CANCEL_WORDS = {"Отмена", "/cancel"}
_SKIP_WORDS = {"Пропустить", "-"}


async def _city_allowed(admin_id: int, code: str | None) -> bool:
    """TOCTOU-перепроверка права на город (тот же приём, что `admin_checkin._city_allowed`) —
    инлайн-клавиатуры в чате не истекают, кнопка чужого города могла остаться от смены
    привязки менеджера между рендером и тапом."""
    if not code:
        return True
    import settings_ops
    return code in await settings_ops.per_city_visible_codes(admin_id)


def _short(text: str, limit: int) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(0, limit - 1)].rstrip() + "…"


def _sessions_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "сессий"
    last = n % 10
    if last == 1:
        return "сессия"
    if 2 <= last <= 4:
        return "сессии"
    return "сессий"


async def _resolve_city_for_screen(admin_id: int) -> str | None:
    """Город экрана «🗓 Программа форума» для этого менеджера: закреплённый город — сразу его;
    модуль городов выключен — единственный (дефолтный) город события, тоже сразу; иначе (модуль
    включён, менеджер не закреплён/выбрал «Все города») — `None`, экран выбора города."""
    own_scope = await _admin_city_scope(admin_id)
    if own_scope is not None:
        return own_scope[0]
    if not await cities_module_on():
        return default_city_code()
    return None


# ── Экран выбора города (когда «Все города» / нет закрепления) ────────────────────────────────

async def render_city_picker_screen() -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"prog_city:{c['code']}")]
        for c in await enabled_cities()
    ]
    from handlers.admin_sections import back_button
    buttons.append([back_button("admin_program")])
    text = "🗓 <b>Программа форума</b>\n\nВыберите город."
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_program")
async def admin_program_entry(callback: types.CallbackQuery):
    code = await _resolve_city_for_screen(callback.from_user.id)
    if code is None:
        text, kb = await render_city_picker_screen()
    else:
        text, kb = await render_city_program_screen(callback.from_user.id, code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_city:"))
async def prog_city_open(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if code not in city_codes():
        await callback.answer("Такого города нет — обновите экран.", show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await render_city_program_screen(callback.from_user.id, code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Экран программы города: дни ────────────────────────────────────────────────────────────────

async def render_city_program_screen(admin_id: int, code: str) -> tuple[str, InlineKeyboardMarkup]:
    label = await city_label(code)
    existing_days = await list_program_days_for_city(code)
    combined = sorted(set(existing_days) | set(await suggested_days_for_city(code)))  # дни окна форума

    lines = [f"🗓 <b>Программа форума</b> — {html_module.escape(label)}"]
    buttons: list[list[InlineKeyboardButton]] = []
    if combined:
        lines.append("")
        for day in combined:
            count = len(await list_program_sessions_for_city_day(code, day)) if day in existing_days else 0
            count_text = f"{count} {_sessions_word(count)}" if count else "пусто"
            buttons.append([InlineKeyboardButton(
                text=f"📅 {day_label(day)} — {count_text}", callback_data=f"prog_day:{code}:{day}",
            )])
    else:
        lines.append("")
        lines.append(
            "Дней пока нет — заведите «🗓 Дата начала форума» в разделе «🎪 Событие» (появятся "
            "подсказки дней) или добавьте день кнопкой ниже."
        )

    buttons.append([InlineKeyboardButton(text="📅 Другой день", callback_data=f"prog_daynew:{code}")])
    buttons.append([InlineKeyboardButton(text="🏛 Залы", callback_data=f"prog_halls:{code}")])
    # Ревью 24.09: экран настроек отзыва — handlers/session_feedback.py (потолок этого файла).
    buttons.append([InlineKeyboardButton(text="⭐ Отзывы о сессиях", callback_data=f"prog_fbset:{code}")])
    # D-29: таблица/фото в Mini App — общий рендер handlers/admin_program_view.py (потолок
    # этого файла, кнопка нужна и хабу «🎪 Форум: функции»).
    from handlers.admin_program_view import program_rows  # + фото программы города
    view_status, view_rows = await program_rows(code, "program")
    lines.append(f"\n{view_status}")
    buttons += view_rows

    other_cities = [c for c in city_codes() if c != code and await _city_allowed(admin_id, c)]
    if other_cities:
        buttons.append([InlineKeyboardButton(
            text="📋 Скопировать программу из города…", callback_data=f"prog_copy:{code}",
        )])

    from handlers.admin_sections import back_button
    buttons.append([back_button("admin_program")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_daynew:"))
async def prog_daynew_start(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.set_data({"pd_city": code})
    await state.set_state(ProgramDayCustom.value)
    await callback.message.answer(
        "Какой день добавить? Формат «ДД.ММ» или «ДД.ММ.ГГГГ», например «31.10».",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(ProgramDayCustom), F.text.in_(_CANCEL_WORDS))
async def prog_daynew_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(ProgramDayCustom.value)
async def prog_daynew_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("pd_city")
    if not code:
        await state.clear()
        await message.answer("Что-то пошло не так — откройте раздел заново.", reply_markup=ReplyKeyboardRemove())
        return
    forum_dt = await own_forum_date(code)
    reference = forum_dt.date() if forum_dt else None
    day = parse_day_input((message.text or "").strip(), reference=reference)
    if day is None:
        await message.answer(
            "Не понял дату — пришлите в формате «ДД.ММ» или «ДД.ММ.ГГГГ», например «31.10»."
        )
        return
    await state.clear()
    await message.answer("✅ Открываю день.", reply_markup=ReplyKeyboardRemove())
    text, kb = await render_day_screen(code, day)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Экран дня: список сессий ─────────────────────────────────────────────────────────────────

async def render_day_screen(code: str, day: str) -> tuple[str, InlineKeyboardMarkup]:
    label = await city_label(code)
    sessions = await sessions_for_city_day(code, day)
    lines = [f"🗓 <b>{day_label(day)}</b> — {html_module.escape(label)}", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if sessions:
        for s in sessions:
            hall_part = f" · {s['hall_name']}" if s.get("hall_name") else ""
            time_part = format_time_range(s["start_time"], s["end_time"])
            lines.append(f"{time_part} — {html_module.escape(s['title'])}{html_module.escape(hall_part)}")
            button_label = _short(f"{time_part} {s['title']}", 55)
            buttons.append([InlineKeyboardButton(text=button_label, callback_data=f"prog_v:{s['id']}")])
    else:
        lines.append("Сессий пока нет.")
    buttons.append([InlineKeyboardButton(text="➕ Сессия", callback_data=f"prog_new:{code}:{day}")])
    if sessions:
        # Форум-ночь п.9 (идея №15, D-24): «📊 Оценки сессий» — только когда в дне уже есть
        # сессии, что оценивать (пустой день -> кнопка не нужна, тот же приём, что «➕ Сессия»
        # выше показывается всегда, а не наоборот — но здесь показывать нечего вовсе).
        buttons.append([InlineKeyboardButton(text="📊 Оценки сессий", callback_data=f"prog_fbday:{code}:{day}")])
    buttons.append([InlineKeyboardButton(text="← К программе", callback_data=f"prog_city:{code}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_day:"))
async def prog_day_open(callback: types.CallbackQuery):
    _, code, day = callback.data.split(":", 2)
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await render_day_screen(code, day)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── Мастер новой сессии: время -> название -> зал -> спикер -> описание ────────────────────────

_TIME_HINT = "Время начала и конца, например «10:00-11:30» (конец должен быть позже начала)."
_TIME_ERROR = (
    "Не понял время — пришлите в формате «ЧЧ:ММ-ЧЧ:ММ», например «10:00-11:30» "
    "(конец должен быть позже начала)."
)


@router.callback_query(F.data.startswith("prog_new:"))
async def prog_new_start(callback: types.CallbackQuery, state: FSMContext):
    _, code, day = callback.data.split(":", 2)
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    await state.set_data({"pmode": "new", "pw_city": code, "pw_day": day})
    await state.set_state(ProgramSessionField.time)
    from handlers.admin_program_halls import wizard_cancel_kb  # инлайн-отмена: reply-клавиатуру не видно
    await callback.message.answer(
        f"➕ <b>Новая сессия</b> — {day_label(day)}\n\n{_TIME_HINT}",
        parse_mode="HTML", reply_markup=wizard_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(ProgramSessionField), F.text.in_(_CANCEL_WORDS))
async def prog_field_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(ProgramSessionField.time)
async def prog_time_step(message: types.Message, state: FSMContext):
    parsed = parse_time_range((message.text or "").strip())
    if parsed is None:
        await message.answer(_TIME_ERROR)
        return
    start, end = parsed
    data = await state.get_data()
    if data.get("pmode") == "new":
        from handlers.admin_program_halls import wizard_after_retime, wizard_cancel_kb
        await state.update_data(pw_start=start, pw_end=end)
        if data.get("pw_title"):  # время заново после конфликта зала — назад к проверке зала
            await wizard_after_retime(message, state)
            return
        await state.set_state(ProgramSessionField.title)
        await message.answer("Название сессии:", reply_markup=wizard_cancel_kb())
        return

    session_id = data.get("pf_session_id")
    session = await get_program_session(session_id) if session_id is not None else None
    if session is None:
        await state.clear()
        await message.answer("Сессия больше недоступна — откройте программу заново.", reply_markup=ReplyKeyboardRemove())
        return
    warning = await hall_conflict_warning(
        session["city"], session["day"], session.get("hall_id"), start, end, exclude_id=session_id,
    )
    if warning:
        await state.update_data(pf_pending_start=start, pf_pending_end=end)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё равно сохранить", callback_data=f"prog_ftyes:{session_id}")],
            [InlineKeyboardButton(text="✏️ Ввести время заново", callback_data=f"prog_ftno:{session_id}")],
        ])
        await message.answer(f"⚠️ {warning}\n\nСохранить всё равно?", reply_markup=ReplyKeyboardRemove())
        await message.answer("Выберите:", reply_markup=kb)
        return
    await state.clear()
    await update_program_session(session_id, start_time=start, end_time=end)
    await _send_card(message, session_id, intro="✅ Время обновлено.")


@router.callback_query(F.data.startswith("prog_ftyes:"))
async def prog_ftyes(callback: types.CallbackQuery, state: FSMContext):
    session_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    start, end = data.get("pf_pending_start"), data.get("pf_pending_end")
    await state.clear()
    if start is None or end is None:
        await callback.answer("Действие устарело — откройте карточку заново.", show_alert=True)
        return
    await update_program_session(session_id, start_time=start, end_time=end)
    await _edit_to_card(callback, session_id)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_ftno:"))
async def prog_ftno(callback: types.CallbackQuery, state: FSMContext):
    session_id = int(callback.data.split(":", 1)[1])
    await state.update_data(pmode="edit", pf_session_id=session_id)
    await state.set_state(ProgramSessionField.time)
    await callback.message.answer(f"Пришлите время заново. {_TIME_HINT}", reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(ProgramSessionField.title)
async def prog_title_step(message: types.Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым — пришлите текст.")
        return
    data = await state.get_data()
    if data.get("pmode") == "new":
        await state.update_data(pw_title=title, pw_hall_picked=False)
        await state.set_state(None)
        text, kb = await _hall_pick_screen(data.get("pw_city"), "w")
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    session_id = data.get("pf_session_id")
    if session_id is None:
        await state.clear()
        await message.answer("Сессия больше недоступна.", reply_markup=ReplyKeyboardRemove())
        return
    await state.clear()
    await update_program_session(session_id, title=title)
    await _send_card(message, session_id, intro="✅ Название обновлено.")


# ── Выбор зала (мастер "w" / правка карточки "f<id>") ───────────────────────────────────────────

async def _hall_pick_screen(city: str, ctx: str, current_hall_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    halls = await list_program_halls(city)
    lines = ["🏛 <b>Зал</b>", "", "Выберите зал для сессии:"]
    buttons: list[list[InlineKeyboardButton]] = []
    for h in halls:
        mark = "✅ " if h["id"] == current_hall_id else ""
        buttons.append([InlineKeyboardButton(text=f"{mark}{h['name']}", callback_data=f"prog_hp:{ctx}:{h['id']}")])
    buttons.append([InlineKeyboardButton(text="🚫 Без зала", callback_data=f"prog_hp:{ctx}:none")])
    buttons.append([InlineKeyboardButton(text="➕ Новый зал", callback_data=f"prog_hpnew:{ctx}")])
    if ctx == "w":
        buttons.append([InlineKeyboardButton(text="❌ Отменить создание", callback_data="prog_wcancel")])
    else:
        buttons.append([InlineKeyboardButton(text="← Отмена", callback_data=f"prog_v:{ctx[1:]}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "prog_wcancel")
async def prog_wcancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Создание сессии отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("prog_hallscreen:"))
async def prog_hallscreen_open(callback: types.CallbackQuery, state: FSMContext):
    ctx = callback.data.split(":", 1)[1]
    if ctx == "w":
        data = await state.get_data()
        city, current = data.get("pw_city"), data.get("pw_hall_id")
        if not city:
            await callback.answer("Начните создание сессии заново.", show_alert=True)
            return
        await state.update_data(pw_hall_picked=False)
    else:
        session = await get_program_session(int(ctx[1:]))
        if session is None:
            await callback.answer("Сессия не найдена.", show_alert=True)
            return
        city, current = session["city"], session.get("hall_id")
    text, kb = await _hall_pick_screen(city, ctx, current)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_hp:"))
async def prog_hp_pick(callback: types.CallbackQuery, state: FSMContext):
    _, ctx, token = callback.data.split(":", 2)
    hall_id = None if token == "none" else int(token)

    if ctx == "w":
        data = await state.get_data()
        city, day = data.get("pw_city"), data.get("pw_day")
        start, end = data.get("pw_start"), data.get("pw_end")
        if not city or not day or not start:
            await callback.answer("Сессия не найдена — начните заново.", show_alert=True)
            return
        if data.get("pw_hall_picked"):  # двойной тап по залу — второй вопрос «Спикер» не задаём
            return await callback.answer()
        await state.update_data(pw_hall_id=hall_id, pw_hall_picked=True)  # до первого await к БД
        warning = await hall_conflict_warning(city, day, hall_id, start, end)
        if warning:
            from handlers.admin_program_halls import wizard_conflict_kb
            kb = wizard_conflict_kb()
            await callback.message.edit_text(f"⚠️ {warning}\n\nСохранить всё равно?", reply_markup=kb)
            await callback.answer()
            return
        try:  # кнопки зала больше не нужны — второй тап по ним ничего бы не дал
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _wizard_ask_speaker(callback.message, state)
        await callback.answer()
        return

    session_id = int(ctx[1:])
    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Сессия не найдена.", show_alert=True)
        return
    warning = await hall_conflict_warning(
        session["city"], session["day"], hall_id, session["start_time"], session["end_time"],
        exclude_id=session_id,
    )
    if warning:
        token_enc = "none" if hall_id is None else str(hall_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё равно", callback_data=f"prog_fhyes:{session_id}:{token_enc}")],
            [InlineKeyboardButton(text="🔁 Выбрать другой зал", callback_data=f"prog_hallscreen:f{session_id}")],
        ])
        await callback.message.edit_text(f"⚠️ {warning}\n\nСохранить всё равно?", reply_markup=kb)
        await callback.answer()
        return
    await update_program_session(session_id, hall_id=hall_id)
    await _edit_to_card(callback, session_id)
    await callback.answer()


@router.callback_query(F.data == "prog_wconfirm_yes")
async def prog_wconfirm_yes(callback: types.CallbackQuery, state: FSMContext):
    await _wizard_ask_speaker(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "prog_wconfirm_no")
async def prog_wconfirm_no(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(pw_hall_picked=False)  # снова выбор зала
    text, kb = await _hall_pick_screen(data.get("pw_city"), "w", data.get("pw_hall_id"))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_fhyes:"))
async def prog_fhyes(callback: types.CallbackQuery):
    _, session_id_s, token = callback.data.split(":", 2)
    session_id = int(session_id_s)
    hall_id = None if token == "none" else int(token)
    await update_program_session(session_id, hall_id=hall_id)
    await _edit_to_card(callback, session_id)
    await callback.answer()


# ── Новый зал "на лету" (из экрана выбора зала мастера/карточки) ───────────────────────────────

@router.callback_query(F.data.startswith("prog_hpnew:"))
async def prog_hpnew_start(callback: types.CallbackQuery, state: FSMContext):
    ctx = callback.data.split(":", 1)[1]
    if ctx == "w":
        data = await state.get_data()
        city = data.get("pw_city")
        if not city:
            await callback.answer("Начните создание сессии заново.", show_alert=True)
            return
        await state.update_data(ph_mode="wizard_hall", ph_city=city)
    else:
        session_id = int(ctx[1:])
        session = await get_program_session(session_id)
        if session is None:
            await callback.answer("Сессия не найдена.", show_alert=True)
            return
        await state.update_data(ph_mode="field_hall", ph_city=session["city"], ph_session_id=session_id)
    await state.set_state(ProgramHallName.value)
    await callback.message.answer("Название нового зала:", reply_markup=get_cancel_kb())
    await callback.answer()


async def _wizard_ask_speaker(message: types.Message, state: FSMContext) -> None:
    await state.set_state(ProgramSessionField.speaker)
    await message.answer(
        "Спикер (если ведущий не назначен — «Пропустить»):", reply_markup=get_skip_kb(),
    )


async def _wizard_ask_description(message: types.Message, state: FSMContext) -> None:
    await state.set_state(ProgramSessionField.description)
    await message.answer(
        "Описание сессии (если не нужно — «Пропустить»):", reply_markup=get_skip_kb(),
    )


@router.message(ProgramSessionField.speaker)
async def prog_speaker_step(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    value = None if not raw or raw in _SKIP_WORDS else raw
    data = await state.get_data()
    if data.get("pmode") == "new":
        await state.update_data(pw_speaker=value)
        await _wizard_ask_description(message, state)
        return

    session_id = data.get("pf_session_id")
    if session_id is None:
        await state.clear()
        await message.answer("Сессия больше недоступна.", reply_markup=ReplyKeyboardRemove())
        return
    await state.clear()
    await update_program_session(session_id, speaker=value)
    await _send_card(message, session_id, intro="✅ Спикер обновлён.")


@router.message(ProgramSessionField.description)
async def prog_description_step(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    value = None if not raw or raw in _SKIP_WORDS else raw
    if value:
        # Ловушка «Enter = отправить» на мобильных (CLAUDE.md) — «;» разделяет строки.
        value = "\n".join(part.strip() for part in value.split(";") if part.strip())
    data = await state.get_data()

    if data.get("pmode") == "new":
        session_id = await create_program_session(
            data["pw_city"], data["pw_day"], data["pw_start"], data["pw_end"], data["pw_title"],
            speaker=data.get("pw_speaker"), hall_id=data.get("pw_hall_id"), description=value,
        )
        await session_feedback.schedule_for_session(session_id)  # Форум-ночь п.9
        await state.clear()
        await message.answer("✅ Сессия добавлена в программу.", reply_markup=ReplyKeyboardRemove())
        screen = await render_session_card(session_id)
        if screen is not None:
            text, kb = screen
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    session_id = data.get("pf_session_id")
    if session_id is None:
        await state.clear()
        await message.answer("Сессия больше недоступна.", reply_markup=ReplyKeyboardRemove())
        return
    await state.clear()
    await update_program_session(session_id, description=value)
    await _send_card(message, session_id, intro="✅ Описание обновлено.")


# ── Имя зала: создание (мастер/карточка/отдельно) и переименование ────────────────────────────

@router.message(StateFilter(ProgramHallName), F.text.in_(_CANCEL_WORDS))
async def prog_hallname_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(ProgramHallName.value)
async def prog_hallname_step(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым — пришлите текст.")
        return
    data = await state.get_data()
    mode = data.get("ph_mode")

    # Ленивый импорт: `render_halls_screen` живёт в `admin_program_halls.py` (хвостовой шов,
    # потолок размера модуля) — тот же приём, что `back_button` из `admin_sections.py` везде
    # по проекту (на момент вызова этой функции хвостовой модуль уже полностью импортирован).
    if mode == "create":
        from handlers.admin_program_halls import render_halls_screen
        code = data.get("ph_city")
        await state.clear()
        await create_program_hall(code, name)
        await message.answer("✅ Зал добавлен.", reply_markup=ReplyKeyboardRemove())
        text, kb = await render_halls_screen(code)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    if mode == "rename":
        from handlers.admin_program_halls import render_halls_screen
        hall_id = data.get("ph_hall_id")
        hall = await get_program_hall(hall_id)
        await state.clear()
        if hall is None:
            await message.answer("Зал больше не существует.", reply_markup=ReplyKeyboardRemove())
            return
        await rename_program_hall(hall_id, name)
        await message.answer("✅ Зал переименован.", reply_markup=ReplyKeyboardRemove())
        text, kb = await render_halls_screen(hall["city"])
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    if mode == "wizard_hall":
        code = data.get("ph_city")
        new_id = await create_program_hall(code, name)
        await state.update_data(pw_hall_id=new_id)
        await message.answer("✅ Зал добавлен.", reply_markup=ReplyKeyboardRemove())
        pw = await state.get_data()
        warning = await hall_conflict_warning(pw.get("pw_city"), pw.get("pw_day"), new_id, pw.get("pw_start"), pw.get("pw_end"))
        if warning:
            from handlers.admin_program_halls import wizard_conflict_kb
            kb = wizard_conflict_kb()
            await message.answer(f"⚠️ {warning}\n\nСохранить всё равно?", reply_markup=kb)
            return
        await _wizard_ask_speaker(message, state)
        return

    if mode == "field_hall":
        session_id = data.get("ph_session_id")
        code = data.get("ph_city")
        new_id = await create_program_hall(code, name)
        session = await get_program_session(session_id)
        await message.answer("✅ Зал добавлен.", reply_markup=ReplyKeyboardRemove())
        if session is None:
            await state.clear()
            await message.answer("Сессия больше недоступна.")
            return
        warning = await hall_conflict_warning(
            session["city"], session["day"], new_id, session["start_time"], session["end_time"],
            exclude_id=session_id,
        )
        if warning:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Всё равно", callback_data=f"prog_fhyes:{session_id}:{new_id}")],
                [InlineKeyboardButton(text="🔁 Выбрать другой зал", callback_data=f"prog_hallscreen:f{session_id}")],
            ])
            await state.clear()
            await message.answer(f"⚠️ {warning}\n\nСохранить всё равно?", reply_markup=kb)
            return
        await state.clear()
        await update_program_session(session_id, hall_id=new_id)
        await _send_card(message, session_id, intro="✅ Зал обновлён.")
        return

    await state.clear()
    await message.answer("Что-то пошло не так — откройте раздел заново.", reply_markup=ReplyKeyboardRemove())


# ── Карточка сессии ──────────────────────────────────────────────────────────────────────────

async def render_session_card(session_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    session = await get_program_session(session_id)
    if session is None:
        return None
    hall_name = None
    hall_capacity = None
    if session.get("hall_id") is not None:
        hall = await get_program_hall(session["hall_id"])
        if hall:
            hall_name = hall["name"]
            hall_capacity = hall.get("capacity")

    arrived = await count_checkins_by_point(point_for_session(session["id"]))
    arrived_line = f"Отмечено: {arrived} из {hall_capacity}" if hall_capacity else f"Отмечено: {arrived}"
    # Форум-ночь п.9 (идея №15, D-24): «⭐ 4.6 (38 оценок) · 12 комментариев» — статистика
    # оценок сессии, сразу после строки посещаемости, тот же экран.
    fb_stats = await session_feedback.session_feedback_stats(session["id"])

    lines = [
        f"🗓 <b>{html_module.escape(session['title'])}</b>", "",
        f"📅 {day_label(session['day'])}",
        f"⏰ {format_time_range(session['start_time'], session['end_time'])}",
        f"🏛 Зал: {html_module.escape(hall_name) if hall_name else 'не указан'}",
        f"🎤 Спикер: {html_module.escape(session['speaker']) if session.get('speaker') else 'не указан'}",
        f"✅ {arrived_line}",
        session_feedback.stats_line(fb_stats),
    ]
    if session.get("description"):
        lines.append("")
        lines.append(html_module.escape(session["description"]))

    sid = session["id"]
    buttons = [
        [InlineKeyboardButton(text="✏ Время", callback_data=f"prog_field:{sid}:time")],
        [InlineKeyboardButton(text="✏ Название", callback_data=f"prog_field:{sid}:title")],
        [InlineKeyboardButton(text="🏛 Зал", callback_data=f"prog_hallscreen:f{sid}")],
        [InlineKeyboardButton(text="🎤 Спикер", callback_data=f"prog_field:{sid}:speaker")],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"prog_field:{sid}:description")],
    ]
    if fb_stats.get("comment_count"):
        buttons.append([InlineKeyboardButton(text="💬 Комментарии", callback_data=f"prog_fbc:{sid}:0")])
    buttons += [
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"prog_d:{sid}")],
        [InlineKeyboardButton(text="← К дню", callback_data=f"prog_day:{session['city']}:{session['day']}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_card(message: types.Message, session_id: int, *, intro: str | None = None) -> None:
    # Форум-ночь п.9: ОБА пути карточки после правки (эта функция и `_edit_to_card` ниже) —
    # единственные вызывающие места после `update_program_session`/`create_program_session`
    # (см. докстринг `services.session_feedback.schedule_for_session`) — переставляет джобу
    # отзыва на новый момент, идемпотентно.
    await session_feedback.schedule_for_session(session_id)
    screen = await render_session_card(session_id)
    if screen is None:
        await message.answer("Сессия больше недоступна.", reply_markup=ReplyKeyboardRemove())
        return
    if intro:
        await message.answer(intro, reply_markup=ReplyKeyboardRemove())
    text, kb = screen
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _edit_to_card(callback: types.CallbackQuery, session_id: int) -> None:
    await session_feedback.schedule_for_session(session_id)
    screen = await render_session_card(session_id)
    if screen is None:
        await callback.message.edit_text("Сессия больше недоступна.")
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("prog_v:"))
async def prog_v_open(callback: types.CallbackQuery):
    session_id = int(callback.data.split(":", 1)[1])
    screen = await render_session_card(session_id)
    if screen is None:
        await callback.answer("Сессия больше недоступна.", show_alert=True)
        return
    text, kb = screen
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


_FIELD_STATES = {
    "time": ProgramSessionField.time,
    "title": ProgramSessionField.title,
    "speaker": ProgramSessionField.speaker,
    "description": ProgramSessionField.description,
}
_FIELD_PROMPTS = {
    "time": (f"Новое время. {_TIME_HINT}", "cancel"),
    "title": ("Новое название:", "cancel"),
    "speaker": ("Новый спикер (если убрать — «Пропустить»):", "skip"),
    "description": ("Новое описание (если убрать — «Пропустить»):", "skip"),
}


@router.callback_query(F.data.startswith("prog_field:"))
async def prog_field_start(callback: types.CallbackQuery, state: FSMContext):
    _, session_id_s, field = callback.data.split(":", 2)
    if field not in _FIELD_STATES:
        await callback.answer("Неизвестное поле — обновите экран.", show_alert=True)
        return
    session_id = int(session_id_s)
    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Сессия больше недоступна.", show_alert=True)
        return
    await state.set_data({"pmode": "edit", "pf_session_id": session_id})
    await state.set_state(_FIELD_STATES[field])
    prompt, kind = _FIELD_PROMPTS[field]
    await callback.message.answer(prompt, reply_markup=get_skip_kb() if kind == "skip" else get_cancel_kb())
    await callback.answer()


# ── Удаление сессии, с подтверждением ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_d:"))
async def prog_delete_confirm(callback: types.CallbackQuery):
    session_id = int(callback.data.split(":", 1)[1])
    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Сессия уже удалена.", show_alert=True)
        return
    time_part = format_time_range(session["start_time"], session["end_time"])
    text = (
        f"🗑 <b>Удалить сессию навсегда?</b>\n\n"
        f"«{html_module.escape(session['title'])}» {time_part} пропадёт из программы."
    )
    # CLAUDE.md: подтверждение называет, что пропадёт. Отметки/оценки остаются в БД без сессии
    # и выпадают из её карточки и отчёта; приглашение оценить снимается (prog_delete_go).
    arrived = await count_checkins_by_point(point_for_session(session_id))
    rated = (await session_feedback.session_feedback_stats(session_id)).get("rating_count", 0)
    if arrived or rated:
        text += f"\nПропадут из отчёта: отметок на сессии — {arrived}, оценок — {rated}."
    if await session_feedback.is_enabled_for_city(session["city"]):
        text += "\nДелегатам не придёт приглашение оценить эту сессию."
    text += ("\n\nНужно поменять время или название — нажмите «← Отмена» и «✏ Время»/«✏ Название» "
             "в карточке: отметки сохранятся.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"prog_dgo:{session_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"prog_v:{session_id}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_dgo:"))
async def prog_delete_go(callback: types.CallbackQuery):
    session_id = int(callback.data.split(":", 1)[1])
    session = await get_program_session(session_id)
    if session is None:
        await callback.answer("Сессия уже удалена.", show_alert=True)
        return
    city, day = session["city"], session["day"]
    await delete_program_session(session_id)
    session_feedback.cancel_for_session(session_id)  # Форум-ночь п.9 — не отправлять отзыв удалённой сессии
    text, kb = await render_day_screen(city, day)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Сессия удалена.")


# Форум-ночь п.4: шов «🏛 Залы» + «📋 Скопировать программу из города…» — импорт ХВОСТОМ (потолок
# размера модуля, tests/test_module_size_convention_260816.py), не архитектурная граница; тот же
# приём, что admin_reject_rules.py -> admin_reject_cond.py.
from handlers import admin_program_halls  # noqa: E402,F401

