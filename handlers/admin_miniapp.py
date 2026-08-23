"""Phase 19 (08, D-06): шов admin_miniapp — экран «🎨 Оформление» Mini App.

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_settings.py`, ПОСЛЕДНЕЙ строкой (после
`admin_dashboard`), как и остальные швы Phase 13/15.

Что здесь: два тумблера («Mini App включён», «Только менеджерам»), восемь чекбоксов разделов
приложения (`miniapp_section_*`, подписи из SETTINGS_SCHEMA — код ключа менеджеру никогда не
показывается, CLAUDE.md «бот для людей»), правка цвета акцента (HEX, текстовый ввод с
валидацией) и логотипа (фото, тот же приём, что PHOTO_FIELDS у `handlers/admin_settings.py`,
но со своей маленькой FSM-группой `MiniAppTheme` — у этого экрана свой экран возврата, не
общий лендинг настроек, тот же довод, что у `GameTaskEdit`/`handlers/admin_game_tasks.py`).

Точки входа приложения (текстовая кнопка меню / inline web_app / кнопка меню чата) и
`sync_chat_menu_button` — план 19-08, задача 2, тот же файл.
"""
import html as html_module
import logging
import re

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from config import config
from database.db import set_setting, delete_setting
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers.admin import router
from handlers.states import MiniAppTheme

logger = logging.getLogger(__name__)

# Порядок разделов на экране (D-06): зеркало восьми экранов делегата/менеджера фазы 19.
SECTION_KEYS = [
    "miniapp_section_tasks",
    "miniapp_section_coins",
    "miniapp_section_leaderboard",
    "miniapp_section_profile",
    "miniapp_section_review",
    "miniapp_section_admin_tasks",
    "miniapp_section_stats",
    "miniapp_section_settings",
]

_SECTION_BY_SUFFIX = {key[len("miniapp_section_"):]: key for key in SECTION_KEYS}

# `^#[0-9A-Fa-f]{6}$` — ровно решётка и шесть hex-символов (T-19-51: та же серверная
# валидация повторяется в `/app/theme.css` fail-soft-дефолтом, план 19-01).
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


async def render_miniapp_settings_text() -> str:
    enabled = await get_setting_typed("miniapp_enabled") == "on"
    staff_only = await get_setting_typed("miniapp_staff_only") == "on"
    accent = await get_setting_typed("miniapp_accent")
    logo = await get_setting_typed("miniapp_logo")

    lines = ["🎨 <b>Оформление приложения</b>", ""]
    lines.append(
        ("✅" if enabled else "☐")
        + " Приложение включено — кнопка в меню бота видна, только пока включено."
    )
    lines.append(
        ("✅" if staff_only else "☐")
        + " Только менеджерам — делегаты кнопку не увидят, у менеджеров есть запасной вход."
    )
    lines.append("")
    lines.append("Разделы, которые видны в приложении:")
    for key in SECTION_KEYS:
        on = await get_setting_typed(key) == "on"
        label = SETTINGS_SCHEMA[key]["label"]
        lines.append(("✅ " if on else "☐ ") + label)
    lines.append("")
    lines.append(f"🎨 Цвет акцента: <b>{html_module.escape(accent or '#037EF3')}</b>")
    lines.append("🖼 Логотип: " + ("✅ загружен" if logo else "не загружен"))
    lines.append("")
    if config.DASHBOARD_PUBLIC_URL:
        url = config.DASHBOARD_PUBLIC_URL.rstrip("/") + "/app"
        lines.append(f"Адрес приложения: {html_module.escape(url)}")
    else:
        lines.append(
            "⚠️ Адрес приложения не задан — точки входа скрыты, пока его не настроят при деплое."
        )
    return "\n".join(lines)


