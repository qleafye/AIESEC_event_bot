"""Одобрить уже отклонённого с карточки /find.

`approve_user_atomic` берёт только pending/waitlist — отклонённого обычная модерация
не трогает. Здесь отдельный атомарный флип `approve_rejected_atomic`, затем то же
письмо, что после обычного одобрения (`apply_decision_effects` зовёт хендлер)."""
from __future__ import annotations

import logging

from database.db import approve_rejected_atomic, get_user, record_admin_action
from domain.cities import get_setting_typed_for_city

logger = logging.getLogger(__name__)


async def preview_approve_rejected(telegram_id: int) -> dict:
    """Только чтение для экрана подтверждения. `payment_enabled` — чтобы менеджер видел,
    откроется ли шаг оплаты: письмо то же, шаг есть только если модуль включён."""
    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "error": "Делегат не найден"}
    if user.get("status") != "rejected":
        return {"ok": False, "error": "Одобрить можно только отклонённую заявку"}
    payment_on = await get_setting_typed_for_city("payment_enabled", user.get("event_city")) == "on"
    return {
        "ok": True,
        "error": None,
        "status": "rejected",
        "payment_enabled": payment_on,
        "full_name": user.get("full_name"),
        "username": user.get("username"),
    }


async def approve_rejected(telegram_id: int, *, by_admin: int) -> dict:
    """Флип одного отклонённого в approved. Журнал решения — с `effects_already_sent`,
    иначе сметатель отправит письмо второй раз. Само письмо шлёт хендлер."""
    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "error": "Делегат не найден"}
    if user.get("status") != "rejected":
        return {"ok": False, "error": "Одобрить можно только отклонённую заявку"}

    flipped = await approve_rejected_atomic(telegram_id)
    if not flipped:
        return {"ok": False, "error": "Статус успели изменить параллельно — одобрение отменено"}

    report: dict = {
        "ok": True,
        "error": None,
        "before_status": "rejected",
        "after_status": "approved",
        "payment_enabled": await get_setting_typed_for_city(
            "payment_enabled", user.get("event_city"),
        ) == "on",
    }

    try:
        await record_admin_action(
            actor_id=by_admin,
            subject_id=telegram_id,
            action="approve_rejected",
            before={"status": "rejected"},
            after={"status": "approved"},
        )
    except Exception as e:
        logger.warning("approve_rejected: журнал действия (%s) не записан: %s", telegram_id, e)

    try:
        from services.applications.applications import record_decision
        from services.scheduler import _now_moscow_naive

        await record_decision(
            telegram_id, "approved", None, by_admin, _now_moscow_naive(),
            effects_already_sent=True,
        )
    except Exception as e:
        logger.error("approve_rejected: журнал решения (%s) не записан: %s", telegram_id, e)

    return report
