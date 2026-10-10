"""Подпись человека и времени для админских списков («👥 Роли и доступы»): имя и @username вместо
голого telegram_id, московское время словами вместо сырой UTC-метки ISO."""
from __future__ import annotations

from datetime import datetime

from database.chat_coins_db import chat_username_entry
from database.db import get_reg_started_by_id, get_user
from services.infra.timeutil import utc_naive_to_msk


async def person_label(telegram_id: int) -> str:
    """«Имя (@username)», «Имя», «@username» — что известно боту; иначе «id N». Без HTML-экранирования
    (экранирует вызывающий: подпись идёт и в текст с parse_mode=HTML, и в кнопку).

    Источники по очереди: анкета, /start, ник и имя из чата или из пересылки при выдаче роли
    (`chat_usernames`) — менеджер, который анкету не подавал, иначе виден как «id N»."""
    user = await get_user(telegram_id)
    if user is not None:
        name, uname = user.get("full_name"), user.get("username")
    else:
        started = await get_reg_started_by_id(telegram_id)
        name, uname = None, (started or {}).get("username")
    if not (name or "").strip() and not (uname or "").strip().lstrip("@").strip("-"):
        seen = await chat_username_entry(telegram_id) or {}
        name, uname = seen.get("first_name"), seen.get("username")
    uname = (uname or "").strip().lstrip("@")
    if uname == "-":  # анкета без username хранит «-» (services/registration/reg_finalize.py) — это «нет»
        uname = ""
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
        from services.infra.timeutil import aware_to_msk
        return aware_to_msk(dt).strftime("%d.%m.%Y %H:%M МСК")
    return utc_naive_to_msk(dt).strftime("%d.%m.%Y %H:%M МСК")
