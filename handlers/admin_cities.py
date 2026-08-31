"""Phase 13 (13-05, REFAC-01): cities admin screen + season reset/import + city-switcher seam.

`admin.py:2357-3205` moved byte-for-byte, contiguous slice, onto the SAME shared `admin.router`
(13-02/13-03/13-04 shared-router seam-import technique). Drift note (13-05-SUMMARY.md): this
contiguous block is wider than the plan's "cities + dedupe" description — Phase 07.3's
«🔄 Новый сезон» (`admin_season_reset`) and «📥 Импорт прошлого события» (`admin_season_import`)
wizards physically sit between the city-CRUD screen (Phase 14-07) and the city-switcher/
`admin_menu`/dedupe cluster in the original file, with no unrelated code between any of the three
groups. Splitting them into a separate file would require a second seam-import insertion point
with no natural boundary in the plan's own module list, so the whole contiguous run travels
together here, documented rather than silently reorganized.
"""
import html as html_module
import logging
import os
import sqlite3
import tempfile

from aiogram import F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from config import config
from settings_schema import get_setting_typed, SETTINGS_SCHEMA
from database.db import (
    get_setting,
    set_setting,
    delete_setting,
    get_staff_city,
    update_city,
    insert_city,
    delete_city_row,
    count_users_by_city,
    count_tasks_by_city,
    count_current_season_users,
    mark_season_ended,
    bulk_insert_users_if_absent,
    count_existing_telegram_ids,
)
from services.sheets import dedupe_sheet_by_id, REFUSED_UNPINNED_TAB
from services.background import spawn as _spawn
from keyboards.builders import get_cancel_kb
from handlers.states import CityForm, SeasonReset, SeasonImport
from cities import (
    CITIES,
    ALL_CITIES,
    ALL_CITIES_LABEL,
    is_city_enabled,
    city_label,
    cities_module_on,
    all_cities,
    reload_cities,
    default_city_code,
    make_city_code,
    admin_selected_city,
    set_admin_city,
)
from handlers.reg_schema import city_row_tab
from handlers.admin_core import admin_keyboard_for
from handlers.admin import router
from handlers.admin_settings import render_settings_group_text, build_settings_group_keyboard  # Phase 13 (13-06): settings moved out of admin.py

logger = logging.getLogger(__name__)

# ── Phase 07.1 (CITY-04) / Phase 14 (14-07, CITY-07): «🏙 Города» admin screen ───────────────
# Phase 14-07 closes CITY-07 fully: add/rename/tab-base/default/delete are now all in-bot, no
# `.env` restart round-trip. The city list itself lives in the `cities` table (cities.py cache,
# `all_cities()`); a manager never types or sees a raw city CODE — only the human-facing label
# and the deep-link it produces (T-14-34).


async def _cities_screen_allowed(user_id: int) -> bool:
    """T-14-32: the registry is shared across every city, so only a manager NOT bound to a
    single city (a bootstrap superadmin, or a manager whose `staff.city` binding is empty) may
    write to it. Called from `show_admin_cities` AND every writing handler below — the gate
    belongs in the handler, not only the keyboard, because an old inline keyboard from before a
    binding existed lives forever in a chat."""
    if user_id in config.ADMIN_IDS:
        return True
    bound = await get_staff_city(user_id)
    return not bound


async def _deny_cities_screen(callback: types.CallbackQuery) -> None:
    bound = await get_staff_city(callback.from_user.id)
    await callback.answer(
        f"Этот экран — для менеджера всех городов. Ваш город: {await city_label(bound)}",
        show_alert=True,
    )


async def _deny_cities_screen_message(message: types.Message) -> None:
    bound = await get_staff_city(message.from_user.id)
    await message.answer(
        f"Этот экран — для менеджера всех городов. Ваш город: {await city_label(bound)}"
    )


