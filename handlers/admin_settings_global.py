"""Общая настройка, открытая при городе в шапке: новое значение получат все города.

Менеджер выбрал в шапке «🔧 Управление» свой город и открыл настройку, у которой нет значения
«для города» (подпись кнопки приложения, «Не присылать сегодня», оффер своей ссылки…). Раньше
редактор сразу ждал ввода, и первое же сообщение меняло текст всем городам. Теперь экран правки
говорит, что настройка общая, а ввод начинается только с кнопки «✏️ Изменить для всех городов».

Вариант-кнопки (enum) и списки пишут значение своими кнопками, ввода у них нет — им только строка
предупреждения. Тот же приём, что у городских настроек: ввод начинает только явная кнопка
(`settings_edit_city`), случайное сообщение при простом просмотре экрана ничего не меняет.

Шов к общему `admin.router`, импортируется хвостом `handlers/admin_settings.py`.
"""
from __future__ import annotations

import html

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import ALL_CITIES, cities_module_on, city_label, is_per_city
from domain.settings.ops import COMMON_DENIED_TEXT, can_write_common, writes_common_value
from handlers.admin import router
from handlers.states import EditSetting
from domain.settings.schema import SETTINGS_SCHEMA

# INVARIANT (13-01 cap-test): каждый `@router.*` декоратор ниже — в ОДНУ строку.

CONFIRM_PREFIX = "settings_edit_all:"
CONFIRM_BUTTON_TEXT = "✏️ Изменить для всех городов"
INPUT_PROMPT = "Пришлите новое значение одним сообщением — его получат все города."


async def is_global_in_city_context(key: str, header_code: str | None) -> bool:
    return bool(
        header_code and header_code != ALL_CITIES and key in SETTINGS_SCHEMA
        and not is_per_city(key) and await cities_module_on()
    )


async def common_write_denied(admin_id: int, key: str) -> bool:
    """Запись `key` меняет общее значение, а писать общее этому админу нельзя (привязан к
    городу). Проверяется на ЗАПИСИ (`settings_edit_value`, списки, кнопка «для всех городов»),
    а не только скрытием кнопки: старая клавиатура в чате живёт вечно."""
    return writes_common_value(key) and not await can_write_common(admin_id)


async def deny(callback_or_message, key: str) -> bool:
    if not await common_write_denied(callback_or_message.from_user.id, key):
        return False
    if hasattr(callback_or_message, "data"):  # нажатие кнопки — всплывающее окно
        await callback_or_message.answer(COMMON_DENIED_TEXT, show_alert=True)
    else:
        await callback_or_message.answer(COMMON_DENIED_TEXT)
    return True


def needs_confirm(key: str) -> bool:
    """Ввод текстом — только через кнопку; enum и списки пишут своими кнопками."""
    return SETTINGS_SCHEMA[key].get("type") not in ("enum", "list")


_INPUT_HINT = "Пришлите новое значение сообщением."


async def warn_screen(key: str, header_code: str, text: str, kb: InlineKeyboardMarkup,
                      denied: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Пометку «Общая настройка (одна на все города)» экран правки уже ставит сам
    (`admin_settings._settings_edit_screen`); здесь — вход в ввод только через кнопку, а
    привязанному к городу — объяснение вместо кнопки."""
    if denied:
        text = text.replace(_INPUT_HINT, "")
        return text + f"\n\n🔒 {html.escape(COMMON_DENIED_TEXT)}", kb
    if needs_confirm(key):
        city = html.escape(await city_label(header_code))
        text = text.replace(_INPUT_HINT, f"Нажмите «{CONFIRM_BUTTON_TEXT}» и пришлите новое значение.")
        text += f"\n\n⚠️ <b>Новое значение получат все города</b>, не только «{city}»."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=CONFIRM_BUTTON_TEXT, callback_data=f"{CONFIRM_PREFIX}{key}")],
            *kb.inline_keyboard,
        ])
    return text, kb


@router.callback_query(F.data.startswith("settings_edit_all:"))
async def settings_edit_all(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data[len(CONFIRM_PREFIX):]
    if key not in SETTINGS_SCHEMA or is_per_city(key) or not needs_confirm(key):
        await callback.answer("Эта кнопка устарела — откройте настройку заново.", show_alert=True)
        return
    if await deny(callback, key):
        return
    await state.clear()
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": key})
    cancel = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]])
    await callback.message.answer(INPUT_PROMPT, reply_markup=cancel)
    await callback.answer()
