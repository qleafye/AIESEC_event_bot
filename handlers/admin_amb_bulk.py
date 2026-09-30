"""Раздел «🤝 Амбассадоры» → «🙋 Кандидаты и команда»: массовые действия (право moderate_game).

- «🙅 Вежливо отказать всем оставшимся (N)» — когда состав финальный. Сначала подтверждение:
  скольким уйдёт, какой текст (`amb_decline_all_text` из реестра) и что будет после. Число
  на кнопке «✅ Отправить N» сверяется перед выполнением: изменилось — ничего не шлём и
  показываем новое. Рассылка идёт фоном через тихие часы, отметка «письмо ушло» ставится ДО
  отправки (`claim_decline_notice`) — повтор досылает только тем, кому ещё не ушло. В конце
  менеджеру приходит отчёт «отправлено / отложено до утра / не доставлено».

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/admin_amb_candidates.py`.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import amb_status_db
from handlers.admin import router
from handlers.admin_amb_candidates import _edit_or_send, _notify, _now, render_list
from handlers.admin_caps import has_capability
from services.background import spawn
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_PAUSE = 0.05  # между сообщениями рассылки — не упираться в лимит Telegram

_NO_CANDIDATES = "Кандидатов нет — отказывать некому."
_ALL_CITIES_ONLY = (
    "Отказ всем действует на кандидатов всех городов. Выберите в админке «Все города», "
    "чтобы увидеть полное число, и нажмите ещё раз."
)


async def bulk_buttons(scope) -> list[list[InlineKeyboardButton]]:
    """Кнопки массовых действий под списком «🙋 Кандидаты и команда»."""
    rows: list[list[InlineKeyboardButton]] = []
    if scope is None:
        n = await amb_status_db.count_by_filter("candidates")
        if n:
            rows.append([InlineKeyboardButton(
                text=f"🙅 Вежливо отказать всем оставшимся ({n})", callback_data="ambc_decl")])
        else:
            pending = len(await amb_status_db.declined_pending_notice())
            if pending:
                rows.append([InlineKeyboardButton(
                    text=f"🙅 Дослать отказ ({pending})", callback_data="ambc_decl")])
    return rows


# ── вежливый отказ всем оставшимся ───────────────────────────────────────────────────────

async def _decline_text() -> str:
    return html.escape(await get_setting_typed("amb_decline_all_text") or "")


async def _decline_confirm(admin_id: int, n: int, pending: int,
                           prefix: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Экран подтверждения: n кандидатов (или досылка pending отказанным при n = 0)."""
    text_line = f"Текст: «{await _decline_text()}»"
    if n:
        text = (
            f"{prefix}<b>🙅 Вежливо отказать всем оставшимся?</b>\n\n"
            f"Уйдёт {n} кандидатам (включая отложенных «в запасе»). {text_line}.\n\n"
            "После этого кнопки «Хочу стать амбассадором» у них не будет в этом сезоне. "
            "Вернуть человека можно: «🙋 Кандидаты и команда» → «Отказано» → «Взять».\n\n"
            "Сообщения уходят с учётом тихих часов; когда закончу — пришлю отчёт."
        )
        go = f"✅ Отправить {n}"
    else:
        text = (
            f"{prefix}<b>🙅 Дослать отказ?</b>\n\n"
            f"Кандидатов не осталось, но {pending} отказанным сообщение ещё не ушло (например, "
            "бот перезапускался во время рассылки). Уйдёт только им, повторно никому. "
            f"{text_line}."
        )
        go = f"✅ Дослать {pending}"
    rows = [[InlineKeyboardButton(text=go, callback_data=f"ambc_decl_go:{n}")]]
    if await has_capability(admin_id, "settings"):
        rows.append([InlineKeyboardButton(
            text="✏️ Изменить текст", callback_data="settings_edit:amb_decline_all_text")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ambc_decl_no")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _scope(admin_id: int):
    from handlers.admin_core import _admin_city_view  # ленивый шов, как у экранов заявок

    scope, _label = await _admin_city_view(admin_id)
    return scope


@router.callback_query(F.data == "ambc_decl")
async def decline_all_confirm(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if await _scope(admin_id) is not None:
        await callback.answer(_ALL_CITIES_ONLY, show_alert=True)
        return
    n = await amb_status_db.count_by_filter("candidates")
    pending = 0 if n else len(await amb_status_db.declined_pending_notice())
    if not n and not pending:
        await callback.answer(_NO_CANDIDATES, show_alert=True)
        return
    await _edit_or_send(callback.message, *await _decline_confirm(admin_id, n, pending))
    await callback.answer()


@router.callback_query(F.data == "ambc_decl_no")
async def decline_all_cancel(callback: types.CallbackQuery):
    text, kb = await render_list(callback.from_user.id, "candidates", 0)
    await _edit_or_send(callback.message, text, kb)
    await callback.answer("Отменено — никому ничего не ушло.")


@router.callback_query(F.data.startswith("ambc_decl_go:"))
async def decline_all_go(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if await _scope(admin_id) is not None:
        await callback.answer(_ALL_CITIES_ONLY, show_alert=True)
        return
    try:
        expected = int(callback.data.split(":", 1)[1])
    except ValueError:
        expected = -1
    n = await amb_status_db.count_by_filter("candidates")
    pending = len(await amb_status_db.declined_pending_notice())
    if not n and not pending:
        await callback.answer(_NO_CANDIDATES, show_alert=True)
        text, kb = await render_list(admin_id, "candidates", 0)
        await _edit_or_send(callback.message, text, kb)
        return
    if n != expected:
        prefix = (f"⚠️ Число кандидатов изменилось: теперь {n}. "
                  "Проверьте и подтвердите ещё раз.\n\n")
        await _edit_or_send(callback.message, *await _decline_confirm(admin_id, n, pending, prefix))
        await callback.answer()
        return
    ids = await amb_status_db.decline_remaining(at=_now(), by=admin_id)
    to_send = len(await amb_status_db.declined_pending_notice())
    logger.info("admin=%s amb_decline_all n=%s to_send=%s", admin_id, len(ids), to_send)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="← К списку", callback_data="ambc:declined:0")]])
    await _edit_or_send(
        callback.message,
        f"🙅 Отказано кандидатам: {len(ids)}. Отправляю сообщение ({to_send}) — "
        "когда закончу, пришлю отчёт.",
        kb,
    )
    await callback.answer()
    spawn(notify_declined(callback.bot, admin_id))


