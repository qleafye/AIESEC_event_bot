"""Сопоставление анкеты внешней формы с делегатом (только с `users`, не с `reg_started`).

Сопоставление — удобство, а не авторизация: ответ не меняет статусов и прав. Значения анкет
в лог не пишутся, только счётчики.
"""
from __future__ import annotations

import logging
import re

from database import db as _db
from database import ext_forms_db as ef
from services.access.person_search import parse_query

logger = logging.getLogger(__name__)

_USERNAME_LABEL = re.compile(r"\bник\b|\bникнейм|телеграм|telegram|\bтг\b|\btg\b|username|юзернейм")
_PHONE_LABEL = re.compile(r"телефон|phone|номер")
_PHONE_EXCLUDE = re.compile(r"групп|комнат|паспорт")


def normalize_phone(raw: str | None) -> str | None:
    """Только цифры; меньше 10 цифр -> None, иначе последние 10 (8999… и +7999… равны)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 10:
        return None
    return digits[-10:]


def username_from_value(raw: str | None) -> str | None:
    kind, value = parse_query(raw or "")
    if kind != "username":
        return None
    return _db.username_needle(str(value))


def guess_key_questions(questions: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """По подписям вопросов предлагает (qkey ника, qkey телефона). Первый подходящий побеждает."""
    uq: str | None = None
    pq: str | None = None
    for qkey, label in questions:
        low = (label or "").lower()
        if uq is None and _USERNAME_LABEL.search(low):
            uq = qkey
            continue
        if pq is None and _PHONE_LABEL.search(low) and not _PHONE_EXCLUDE.search(low):
            pq = qkey
    return uq, pq


def _value_of(items: list[dict], qkey: str | None) -> str | None:
    if not qkey:
        return None
    for it in items:
        if it.get("q") == qkey:
            return it.get("value")
    return None


async def _users_by_phone(phone10: str) -> list[int]:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT telegram_id, phone FROM users WHERE phone IS NOT NULL AND TRIM(phone) != ''"
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows if normalize_phone(r[1]) == phone10]


async def _phone_map() -> dict[str, list[int]]:
    """Карта «телефон -> делегаты» одним проходом по users (для прогона rematch)."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT telegram_id, phone FROM users WHERE phone IS NOT NULL AND TRIM(phone) != ''"
        ) as cursor:
            rows = await cursor.fetchall()
    result: dict[str, list[int]] = {}
    for tid, phone in rows:
        key = normalize_phone(phone)
        if key:
            result.setdefault(key, []).append(tid)
    return result


async def match_answer(
    form: dict, items: list[dict], phone_map: dict[str, list[int]] | None = None,
) -> tuple[int | None, str | None]:
    uname = username_from_value(_value_of(items, form.get("key_username_q")))
    if uname:
        user = await _db.get_user_by_username(uname)
        if user:
            return user["telegram_id"], "username"
    phone = normalize_phone(_value_of(items, form.get("key_phone_q")))
    if phone:
        found = phone_map.get(phone, []) if phone_map is not None else await _users_by_phone(phone)
        if len(found) == 1:
            return found[0], "phone"
    return None, None


def answer_username_needle(form: dict, items: list[dict]) -> str | None:
    """Ник из ответа для дашборда: регистр и «@» срезаны, чтобы сравнивать с
    LOWER(chat_usernames.username). None — у формы нет вопроса про ник; '' — ника в ответе нет."""
    qkey = form.get("key_username_q")
    if not qkey:
        return None
    return (username_from_value(_value_of(items, qkey)) or "").lower()


async def backfill_username_needles(limit: int = 1000) -> int:
    """Догон username_needle у ответов, принятых до появления колонки или после смены вопроса
    про ник. Идемпотентно; в лог — только счётчик. Fail-soft."""
    try:
        rows = await ef.list_answers_without_needle(limit)
        forms: dict[int, dict | None] = {}
        updates: list[tuple[int, str]] = []
        for row in rows:
            fid = row["form_id"]
            if fid not in forms:
                forms[fid] = await ef.get_form(fid)
            form = forms[fid]
            if not form:
                continue
            needle = answer_username_needle(form, row["payload"])
            if needle is not None:
                updates.append((row["id"], needle))
        await ef.set_answer_needles(updates)
    except Exception as e:  # noqa: BLE001
        logger.warning("ext_forms: догон ников ответов: %s", type(e).__name__)
        return 0
    if updates:
        logger.info("ext_forms: разобрано ников ответов: %d", len(updates))
    return len(updates)


async def rematch_unmatched(limit: int = 500) -> int:
    await backfill_username_needles()
    rows = await ef.list_unmatched_answers(limit)
    forms: dict[int, dict | None] = {}
    phones = await _phone_map() if rows else None
    done = 0
    for row in rows:
        fid = row["form_id"]
        if fid not in forms:
            forms[fid] = await ef.get_form(fid)
        form = forms[fid]
        if not form or not (form.get("key_username_q") or form.get("key_phone_q")):
            continue
        tid, how = await match_answer(form, row["payload"], phones)
        if tid is not None and await ef.set_answer_match(row["id"], tid, how):
            done += 1
            # Делегации вузов: человек появился позже ответа — тот же хук, что при приёме.
            try:
                from services.delegations.delegations import on_answer_available
                await on_answer_available(row["form_id"], row["answer_id"], reason="rematch")
            except Exception as e:  # noqa: BLE001
                logger.warning("delegations: хук ответа %s формы %s: %s",
                               row["answer_id"], row["form_id"], type(e).__name__)
    if done:
        logger.info("ext_forms: привязано анкет %d из %d", done, len(rows))
    return done
