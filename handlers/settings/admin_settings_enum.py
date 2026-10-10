"""Enum-настройки в общем редакторе — кнопками с человеческими подписями (правило «бот для
людей»): код варианта менеджеру не показываем и ввести не просим.

Кнопка ведёт тем же путём, что ввод текстом (`admin_settings.settings_edit_value`): проверка,
подтверждение опасных значений, хуки и запись — значение подставляется как текст сообщения от
имени нажавшего. Куда писать, решает FSM: общий экран кладёт туда общий ключ, «✏️ Изменить для
…» (`settings_edit_city`) — ключ города, так что та же кнопка пишет в свою область.

Шов к общему `admin.router`, импортируется хвостом `handlers/admin.py`.
"""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton

from database.db import get_setting
from handlers.settings import admin_settings
from handlers.admin import router
from domain.settings.schema import SETTINGS_SCHEMA, option_label
from domain.settings.validation import ON_OFF_LABELS


def enum_options(key: str) -> list[str]:
    entry = SETTINGS_SCHEMA.get(admin_settings._base_setting_key(key or ""), {})
    return list(entry.get("options") or []) if entry.get("type") == "enum" else []


ENUM_HINT = "\n\n<i>Выберите вариант кнопкой ниже.</i>"
# Тумблеры on/off в реестре без option_labels — без этой подписи кнопки и «Сейчас задано»
# показывали бы сырой код. Те же слова валидатор принимает текстом.
_ON_OFF = ON_OFF_LABELS


def enum_label(key: str, code: str) -> str:
    """Человеческая подпись варианта enum-ключа (общего или своего у города); не enum — как есть."""
    label = option_label(key, code)
    return _ON_OFF.get(code, label) if label == code and enum_options(key) else label


def _now_value(key: str, value: str | None) -> str:
    """HTML: подпись значения; не задано — подпись дефолта реестра «(по умолчанию)», а если
    дефолта нет — «не выбрано» (менеджер видит, что действует сейчас)."""
    if value:
        return f"<b>{html_module.escape(enum_label(key, value))}</b>"
    dflt = SETTINGS_SCHEMA.get(admin_settings._base_setting_key(key), {}).get("default")
    if dflt is None or dflt == "":
        return "<i>не выбрано</i>"
    return f"<b>{html_module.escape(enum_label(key, str(dflt)))}</b> (по умолчанию)"


def enum_now_line(key: str, current: str | None) -> str:
    """«Сейчас: …» над кнопками enum на общем экране (обе шапки)."""
    return f"Сейчас: {_now_value(key, current)}"


async def city_now_line(key: str, current: str | None) -> str:
    """Строка «Сейчас у города» на экране «✏️ Изменить для …». У enum без своего значения —
    что действует («как везде» = общее значение или дефолт); у остальных — как раньше."""
    if enum_options(key):
        if current:
            return f"Сейчас у города: {_now_value(key, current)}"
        return f"Сейчас у города: как везде — {_now_value(key, await get_setting(key))}"
    if current:
        return f"Сейчас у города:\n<b>{html_module.escape(current)}</b>"
    from handlers.forum.admin_forum_date import is_city_only_key
    return f"Сейчас у города: <i>{'даты нет' if is_city_only_key(key) else 'как везде'}</i>"


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