async def notify_declined(bot, admin_id: int) -> tuple[int, int, int]:
    """Обходит ВСЕХ отказанных без отметки (не только отказанных этим нажатием — так повтор
    досылает хвост после рестарта). Отметка до отправки: вторая вкладка / повтор второе письмо
    не шлют. Ошибка одному не останавливает остальных. -> (отправлено, отложено, не доставлено)."""
    sent = deferred = failed = 0
    try:
        for tid in await amb_status_db.declined_pending_notice():
            if not await amb_status_db.claim_decline_notice(tid, at=_now()):
                continue
            result = await _notify(bot, tid, "amb_decline_all_text")
            if result is True:
                sent += 1
            elif result is False:
                deferred += 1
            else:
                failed += 1
            await asyncio.sleep(_PAUSE)
    except Exception:
        logger.exception("amb_decline_all: рассылка прервана (admin=%s)", admin_id)
    logger.info("admin=%s amb_decline_all_done sent=%s deferred=%s failed=%s",
                admin_id, sent, deferred, failed)
    report = (f"🙅 Отказ кандидатам. Готово: отправлено {sent}, отложено до утра {deferred}, "
              f"не доставлено {failed}.")
    if failed:
        report += "\nНе доставлено — скорее всего, эти люди заблокировали бота."
    try:
        await bot.send_message(admin_id, report)
    except Exception:
        logger.exception("amb_decline_all: отчёт менеджеру не доставлен (admin=%s)", admin_id)
    return sent, deferred, failed
