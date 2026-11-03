"""Лист ожидания: кнопка на карточке заявки и тумблер в разделе «📋 Заявки».

Своего Router() нет — хендлеры сидят на общем handlers.admin.router. Модуль
подключается хвостом handlers/settings/admin_sections.py, после остальных швов
раздела, чтобы снимок порядка хендлеров остался чистой вставкой.
"""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton

from handlers.admin import router

_BUTTON = "📋 В лист ожидания"
_OFF_ALERT = (
    "Лист ожидания выключен. Включите «📋 Лист ожидания» в разделе «📋 Заявки»."
)
_DONE = "Заявка в листе ожидания. Делегат увидит: Лист ожидания."
_ALREADY = "Заявка уже решена"


async def _enabled() -> bool:
    from database.db import get_setting
    return await get_setting("apps_waitlist_enabled") == "on"


async def waitlist_row(tid: int) -> list | None:
    """Один ряд клавиатуры карточки, либо None, когда тумблер выключен."""
    if not await _enabled():
        return None
    return [InlineKeyboardButton(text=_BUTTON, callback_data=f"appr_waitlist:{tid}")]


async def waitlist_toggle_row() -> list[list[InlineKeyboardButton]]:
    on = await _enabled()
    text = (
        "📋 Лист ожидания: ✅ Вкл → ❌ Выкл" if on
        else "📋 Лист ожидания: ❌ Выкл → ✅ Вкл"
    )
    return [[InlineKeyboardButton(text=text, callback_data="toggle_apps_waitlist")]]


@router.callback_query(F.data.startswith("appr_waitlist:"))
async def appr_waitlist(callback: types.CallbackQuery, state: FSMContext):
    from handlers.applications.admin_moderation import _parse_appr, _show_current_card
    from handlers.settings.admin_core import _OUT_OF_SCOPE_ALERT, _card_out_of_scope
    from services.applications.application_effects import apply_decision_effects
    from services.applications.applications import claim_waitlist
    from services.infra.background import spawn as _spawn

    _, tid = _parse_appr(callback.data)
    if tid is None:
        await callback.answer(_ALREADY, show_alert=True)
        return
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    if not await _enabled():
        await callback.answer(_OFF_ALERT, show_alert=True)
        return
    if not await claim_waitlist(tid):
        await callback.answer(_ALREADY, show_alert=True)
        return
    _spawn(apply_decision_effects(callback.bot, tid, "waitlist"))
    await callback.answer(_DONE, show_alert=True)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _show_current_card(callback.message, state)


@router.callback_query(F.data == "toggle_apps_waitlist")
async def toggle_apps_waitlist(callback: types.CallbackQuery):
    from handlers.settings.admin_settings import _toggle_module_setting
    await _toggle_module_setting(callback, "apps_waitlist_enabled", "📋 Лист ожидания")
