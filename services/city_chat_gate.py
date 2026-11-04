"""Кто в чате города свой, кого показать в сводке и можно ли принять заявку на вход.

Без aiogram: решение читает уже записанный состав и статус заявки. Состав здесь
не пишется и второй таблицей людей не заводится.
"""
from __future__ import annotations

from database import city_chat_db, db as _db
from database.db import chat_member_ids, users_status_city
from domain.cities import normalize_city
from services.chat_tracking import bound_chats

REASON_NO_BOT = "нет в боте"
REASON_REJECTED = "отклонён"
REASON_OTHER_CITY = "другой город"
REASON_NOT_APPROVED = "заявка ещё не одобрена"

_REASON_RANK = {
    REASON_NO_BOT: 0,
    REASON_REJECTED: 1,
    REASON_OTHER_CITY: 2,
    REASON_NOT_APPROVED: 3,
}
_NO_NAME = "без имени и ника"
_CHUNK = 500
_MISSING = object()


async def _binding(chat_id: int) -> dict | None:
    wanted = int(chat_id)
    for entry in await bound_chats():
        if entry["chat_id"] == wanted:
            return entry
    return None


def _is_ours(status, event_city, chat_city) -> bool:
    if status != "approved":
        return False
    if chat_city is None:
        return True
    return normalize_city(event_city) == normalize_city(chat_city)


def _reason(status, event_city, chat_city) -> str:
    if status is _MISSING:
        return REASON_NO_BOT
    if status == "rejected":
        return REASON_REJECTED
    if chat_city is not None and normalize_city(event_city) != normalize_city(chat_city):
        return REASON_OTHER_CITY
    return REASON_NOT_APPROVED


def _clean_username(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    nick = raw.strip().lstrip("@")
    return nick or None


def _display_name(full_name, first_name) -> str:
    for raw in (full_name, first_name):
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return _NO_NAME


async def _names(ids: list[int]) -> dict[int, dict]:
    out = {
        int(tid): {"full_name": None, "username": None, "first_name": None, "chat_username": None}
        for tid in ids
    }
    if not ids:
        return out
    async with _db._connect() as conn:
        for offset in range(0, len(ids), _CHUNK):
            part = [int(tid) for tid in ids[offset:offset + _CHUNK]]
            marks = ",".join("?" for _ in part)
            async with conn.execute(
                f"SELECT telegram_id, full_name, username FROM users "
                f"WHERE telegram_id IN ({marks})",
                part,
            ) as cur:
                for tid, full_name, username in await cur.fetchall():
                    slot = out[int(tid)]
                    slot["full_name"] = full_name
                    slot["username"] = username
            async with conn.execute(
                f"SELECT telegram_id, first_name, username FROM chat_usernames "
                f"WHERE telegram_id IN ({marks})",
                part,
            ) as cur:
                for tid, first_name, username in await cur.fetchall():
                    slot = out[int(tid)]
                    slot["first_name"] = first_name
                    slot["chat_username"] = username
    return out


async def outsider_rows(chat_id: int) -> list[dict]:
    """Люди в составе, которых сводке надо показать. Нет привязки — пустой список."""
    entry = await _binding(chat_id)
    if entry is None:
        return []
    present = await chat_member_ids(int(chat_id))
    allowed = await city_chat_db.allowed_ids(int(chat_id))
    ids = sorted(present - allowed)
    if not ids:
        return []
    known = await users_status_city(ids)
    names = await _names(ids)
    chat_city = entry["city"]
    rows = []
    for tid in ids:
        profile = names.get(tid) or {}
        if tid not in known:
            status, event_city = _MISSING, None
        else:
            status, event_city = known[tid]
        if status is not _MISSING and _is_ours(status, event_city, chat_city):
            continue
        username = _clean_username(profile.get("username")) or _clean_username(
            profile.get("chat_username")
        )
        rows.append({
            "telegram_id": tid,
            "name": _display_name(profile.get("full_name"), profile.get("first_name")),
            "username": username,
            "reason": _reason(status, event_city, chat_city),
        })
    rows.sort(key=lambda row: (_REASON_RANK[row["reason"]], row["name"], row["telegram_id"]))
    return rows


async def may_approve(chat_id: int, telegram_id: int) -> bool:
    """Привязанный чат и человек был бы своим одобренным делегатом. Ничего не пишет."""
    entry = await _binding(chat_id)
    if entry is None:
        return False
    known = await users_status_city([int(telegram_id)])
    row = known.get(int(telegram_id))
    if row is None:
        return False
    status, event_city = row
    return _is_ours(status, event_city, entry["city"])
