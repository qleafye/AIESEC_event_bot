"""Phase 15 (15-02, D-19) — шов admin_settings: экран «📊 Дашборд», тумблеры блоков.

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_settings.py`, ПОСЛЕ admin_settings_lists (тот сам
на потолке размера — см. tests/test_module_size_convention_260816.py), как и остальные швы
Phase 13.

Что здесь: экран с восемью независимыми чекбоксами блоков веб-дашборда (ключи —
`dashboard_block_*` в SETTINGS_SCHEMA, группа "dashboard", вне SETTINGS_FIELDS — у них своя
поверхность правки, см. соседний коммит этого плана). Вкл/выкл самого дашборда и ключи API на
этом экране сознательно ОТСУТСТВУЮТ (D-19) — адрес дашборда задаётся при деплое
(`config.DASHBOARD_PUBLIC_URL`), название в шапке берётся из существующего `event_name`.
"""
from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import set_setting
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers.admin import router

# Порядок блоков на странице дашборда (D-14): воронка -> динамика ->
# города/источники/вузы/курсы/направления -> где бросают -> гейма.
DASHBOARD_BLOCKS = [
    "dashboard_block_funnel",
    "dashboard_block_dynamics",
    "dashboard_block_universities",
    "dashboard_block_sources",
    "dashboard_block_courses",
    "dashboard_block_study_fields",
    "dashboard_block_dropout",
    "dashboard_block_game",
]


def _suffix(key: str) -> str:
    return key[len("dashboard_block_"):]


_BLOCK_BY_SUFFIX = {_suffix(key): key for key in DASHBOARD_BLOCKS}


async def render_dashboard_settings_text() -> str:
    return (
        "📊 <b>Дашборд</b>\n\n"
        "Отметьте, какие блоки показывать на веб-дашборде. Выключенный блок просто не "
        "рисуется на странице — данные при этом никуда не пропадают.\n\n"
        "Название дашборда в шапке берётся из «Название события». Адрес дашборда "
        "задаётся при деплое стенда, здесь его не меняем."
    )


async def build_dashboard_settings_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for key in DASHBOARD_BLOCKS:
        on = await get_setting_typed(key) == "on"
        label = SETTINGS_SCHEMA[key]["label"]
        buttons.append([InlineKeyboardButton(
            text=("✅ " if on else "☐ ") + label,
            callback_data=f"dash_block:{_suffix(key)}",
        )])
    # Phase 20 (20-03): «Назад» ведёт в раздел-владелец экрана («📊 Данные»), а не в общий
    # корень настроек. Цель выводится из SECTIONS — второй карты «экран -> раздел» нет.
    from handlers.admin_sections import back_button  # ленивый шов: модульный импорт даст цикл
    buttons.append([back_button("admin_dashboard_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_dashboard_settings")
async def open_dashboard_settings(callback: types.CallbackQuery):
    await callback.message.edit_text(
        await render_dashboard_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_dashboard_settings_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dash_block:"))
async def toggle_dashboard_block(callback: types.CallbackQuery):
    suffix = callback.data.split(":", 1)[1]
    key = _BLOCK_BY_SUFFIX.get(suffix)
    if key is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return

    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    label = SETTINGS_SCHEMA[key]["label"]
    toast = f"{label}: {'показываем' if new_val == 'on' else 'скрыта'}"
    await callback.answer(toast)

    await callback.message.edit_text(
        await render_dashboard_settings_text(),
        parse_mode="HTML",
        reply_markup=await build_dashboard_settings_keyboard(),
    )
