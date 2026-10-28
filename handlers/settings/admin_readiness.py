"""Экран «✅ Готовность к форуму»: что ещё не настроено, кнопка «🛠» прямо на нужный экран и
«🙈 Не нужно для этого события».

Пункты и их проверки — `services/forum/readiness.py::READINESS_ITEMS` (одна декларация), здесь
только отрисовка и пять хендлеров. Вход — первая кнопка корня `/admin`
(`handlers/settings/admin_core.py::build_admin_keyboard`) и кнопка в утреннем напоминании.

Шов на ОБЩЕМ `from handlers.admin import router` (как `handlers/forum/admin_program.py`),
импортируется хвостом `handlers/settings/admin_sections.py`; `admin_core`/`admin_settings` этот
модуль не импортирует вовсе (цикла нет).

Права: все callback-и экрана (`ready_list`, `ready_city:`, `ready_skip:`, `ready_unskip:`,
`ready_remind_toggle`) закрыты одной записью `ready_*` -> settings, зарегистрированной ниже
через `ADMIN_CAPS.update` — сам `admin_caps.py` на потолке и не растёт. Кнопка «🛠» рисуется
только тому, у кого есть право на экран исправления; сам экран защищён своей записью.

id пункта и код города из callback_data сверяются точным сравнением с реестром / списком
городов ДО записи; в текст и в имя ключа присланная строка не попадает.
"""
import html
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import (
    ALL_CITIES, admin_selected_city, city_codes, city_label, enabled_cities, set_admin_city,
)
from domain.settings.schema import get_setting_typed
from handlers.access.admin_caps import ADMIN_CAPS, _holds, required_capability, resolve_capabilities
from handlers.admin import router
from services.forum import readiness as rd

logger = logging.getLogger(__name__)

# Одна запись на все callback-и экрана (см. докстринг): admin_caps.py не растёт.
ADMIN_CAPS.update({"ready_*": "settings"})

HINT = "«🙈 Не нужно» — если этот пункт этому событию не нужен; напоминание про него не придёт."
PICK_CITY = "Готовность считается по каждому городу — выберите город:"


# ── отрисовка (без I/O) ───────────────────────────────────────────────────────────────────

def _status_lines(statuses) -> list[str]:
    lines = []
    for item, st in statuses:
        if st == rd.TODO:
            lines.append(f"⚠️ {item.title}")
        elif st == rd.OK:
            lines.append(f"✅ {item.title}")
        elif st == rd.SKIPPED:
            lines.append(f"➖ {item.title} — не нужно")
    return lines


def render_text(statuses, city_title: str | None = None, *, all_cities: bool = False) -> str:
    head = "✅ <b>Готовность к форуму</b>"
    if city_title and not all_cities:
        head += f" — {html.escape(str(city_title))}"
    lines = [head, ""]
    if all_cities:
        lines.append(PICK_CITY)
        body = _status_lines(statuses)
        if body:
            lines += ["", "Общее для всех городов:", *body]
    else:
        n = sum(1 for _i, st in statuses if st == rd.TODO)
        lines.append(f"⚠️ Не настроено: {n}" if n else "Всё настроено 👍")
        body = _status_lines(statuses)
        if body:
            lines += ["", *body]
    lines += ["", HINT]
    return "\n".join(lines)


def _item_rows(statuses, caps: set) -> list[list[InlineKeyboardButton]]:
    rows = []
    for item, st in statuses:
        if st == rd.TODO:
            row = []
            cap = required_capability(callback_data=item.fix_callback)
            if cap is not None and _holds(caps, cap):
                row.append(InlineKeyboardButton(text=f"🛠 {item.short}", callback_data=item.fix_callback))
            row.append(InlineKeyboardButton(text="🙈 Не нужно", callback_data=f"ready_skip:{item.id}"))
            rows.append(row)
        elif st == rd.SKIPPED:
            rows.append([InlineKeyboardButton(
                text=f"↩️ Вернуть: {item.short}", callback_data=f"ready_unskip:{item.id}")])
    return rows


def render_keyboard(statuses, caps: set, *, remind_on: bool, remind_days,
                    city_buttons: list[tuple[str, str]] | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=text, callback_data=cb)] for text, cb in (city_buttons or [])]
    rows += _item_rows(statuses, caps)
    rows.append([InlineKeyboardButton(
        text=f"🔔 Напоминать за {remind_days} дн.: {'вкл' if remind_on else 'выкл'}",
        callback_data="ready_remind_toggle")])
    rows.append([InlineKeyboardButton(text="📅 За сколько дней напоминать",
                                      callback_data="settings_edit:ready_remind_days")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── экран ─────────────────────────────────────────────────────────────────────────────────

async def readiness_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    city = await admin_selected_city(admin_id)  # шапка читается ОДИН раз на рендер
    caps = await resolve_capabilities(admin_id)
    remind_on = await get_setting_typed("ready_remind_enabled") == "on"
    remind_days = await get_setting_typed("ready_remind_days")
    if city == ALL_CITIES:
        statuses = await rd.item_statuses(None, "global")
        buttons = []
        for c in await enabled_cities():
            n = await rd.pending_count(c["code"], "city")
            label = await city_label(c["code"])
            buttons.append((f"🏙 {label} — " + (f"не настроено {n}" if n else "✅"), f"ready_city:{c['code']}"))
        return (render_text(statuses, all_cities=True),
                render_keyboard(statuses, caps, remind_on=remind_on, remind_days=remind_days,
                                city_buttons=buttons))
    statuses = await rd.item_statuses(city)
    title = await city_label(city) if city else None
    return (render_text(statuses, title),
            render_keyboard(statuses, caps, remind_on=remind_on, remind_days=remind_days))


async def _redraw(callback: types.CallbackQuery) -> None:
    text, kb = await readiness_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── хендлеры ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ready_list")
async def ready_list(callback: types.CallbackQuery):
    await _redraw(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("ready_city:"))
async def ready_city(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if code not in city_codes():
        await callback.answer("Город не найден.", show_alert=True)
        return
    if not await set_admin_city(callback.from_user.id, code):
        await callback.answer("Этот город вам недоступен — откройте свой город в шапке /admin.", show_alert=True)
        return
    await _redraw(callback)
    await callback.answer()


async def _skip(callback: types.CallbackQuery, on: bool) -> None:
    item = rd.get_item(callback.data.split(":", 1)[1])
    if item is None:
        await callback.answer("Пункт не найден.", show_alert=True)
        return
    city = await admin_selected_city(callback.from_user.id)
    if city == ALL_CITIES:
        if item.scope == "city":
            await callback.answer("Сначала выберите город.", show_alert=True)
            return
        city = None
    await rd.set_skipped(item.id, city, on)
    await _redraw(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("ready_skip:"))
async def ready_skip(callback: types.CallbackQuery):
    await _skip(callback, True)


@router.callback_query(F.data.startswith("ready_unskip:"))
async def ready_unskip(callback: types.CallbackQuery):
    await _skip(callback, False)


@router.callback_query(F.data == "ready_remind_toggle")
async def ready_remind_toggle(callback: types.CallbackQuery):
    from services.settings.audit import set_setting_by_admin
    current = await get_setting_typed("ready_remind_enabled")
    await set_setting_by_admin(callback.from_user.id, "ready_remind_enabled", "off" if current == "on" else "on")
    await _redraw(callback)
    await callback.answer()
