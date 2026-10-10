"""Сканер Mini App x запись на сессии: подсказка «записан / не записан» на плашке точки-сессии
и правка записи волонтёром одной кнопкой.

«Любой волонтёр» = право `checkin` + привязка к городу (`_point_city_denial`); проверки «волонтёр
именно этого зала» нет. Запись игнорирует закрытие и лимит, пишет `source='scan'` и `by_staff_id`.
Отметка входа от записи не зависит: сбой подсказки скан не роняет."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from domain.cities import get_setting_typed_for_city
from domain.settings.schema import SETTINGS_SCHEMA
from miniapp.deps import Principal, require_cap, require_section
from miniapp.routers.checkin import _CAP, _SECTION, _bound_city, _point_city_denial
from services.forum.session_enroll import enroll_by_staff, scan_hint

logger = logging.getLogger(__name__)

router = APIRouter()

_MARKED = frozenset({"new", "duplicate", "moved"})


async def with_enroll_hint(result: dict, user: dict | None, point: str, bound: str | None) -> dict:
    """Дописывает в ответ скана `hint` и `enroll` для точки-сессии. Fail-soft."""
    try:
        if not (point or "").startswith("session:") or not user:
            return result
        if result.get("status") not in _MARKED:
            return result
        extra = await scan_hint(user, int(point.split(":", 1)[1]))
        if not extra:
            return result
        hint = extra["hint"]
        if result.get("hint"):
            hint = f"{result['hint']}\n{hint}"
        return {**result, "hint": hint, "enroll": extra["enroll"]}
    except Exception:  # noqa: BLE001
        logger.exception("checkin: подсказка записи не добавлена (%s)", point)
        return result


class EnrollBody(BaseModel):
    telegram_id: int
    session_id: int


async def _text(key: str | None, city: str | None, title: str = "") -> str | None:
    if not key:
        return None
    try:
        raw = await get_setting_typed_for_city(key, city)
    except Exception:  # noqa: BLE001
        return None
    text = (raw or "").replace("{title}", title)
    return text or str(SETTINGS_SCHEMA.get(key, {}).get("default") or "").replace("{title}", title) or None


@router.post("/app/api/checkin/enroll")
async def checkin_enroll(
    body: EnrollBody, request: Request,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    bound = await _bound_city(request, p)
    denial = await _point_city_denial(bound, f"session:{body.session_id}")
    if denial is not None:
        return denial
    outcome = await enroll_by_staff(body.telegram_id, body.session_id, by_staff_id=p.telegram_id)
    session = outcome.session or {}
    city = session.get("city")
    if outcome.status in ("ok", "already"):
        message = await _text("session_enroll_scan_done_text", city, session.get("title") or "")
        return {"status": "ok", "message": message or ""}
    message = (await _text(outcome.text_key, city)
               or await _text("session_enroll_scan_error_text", city) or "")
    return {"status": outcome.status, "message": message}
