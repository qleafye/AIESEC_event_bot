"""«✅ Одобрить» на карточке /find для уже отклонённого.

Шов той же формы, что «↩️ Вернуть в ожидание»: общего Router() нет, хендлеры
декорируют admin.router, модуль импортируется хвостом handlers/admin.py.
Письмо — то же, что обычное одобрение (`apply_decision_effects`); шаг оплаты
откроется только если модуль оплаты включён."""
from __future__ import annotations

import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_user
from domain.cities import normalize_city
from handlers.admin import router
from handlers.forum.admin_checkin import _city_allowed
from services.applications.approve_rejected import approve_rejected, preview_approve_rejected

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN = "Этот город вне вашей зоны ответственности."
_NOT_FOUND = "Делегат не найден — возможно, карточка устарела."
_NOT_REJECTED = "Одобрить можно только отклонённую заявку."


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() else None


async def _render_confirm(tid: int) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await get_user(tid)
    if user is None:
        return None
    preview = await preview_approve_rejected(tid)
    if not preview.get("ok"):
        return None

    name = html_module.escape(str(user.get("full_name") or "-"))
    username = html_module.escape(str(user.get("username") or "-"))
    if preview.get("payment_enabled"):
        payment_line = (
            "Делегату уйдёт то же письмо, что после обычного одобрения, и откроется шаг оплаты."
        )
    else:
        payment_line = (
            "Делегату уйдёт то же письмо, что после обычного одобрения. "
            "Шаг оплаты не откроется — оплата выключена."
        )
    text = (
        "✅ <b>Одобрить отклонённую заявку?</b>\n\n"
        f"{name} ({username})\n"
        "Статус: ❌ Отклонена → ✅ Одобрена\n\n"
        f"{payment_line} QR-пропуск начнёт пускать."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"apprej_apply:{tid}")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"apprej_cancel:{tid}")],
    ])
    return text, kb


@router.callback_query(F.data.startswith("apprej_start:"))
async def apprej_start(callback: types.CallbackQuery):
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN, show_alert=True)
        return
    rendered = await _render_confirm(tid)
    if rendered is None:
        await callback.answer(_NOT_REJECTED, show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("apprej_apply:"))
async def apprej_apply(callback: types.CallbackQuery):
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN, show_alert=True)
        return

    report = await approve_rejected(tid, by_admin=callback.from_user.id)
    if not report.get("ok"):
        await callback.message.edit_text(
            f"❌ Не одобрил(а): {html_module.escape(str(report.get('error') or '-'))}",
        )
        await callback.answer()
        return

    from services.applications.application_effects import apply_decision_effects

    try:
        await apply_decision_effects(callback.bot, tid, "approved")
    except Exception:
        logger.exception("apprej_apply: письмо одобрения %s не ушло", tid)
    name = html_module.escape(str(user.get("full_name") or "-"))
    lines = [f"✅ <b>{name}</b> одобрен(а)."]
    if report.get("payment_enabled"):
        lines.append("Ушло обычное письмо, откроется шаг оплаты.")
    else:
        lines.append("Ушло обычное письмо. Шаг оплаты не открывался — оплата выключена.")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("apprej_cancel:"))
async def apprej_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("✖️ Одобрение отменено.")
    await callback.answer()