async def render_cities_text() -> str:
    module_on = await cities_module_on()
    module_status = "✅ Вкл" if module_on else "❌ Выкл"
    cities = all_cities()
    default_code = default_city_code()
    lines = [
        "🏙 <b>Города мероприятия</b>",
        "",
        f"Модуль выбора города: {module_status}",
    ]
    if not module_on:
        lines.append(
            "Пока выключен — экран выбора города делегатам не показывается, "
            "все заявки идут в основной лист."
        )
    lines.append("")
    any_hidden_delete = False
    for c in cities:
        code = c["code"]
        enabled = await is_city_enabled(code)
        label = await city_label(code)
        tab = await city_row_tab(code, None) or "основной лист"
        icon = "✅" if enabled else "⛔"
        star = " ⭐" if code == default_code else ""
        lines.append(
            f"{icon} {html_module.escape(label)}{star} — "
            f"<code>?start=city_{html_module.escape(code)}</code> → {html_module.escape(tab)}"
        )
        if await count_users_by_city(code) > 0 or await count_tasks_by_city(code) > 0:
            any_hidden_delete = True
    lines.append("")
    lines.append("⭐ — город по умолчанию: в него попадают заявки без выбранного города.")
    if any_hidden_delete:
        lines.append(
            "🗑 У городов, где уже есть делегаты или задания, удаления нет — такой город можно "
            "только выключить (⛔): он исчезнет из выбора, а собранные заявки останутся на месте."
        )
    return "\n".join(lines)


