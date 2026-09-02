"""Quick 260902-vth — шов admin_sections: экран «🕓 Журналы в таблицу» (раздел «📊 Данные»).

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_sections.py` (не `admin_settings.py` — тот стоит
ровно на потолке размера, `tests/test_module_size_convention_260816.py`).

Два новых листа в ТОЙ ЖЕ Google-таблице заявок — «История правок» и «Вопросы» — пересобираются
целиком из БД (`services/sheet_logs.py`), не append по событию. Названия листов правятся общим
`settings_edit:{key}` (у `SETTINGS_SCHEMA["history_sheet_tab"|"questions_sheet_tab"]` есть
`prompt`, в `handlers.admin_settings.SETTINGS_FIELDS`/`_SHEETS_FIELD_ORDER` ключу быть не
обязано — тот список тоже живёт в файле на потолке и пополнить его нельзя). Известный нюанс
UX: после сохранения `_group_of_setting_key` вернёт `None` (ключа нет в `SETTINGS_GROUPS`),
и менеджер приземлится на корень `/admin`, а не обратно на этот экран — не тупик (корень —
список разделов, возврат в два нажатия), не повод трогать файл на потолке.
"""
from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import set_setting
from settings_schema import get_setting_typed, SETTINGS_SCHEMA
from services.sheet_logs import sync_sheet_logs
from handlers.admin import router


async def _tab_name(key: str) -> str:
    return (await get_setting_typed(key) or "").strip() or SETTINGS_SCHEMA[key]["default"]


async def render_sheet_logs_text() -> str:
    history_tab = await _tab_name("history_sheet_tab")
    questions_tab = await _tab_name("questions_sheet_tab")
    autosync_on = await get_setting_typed("sheet_logs_autosync") == "on"
    state_word = "включено" if autosync_on else "выключено"
    return (
        "🕓 <b>Журналы в таблицу</b>\n\n"
        "Два листа в ТОЙ ЖЕ таблице заявок: кто что менял в уже поданной анкете и что "
        "спрашивают делегаты. Лист пересобирается целиком, поэтому дубли не копятся.\n\n"
        f"🕓 История правок: «{history_tab}»\n"
        f"❓ Вопросы делегатов: «{questions_tab}»\n\n"
        f"Автообновление после каждой правки/вопроса: {state_word}."
    )


def build_sheet_logs_keyboard(autosync_on: bool) -> InlineKeyboardMarkup:
    from handlers.admin_sections import back_button  # ленивый шов (см. докстринг модуля)

    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if autosync_on else "☐ ") + "Обновлять журналы автоматически",
            callback_data="sheet_logs_autosync_toggle",
        )],
        [InlineKeyboardButton(text="🔄 Обновить оба листа сейчас", callback_data="sheet_logs_sync_go")],
        [InlineKeyboardButton(text="✏️ 🕓 История правок", callback_data="settings_edit:history_sheet_tab")],
        [InlineKeyboardButton(text="✏️ ❓ Вопросы делегатов", callback_data="settings_edit:questions_sheet_tab")],
        [back_button("sheet_logs_open")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_sheet_logs(callback: types.CallbackQuery) -> None:
    autosync_on = await get_setting_typed("sheet_logs_autosync") == "on"
    await callback.message.edit_text(
        await render_sheet_logs_text(),
        parse_mode="HTML",
        reply_markup=build_sheet_logs_keyboard(autosync_on),
    )


@router.callback_query(F.data == "sheet_logs_open")
async def sheet_logs_open(callback: types.CallbackQuery):
    await _show_sheet_logs(callback)
    await callback.answer()


@router.callback_query(F.data == "sheet_logs_autosync_toggle")
async def sheet_logs_autosync_toggle(callback: types.CallbackQuery):
    autosync_on = await get_setting_typed("sheet_logs_autosync") == "on"
    new_value = "off" if autosync_on else "on"
    await set_setting("sheet_logs_autosync", new_value)
    toast = "Автообновление журналов: включено" if new_value == "on" else "Автообновление журналов: выключено"
    await callback.answer(toast)
    await _show_sheet_logs(callback)


@router.callback_query(F.data == "sheet_logs_sync_go")
async def sheet_logs_sync_go(callback: types.CallbackQuery):
    from handlers.admin_sections import op_return_keyboard  # ленивый шов (см. докстринг модуля)

    await callback.answer("🔄 Обновляю…")
    history_n, questions_n = await sync_sheet_logs()
    history_tab = await _tab_name("history_sheet_tab")
    questions_tab = await _tab_name("questions_sheet_tab")
    if history_n < 0 or questions_n < 0:
        text = "⚠️ Не удалось записать в таблицу (проверьте доступ к Google Sheets)."
    else:
        text = (
            f"✅ Готово.\n«{history_tab}»: {history_n} строк.\n«{questions_tab}»: {questions_n} строк."
        )
    await callback.message.edit_text(
        text, reply_markup=await op_return_keyboard(callback.from_user.id, "sheet_logs_open"),
    )
