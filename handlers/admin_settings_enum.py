"""Enum-настройки в общем редакторе — кнопками с человеческими подписями (правило «бот для
людей»): код варианта менеджеру не показываем и ввести не просим.

Кнопка ведёт тем же путём, что ввод текстом (`admin_settings.settings_edit_value`): проверка,
подтверждение опасных значений, хуки и запись — значение подставляется как текст сообщения от
имени нажавшего. Куда писать, решает FSM: общий экран кладёт туда общий ключ, «✏️ Изменить для
…» (`settings_edit_city`) — ключ города, так что та же кнопка пишет в свою область.

Шов к общему `admin.router`, импортируется хвостом `handlers/admin.py`.
"""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton

from handlers import admin_settings
from handlers.admin import router
from settings_schema import SETTINGS_SCHEMA, option_label


def enum_options(key: str) -> list[str]:
    entry = SETTINGS_SCHEMA.get(admin_settings._base_setting_key(key or ""), {})
    return list(entry.get("options") or []) if entry.get("type") == "enum" else []


ENUM_HINT = "\n\n<i>Выберите вариант кнопкой ниже.</i>"
# Тумблеры on/off в реестре без option_labels — без этой подписи кнопки и «Сейчас задано»
# показывали бы сырой код.
_ON_OFF = {"on": "Включено", "off": "Выключено"}


def enum_label(key: str, code: str) -> str:
    """Человеческая подпись варианта enum-ключа (общего или своего у города); не enum — как есть."""
    label = option_label(key, code)
    return _ON_OFF.get(code, label) if label == code and enum_options(key) else label


def enum_rows(key: str, current: str | None) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(
            text=("✅ " if opt == current else "") + enum_label(key, opt),
            callback_data=f"settings_enum_pick:{i}",
        )]
        for i, opt in enumerate(enum_options(key))
    ]


@router.callback_query(F.data.startswith("settings_enum_pick:"))
async def settings_enum_pick(callback: types.CallbackQuery, state: FSMContext):
    key = (await state.get_data()).get("setting_key")
    options = enum_options(key)
    try:
        value = options[int(callback.data.split(":", 1)[1])]
    except (ValueError, IndexError):
        value = None
    if not key or value is None:
        await callback.answer("Экран устарел — откройте настройку заново", show_alert=True)
        return
    await callback.answer()
    proxy = callback.message.model_copy(update={
        "text": value, "entities": None, "from_user": callback.from_user,
    }).as_(callback.bot)
    await admin_settings.settings_edit_value(proxy, state)