async def build_cities_keyboard() -> InlineKeyboardMarkup:
    module_on = await cities_module_on()
    master_text = ("🏙 Выбор города: ✅ Вкл → ❌ Выкл" if module_on
                   else "🏙 Выбор города: ❌ Выкл → ✅ Вкл")
    buttons = [[InlineKeyboardButton(text=master_text, callback_data="toggle_event_city_enabled")]]
    cities = all_cities()
    default_code = default_city_code()
    for c in cities:
        code = c["code"]
        enabled = await is_city_enabled(code)
        label = await city_label(code)
        icon = "✅" if enabled else "⛔"
        row1 = [InlineKeyboardButton(text=f"{icon} {label}", callback_data=f"city_toggle:{code}")]
        if code != default_code:
            row1.append(InlineKeyboardButton(text="⭐", callback_data=f"city_default:{code}"))
        buttons.append(row1)
        row2 = [
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"city_rename:{code}"),
            InlineKeyboardButton(text="📄 База вкладки", callback_data=f"city_tab:{code}"),
        ]
        if await count_users_by_city(code) == 0 and await count_tasks_by_city(code) == 0:
            row2.append(InlineKeyboardButton(text="🗑", callback_data=f"city_del:{code}"))
        buttons.append(row2)
    buttons.append([InlineKeyboardButton(text="➕ Добавить город", callback_data="city_add")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_cities")
async def show_admin_cities(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())
    await callback.answer()


@router.callback_query(F.data == "toggle_event_city_enabled")
async def toggle_event_city_enabled(callback: types.CallbackQuery):
    current = await get_setting_typed("event_city_enabled")
    new_val = "off" if current == "on" else "on"
    await set_setting("event_city_enabled", new_val)
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"Выбор города: {label}", show_alert=True)

    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.callback_query(F.data.startswith("city_toggle:"))
async def city_toggle(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return

    current = await is_city_enabled(code)
    new_val = 0 if current else 1
    await update_city(code, enabled=new_val)
    # Легаси-ключ (Phase 07.1) имеет приоритет в is_city_enabled — если его не убрать,
    # тумблер перестанет менять фактическое поведение после первого же переключения.
    await delete_setting(f"city_enabled__{code}")
    await reload_cities()
    label_text = "✅ Вкл" if new_val else "⛔ Выкл"
    await callback.answer(f"{await city_label(code)}: {label_text}", show_alert=True)

    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.callback_query(F.data.startswith("city_default:"))
async def city_default(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    cities = all_cities()
    if code not in {c["code"] for c in cities}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await update_city(code, sort_order=0)
    others = [c["code"] for c in cities if c["code"] != code]
    for i, other_code in enumerate(others, start=1):
        await update_city(other_code, sort_order=i)
    await reload_cities()
    await callback.answer(f"Город по умолчанию: {await city_label(code)}", show_alert=True)

    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


# ── Phase 14 (14-07, CITY-07): «➕ Добавить город» wizard + rename / tab-base edit ───────────
#
# The city CODE is NEVER human input — `city_add`'s add_tab step is the only call site of
# `make_city_code()` in this file. A manager only ever types a LABEL and a tab-base string.

def _dash_or_text(raw: str | None) -> str:
    """«—»/empty input -> "" (same tab as the main city); anything else -> `.strip()`ped
    text. Shared by the add-wizard's add_tab step and the edit_tab step so both accept the
    same «Enter/«—»» escape hatch the plan's copy promises."""
    text = (raw or "").strip()
    if not text or text == "—":
        return ""
    return text


@router.callback_query(F.data == "city_add")
async def city_add(callback: types.CallbackQuery, state: FSMContext):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    await state.set_data({})
    await callback.message.answer(
        "Как называется город? Напишите подпись, которую увидят делегаты — например: "
        "Казань, 14 ноября.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.add_label)
    await callback.answer()


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) — same shape as cancel_game_task_create/cancel_edit_setting.
@router.message(StateFilter(CityForm), Command("cancel"))
@router.message(StateFilter(CityForm), F.text == "Отмена")
async def cancel_city_form(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.message(CityForm.add_label)
async def city_add_label_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    label = (message.text or "").strip()
    if not label:
        await message.answer("Подпись не может быть пустой. Как называется город?")
        return
    await state.update_data(city_new_label=label)
    await message.answer(
        "На какую вкладку таблицы писать заявки этого города? Напишите базу имени вкладки — "
        "например: Казань. Если писать в ту же вкладку, что и основной город, пришлите «—» "
        "(или Enter).",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.add_tab)


async def _materialize_new_city_tabs() -> None:
    """T-14-38: Sheets failure must never block the screen — this runs fire-and-forget
    (`_spawn`) with its own try/except, mirroring main.py's startup materialization. Local
    import only: `main.py` imports `handlers.*`, so a module-level `from main import ...`
    here would create an import cycle."""
    try:
        from main import _maybe_ensure_city_sheet_headers
        await _maybe_ensure_city_sheet_headers()
    except Exception as e:
        logger.warning("Не удалось материализовать вкладки нового города: %s", e)


@router.message(CityForm.add_tab)
async def city_add_tab_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    data = await state.get_data()
    label = data.get("city_new_label", "")
    tab_base = _dash_or_text(message.text)
    existing = {c["code"] for c in all_cities()}
    code = make_city_code(label, existing=existing)
    sort_order = max((c.get("sort_order") or 0) for c in all_cities()) + 1 if existing else 1
    await insert_city(code, label, tab_base, sort_order, enabled=1)
    await reload_cities()
    _spawn(_materialize_new_city_tabs())

    await state.set_state(None)
    await message.answer(f"✅ Город добавлен: {label}", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.callback_query(F.data.startswith("city_rename:"))
async def city_rename_start(callback: types.CallbackQuery, state: FSMContext):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await state.set_data({"city_code": code})
    label = await city_label(code)
    await callback.message.answer(
        f"Как теперь называть город «{label}»? Напишите новую подпись.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.edit_label)
    await callback.answer()


@router.callback_query(F.data.startswith("city_tab:"))
async def city_tab_start(callback: types.CallbackQuery, state: FSMContext):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await state.set_data({"city_code": code})
    label = await city_label(code)
    await callback.message.answer(
        f"На какую вкладку писать заявки города «{label}»? Напишите базу имени вкладки, или "
        "«—» — писать в ту же вкладку, что и основной город.\n\n"
        "⚠️ Уже собранные заявки останутся в старой вкладке — бот переносит только новые.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.edit_tab)
    await callback.answer()


@router.message(CityForm.edit_label)
async def city_edit_label_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    data = await state.get_data()
    code = data.get("city_code")
    label = (message.text or "").strip()
    if not label:
        await message.answer("Подпись не может быть пустой. Как теперь называть город?")
        return
    await update_city(code, label=label)
    await delete_setting(f"city_label__{code}")  # легаси-override иначе продолжит перекрывать колонку
    await reload_cities()

    await state.set_state(None)
    await message.answer(f"✅ Подпись обновлена: {label}", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.message(CityForm.edit_tab)
async def city_edit_tab_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    data = await state.get_data()
    code = data.get("city_code")
    tab_base = _dash_or_text(message.text)
    await update_city(code, tab_base=tab_base)
    await delete_setting(f"city_tab__{code}")  # легаси-override иначе продолжит перекрывать колонку
    await reload_cities()

    await state.set_state(None)
    await message.answer("✅ База вкладки обновлена.", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


# ── Phase 14 (14-07, CITY-07): удаление города — три независимые серверные проверки (T-14-35):
# кнопка в клавиатуре (build_cities_keyboard), это подтверждение, и исполнение (city_delete_go).

@router.callback_query(F.data.startswith("city_del:"))
async def city_delete_confirm(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    users_n = await count_users_by_city(code)
    tasks_n = await count_tasks_by_city(code)
    if users_n > 0 or tasks_n > 0:
        await callback.answer(
            f"На этом городе уже есть делегаты ({users_n}) или задания ({tasks_n}) — его можно "
            "только выключить (⛔), тогда он пропадёт из выбора, а собранные заявки останутся "
            "на месте",
            show_alert=True,
        )
        text = await render_cities_text()
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())
        return
    if code == default_city_code():
        await callback.answer(
            "Это город по умолчанию: в него попадают заявки без выбранного города. Сначала "
            "назначьте другой город ⭐, потом удаляйте",
            show_alert=True,
        )
        return
    label = await city_label(code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"city_del_go:{code}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_cities")],
    ])
    await callback.message.edit_text(
        f"🗑 <b>Удалить город «{html_module.escape(label)}»?</b>\n\n"
        "Город пропадёт из выбора у делегатов и из фильтров рассылок; его ссылка-приглашение "
        "перестанет работать. Вкладка таблицы и её строки НЕ удаляются. Вернуть можно только "
        "заведя город заново.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("city_del_go:"))
async def city_delete_go(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    users_n = await count_users_by_city(code)
    tasks_n = await count_tasks_by_city(code)
    if users_n > 0 or tasks_n > 0:
        await callback.answer(
            "На этом городе уже есть делегаты/задания — его можно только выключить (⛔)",
            show_alert=True,
        )
        text = await render_cities_text()
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())
        return
    if code == default_city_code():
        await callback.answer(
            "Это город по умолчанию — сначала назначьте другой город ⭐, потом удаляйте",
            show_alert=True,
        )
        return
    # Никаких каскадных удалений: только строка реестра. Легаси-ключи city_*__{code} в
    # bot_settings НЕ трогаются — заведение города с тем же кодом заново подхватит их.
    if await delete_city_row(code):
        await reload_cities()
        await callback.answer("Город удалён", show_alert=True)
    else:
        await callback.answer("Город уже удалён", show_alert=True)
    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


# ── Phase 07.3 (02, RET-01): «🔄 Новый сезон» wizard ──────────────────────────────────────────
# T-073-02-01 (Elevation of Privilege): the `settings` capability entry (admin_caps.py) gets a
# holder INTO this screen, but every single handler below re-checks `config.ADMIN_IDS` itself —
# a stale inline keyboard rendered before someone's rights changed lives in the chat forever
# (same posture as roles_city_start/roles_city_pick). T-073-02-02/03 (Tampering/Repudiation):
# a two-tap numbers confirm PLUS a typed passphrase (the old season's exact name, or the literal
# "НОВЫЙ СЕЗОН" if there was none) gate the actual bulk UPDATE, and the action is logged with
# the admin's id before the reply is sent.

def _season_sheet_reminder() -> str:
    """UAT 19.08: при смене сезона менеджер должен завести новую Google-таблицу/вкладки и
    переключить бота на них — иначе новые регистрации поедут в прошлогоднюю таблицу.
    Лейбл настройки берём из реестра, чтобы подсказка не разъехалась с экраном настроек."""
    tab_label = SETTINGS_SCHEMA["main_sheet_tab"]["label"]
    return (
        "📄 <b>Не забудь про таблицу:</b> создай новую Google-таблицу (или новые вкладки) под "
        "новый сезон и обнови название вкладки в боте: "
        f"⚙️ Настройки → 📄 Вкладки таблицы → «{html_module.escape(tab_label)}»."
    )


@router.callback_query(F.data == "admin_season_reset")
async def season_reset_start(callback: types.CallbackQuery, state: FSMContext):
    # Positive-form idiom (byte-identical to roles_city_start/admin_city_switch) — D-01/T-08-18's
    # structural gate forbids a bare "not in config.ADMIN_IDS" anywhere in this file.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer("Новый сезон может начать только суперадмин.", show_alert=True)
        return
    await state.set_data({})
    old = (await get_setting("event_season") or "").strip()
    await state.update_data(season_old=old)
    old_label = html_module.escape(old) if old else "не задан"
    await callback.message.answer(
        f"🔄 <b>Новый сезон</b>\n\nСейчас сезон: <b>{old_label}</b>.\n\n"
        "Напиши название нового сезона — им будут помечаться все новые регистрации.\n"
        "Например: YL'26",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(SeasonReset.naming)
    await callback.answer()


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) — same shape as cancel_city_form/cancel_game_task_create.
@router.message(StateFilter(SeasonReset), Command("cancel"))
@router.message(StateFilter(SeasonReset), F.text == "Отмена")
async def cancel_season_reset(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Сезон не менялся.", reply_markup=ReplyKeyboardRemove())


@router.message(SeasonReset.naming)
async def season_reset_name_step(message: types.Message, state: FSMContext):
    # T-073-02-01: the gate belongs in the handler, not only the button that opened it — a
    # message can arrive at any moment after the wizard was entered.
    is_superadmin = message.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await message.answer("Новый сезон может начать только суперадмин.")
        return
    new = (message.text or "").strip()
    if not new:
        await message.answer("Название сезона не может быть пустым. Напиши, например: YL'26")
        return
    if len(new) > 64:
        await message.answer("Слишком длинно. Уложись в 64 символа.")
        return
    await state.update_data(season_new=new)
    data = await state.get_data()
    old = data.get("season_old") or ""
    n = await count_current_season_users(old or None)
    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Продолжить", callback_data="season_reset_go")],
        # Цель берётся из реестра разделов: кнопка «🔄 Новый сезон» уехала из группы
        # «🎪 Событие/Медиа» в «🔧 Управление», и литеральная отмена возвращала менеджера на
        # экран, где нажатой кнопки больше нет. Литерал уехал бы снова при следующем переезде.
        [back_button("admin_season_reset", text="← Отмена")],
    ])
    # State stays SeasonReset.naming — the next step is a tap (season_reset_go), not text; a
    # repeated text message here just re-renders this same screen with the new name.
    await message.answer(
        f"🔄 <b>Начать сезон «{html_module.escape(new)}»?</b>\n\n"
        f"• {n} делегатов текущего сезона станут «прошлыми»\n"
        "• статусы, монеты и чеки не трогаем, база не чистится\n"
        "• они смогут обновить анкету по /start\n\n"
        f"{_season_sheet_reminder()}\n\n"
        "Продолжить?",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "season_reset_go")
async def season_reset_go(callback: types.CallbackQuery, state: FSMContext):
    # T-073-02-01: re-checked again, T-073-02-05: re-checked against a possibly-stale screen.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer("Новый сезон может начать только суперадмин.", show_alert=True)
        return
    data = await state.get_data()
    new = data.get("season_new")
    if not new:
        await callback.answer("Экран устарел, начни заново", show_alert=True)
        await state.set_state(None)
        return
    old = data.get("season_old") or ""
    phrase = old if old else "НОВЫЙ СЕЗОН"
    await state.update_data(season_phrase=phrase)
    await callback.message.answer(
        f"Чтобы подтвердить, напиши: <code>{html_module.escape(phrase)}</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(SeasonReset.passphrase)
    await callback.answer()


@router.message(SeasonReset.passphrase)
async def season_reset_passphrase_step(message: types.Message, state: FSMContext):
    # T-073-02-01: re-checked a third time — this is the step that actually executes the write.
    is_superadmin = message.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await message.answer("Новый сезон может начать только суперадмин.")
        return
    data = await state.get_data()
    phrase = (data.get("season_phrase") or "").strip()
    typed = (message.text or "").strip()
    if typed != phrase:
        await message.answer("Фраза не совпала, ничего не сделано.")
        await state.set_state(None)
        return
    old = data.get("season_old") or ""
    new = data.get("season_new") or ""
    # Order matters (T-073-02-02): mark old-season rows FIRST, while event_season still reads
    # as the old value, then flip event_season last — makes the switch atomic from a delegate's
    # point of view.
    affected = await mark_season_ended(old or None)
    await set_setting("event_season", new)
    logger.warning(
        f"SEASON RESET by admin {message.from_user.id}: '{old}' -> '{new}', marked {affected} users"
    )
    await message.answer(
        f"✅ Новый сезон: <b>{html_module.escape(new)}</b>\nПрошлыми отмечены: {affected}\n\n"
        "Статусы, монеты и чеки не тронуты.\n\n"
        f"{_season_sheet_reminder()}",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(None)


# ── Phase 07.3 (06, RET-04): «📥 Импорт прошлого события» wizard ─────────────────────────────
# T-073-06-01 (Tampering): the uploaded file is opened read-only, in ITS OWN sqlite3 connection
# — never config.DB_PATH, never the live DB's async driver. T-073-06-03 (DoS): the 20 MB guard runs BEFORE
# bot.download; the temp file is always removed in `finally` (T-073-06-05, Information
# Disclosure — nothing from a past event's personal data touches disk after this handler
# returns). T-073-06-02 (column-name injection) is mitigated inside
# database.db.bulk_insert_users_if_absent itself, not here.

_IMPORT_MAX_BYTES = 20 * 1024 * 1024


@router.callback_query(F.data == "admin_season_import")
async def season_import_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})
    await callback.message.answer(
        "📥 <b>Импорт прошлого события</b>\n\n"
        "Пришли файл базы старого бота — я прочитаю из него делегатов и добавлю тех, кого "
        "ещё нет.\n\n"
        "Монеты, оплаты и рефералы не переносятся, существующие записи не меняются. Файл "
        "после импорта не храню.\n\n"
        "Размер — до 20 МБ.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(SeasonImport.waiting_file)
    await callback.answer()


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) — same shape as cancel_city_form/cancel_season_reset.
@router.message(StateFilter(SeasonImport), Command("cancel"))
@router.message(StateFilter(SeasonImport), F.text == "Отмена")
async def cancel_season_import(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Ничего не импортировано.", reply_markup=ReplyKeyboardRemove())


@router.message(SeasonImport.waiting_file, F.document)
async def season_import_file_step(message: types.Message, state: FSMContext, bot: Bot):
    if (message.document.file_size or 0) > _IMPORT_MAX_BYTES:
        await message.answer("Файл больше 20 МБ — столько я принять не могу.")
        return

    tmp_path = None
    con = None
    try:
        buf = await bot.download(message.document.file_id)
        buf.seek(0)
        content = buf.read()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # T-073-06-01: read-only, own connection — physically cannot write to the file.
            con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            table_check = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            if not table_check:
                await message.answer("В этом файле нет таблицы делегатов. Похоже, это база другого приложения.")
                return
            raw_rows = con.execute("SELECT * FROM users").fetchall()
        except sqlite3.DatabaseError:
            await message.answer(
                "Это не похоже на базу бота: не удалось её прочитать. Пришли файл базы "
                "старого бота (обычно называется forum.db)."
            )
            return

        rows = []
        for r in raw_rows:
            d = dict(r)
            try:
                tg_id = int(d.get("telegram_id"))
            except (TypeError, ValueError):
                continue
            if not tg_id:
                continue
            d["telegram_id"] = tg_id  # normalize to int — matches count_existing_telegram_ids
            rows.append(d)

        found = len(rows)
        if found == 0:
            await message.answer("В файле нет делегатов — импортировать нечего.")
            await state.set_state(None)
            return

        ids = [r["telegram_id"] for r in rows]
        existing = await count_existing_telegram_ids(ids)

        await state.update_data(import_rows=rows, import_found=found, import_existing=existing)
        await message.answer(
            f"📥 <b>Файл прочитан</b>\n\n"
            f"Найдено делегатов: <b>{found}</b>\n"
            f"Из них уже есть в базе: <b>{existing}</b> — их пропущу\n"
            f"Будет добавлено: <b>{found - existing}</b>\n\n"
            "Напиши название сезона для импортируемых — например: YL'25",
            parse_mode="HTML",
            reply_markup=get_cancel_kb(),
        )
        await state.set_state(SeasonImport.naming)
    finally:
        if con is not None:
            con.close()
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception as e:
                logger.warning(f"season_import_file_step: не удалось удалить временный файл {tmp_path}: {e}")


@router.message(SeasonImport.waiting_file)
async def season_import_file_invalid(message: types.Message):
    await message.answer("Пришли файл базы документом (не фото и не архив).")


@router.message(SeasonImport.naming)
async def season_import_name_step(message: types.Message, state: FSMContext):
    label = (message.text or "").strip()
    if not label:
        await message.answer("Название сезона не может быть пустым. Напиши, например: YL'25")
        return
    if len(label) > 64:
        await message.answer("Слишком длинно. Уложись в 64 символа.")
        return

    current_season = (await get_setting("event_season") or "").strip()
    if label == current_season:
        # CONTEXT A's "прошлый делегат" formula depends on season != event_season — importing
        # into the CURRENT season would make imported rows indistinguishable from live ones.
        await message.answer(
            "Это название текущего сезона — импортированные делегаты тогда будут считаться "
            "участниками ТЕКУЩЕГО события. Напиши название прошлого сезона."
        )
        return

    await state.update_data(import_season=label)
    data = await state.get_data()
    found = data.get("import_found", 0)
    existing = data.get("import_existing", 0)
    to_add = found - existing
    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Да, импортировать", callback_data="season_import_go")],
        [back_button("admin_season_import", text="← Отмена")],  # цель из реестра, см. выше
    ])
    await message.answer(
        f"📥 <b>Добавить {to_add} делегатов сезона «{html_module.escape(label)}»?</b>\n\n"
        "Статусы возьму из файла. Монеты, оплаты и рефералы не переносятся, существующие "
        "записи не меняются.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "season_import_go")
async def season_import_go(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rows = data.get("import_rows")
    season_label = data.get("import_season")
    if not rows or not season_label:
        # T-073-06-05-adjacent form-recheck-at-execute (same posture as city_delete_go): a
        # stale screen (e.g. after a restart, or a second tap) must not silently no-op-insert.
        await callback.answer("Экран устарел, начни импорт заново", show_alert=True)
        await state.set_state(None)
        return

    inserted = await bulk_insert_users_if_absent(rows, season_label)
    logger.warning(
        f"SEASON IMPORT by admin {callback.from_user.id}: season='{season_label}', "
        f"rows={len(rows)}, inserted={inserted}"
    )
    await callback.message.answer(
        f"✅ Импортировано: {inserted}\nПропущено (уже были): {len(rows) - inserted}"
    )
    await state.set_state(None)
    await state.set_data({})  # T-073-06-05: don't keep a past event's rows in memory longer than needed
    text = await render_settings_group_text("event", callback.from_user.id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard("event", callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "admin_city_switch")
async def admin_city_switch(callback: types.CallbackQuery):
    """Phase 07.2 (CITY-02): pick the city the admin panel is currently scoped to. Disabled
    cities are still listed (their past applications still need moderating), marked ❌.

    Phase 09.1 (C, ROLE-03): a manager bound to a city (`get_staff_city`) can't switch at
    all — the picker is never shown, only an alert naming their city. Bootstrap superadmins
    (`config.ADMIN_IDS`, D-12) are never restricted."""
    # Phase 09.3 (CITY-08): the bound-manager lock above is the ONLY gate this screen needs --
    # it already computes is_superadmin/bound and returns early for anyone with a locked city,
    # so everyone who reaches the code below (superadmin or an unbound manager) is exactly who
    # is allowed to see the "🌍 Все города" row too. No separate capability check needed here;
    # ADMIN_CAPS/CapabilityMiddleware already gated entry to this handler at ANY_CAPABILITY.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    bound = None if is_superadmin else await get_staff_city(callback.from_user.id)
    if bound:
        await callback.answer(
            f"Ваш город — {await city_label(bound)}, менять может суперадмин",
            show_alert=True,
        )
        return

    current = await admin_selected_city(callback.from_user.id)
    all_prefix = "✅ " if current == ALL_CITIES else ""
    buttons = [
        [InlineKeyboardButton(text=f"{all_prefix}{ALL_CITIES_LABEL}", callback_data=f"admin_city_pick:{ALL_CITIES}")],
    ]
    for c in CITIES:
        code = c["code"]
        label = await city_label(code)
        enabled = await is_city_enabled(code)
        prefix = "✅ " if code == current else ""
        suffix = "" if enabled else " ❌"
        buttons.append([InlineKeyboardButton(text=f"{prefix}{label}{suffix}", callback_data=f"admin_city_pick:{code}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")])
    text = (
        "🏙 <b>Город админки</b>\n\n"
        "Всё в админке — про выбранный город: заявки, чеки, выгрузка, гейма, тексты и кнопки меню.\n"
        "«🌍 Все города» — данные без фильтра и общие тексты (то, что видят города без своего значения)."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_city_pick:"))
async def admin_city_pick(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if not await set_admin_city(callback.from_user.id, code):
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await callback.answer(f"Город: {await city_label(code)}", show_alert=True)
    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>",
        parse_mode="HTML",
        reply_markup=await admin_keyboard_for(callback.from_user.id),
    )


@router.callback_query(F.data == "admin_menu")
async def admin_menu_root(callback: types.CallbackQuery):
    """Back to the admin panel keyboard (also fixes the previously dead «Отмена» buttons
    that pointed at admin_menu without a handler)."""
    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>", parse_mode="HTML", reply_markup=await admin_keyboard_for(callback.from_user.id)
    )
    await callback.answer()


@router.callback_query(F.data == "admin_dedupe_sheet")
async def dedupe_sheet_confirm(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Да, убрать дубли", callback_data="admin_dedupe_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(
        "🧹 <b>Убрать дубли из таблицы?</b>\n\n"
        "Удалю повторные строки с одинаковым Telegram ID (от повторных регистраций / "
        "тестов админов), оставлю <b>самую свежую</b> по каждому.\n\n"
        "⚠️ Удаляются целые строки — если на старой строке-дубле были ручные заметки, "
        "они пропадут (на оставленной строке всё цело).",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_dedupe_sheet_go")
async def dedupe_sheet_run(callback: types.CallbackQuery):
    await callback.answer("🧹 Убираю дубли…")
    logger.info(f"admin={callback.from_user.id} action=dedupe_sheet start")
    removed = await dedupe_sheet_by_id()
    if removed == REFUSED_UNPINNED_TAB:
        text = (
            "⛔ Убрать дубли нельзя: основная вкладка не задана.\n\n"
            "Без неё удаление строк могло бы задеть не ту вкладку. Укажите вкладку в "
            "«⚙️ Настройки → 📄 Вкладки таблицы → 📄 Основная (регистрации)» — сработает сразу, "
            "без перезапуска. Вариант для разработчика — <code>GOOGLE_SHEET_TAB</code> в .env "
            "(тогда нужен перезапуск)."
        )
    elif removed < 0:
        text = "⚠️ Не удалось (проверь доступ к Google Sheets, подробности в логах)."
    elif removed == 0:
        text = "✅ Дублей не найдено — таблица чистая."
    else:
        text = f"✅ Удалено дублей: <b>{removed}</b>. Оставлены свежие строки."
    logger.info(f"admin={callback.from_user.id} action=dedupe_sheet removed={removed}")
    from handlers.admin_sections import op_return_keyboard  # ленивый шов
    # Гейт подтверждения дал callback «…_go», в разделе объявлена сама кнопка — называем её.
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await op_return_keyboard(callback.from_user.id, "admin_dedupe_sheet"))
