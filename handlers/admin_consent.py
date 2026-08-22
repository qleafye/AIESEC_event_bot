"""Quick 260822 — шов admin_settings: версия согласия и напоминание о целях обработки.

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`) и импортируется из
ХВОСТА `handlers/admin_settings.py` (тот сам на потолке размера — см.
tests/test_module_size_convention_260816.py), как и остальные швы Phase 13.

Что здесь:
- тумблер «🔁 Просить пересогласие при новой редакции» на экране «📋 Согласия»
  (строка статуса + кнопка — их admin_settings подмешивает в рендер группы `consent`);
- напоминание менеджеру «цели обработки изменились — перечитайте согласие» с кнопкой
  «📋 Открыть согласия»: после смены пресета типа события и после ВКЛЮЧЕНИЯ модулей,
  расширяющих обработку (оплата — чеки и реквизиты; резюме — Nextcloud). Выключение
  ничего не расширяет — напоминания нет.
"""
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import set_setting
from settings_schema import get_setting_typed
from services.consent import (
    purpose_reminder_text, PURPOSE_REMINDER_BUTTON, PURPOSE_REMINDER_CALLBACK,
)
from handlers.admin import router
from handlers.admin_settings import render_settings_group_text, build_settings_group_keyboard

logger = logging.getLogger(__name__)

RECOLLECT_LABEL = "🔁 Просить пересогласие при новой редакции"

# Ключ -> человеческое «что изменилось» для напоминания. Только расширяющие обработку
# модули; consent_enabled/party_*/preselect/nudge данных третьим лицам не добавляют.
WIDENING_KEYS = {
    "payment_enabled": "включена оплата: чеки и реквизиты",
    "reg_q_resume": "включён сбор резюме, файлы уходят в Nextcloud",
}


def purpose_reminder_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=PURPOSE_REMINDER_BUTTON, callback_data=PURPOSE_REMINDER_CALLBACK)
    ]])


async def _send_reminder(message: types.Message, what: str | None) -> None:
    # Fail-soft: напоминание — подсказка, оно не должно ронять сам тумблер/пресет.
    try:
        await message.answer(purpose_reminder_text(what), reply_markup=purpose_reminder_kb())
    except Exception as e:
        logger.error(f"consent purpose reminder not sent: {e}")


async def remind_consent_purposes_if_widened(message: types.Message, key: str, new_val: str) -> bool:
    """После тумблера: напоминание только при ВКЛЮЧЕНИИ расширяющего модуля. True = отправлено."""
    if key not in WIDENING_KEYS or new_val != "on":
        return False
    await _send_reminder(message, WIDENING_KEYS[key])
    return True


async def remind_consent_purposes_after_preset(message: types.Message, preset_label: str) -> None:
    """После смены типа события (пресет forum/conference/… или правка event_type)."""
    await _send_reminder(message, f"тип события: {preset_label}")


# ── экран «📋 Согласия»: строка статуса + тумблер пересогласия ──────────────────────────

async def consent_group_extra_lines() -> list[str]:
    on = await get_setting_typed("consent_recollect_enabled") == "on"
    return [f"{RECOLLECT_LABEL}: <b>{'✅ Вкл' if on else '❌ Выкл'}</b>"]


def consent_group_extra_buttons() -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(text=RECOLLECT_LABEL, callback_data="toggle_consent_recollect")]]


@router.callback_query(F.data == "toggle_consent_recollect")
async def toggle_consent_recollect(callback: types.CallbackQuery):
    current = await get_setting_typed("consent_recollect_enabled")
    new_val = "off" if current == "on" else "on"
    await set_setting("consent_recollect_enabled", new_val)
    if new_val == "on":
        note = (
            "✅ Вкл. Делегаты, подписавшие старую редакцию, при следующем /start "
            "увидят согласие ещё раз."
        )
    else:
        note = "❌ Выкл. Повторно согласие никого просить не будем."
    await callback.answer(note, show_alert=True)
    text = await render_settings_group_text("consent", callback.from_user.id)
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=await build_settings_group_keyboard("consent", callback.from_user.id),
    )
