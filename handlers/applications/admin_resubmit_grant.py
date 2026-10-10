"""Phase 33 (delegate-card admin actions, задача 2): «🔁 Разрешить повторную подачу» — кнопка
на карточке `/find` для отклонённого делегата (`handlers/admin.py::cmd_find_user`). Сам
примитив исключения — `services/applications/delegate_overrides.py`, единая точка правды для обоих
персональных гейтов фазы (эта задача — `kind="resubmit"`, соседняя задача 3 — `kind="edit"`).

Не форумная функция (админ-действие модератора) — своей строки в хабе «🎪 Форум: функции» нет
и не нужно, тот же посыл, что у `handlers/cities/admin_city_move.py`/`admin_revert_pending.py`.

Шов той же формы, что соседние: своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`admin.router`, модуль импортируется ХВОСТОМ `handlers/admin.py` (golden snapshot: чистая
вставка). `_city_allowed` — импорт из `handlers/forum/admin_checkin.py` (не владеем файлом, только
вызываем).

Экран подтверждения честно объясняет судьбу старой анкеты/строки листа (проверено по
`services/registration/reg_finalize.py::_finalize_data_impl`, ветка resubmit, mode="edit"): делегат
проходит СВОИМ обычным /start → «Заполнить заново», финал ищет старую строку листа по ID
(`update_row_by_id`) и правит на месте — новая строка добавляется, только если старую не
нашли, дубля в норме не будет."""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import normalize_city
from database.db import get_user
from handlers.admin import router
from handlers.forum.admin_checkin import _city_allowed
from services.applications import delegate_overrides

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."
_NOT_REJECTED_ALERT = "Заявка не отклонена — разрешать повторную подачу не с чего."


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


async def _render_confirm(tid: int, notify: bool) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await get_user(tid)
    if user is None or user.get("status") != "rejected":
        return None
    existing = await delegate_overrides.active_override(tid, delegate_overrides.KIND_RESUBMIT)
    if existing is not None:
        return None

    name = html_module.escape(str(user.get("full_name") or "-"))
    username = html_module.escape(str(user.get("username") or "-"))
    lines = [
        "🔁 <b>Разрешить повторную подачу?</b>\n",
        f"{name} ({username})",
        "",
        "Что изменится: делегат сможет ещё раз пройти /start и заполнить анкету заново, даже "
        "если общий переключатель «Повторная подача после отказа» стоит на «нельзя». "
        "Разрешение одноразовое — погаснет сразу после того, как делегат подаст анкету.",
        "Старая анкета и строка в таблице не задваиваются: финал подачи ищет прежнюю строку "
        "по ID и правит её на месте.",
    ]
    toggle_text = f"🔔 Сообщить делегату: {'ВКЛ' if notify else 'ВЫКЛ'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"resubg_toggle:{tid}:{0 if notify else 1}")],
        [InlineKeyboardButton(
            text="✅ Разрешить", callback_data=f"resubg_apply:{tid}:{1 if notify else 0}",
        )],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"resubg_cancel:{tid}")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("resubg_start:"))
async def resubg_start(callback: types.CallbackQuery):
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    rendered = await _render_confirm(tid, notify=True)
    if rendered is None:
        await callback.answer(_NOT_REJECTED_ALERT, show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("resubg_toggle:"))
async def resubg_toggle(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    if tid is None or parts[2] not in ("0", "1"):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    rendered = await _render_confirm(tid, notify=(parts[2] == "1"))
    if rendered is None:
        await callback.answer(_NOT_REJECTED_ALERT, show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("resubg_apply:"))
async def resubg_apply(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    if tid is None or parts[2] not in ("0", "1"):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    notify = parts[2] == "1"
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    admin_id = callback.from_user.id
    if not await _city_allowed(admin_id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    if user.get("status") != "rejected":
        await callback.answer(_NOT_REJECTED_ALERT, show_alert=True)
        return

    result = await delegate_overrides.grant_override(tid, delegate_overrides.KIND_RESUBMIT, admin_id)
    if not result.get("ok"):
        await callback.message.edit_text(
            f"❌ Не выдал(а): {html_module.escape(str(result.get('error') or '-'))}",
        )
        await callback.answer()
        return

    name = html_module.escape(str(user.get("full_name") or "-"))
    lines = [f"✅ Разрешение на повторную подачу выдано делегату <b>{name}</b>."]
    notified = False
    if notify:
        notified = await _notify_delegate(callback.bot, tid, user.get("event_city"))
        lines.append("Делегату отправлено сообщение." if notified else "Сообщение делегату отправить не удалось.")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("resubg_cancel:"))
async def resubg_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("✖️ Разрешение не выдано.")
    await callback.answer()


@router.callback_query(F.data.startswith("resubg_revoke:"))
async def resubg_revoke(callback: types.CallbackQuery):
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if user is None:
        await callback.answer(_NOT_FOUND_ALERT, show_alert=True)
        return
    admin_id = callback.from_user.id
    if not await _city_allowed(admin_id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    result = await delegate_overrides.revoke_override(tid, delegate_overrides.KIND_RESUBMIT, admin_id)
    if not result.get("ok"):
        await callback.message.edit_text(
            f"❌ Не отозвал(а): {html_module.escape(str(result.get('error') or '-'))}",
        )
    else:
        name = html_module.escape(str(user.get("full_name") or "-"))
        await callback.message.edit_text(f"✅ Разрешение на повторную подачу для <b>{name}</b> отозвано.", parse_mode="HTML")
    await callback.answer()


async def _notify_delegate(bot, telegram_id: int, event_city: str | None) -> bool:
    """`True` — отправлено сейчас или поставлено в очередь тихих часов (для вызывающего это
    успех, тот же контракт, что `services/comms/quiet_hours.py::send_or_queue_text`); `False` —
    сбой (делегат заблокировал бота и т.п.), fail-soft, не рвёт саму выдачу разрешения."""
    try:
        from domain.cities import get_setting_typed_for_city
        from services.comms import quiet_hours
        from services.i18n.i18n import context as _i18n_context, tr as _i18n_tr
        from services.scheduler import _now_moscow_naive
        from domain.settings.schema import SETTINGS_SCHEMA

        template = await get_setting_typed_for_city("resubmit_granted_notify_text", event_city)
        if not template:
            template = SETTINGS_SCHEMA["resubmit_granted_notify_text"]["default"]
        lang, tr_map = await _i18n_context(telegram_id)
        text = _i18n_tr(template, lang, tr_map)
        await quiet_hours.send_or_queue_text(
            _now_moscow_naive(), telegram_id, text,
            sender=lambda: bot.send_message(telegram_id, text, parse_mode="HTML"),
        )
        return True
    except Exception as e:
        logger.warning(f"resubg: delegate notify({telegram_id}) failed: {e}")
        return False
