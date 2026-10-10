"""Форум-ночь п.4 (расписание форума в боте, FORUM-CHECKIN.md D-18..D-20/D-24) — интерактивная
программа: день (если их несколько — выбор), сессии по времени, параллельные сгруппированы, в
день форума помечены «🔴 Идёт сейчас»/«⏭ Следующая».

D-29 (владелец, 24.09): своей кнопки меню больше нет — это ЗАПАСНОЙ вид объединённой кнопки
«📅 Программа форума» (`handlers/user_actions.py::show_program`), когда фото не загружено,
но в боте заведена хотя бы одна сессия (`send_program_schedule_text` вызывается оттуда
напрямую, `F.text`-фильтра на модуль больше нет). Данные ведёт менеджер в
`handlers/forum/admin_program.py`, здесь только чтение.

Форма шва — та же, что у соседних делегатских экранов (FAQ/чек-ин): своего `Router()` нет,
`from handlers.user_actions import router`; импортирован ХВОСТОМ `handlers/user_actions.py`
(нужен для callback-хендлеров дня ниже, message-хендлера теперь нет). Тексты — через
`reg_i18n.tr_text` (тот же перевод, что остальные экраны делегатского чата), литералы
зарегистрированы в `services/i18n_sources.py::code_literals()` (сторож
`tests/test_i18n_literal_corpus_guard_260906.py`, SCANNED_FILES дополнен этим модулем)."""
import html

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import default_city_code
from database.db import list_program_days_for_city
from handlers.i18n import reg_i18n
from handlers.user_actions import _delegate_city, router
from services.program import day_label, format_time_range, group_parallel, sessions_for_city_day
from services.timeutil import city_offset_hours, msk_now, shift_hours


async def _resolve_delegate_city(telegram_id: int) -> str:
    """Расписанию «нет города» не бывает (в отличие от прочих делегатских экранов) — модуль
    городов выключен -> единственный (дефолтный) город события, тот же приём, что у
    `handlers.forum.admin_program._resolve_city_for_screen`/`keyboards.builders.get_main_menu_kb`."""
    code = await _delegate_city(telegram_id)
    return code or default_city_code()


def _time_marker(group: list[dict], now_hhmm: str, next_start: str | None) -> str | None:
    start_min = min(s["start_time"] for s in group)
    end_max = max(s["end_time"] for s in group)
    if start_min <= now_hhmm < end_max:
        return "now"
    if next_start is not None and start_min == next_start:
        return "next"
    return None


def _next_start(sessions: list[dict], now_hhmm: str) -> str | None:
    upcoming = [s["start_time"] for s in sessions if s["start_time"] > now_hhmm]
    return min(upcoming) if upcoming else None


async def _render_day_text(code: str, day: str, *, is_today: bool, lang: str, tr_map: dict) -> str:
    def tr(text: str) -> str:
        return reg_i18n.tr_text(text, lang, tr_map)

    sessions = await sessions_for_city_day(code, day)
    lines = [f"🗓 <b>{tr('Программа')}</b> — {day_label(day)}", ""]
    if not sessions:
        lines.append(tr("Сессий в этот день пока нет."))
        return "\n".join(lines)

    # Время сессий менеджер города вводит по-местному — «сейчас» считаем по часам города.
    now_hhmm = (
        shift_hours(msk_now(), await city_offset_hours(code)).strftime("%H:%M") if is_today else None
    )
    next_start = _next_start(sessions, now_hhmm) if is_today else None

    def marker_line(group: list[dict]) -> str | None:
        marker = _time_marker(group, now_hhmm, next_start) if is_today else None
        if marker == "now":
            return f"🔴 {tr('Идёт сейчас')}"
        if marker == "next":
            return f"⏭ {tr('Следующая')}"
        return None

    for group in group_parallel(sessions):
        if len(group) == 1:
            line = marker_line(group)
            if line:
                lines.append(line)
            s = group[0]
            lines.append(f"⏰ {format_time_range(s['start_time'], s['end_time'])} — {html.escape(s['title'])}")
            hall_text = html.escape(s["hall_name"]) if s.get("hall_name") else "—"
            lines.append(f"🏛 {tr('Зал:')} {hall_text}")
            if s.get("speaker"):
                lines.append(f"🎤 {tr('Спикер:')} {html.escape(s['speaker'])}")
        else:
            group_sorted = sorted(group, key=lambda s: (s["start_time"], s.get("id") or 0))
            time_span = format_time_range(
                min(s["start_time"] for s in group), max(s["end_time"] for s in group),
            )
            lines.append(f"⏰ {time_span} · {tr('параллельно')}:")
            # Приёмка 01.10: метка и время — у каждой параллельной сессии своя. Раньше блок
            # «03:00–06:00 · параллельно» целиком шёл под «🔴 Идёт сейчас», хотя вторая
            # сессия (04:30) ещё не началась.
            for s in group_sorted:
                line = marker_line([s])
                if line:
                    lines.append(line)
                hall_text = html.escape(s["hall_name"]) if s.get("hall_name") else "—"
                bullet = (
                    f"• {format_time_range(s['start_time'], s['end_time'])} "
                    f"{html.escape(s['title'])} — 🏛 {hall_text}"
                )
                if s.get("speaker"):
                    bullet += f" · 🎤 {html.escape(s['speaker'])}"
                lines.append(bullet)
        lines.append("")

    return "\n".join(lines).rstrip()


