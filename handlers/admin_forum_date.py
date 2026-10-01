"""Дата и длина форума — всегда для конкретного города.

Форумные джобы (QR накануне, утренний повтор, шпаргалка волонтёрам, опрос неявившихся, отчёт
дня, меню дня форума, SOS) читают ТОЛЬКО свою дату города (`services.reject_rules.forum_date_for`).
Раньше «🗓 Задать дату форума» с шапкой «🌍 Все города» молча писала общий `forum_date`, и его
наследовал каждый город без своей даты — Москве уходил QR «Завтра форум!» за чужой
региональный форум. Поэтому:

- с шапкой «🌍 Все города» правка даты не начинается сразу, а сначала просит выбрать город
  кнопками (рядом видно, у кого какая дата уже стоит);
- кнопки светофора «🚦 Готовность» несут город светофора в callback
  (`settings_edit_city:<ключ>@<код>`), а не полагаются на шапку админки: светофор Тюмени при
  шапке «СПб» правит Тюмень.

Выбор города переключает шапку админки на этот город: проверка права при сохранении
(`handlers/admin_settings.settings_edit_value`) сверяет город правки с шапкой, а менеджер видит,
в каком городе он теперь работает. Своих хендлеров у модуля нет: кнопки ведут в
`handlers/admin_settings.settings_edit_city` — то же право «Настройки», что у экрана настройки."""
from __future__ import annotations

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import (
    admin_selected_city,
    cities_module_on,
    city_codes,
    city_label,
    enabled_cities,
    set_admin_city,
)
from services.reject_rules import forum_date_for
from settings_ops import per_city_visible_codes

# Ключи, которые светофор и выбор города правят «для города»: оба per_city в реестре.
CITY_FORUM_KEYS = ("forum_date", "sos_active_days")


def city_edit_callback(key: str, code: str) -> str:
    """Кнопка правки `key` для города `code` — тот же `settings_edit_city`, что у экрана
    настройки (и то же право «Настройки»), но с городом в самой кнопке."""
    return f"settings_edit_city:{key}@{code}"


async def forum_date_city_picker(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «для какого города дата?» — по кнопке на каждый включённый город, который этот
    админ вправе править, с уже заданной датой рядом."""
    visible = set(await per_city_visible_codes(admin_id))
    rows: list[list[InlineKeyboardButton]] = []
    for c in await enabled_cities():
        code = c["code"]
        if code not in visible:
            continue
        label = await city_label(code)
        current = await forum_date_for(code)
        rows.append([InlineKeyboardButton(
            text=f"🏙 {label} — {current or 'дата не задана'}",
            callback_data=city_edit_callback("forum_date", code),
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")])
    text = (
        "🗓 <b>Дата форума — для какого города?</b>\n\n"
        "У каждого города своя дата: по ней городу уходят QR накануне, утренний повтор, "
        "шпаргалка волонтёрам, отчёт и опрос после форума. Город без своей даты ничего этого "
        "не получит — общей даты «для всех» нет, чтобы QR не ушёл Москве за чужой форум.\n\n"
        "Выберите город — шапка админки переключится на него."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_forum_date_city_picker(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # ни один ввод не должен лечь в общий ключ, пока город не выбран
    text, kb = await forum_date_city_picker(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def switch_header_to_button_city(callback: types.CallbackQuery, raw: str) -> str | None:
    """`settings_edit_city:<ключ>@<код>` — кнопка несёт город. Переключает шапку админки на этот
    город (проверка права при сохранении сверяет город правки с шапкой) и возвращает ключ;
    `None` — отказ уже показан алертом."""
    key, _, code = raw.partition("@")
    admin_id = callback.from_user.id
    if key not in CITY_FORUM_KEYS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return None
    if not await cities_module_on():  # светофор без городов шлёт обычный settings_edit:<ключ>
        await callback.answer("Города выключены", show_alert=True)
        return None
    if code not in city_codes() or code not in await per_city_visible_codes(admin_id):
        await callback.answer("Этот город правит суперадмин.", show_alert=True)
        return None
    if await admin_selected_city(admin_id) != code and not await set_admin_city(admin_id, code):
        await callback.answer("Не получилось переключиться на этот город.", show_alert=True)
        return None
    return key
