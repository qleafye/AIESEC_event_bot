"""Phase 33 (delegate-card admin actions, задача 3): «✏️ Открыть правку после решения» —
кнопка на карточке `/find` для одобренного делегата (`handlers/admin.py::cmd_find_user`).
Сам примитив исключения — `services/delegate_overrides.py` (`kind="edit"`), общий с задачей 2.

Видна только для `status == "approved"` — `services/reg_edit_policy.py::edit_gate` (Р-1 в её
докстринге) НИКОГДА не гейтит `rejected` (правка отклонённой анкеты технически неотличима от
первичной подачи) и не гейтит `pending` при положении `until_decision` (гейт срабатывает,
только когда `status == "approved"`) — исключение для любого другого статуса было бы
исключением из правила, которого нет.

Экран подтверждения честно читает ТЕКУЩЕЕ положение тумблера «Изменённая анкета — снова на
модерацию» (`toggle_reg_edit_remoderation`) и объясняет follow-up словами делегата: включён —
правка вернёт заявку на модерацию (как у любой другой правки одобренной анкеты), выключен —
статус останется «Одобрена» (SEED: «поведение как у обычной правки до решения»). Исключение
только ОТКРЫВАЕТ доступ к правке — само решение «что дальше со статусом» этим не меняется, им
управляет тот же тумблер, что и всегда.

Не форумная функция — своей строки в хабе «🎪 Форум: функции» нет и не нужно, тот же посыл,
что у соседних карточных действий."""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import normalize_city
from database.db import get_user
from handlers.admin import router
from handlers.admin_checkin import _city_allowed
from services import delegate_overrides
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."
_NOT_APPROVED_ALERT = "Заявка не одобрена — правку после решения открывать не для чего."


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


async def _render_confirm(tid: int, notify: bool) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await get_user(tid)
    if user is None or user.get("status") != "approved":
        return None
    existing = await delegate_overrides.active_override(tid, delegate_overrides.KIND_EDIT)
    if existing is not None:
        return None

    name = html_module.escape(str(user.get("full_name") or "-"))
    username = html_module.escape(str(user.get("username") or "-"))
    remoderation_on = await get_setting_typed("toggle_reg_edit_remoderation") == "on"
    follow_up = (
        "правка вернёт заявку на модерацию (тумблер «Изменённая анкета — снова на модерацию» "
        "включён — как у любой другой правки одобренной анкеты)."
        if remoderation_on else
        "статус останется «Одобрена» (тумблер «Изменённая анкета — снова на модерацию» "
        "выключен — правка ничего не поменяет в статусе)."
    )
    lines = [
        "✏️ <b>Открыть правку после решения?</b>\n",
        f"{name} ({username})",
        "",
        "Что изменится: делегат сможет один раз открыть и отредактировать уже одобренную "
        "анкету, даже если общий переключатель «Правка анкеты» стоит на «нельзя». "
        f"После того, как делегат реально что-то поменяет и отправит — {follow_up}",
        "Разрешение одноразовое — погаснет после первой применённой правки.",
    ]
    toggle_text = f"🔔 Сообщить делегату: {'ВКЛ' if notify else 'ВЫКЛ'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"editg_toggle:{tid}:{0 if notify else 1}")],
        [InlineKeyboardButton(
            text="✅ Открыть правку", callback_data=f"editg_apply:{tid}:{1 if notify else 0}",
        )],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"editg_cancel:{tid}")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("editg_start:"))
async def editg_start(callback: types.CallbackQuery):
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
        await callback.answer(_NOT_APPROVED_ALERT, show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("editg_toggle:"))
async def editg_toggle(callback: types.CallbackQuery):
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
        await callback.answer(_NOT_APPROVED_ALERT, show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("editg_apply:"))
async def editg_apply(callback: types.CallbackQuery):
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
    if user.get("status") != "approved":
        await callback.answer(_NOT_APPROVED_ALERT, show_alert=True)
        return

    result = await delegate_overrides.grant_override(tid, delegate_overrides.KIND_EDIT, admin_id)
    if not result.get("ok"):
        await callback.message.edit_text(
            f"❌ Не открыл(а): {html_module.escape(str(result.get('error') or '-'))}",
        )
        await callback.answer()
        return

    name = html_module.escape(str(user.get("full_name") or "-"))
    lines = [f"✅ Правка после решения открыта делегату <b>{name}</b>."]
    if notify:
        notified = await _notify_delegate(callback.bot, tid, user.get("event_city"))
        lines.append("Делегату отправлено сообщение." if notified else "Сообщение делегату отправить не удалось.")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("editg_cancel:"))
async def editg_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("✖️ Правка не открыта.")
    await callback.answer()


@router.callback_query(F.data.startswith("editg_revoke:"))
async def editg_revoke(callback: types.CallbackQuery):
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

    result = await delegate_overrides.revoke_override(tid, delegate_overrides.KIND_EDIT, admin_id)
    if not result.get("ok"):
        await callback.message.edit_text(
            f"❌ Не отозвал(а): {html_module.escape(str(result.get('error') or '-'))}",
        )
    else:
        name = html_module.escape(str(user.get("full_name") or "-"))
        await callback.message.edit_text(f"✅ Разрешение на правку для <b>{name}</b> отозвано.", parse_mode="HTML")
    await callback.answer()


async def _notify_delegate(bot, telegram_id: int, event_city: str | None) -> bool:
    """`True` — отправлено сейчас или поставлено в очередь тихих часов; `False` — сбой
    (fail-soft, не рвёт саму выдачу разрешения), тот же контракт, что
    `handlers/admin_resubmit_grant.py::_notify_delegate`."""
    try:
        from cities import get_setting_typed_for_city
        from services import quiet_hours
        from services.i18n import context as _i18n_context, tr as _i18n_tr
        from services.scheduler import _now_moscow_naive
        from settings_schema import SETTINGS_SCHEMA

        template = await get_setting_typed_for_city("edit_granted_notify_text", event_city)
        if not template:
            template = SETTINGS_SCHEMA["edit_granted_notify_text"]["default"]
        lang, tr_map = await _i18n_context(telegram_id)
        text = _i18n_tr(template, lang, tr_map)
        await quiet_hours.send_or_queue_text(
            _now_moscow_naive(), telegram_id, text,
            sender=lambda: bot.send_message(telegram_id, text, parse_mode="HTML"),
        )
        return True
    except Exception as e:
        logger.warning(f"editg: delegate notify({telegram_id}) failed: {e}")
        return False
