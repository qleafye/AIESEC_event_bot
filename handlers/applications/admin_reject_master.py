"""Включённое правило при выключенном общем рубильнике автоотказа.

Менеджер включает правило, а все правила выключены разом (`reject_rules_enabled`): сервис гасит
всё первой же строкой, правило не срабатывает. Раньше бот при этом говорил «теперь действует»,
а предупреждение было только в шапке списка, куда с карточки не возвращаются. Теперь строку
видно в карточке включённого правила, на экране «Включить правило?» и во всплывающем окне после
включения. Тому, кто управляет рубильником (суперадмин или менеджер без привязки к городу — то
же право, что у кнопки в списке правил), карточка даёт кнопку «✅ Включить все правила», и после
неё он остаётся на карточке. Менеджеру с городом — кто может включить, без кнопки.

Шов к общему `admin.router`, импортируется из `handlers/applications/admin_reject_rules.py`.
"""
from __future__ import annotations

from aiogram import F, types
from aiogram.types import InlineKeyboardButton

from domain.settings.schema import get_setting_typed
from handlers.admin import router
from services.applications.reject_rules import can_edit_city
from services.settings.audit import set_setting_by_admin

# INVARIANT (13-01 cap-test): каждый `@router.*` декоратор ниже — в ОДНУ строку.

OFF_LINE = "⚠️ Правило пока не работает: все правила выключены разом."
GATE_LINE = "⚠️ Сейчас все правила выключены разом — это правило не заработает, пока их не включат."
BOUND_LINE = "Включить их может руководитель без привязки к городу."
BUTTON_TEXT = "✅ Включить все правила"
ON_PREFIX = "arr_master_on:"
ENABLED_TEXT = "✅ Правило включено и теперь действует на подходящие заявки."
DENIED_TEXT = "Включить правила всех городов может только руководитель без привязки к городу."


async def kill_switch_off() -> bool:
    return not await get_setting_typed("reject_rules_enabled")


async def card_warning(admin_id: int, rule_id: int, rule: dict) -> tuple[list[str], list[list[InlineKeyboardButton]]]:
    """Строки под статусом карточки и кнопка рубильника — только у включённого правила."""
    if not rule.get("enabled") or rule.get("paused_reason") or not await kill_switch_off():
        return [], []
    if await can_edit_city(admin_id, None):
        return [OFF_LINE], [[InlineKeyboardButton(text=BUTTON_TEXT, callback_data=f"{ON_PREFIX}{rule_id}")]]
    return [OFF_LINE, BOUND_LINE], []


async def gate_warning(admin_id: int) -> list[str]:
    """Строки для экрана «Включить правило?» — до нажатия «Всё равно включить»."""
    if not await kill_switch_off():
        return []
    lines = ["", GATE_LINE]
    if not await can_edit_city(admin_id, None):
        lines.append(BOUND_LINE)
    return lines


async def enabled_alert(admin_id: int) -> str:
    """Всплывающее окно после включения правила (лимит Telegram — 200 символов)."""
    if not await kill_switch_off():
        return ENABLED_TEXT
    tail = (f"Включить их — кнопка «{BUTTON_TEXT}» в карточке." if await can_edit_city(admin_id, None)
            else BOUND_LINE)
    return f"✅ Правило включено.\n\n{OFF_LINE}\n{tail}"


@router.callback_query(F.data.startswith("arr_master_on:"))
async def arr_master_on(callback: types.CallbackQuery):
    """Только включает (старая кнопка не выключит правила), без подтверждения — как включение
    в списке правил; потом перерисовывает ту же карточку."""
    from handlers.applications.admin_reject_rules import render_rule_card  # цикл импорта
    if not await can_edit_city(callback.from_user.id, None):
        await callback.answer(DENIED_TEXT, show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, "reject_rules_enabled", "on")
    try:
        rule_id = int(callback.data[len(ON_PREFIX):])
    except ValueError:
        rule_id = None
    screen = await render_rule_card(callback.from_user.id, rule_id) if rule_id is not None else None
    if screen is not None:
        text, kb = screen
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("✅ Правила снова работают.", show_alert=True)
