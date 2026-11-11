"""Одно письмо одному человеку. Тот же допуск, что кнопка «Написать» на карточке.

Не рассылка: выключатели глушения это письмо не читают. Фото и файл остаются в чате,
потому что оттуда сообщение копируется как есть. Из приложения уходит только текст.
"""
from __future__ import annotations

import html
import logging

from database.db import (
    get_reg_started_by_id,
    get_user,
    record_admin_action,
    search_users_by_name,
)
from domain.cities import city_label_or_none, normalize_city

logger = logging.getLogger(__name__)

NEED_CAP = (
    "Чтобы написать одному человеку, нужно право заявок. "
    "Попросите того, кто выдаёт доступ, его включить."
)
NOT_FOUND = "Человек не найден — карточка устарела."
OTHER_CITY = "Этот человек из другого города. Написать ему может тот, кто ведёт этот город."
EMPTY = "Напишите текст письма. Пустое не уходит."
TOO_LONG = "Текст длиннее одного сообщения. Сократите его и отправьте снова."
FILE_NOTE = "Фото и файл по-прежнему уходят кнопкой «Написать» в чате."
BLOCKED = "Не дошло: человек заблокировал бота."
FAILED = "Не смог отправить письмо. Откройте карточку в чате и попробуйте ещё раз."
_MAX = 4000


def person_name(person: dict | None) -> str:
    text = " ".join(str((person or {}).get("full_name") or "").split())
    if text:
        return text
    username = str((person or {}).get("username") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return "без имени"


async def city_allowed(admin_id: int, code: str | None) -> bool:
    """Тот же допуск, что у кнопки чата: привязанный менеджер пишет только своему городу."""
    if code is None:
        return True
    from domain.settings.ops import per_city_visible_codes

    return code in await per_city_visible_codes(admin_id)


async def load_person(tid: int) -> tuple[dict | None, str | None]:
    user = await get_user(tid)
    if user is not None:
        return user, normalize_city(user.get("event_city"))
    started = await get_reg_started_by_id(tid)
    if started is not None:
        return started, normalize_city(started.get("event_city"))
    return None, None


async def gate(admin_id: int, tid: int) -> tuple[dict | None, str | None]:
    person, city = await load_person(tid)
    if person is None:
        return None, NOT_FOUND
    if not await city_allowed(admin_id, city):
        return None, OTHER_CITY
    return person, None


async def mark_sent(admin_id: int, tid: int) -> None:
    await record_admin_action(actor_id=admin_id, subject_id=tid, action="write_message")


async def screen(admin_id: int, query: str) -> dict:
    needle = " ".join((query or "").split())
    if len(needle) < 2:
        return {
            "text": "Введите имя — хотя бы две буквы. Покажем людей, которым вы можете написать.",
            "people": [],
            "file_note": FILE_NOTE,
        }
    rows = await search_users_by_name(needle, limit=20)
    people = []
    for row in rows:
        city = normalize_city(row.get("event_city"))
        if not await city_allowed(admin_id, city):
            continue
        label = await city_label_or_none(row.get("event_city"))
        people.append({
            "id": int(row["telegram_id"]),
            "name": person_name(row),
            "city": label or "",
        })
    if people:
        text = "Один человек — одно письмо. Это не рассылка."
    else:
        text = "Никого с таким именем не нашлось. Проверьте написание или откройте карточку в чате."
    return {"text": text, "people": people, "file_note": FILE_NOTE}


async def letter_decision(admin_id: int, tid: int, text: str, *, confirm: bool) -> dict:
    person, error = await gate(admin_id, tid)
    if error:
        return {"ok": False, "text": error}
    body = (text or "").strip()
    if not body:
        return {"ok": False, "text": EMPTY}
    if len(body) > _MAX:
        return {"ok": False, "text": TOO_LONG}
    name = person_name(person)
    if not confirm:
        shown = body if len(body) <= 300 else body[:300] + "…"
        return {
            "ok": True,
            "need_confirm": True,
            "text": (
                f"Отправить {name} одно письмо?\n\n"
                f"{shown}\n\n"
                "Уйдёт только этот текст, не рассылка. "
                "Фото и файл по-прежнему из чата."
            ),
        }
    return {
        "ok": True,
        "text": f"Письмо для {name} поставлено в очередь.",
        "enqueue": {
            "admin_id": int(admin_id),
            "telegram_id": int(tid),
            "text": body,
        },
    }


async def deliver_text(bot, payload: dict) -> None:
    """Отправка из очереди приложения. Чат по-прежнему копирует сообщение менеджера сам."""
    try:
        admin_id = int(payload.get("admin_id") or 0)
        tid = int(payload.get("telegram_id") or 0)
    except (TypeError, ValueError):
        return
    body = str(payload.get("text") or "").strip()
    if admin_id <= 0 or tid <= 0 or not body or len(body) > _MAX:
        return
    person, error = await gate(admin_id, tid)
    if person is None or error:
        return
    from aiogram.exceptions import TelegramForbiddenError

    try:
        await bot.send_message(tid, html.escape(body), parse_mode="HTML")
    except TelegramForbiddenError:
        await _tell(bot, admin_id, BLOCKED)
        return
    except Exception:
        logger.exception("personal_letter: не ушло id=%s", tid)
        await _tell(bot, admin_id, FAILED)
        return
    await mark_sent(admin_id, tid)


async def _tell(bot, admin_id: int, text: str) -> None:
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.info("personal_letter: менеджеру %s не сказали про сбой", admin_id)