def _day_picker_kb(days: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"📅 {day_label(d)}", callback_data=f"pds_day:{d}")]
        for d in days
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_day_screen(message_or_callback, code: str, day: str, days: list[str], *, edit: bool) -> None:
    lang, tr_map = await reg_i18n.ctx_for(message_or_callback)
    today = shift_hours(msk_now(), await city_offset_hours(code)).strftime("%Y-%m-%d")
    text = await _render_day_text(code, day, is_today=(day == today), lang=lang, tr_map=tr_map)

    kb = None
    if len(days) > 1:
        back_text = reg_i18n.tr_text("← Дни", lang, tr_map)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=back_text, callback_data="pds_days")]])

    if edit:
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=kb)


async def send_program_schedule_text(message: types.Message) -> bool:
    """Текстовый вид программы — запасной для объединённой кнопки «📅 Программа форума»
    (`handlers.user_actions.show_program`, D-29), когда фото не загружено. Возвращает `False`,
    если у города делегата тоже нет ни одной сессии — тогда вызывающий покажет
    `program_empty_text` вместо этого экрана; `ensure_registered` уже проверен вызывающим."""
    code = await _resolve_delegate_city(message.from_user.id)
    days = await list_program_days_for_city(code)
    if not days:
        return False

    if len(days) == 1:
        await _send_day_screen(message, code, days[0], days, edit=False)
        return True

    lang, tr_map = await reg_i18n.ctx_for(message)
    header = f"🗓 <b>{reg_i18n.tr_text('Программа', lang, tr_map)}</b>\n\n{reg_i18n.tr_text('Выберите день:', lang, tr_map)}"
    await message.answer(header, parse_mode="HTML", reply_markup=_day_picker_kb(days))
    return True


@router.callback_query(F.data.startswith("pds_day:"))
async def pds_day_open(callback: types.CallbackQuery):
    day = callback.data.split(":", 1)[1]
    code = await _resolve_delegate_city(callback.from_user.id)
    days = await list_program_days_for_city(code)
    if day not in days:
        lang, tr_map = await reg_i18n.ctx_for(callback)
        await callback.answer(reg_i18n.tr_text("Сессий в этот день пока нет.", lang, tr_map), show_alert=True)
        return
    await _send_day_screen(callback, code, day, days, edit=True)
    await callback.answer()


@router.callback_query(F.data == "pds_days")
async def pds_days_back(callback: types.CallbackQuery):
    code = await _resolve_delegate_city(callback.from_user.id)
    days = await list_program_days_for_city(code)
    lang, tr_map = await reg_i18n.ctx_for(callback)
    if not days:
        await callback.answer(reg_i18n.tr_text("Программа пока пуста.", lang, tr_map), show_alert=True)
        return
    header = f"🗓 <b>{reg_i18n.tr_text('Программа', lang, tr_map)}</b>\n\n{reg_i18n.tr_text('Выберите день:', lang, tr_map)}"
    await callback.message.edit_text(header, parse_mode="HTML", reply_markup=_day_picker_kb(days))
    await callback.answer()
