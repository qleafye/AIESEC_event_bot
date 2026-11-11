"""Одобрить уже отклонённого с карточки /find.

`approve_user_atomic` берёт только pending/waitlist — отклонённого обычная модерация
не трогает. Здесь отдельный атомарный флип `approve_rejected_atomic`, затем то же
письмо, что после обычного одобрения (`apply_decision_effects` зовёт хендлер)."""
from __future__ import annotations

import logging

from database.db import (
    approve_rejected_atomic,
    get_user,
    record_admin_action,
    search_users_by_name,
)
from domain.cities import city_label_or_none, get_setting_typed_for_city, normalize_city

logger = logging.getLogger(__name__)

NEED_CAP = (
    "Чтобы снять отказ с заявки, нужно право заявок. "
    "Попросите того, кто выдаёт доступ, его включить."
)
NOT_REJECTED = "Одобрить можно только отклонённую заявку."
NOT_FOUND = "Человек не найден — карточка устарела."
OTHER_CITY = "Этот человек из другого города. Снять отказ может тот, кто ведёт этот город."
SHORT_QUERY = "Введите имя — хотя бы две буквы. Покажем отклонённые заявки, которые вы ведёте."
NONE_FOUND = "Такой отклонённой заявки нет. Проверьте имя или откройте карточку в чате."


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


def _name(user: dict | None) -> str:
    text = " ".join(str((user or {}).get("full_name") or "").split())
    if text:
        return text
    username = str((user or {}).get("username") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return "без имени"


async def _city_ok(admin_id: int, code: str | None) -> bool:
    """Тот же допуск, что у кнопки на карточке: чужой город привязанному менеджеру закрыт."""
    if code is None:
        return True
    from domain.settings.ops import per_city_visible_codes

    return code in await per_city_visible_codes(admin_id)


async def rejected_screen(admin_id: int, query: str) -> dict:
    """Поиск одной отклонённой заявки. Список целиком отсюда не одобряется."""
    needle = " ".join((query or "").split())
    if len(needle) < 2:
        return {"text": SHORT_QUERY, "people": []}
    rows = await search_users_by_name(needle, limit=20)
    people = []
    for row in rows:
        user = await get_user(int(row["telegram_id"]))
        if user is None or user.get("status") != "rejected":
            continue
        if not await _city_ok(admin_id, normalize_city(user.get("event_city"))):
            continue
        label = await city_label_or_none(user.get("event_city"))
        people.append({
            "id": int(user["telegram_id"]),
            "name": _name(user),
            "city": label or "",
        })
    if people:
        text = "Один человек. Отказ снимается только у того, кого вы выберете."
    else:
        text = NONE_FOUND
    return {"text": text, "people": people}


async def approve_decision(admin_id: int, telegram_id: int, *, confirm: bool) -> dict:
    """Подтверждение и флип одного человека. Письмо решения ставится в ту же очередь,
    что и обычное одобрение из приложения. Без подтверждения статус не меняется."""
    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "text": NOT_FOUND}
    if not await _city_ok(admin_id, normalize_city(user.get("event_city"))):
        return {"ok": False, "text": OTHER_CITY}
    if user.get("status") != "rejected":
        return {"ok": False, "text": NOT_REJECTED}
    name = _name(user)
    if not confirm:
        preview = await preview_approve_rejected(telegram_id)
        if preview.get("payment_enabled"):
            tail = "Человеку уйдёт решение по заявке, и откроется шаг оплаты."
        else:
            tail = "Человеку уйдёт решение по заявке. Шаг оплаты не откроется — оплата выключена."
        return {
            "ok": True,
            "need_confirm": True,
            "text": f"Снять отказ у {name}?\n\nОтказ будет снят. {tail}",
        }
    report = await approve_rejected(telegram_id, by_admin=admin_id)
    if not report.get("ok"):
        error = str(report.get("error") or NOT_REJECTED)
        if error == "Одобрить можно только отклонённую заявку":
            error = NOT_REJECTED
        return {"ok": False, "text": error}
    return {
        "ok": True,
        "text": f"Отказ у {name} снят. Решение уйдёт человеку.",
        "enqueue": {"telegram_id": int(telegram_id), "status": "approved"},
    }
