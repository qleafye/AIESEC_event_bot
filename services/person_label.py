"""Подпись человека и времени для админских списков («👥 Роли и доступы»): имя и @username вместо
голого telegram_id, московское время словами вместо сырой UTC-метки ISO."""
from __future__ import annotations

from datetime import datetime

from database.db import get_reg_started_by_id, get_user
from services.timeutil import utc_naive_to_msk


async def person_label(telegram_id: int) -> str:
    """«Имя (@username)», «Имя», «@username» — что известно боту; иначе «id N». Без HTML-экранирования
    (экранирует вызывающий: подпись идёт и в текст с parse_mode=HTML, и в кнопку)."""
    user = await get_user(telegram_id)
    if user is not None:
        name, uname = user.get("full_name"), user.get("username")
    else:
        started = await get_reg_started_by_id(telegram_id)
        name, uname = None, (started or {}).get("username")
    uname = (uname or "").strip().lstrip("@")
    name = (name or "").strip()
    if name and uname:
        return f"{name} (@{uname})"
    if name or uname:
        return name or f"@{uname}"
    return f"id {telegram_id}"


def msk_stamp_from_utc_iso(raw: str | None) -> str:
    """Метка `datetime.utcnow().isoformat()` из БД -> «10.09.2026 00:42 МСК»; нечитаемое — как есть."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return str(raw)
    if dt.tzinfo is not None:
        from services.timeutil import aware_to_msk
        return aware_to_msk(dt).strftime("%d.%m.%Y %H:%M МСК")
    return utc_naive_to_msk(dt).strftime("%d.%m.%Y %H:%M МСК")
