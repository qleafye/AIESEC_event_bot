"""Phase 33 (delegate-card admin actions): «↩️ Вернуть в ожидание» — кнопка на карточке
`/find` (`handlers/admin.py::cmd_find_user`), сам перевод — `services/revert_pending.py`.

Не форумная функция (админ-действие модератора) — своей строки в хабе «🎪 Форум: функции»
нет и не нужно (см. рабочее задание фазы, тот же посыл, что у `handlers/admin_city_move.py`).

Шов той же формы, что соседние: своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`admin.router`, модуль импортируется ХВОСТОМ `handlers/admin.py` (golden snapshot: чистая
вставка). `_city_allowed` — импорт из `handlers/admin_checkin.py` (тот же приём, что у
`admin_city_move.py` — не владеем файлом, только вызываем его публичную функцию).

Тумблер «🔔 Сообщить делегату» — состояние живёт в самом `callback_data` (тот же приём, что
`citymv_apply:{tid}:{code}:{status_mode}`), не в FSM: `revertp_toggle:{tid}:{next_notify}`
перерисовывает тот же экран с новым положением, `revertp_apply:{tid}:{notify}` читает текущее
отображённое положение из данных кнопки, которую менеджер реально нажал."""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_user
from handlers.admin import router
from handlers.admin_checkin import _city_allowed
from services.revert_pending import preview_revert_pending, revert_to_pending
from cities import city_label, normalize_city

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."

_STATUS_LABELS = {"approved": "✅ Одобрена", "rejected": "❌ Отклонена"}


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


async def _render_confirm(tid: int, notify: bool) -> tuple[str, InlineKeyboardMarkup] | None:
    """`None` — делегата нет или заявка уже не в возвращаемом статусе (гонка между открытием
    карточки и тапом по кнопке) — вызывающий сам решает, каким алертом это показать."""
    user = await get_user(tid)
    if user is None:
        return None
    preview = await preview_revert_pending(tid)
    if not preview.get("ok"):
        return None

    name = html_module.escape(str(user.get("full_name") or "-"))
    username = html_module.escape(str(user.get("username") or "-"))
    status_label = _STATUS_LABELS.get(user.get("status"), user.get("status") or "-")

    lines = [
        "↩️ <b>Вернуть в ожидание модерации?</b>\n",
        f"{name} ({username})",
        f"Статус: {status_label} → ⏳ Ожидает модерации",
        "",
        "Что изменится: снимутся напоминания об оплате (если были), строка в таблице станет "
        "«Новая», заявка снова попадёт в очередь модерации/дайджест менеджерам, QR-пропуск на "
        "форум перестанет пускать.",
    ]
    coins_balance = preview.get("coins_balance") or 0
    coins_line = f"Монеты делегата останутся как есть: {coins_balance}."
    referrer = preview.get("referrer")
    if referrer:
        ref_name = html_module.escape(str(referrer.get("full_name") or referrer.get("telegram_id")))
        coins_line += f" У пригласившего ({ref_name}) — {referrer.get('coins_balance') or 0}, тоже не меняется."
    lines.append(coins_line)

    if preview.get("payment_status") == "paid":
        payment_line = "⚠️ Оплата подтверждена"
        payment_option = preview.get("payment_option")
        if payment_option:
            payment_line += f" ({html_module.escape(str(payment_option))})"
        payment_line += " — статус оплаты НЕ меняется."
        lines.append(payment_line)

    toggle_text = f"🔔 Сообщить делегату: {'ВКЛ' if notify else 'ВЫКЛ'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"revertp_toggle:{tid}:{0 if notify else 1}")],
        [InlineKeyboardButton(
            text="✅ Вернуть в ожидание", callback_data=f"revertp_apply:{tid}:{1 if notify else 0}",
        )],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"revertp_cancel:{tid}")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("revertp_start:"))
async def revertp_start(callback: types.CallbackQuery):
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

    rendered = await _render_confirm(tid, notify=False)
    if rendered is None:
        await callback.answer("Заявка не одобрена и не отклонена — возвращать не с чего.", show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("revertp_toggle:"))
async def revertp_toggle(callback: types.CallbackQuery):
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
    admin_id = callback.from_user.id
    if not await _city_allowed(admin_id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    rendered = await _render_confirm(tid, notify=(parts[2] == "1"))
    if rendered is None:
        await callback.answer("Заявка не одобрена и не отклонена — возвращать не с чего.", show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("revertp_apply:"))
async def revertp_apply(callback: types.CallbackQuery):
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
    # Тот же двойной перечёт прав, что у city_move — между экранами могло пройти любое время.
    if not await _city_allowed(admin_id, normalize_city(user.get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    # Координатор 25.09: в режиме уведомлений «каждая заявка» карточка менеджерам должна
    # называть, КТО вернул заявку на модерацию (в digest-режиме это уже видно из отдельного
    # блока пачки, 9d15517) — тот же приём резолва имени, что у sos_claim/sos_resolve
    # (handlers/admin_sos.py): full_name -> username -> код-фолбэк. `getattr` — минимальные
    # тестовые дублёры callback.from_user (напр. test_card_actions_260925.py::_FakeUser) несут
    # только `.id`, настоящий aiogram User несёт оба поля всегда.
    admin_name = (
        getattr(callback.from_user, "full_name", None)
        or getattr(callback.from_user, "username", None)
        or "Админ"
    )
    report = await revert_to_pending(
        tid, by_admin=admin_id, admin_name=admin_name, notify=notify, bot=callback.bot,
    )
    if not report.get("ok"):
        await callback.message.edit_text(
            f"❌ Не вернул(а): {html_module.escape(str(report.get('error') or '-'))}",
        )
        await callback.answer()
        return

    name = html_module.escape(str(user.get("full_name") or "-"))
    lines = [f"✅ <b>{name}</b> возвращён(а) в ожидание модерации."]
    if report.get("sheet_updated"):
        lines.append("Строка в таблице обновлена.")
    if notify:
        lines.append("Делегату отправлено сообщение." if report.get("notified") else "Сообщение делегату отправить не удалось.")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("revertp_cancel:"))
async def revertp_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("✖️ Возврат в ожидание отменён.")
    await callback.answer()
