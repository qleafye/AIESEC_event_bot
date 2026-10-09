"""«📨 Отправить решение заново» — кнопка на карточке `/find` (`handlers/admin.py::
cmd_find_user`) для одобренной или отклонённой заявки. Сама отправка — `services/
decision_delivery.py::resend_one_decision` (тот же путь и те же тексты, что у массовой «📨
Переотправить решения» в сверке таблицы).

Шов той же формы, что `admin_revert_pending.py`: своего `Router()` нет, хендлеры декорируют
ОБЩИЙ `admin.router`, модуль импортируется хвостом `handlers/admin.py`. Право — `moderate_reg`
(`decresend_*` в `handlers/admin_caps.py`) плюс проверка города менеджера."""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import normalize_city
from database.db import get_user
from handlers.admin import router
from handlers.admin_checkin import _city_allowed
from services import decision_delivery

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."
_NO_DECISION_ALERT = "По заявке ещё нет решения — отправлять нечего."


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


async def _checked_user(callback: types.CallbackQuery, raw_tid: str):
    """Общая проверка трёх хендлеров: id разобрался, делегат есть, город менеджеру доступен.
    `None` — ответ уже отправлен."""
    tid = _parse_tid(raw_tid)
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return None
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return None
    if not await _city_allowed(callback.from_user.id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return None
    return user


@router.callback_query(F.data.startswith("decresend_start:"))
async def decresend_start(callback: types.CallbackQuery):
    user = await _checked_user(callback, callback.data.split(":", 1)[1])
    if user is None:
        return
    if user.get("status") not in decision_delivery.DECIDED_STATUSES:
        await callback.answer(_NO_DECISION_ALERT, show_alert=True)
        return
    tid = user["telegram_id"]
    name = html_module.escape(str(user.get("full_name") or "-"))
    preview = html_module.escape(await decision_delivery.preview_decision_text(user))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data=f"decresend_go:{tid}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"decresend_cancel:{tid}")],
    ])
    await callback.message.edit_text(
        f"📨 Отправить <b>{name}</b> письмо о решении ещё раз?\n\n"
        f"Он(а) получит: «{preview}»",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("decresend_go:"))
async def decresend_go(callback: types.CallbackQuery):
    user = await _checked_user(callback, callback.data.split(":", 1)[1])
    if user is None:
        return
    result = await decision_delivery.resend_one_decision(callback.bot, user["telegram_id"])
    if not result.get("ok"):
        await callback.answer(str(result.get("error") or "не удалось отправить"), show_alert=True)
        return
    if result.get("delivered"):
        text = "✅ Доставлено"
    elif result.get("queued"):
        text = "⏳ У делегата сейчас тихие часы — письмо уйдёт утром."
    else:
        reason = html_module.escape(str(result.get("error") or "не доставлено"))
        text = f"❌ Не доставлено: {reason} — свяжитесь с делегатом напрямую"
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("decresend_cancel:"))
async def decresend_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отправка письма отменена.")
    await callback.answer()
