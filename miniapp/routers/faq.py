"""Quick 260906-8uq (FAQ-01..06), задача 5: раздел делегата «❓ Частые вопросы» в Mini App.

`GET /app/api/faq` — единственный делегатский маршрут этой задачи (менеджерский `POST` за
кнопку «В FAQ» приезжает задачей 6 в этот же файл). Правило видимости пункта («городской
пункт перекрывает общий») живёт ОДИН раз в `services/faq.py::apply_city_overrides` — здесь
второй копии нет, только `list_faq_for_city` + применение правила (тот же приём, что
`handlers/user_actions.py::_faq_visible_items` использует на стороне бота).

Город делегата: `Principal.city` — привязка СОТРУДНИКА (`staff_city`), у делегата она всегда
`None`, поэтому город читаем из его собственной строки `users.event_city` — тот же fail-soft
приём, что `handlers.user_actions._delegate_city_for_faq` (ошибка чтения не роняет экран,
только сужает список до общих пунктов)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from cities import cities_module_on, normalize_city
from database.db import get_user, list_faq_for_city
from services.faq import apply_city_overrides
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_section

router = APIRouter()


async def _delegate_city(p: Principal) -> str | None:
    if not await cities_module_on():
        return None
    try:
        user = await get_user(p.telegram_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception:
        return None


@router.get("/app/api/faq")
async def faq_list(
    p: Principal = Depends(delegate_gate),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    city = await _delegate_city(p)
    try:
        rows = await list_faq_for_city(city)
    except Exception:
        rows = []
    items = apply_city_overrides(rows, city)
    return {
        "items": [
            {"id": row["id"], "question": row["question"], "answer": row["answer"]}
            for row in items
        ],
        "empty_text": await get_setting_typed("faq_empty_text"),
    }


__all__ = ["router"]
