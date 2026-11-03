"""Выпуск и проверка закрытой ссылки чата города. Без aiogram: клиент передаёт вызывающий.

Наружу уходит только URL из свежего ответа Telegram. Срок и лимит в своей таблице
сами ссылку не отдают. Ошибка запроса — фраза менеджеру, без URL и без кода настройки.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from config import config
from database import city_chat_db
from domain.cities import cities_module_on, city_label, normalize_city
from services import chat_tracking

logger = logging.getLogger(__name__)

_NO_LINK = "Ссылки ещё нет. Выберите, как пускать людей, и бот её создаст."
_JOIN = (
    "Сейчас в чат пускают по заявке. Одобренный делегат этого города проходит сам, "
    "остальные ждут вас в заявках."
)
_OPEN = (
    "Сейчас ссылка пускает сразу. Кто зайдёт и не является одобренным делегатом "
    "этого города, попадёт в сводку. Сам бот никого не выгоняет."
)
_NO_RIGHTS = "Бот не может проверить ссылку: в чате ему не дали право приглашать людей."
_CHECK_FAILED = "Не смог проверить ссылку в чат. Старую ссылку не отправил."
_BAD_LIMIT = "Лимит — число от 1 до 99999, например 200."
_BAD_DAYS = "Срок — число дней, например 14."
_NAME_LIMIT = 32


@dataclass
class LinkResult:
    ok: bool
    url: str | None
    error: str | None
    creates_join_request: bool | None


def _unix(value) -> int | None:
    if value is None or value == "":
        return None
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _past(value, now: float) -> bool:
    stamp = _unix(value)
    return stamp is not None and stamp <= now


def _dead_text(exc: Exception) -> bool:
    text = str(exc).upper()
    return "INVITE_HASH_EXPIRED" in text or "EXPIRE_DATE_INVALID" in text


def _no_rights(exc: Exception) -> bool:
    text = str(exc).upper()
    return (
        "CHAT_ADMIN_REQUIRED" in text
        or "NOT_ENOUGH_RIGHTS" in text
        or "RIGHT_FORBIDDEN" in text
        or "HAVE NO RIGHTS" in text
    )


def _log(city: str | None, exc: Exception) -> None:
    logger.warning(
        "city_chat_link: city=%s exc=%s",
        city or "",
        type(exc).__name__,
    )


async def _offline(city: str | None) -> str:
    if not city:
        return "Чат города не подключён."
    label = await city_label(city)
    return f"Чат города «{label}» не подключён."


def _fail(city: str | None, exc: Exception) -> LinkResult:
    _log(city, exc)
    phrase = _NO_RIGHTS if _no_rights(exc) else _CHECK_FAILED
    return LinkResult(False, None, phrase, None)


def _in_range(value, low: int, high: int) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return low <= number <= high


async def _name(city: str | None) -> str:
    base = "Делегаты"
    if not city:
        return base
    text = f"{base} {await city_label(city)}"
    return text[:_NAME_LIMIT]


def _expire_at(lifetime_days: int | None, now: float) -> int | None:
    if not lifetime_days:
        return None
    return int(now) + int(lifetime_days) * 86400


def _words(row: dict) -> str:
    join = bool(row.get("creates_join_request"))
    head = _JOIN if join else _OPEN
    days = row.get("lifetime_days")
    term = f"на {int(days)} дней" if days else "без срока"
    limit = row.get("member_limit")
    people = "без лимита" if join or not limit else f"не больше {int(limit)} человек"
    return f"{head}\nСрок: {term}. Лимит: {people}."


async def _persist(city: str | None, chat_id: int, resp, lifetime_days: int | None) -> str | None:
    url = getattr(resp, "invite_link", None) or None
    if not url:
        return None
    join = bool(getattr(resp, "creates_join_request", False))
    limit = None if join else _unix(getattr(resp, "member_limit", None))
    await city_chat_db.save_link(
        city,
        chat_id=chat_id,
        invite_link=url,
        creates_join_request=1 if join else 0,
        expire_date=_unix(getattr(resp, "expire_date", None)),
        member_limit=limit,
        lifetime_days=lifetime_days,
    )
    return url


async def _revoke(client, chat_id: int, url: str | None, city: str | None) -> None:
    if not url:
        return
    try:
        await client.revoke_invite(chat_id, url)
    except Exception as exc:
        if "INVITE_HASH_EXPIRED" not in str(exc).upper():
            _log(city, exc)


async def _mint(
    client,
    city: str | None,
    chat_id: int,
    *,
    join: bool,
    member_limit: int | None,
    lifetime_days: int | None,
    old_url: str | None,
) -> LinkResult:
    now = time.time()
    limit = None if join else member_limit
    try:
        resp = await client.create_invite(
            chat_id,
            await _name(city),
            _expire_at(lifetime_days, now),
            limit,
            join,
        )
    except Exception as exc:
        return _fail(city, exc)
    url = await _persist(city, chat_id, resp, lifetime_days)
    if not url:
        return LinkResult(False, None, _CHECK_FAILED, None)
    if old_url and old_url != url:
        await _revoke(client, chat_id, old_url, city)
    return LinkResult(True, url, None, join)


async def _probe(client, city: str | None, chat_id: int, row: dict) -> LinkResult:
    try:
        resp = await client.edit_invite(
            chat_id,
            row.get("invite_link"),
            await _name(city),
            _unix(row.get("expire_date")),
            row.get("member_limit"),
        )
    except Exception as exc:
        if _dead_text(exc):
            return await _mint(
                client, city, chat_id,
                join=bool(row.get("creates_join_request", 1)),
                member_limit=row.get("member_limit"),
                lifetime_days=row.get("lifetime_days"),
                old_url=row.get("invite_link"),
            )
        return _fail(city, exc)
    if bool(getattr(resp, "is_revoked", False)) or _past(getattr(resp, "expire_date", None), time.time()):
        return await _mint(
            client, city, chat_id,
            join=bool(row.get("creates_join_request", 1)),
            member_limit=row.get("member_limit"),
            lifetime_days=row.get("lifetime_days"),
            old_url=row.get("invite_link"),
        )
    url = await _persist(city, chat_id, resp, row.get("lifetime_days"))
    if not url:
        return LinkResult(False, None, _CHECK_FAILED, None)
    return LinkResult(True, url, None, bool(getattr(resp, "creates_join_request", False)))


async def issue(client, city: str | None) -> LinkResult:
    chat = await chat_tracking.chat_for_city(city)
    if not chat:
        return LinkResult(False, None, await _offline(city), None)
    row = await city_chat_db.get_link(city)
    if not row or not row.get("invite_link"):
        return await _mint(
            client, city, chat["chat_id"],
            join=True, member_limit=None, lifetime_days=None, old_url=None,
        )
    return await _probe(client, city, chat["chat_id"], row)


async def set_policy(
    client,
    city: str | None,
    *,
    join_requests: bool,
    member_limit: int | None,
    lifetime_days: int | None,
) -> LinkResult:
    if lifetime_days is not None and not _in_range(lifetime_days, 1, 365):
        return LinkResult(False, None, _BAD_DAYS, None)
    if not join_requests and member_limit is not None and not _in_range(member_limit, 1, 99999):
        return LinkResult(False, None, _BAD_LIMIT, None)
    chat = await chat_tracking.chat_for_city(city)
    if not chat:
        return LinkResult(False, None, await _offline(city), None)
    row = await city_chat_db.get_link(city)
    return await _mint(
        client, city, chat["chat_id"],
        join=bool(join_requests),
        member_limit=None if join_requests else member_limit,
        lifetime_days=lifetime_days,
        old_url=(row or {}).get("invite_link"),
    )


async def describe(client, city: str | None) -> str:
    chat = await chat_tracking.chat_for_city(city)
    if not chat:
        return await _offline(city)
    row = await city_chat_db.get_link(city)
    if not row or not row.get("invite_link"):
        return _NO_LINK
    result = await _probe(client, city, chat["chat_id"], row)
    if not result.ok:
        return result.error or _CHECK_FAILED
    fresh = await city_chat_db.get_link(city)
    return _words(fresh or row)


async def recipients_for_city(city: str | None) -> list[int]:
    from database.db import get_staff_city, get_staff_ids_by_role
    from domain.settings.schema import get_setting_typed
    from handlers.access.admin_caps import ROLES, role_caps_key, role_enabled_key

    holders: list[int] = []
    seen: set[int] = set()
    for admin_id in config.ADMIN_IDS:
        if admin_id not in seen:
            holders.append(admin_id)
            seen.add(admin_id)
    for role in ROLES:
        if await get_setting_typed(role_enabled_key(role)) != "on":
            continue
        caps = await get_setting_typed(role_caps_key(role)) or []
        if "settings" not in caps:
            continue
        for uid in await get_staff_ids_by_role(role):
            if uid not in seen:
                holders.append(uid)
                seen.add(uid)
    if city is None or not await cities_module_on():
        return holders
    target = normalize_city(city)
    picked: list[int] = []
    for uid in holders:
        if uid in config.ADMIN_IDS:
            picked.append(uid)
            continue
        bound = await get_staff_city(uid)
        if not bound or normalize_city(bound) == target:
            picked.append(uid)
    return picked