async def build_miniapp_settings_keyboard() -> InlineKeyboardMarkup:
    enabled = await get_setting_typed("miniapp_enabled") == "on"
    staff_only = await get_setting_typed("miniapp_staff_only") == "on"
    logo = await get_setting_typed("miniapp_logo")

    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if enabled else "☐ ") + "Mini App включён",
            callback_data="miniapp_toggle_enabled",
        )],
        [InlineKeyboardButton(
            text=("✅ " if staff_only else "☐ ") + "Только менеджерам",
            callback_data="miniapp_toggle_staff_only",
        )],
    ]
    for key in SECTION_KEYS:
        on = await get_setting_typed(key) == "on"
        label = SETTINGS_SCHEMA[key]["label"]
        suffix = key[len("miniapp_section_"):]
        buttons.append([InlineKeyboardButton(
            text=("✅ " if on else "☐ ") + label,
            callback_data=f"miniapp_section:{suffix}",
        )])
    buttons.append([InlineKeyboardButton(text="🎨 Цвет акцента", callback_data="miniapp_edit_accent")])
    buttons.append([InlineKeyboardButton(text="🖼 Логотип", callback_data="miniapp_edit_logo")])
    if logo:
        buttons.append([InlineKeyboardButton(text="🗑 Убрать логотип", callback_data="miniapp_remove_logo")])
    buttons.append([InlineKeyboardButton(text="← К настройкам", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def sync_chat_menu_button(bot) -> None:
    """Phase 19 (08, D-10/T-19-52): the ONE function that sets the chat menu button, called
    from TWO places — `main.py` at startup and `toggle_miniapp_enabled` below, right after the
    setting is written. Without a single shared function called from both, the toggle would
    only take visible effect on the NEXT bot restart, breaking the "выключение тумблера
    убирает точки входа сразу" success criterion. Fail-soft is the CALLER's job (both call
    sites wrap this in try/except) — an unreachable Telegram must never break the settings
    screen or block startup."""
    enabled = await get_setting_typed("miniapp_enabled") == "on"
    url = config.DASHBOARD_PUBLIC_URL
    if enabled and url:
        button_text = await get_setting_typed("miniapp_open_button")
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=button_text,
                web_app=WebAppInfo(url=url.rstrip("/") + "/app"),
            ),
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())


async def _rerender(callback: types.CallbackQuery):
    await callback.message.edit_text(
        await render_miniapp_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_settings_keyboard(),
    )


@router.callback_query(F.data == "admin_miniapp_settings")
async def open_miniapp_settings(callback: types.CallbackQuery, state: FSMContext):
    # Defensive clear (тот же приём, что `settings_edit_start`): заход на экран не должен
    # оставлять зависшую FSM правки акцента/лого с прошлого визита.
    await state.clear()
    await callback.message.edit_text(
        await render_miniapp_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_settings_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "miniapp_toggle_enabled")
async def toggle_miniapp_enabled(callback: types.CallbackQuery):
    current = await get_setting_typed("miniapp_enabled")
    new_val = "off" if current == "on" else "on"
    await set_setting("miniapp_enabled", new_val)
    toast = "Приложение: " + ("включено" if new_val == "on" else "выключено")
    # T-19-52: kept in sync with the toggle immediately, not only at next restart. Fail-soft —
    # an unreachable Telegram must not break this screen, only delay the chat menu button.
    try:
        await sync_chat_menu_button(callback.bot)
    except Exception:
        logger.warning("sync_chat_menu_button failed after toggle", exc_info=True)
        toast += " — кнопка меню обновится при следующем запуске"
    await callback.answer(toast)
    await _rerender(callback)


@router.callback_query(F.data == "miniapp_toggle_staff_only")
async def toggle_miniapp_staff_only(callback: types.CallbackQuery):
    current = await get_setting_typed("miniapp_staff_only")
    new_val = "off" if current == "on" else "on"
    await set_setting("miniapp_staff_only", new_val)
    await callback.answer("Только менеджерам: " + ("да" if new_val == "on" else "нет"))
    await _rerender(callback)


@router.callback_query(F.data.startswith("miniapp_section:"))
async def toggle_miniapp_section(callback: types.CallbackQuery):
    suffix = callback.data.split(":", 1)[1]
    key = _SECTION_BY_SUFFIX.get(suffix)
    if key is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return

    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    label = SETTINGS_SCHEMA[key]["label"]
    toast = f"{label}: {'показываем' if new_val == 'on' else 'скрыт'}"
    await callback.answer(toast)
    await _rerender(callback)


@router.callback_query(F.data == "miniapp_edit_accent")
async def miniapp_edit_accent_start(callback: types.CallbackQuery, state: FSMContext):
    current = await get_setting_typed("miniapp_accent")
    text = (
        "🎨 <b>Цвет акцента</b>\n\n"
        f"Сейчас: <b>{html_module.escape(current or '#037EF3')}</b>\n\n"
        "Пришлите новый цвет в формате HEX: решётка и шесть символов после неё, "
        "например <code>#037EF3</code>."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="miniapp_cancel_edit")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(MiniAppTheme.accent)
    await callback.answer()


@router.message(MiniAppTheme.accent)
async def miniapp_accent_step(message: types.Message, state: FSMContext):
    value = (message.text or "").strip()
    if not _HEX_RE.match(value):
        await message.answer(
            "Нужно шесть символов после решётки, например #037EF3. Пришлите цвет ещё раз."
        )
        return
    await set_setting("miniapp_accent", value)
    await state.set_state(None)
    await message.answer(f"Готово: цвет акцента — {value}")
    await message.answer(
        await render_miniapp_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_settings_keyboard(),
    )


@router.callback_query(F.data == "miniapp_edit_logo")
async def miniapp_edit_logo_start(callback: types.CallbackQuery, state: FSMContext):
    text = "🖼 <b>Логотип мероприятия</b>\n\nПришлите фото — оно появится в шапке приложения."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="miniapp_cancel_edit")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(MiniAppTheme.logo)
    await callback.answer()


@router.message(MiniAppTheme.logo, F.photo)
async def miniapp_logo_step(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await set_setting("miniapp_logo", file_id)
    await state.set_state(None)
    await message.answer("Логотип обновлён.")
    await message.answer(
        await render_miniapp_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_settings_keyboard(),
    )


@router.message(MiniAppTheme.logo)
async def miniapp_logo_step_invalid(message: types.Message):
    await message.answer("Не понял — пришлите фото логотипа сообщением.")


@router.callback_query(F.data == "miniapp_remove_logo")
async def miniapp_remove_logo(callback: types.CallbackQuery):
    await delete_setting("miniapp_logo")
    await callback.answer("Логотип убран")
    await _rerender(callback)


@router.callback_query(F.data == "miniapp_cancel_edit")
async def miniapp_cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.answer()
    await _rerender(callback)
