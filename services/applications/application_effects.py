"""Phase 23 (23-02, APP-TINDER-01) — хвост решения по заявке, которому физически нужен `bot`.

Перенесено из `handlers/applications/admin_moderation.py` (Phase 23, план 23-02): тело `appr_approve`'s
эффект-хвоста + `appr_reject_reason`'s эффект-хвост -> `apply_decision_effects`; `_welcome_flipped`
+ хвост `appr_all_yes` -> `mass_approve_effects`.

Зачем модуль отдельный от `services/applications/applications.py`: ядро отбора обязано остаться aiogram-free
(веб-процесс `miniapp/` не имеет права импортировать aiogram, `miniapp/deps.py`: «Модуль
aiogram-free»), а «отправить приветствие» и «отправить сообщение делегату» физически требуют
объекта бота — тот же разрез, что `services/registration/reg_finalize.py::finalize_data`/`post_finalize`.

Зовёт эти две функции и чат (боту, напрямую после решения — `_spawn(apply_decision_effects(...))`
в `handlers/applications/admin_moderation.py`), и (со следующего плана) джоба очереди событий веба, когда
истечёт окно отмены (D-06) — один и тот же журнал вызовов и текстов для обеих поверхностей.

Импорт `handlers.reg.reg_schema` — ЛОКАЛЬНЫЙ внутри функции (тот же приём, что
`services/registration/reg_finalize.py::post_finalize`): `handlers/applications/admin_moderation.py` импортирует ИЗ этого
модуля на своём верхнем уровне, обратный модульный импорт дал бы цикл при загрузке пакета
`handlers`.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from domain.regform.labels import STATUS_LABELS
from services.applications.applications import reject_message_text
from services.applications.decision_delivery import ERROR_BLOCKED, ERROR_CHAT_NOT_FOUND, ERROR_DEACTIVATED
from services.sheets.sheets import bulk_update_status_in_sheet, update_status_in_sheet
from services.infra.telegram_send import send_with_retry

logger = logging.getLogger(__name__)


# ── Координатор 25.09: учёт доставки решения ────────────────────────────────────────────────
#
# Человеческая причина сбоя — ТОЛЬКО для отчёта «Сверить с БД»/«📨 Переотправить решения»
# (services/sheets/sheet_reconcile.py, services/applications/decision_delivery.py), НЕ для решения «слать ли
# повторно» — та развилка (D-01, services/scheduler.py::_PERMANENT_SEND_ERRORS) намеренно
# классифицирует по ТИПУ исключения, не тексту («формулировка Telegram меняется без анонса»).
# Здесь текст читаем осознанно: при неузнанной формулировке функция просто падает в
# «ошибка отправки: <кратко>» — ничего не ломается, отчёт честно показывает исходную причину.
def _classify_decision_delivery_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if isinstance(exc, TelegramForbiddenError):
        if "deactivated" in msg:
            return ERROR_DEACTIVATED
        return ERROR_BLOCKED
    if isinstance(exc, TelegramBadRequest) and "chat not found" in msg:
        return ERROR_CHAT_NOT_FOUND
    text = str(exc).strip()
    if len(text) > 160:
        text = text[:160] + "…"
    return f"ошибка отправки: {text}" if text else f"ошибка отправки: {type(exc).__name__}"


async def _record_delivery_fail_soft(telegram_id: int, decision: str, status: str,
                                      error: Exception | None = None) -> None:
    """Пишет `users.decision_delivery_*` (`database.db.record_decision_delivery`) — fail-soft:
    сбой УЧЁТА не должен ломать само решение (координатор 25.09, п.2), поэтому любое исключение
    здесь только логируется, никогда не поднимается наружу."""
    try:
        from database.db import record_decision_delivery
        error_text = _classify_decision_delivery_error(error) if error is not None else None
        await record_decision_delivery(telegram_id, decision, status, error_text)
    except Exception as e:
        logger.error(f"decision_delivery: не удалось записать учёт доставки для {telegram_id}: {e}")


async def apply_decision_effects(bot, telegram_id: int, status: str, reason: str | None = None, *,
                                 notify: bool = True, sheet: bool = True, resend: bool = False) -> None:
    """Хвост одного решения по заявке. `approved`: приветствие (`approve_user`, ровно один раз
    — D-10) затем лист. `rejected`: сообщение делегату (`reject_message_text`, `parse_mode=HTML`)
    затем лист; сбой отправки — только в лог, решение НЕ откатывается (паритет с ботом).

    Quick 260904-dq1: `notify=False`/`sheet=False` — обратно совместимые kwargs. Лист
    обновляется НЕЗАВИСИМО от тихих часов (рабочий инструмент менеджера, автосинк, морозить
    до утра нельзя); уведомление делегату — единственное, что откладывается. Если `notify`
    попал в окно тишины делегата, решение кладётся в очередь `services.quiet_hours` с
    due_at = конец окна, а немедленной отправки НЕ происходит (`quiet_hours.flush_due`
    перечитывает `users.status` на разборе и доставляет — Task 3 260904-dq1-PLAN.md).

    Координатор 25.09 (учёт доставки решения, память auto-approve-incident-260906): каждая
    попытка отправить письмо о решении фиксируется в `users.decision_delivery_*`
    (`_record_delivery_fail_soft`) — «в очереди» при уходе в тихие часы (этот же вызов, когда
    его позовёт `services.quiet_hours._flush_application_decision_row`, перезапишет статус на
    «доставлено»/«не доставлено»), «доставлено»/«не доставлено» при немедленной попытке.
    `notify=False` — эффект без попытки отправки (например, узкий пересчёт листа) — учёт НЕ
    трогается вовсе, писать «не доставлено» о письме, которое и не пытались слать, было бы
    ложью.

    Координатор 25.09 («📨 Переотправить решения»): `resend=True` — для `approved` шлёт
    `handlers.reg.reg_schema.resend_approve_text` (только текст решения) ВМЕСТО `approve_user` —
    шаг оплаты не открывается и FSM делегата не трогается, даже если `payment_enabled=on`
    (решение координатора: обычная переотправка через `approve_user` заново рисовала бы пикер
    тарифов уже одобренному делегату). Для `rejected` разницы нет — там шага оплаты никогда не
    было."""
    notify_now = notify
    if notify:
        from services import quiet_hours
        from services.scheduler import _now_moscow_naive
        now = _now_moscow_naive()
        due = await quiet_hours.defer_until(now, telegram_id)
        if due is not None:
            await quiet_hours.enqueue(
                telegram_id, quiet_hours.KIND_APPLICATION_DECISION,
                {"status": status, "reason": reason}, due, now,
            )
            notify_now = False
            await _record_delivery_fail_soft(telegram_id, status, "queued")

    if status == "approved":
        if notify_now:
            if resend:
                from handlers.reg.reg_schema import resend_approve_text  # локальный импорт против цикла
                send_err = await resend_approve_text(bot, telegram_id)  # текст решения, без шага оплаты
            else:
                from handlers.reg.reg_schema import approve_user  # локальный импорт против цикла
                send_err = await approve_user(bot, telegram_id)  # welcome exactly once (D-10)
            await _record_delivery_fail_soft(
                telegram_id, "approved", "failed" if send_err else "delivered", send_err,
            )
        if sheet:
            await update_status_in_sheet(telegram_id, STATUS_LABELS["approved"])
    elif status == "rejected":
        if notify_now:
            # Квик 260917-en (приёмка 17.09, п.4): «reject_text» — group "reg", уже в
            # делегатском корпусе — не хватало только точки перевода на отправке (тот же
            # класс дыры, что у approve_text в handlers/reg/reg_schema.py).
            from services.i18n.i18n import context as _i18n_context
            lang, tr_map = await _i18n_context(telegram_id)
            text = await reject_message_text(reason, lang, tr_map)
            send_err = await send_with_retry(
                lambda: bot.send_message(telegram_id, text, parse_mode="HTML"),
            )
            if send_err is not None:
                logger.error(f"Failed to notify rejected user {telegram_id}: {send_err}")
            await _record_delivery_fail_soft(
                telegram_id, "rejected", "failed" if send_err else "delivered", send_err,
            )
        if sheet:
            await update_status_in_sheet(telegram_id, STATUS_LABELS["rejected"])


async def mass_approve_effects(bot, ids: list) -> None:
    """Перенесённый `_welcome_flipped` (обработка `TelegramRetryAfter`, пауза 0.05 между
    отправками) + один `bulk_update_status_in_sheet`. Пустой список — выход без единого вызова.

    Quick 260904-dq1: та же проверка окна на КАЖДОГО делегата — попал в тихие часы, строка в
    очередь, приветствие не шлётся. `bulk_update_status_in_sheet` — для ВСЕХ id одним вызовом,
    как раньше (лист не ждёт тихих часов).

    Координатор 25.09 (учёт доставки решения): каждый `tid` получает свою запись
    `users.decision_delivery_*` — «в очереди»/«доставлено»/«не доставлено», тем же приёмом, что
    `apply_decision_effects`. `approve_user` (после правки того же коммита) сама ретраит один
    раз на 429 и возвращает исключение вместо того, чтобы поднимать его — внешний
    `except TelegramRetryAfter` ниже оставлен как защита на случай сбоя ДО входа в неё (например,
    чтения `defer_until`), а не как основной путь ретрая."""
    if not ids:
        return
    from services import quiet_hours
    from services.scheduler import _now_moscow_naive

    for tid in ids:
        try:
            now = _now_moscow_naive()
            due = await quiet_hours.defer_until(now, tid)
            if due is not None:
                await quiet_hours.enqueue(
                    tid, quiet_hours.KIND_APPLICATION_DECISION,
                    {"status": "approved", "reason": None}, due, now,
                )
                await _record_delivery_fail_soft(tid, "approved", "queued")
                await asyncio.sleep(0.05)
                continue
            from handlers.reg.reg_schema import approve_user  # локальный импорт против цикла
            send_err = await approve_user(bot, tid)
            await _record_delivery_fail_soft(
                tid, "approved", "failed" if send_err else "delivered", send_err,
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                from handlers.reg.reg_schema import approve_user  # локальный импорт против цикла
                send_err = await approve_user(bot, tid)
                await _record_delivery_fail_soft(
                    tid, "approved", "failed" if send_err else "delivered", send_err,
                )
            except Exception as e2:
                logger.error(f"Mass-approve welcome retry failed for {tid}: {e2}")
                await _record_delivery_fail_soft(tid, "approved", "failed", e2)
        except Exception as e:
            logger.error(f"Mass-approve welcome failed for {tid}: {e}")
            await _record_delivery_fail_soft(tid, "approved", "failed", e)
        await asyncio.sleep(0.05)
    await bulk_update_status_in_sheet({str(t): STATUS_LABELS["approved"] for t in ids})
