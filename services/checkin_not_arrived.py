"""Форум-ночь п.6 (D-25, идея №14 бэклога чек-ина): шаблон «Не пришёл» — одобренным текущего
сезона без отметки «Вход» уходит вопрос «Мы тебя не видим на форуме, всё в порядке?» с тремя
кнопками ответа. Аудитория — `database.db.checkin_not_arrived_pending_ids`
(поле фильтра `checkin_entry`=`CHECKIN_NO`, тот же SQL, что использует общий мастер фильтра
рассылки, `.planning/FORUM-CHECKIN.md` D-25).

ТОЛЬКО ручной запуск, БЕЗ авто-рассылки и БЕЗ джобы APScheduler (владелец не ответил, можно ли
доверять этому в CSV-режиме — регионы могут ещё догружать отметки файлами, часть пришедших
получила бы сообщение по ошибке; риск явно называется в подтверждении менеджеру,
`handlers/admin_checkin.py`). Идемпотентно по (делегат, день) —
`checkin_not_arrived_mark_sent` пишет строку ПЕРЕД отправкой (не после): повторный тап «в
процессе» не берёт того же человека дважды.

Тихие часы делегата — `services.quiet_hours.send_or_queue_text` НА КАЖДОГО, не рассылка города
целиком (в отличие от `services/checkin_broadcast.py`, где QR — фото и не умещается в очередь
тихих часов по `file_id`): это простой текст + три кнопки, ровно то, что очередь несёт как
есть."""
from __future__ import annotations

import asyncio
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import (
    CNA_CANT,
    CNA_COMING,
    CNA_HERE,
    checkin_not_arrived_mark_sent,
    checkin_not_arrived_pending_ids,
    checkin_not_arrived_summary,
    get_user,
)
from services import scheduler as _sched
from services.timeutil import msk_now

logger = logging.getLogger(__name__)


def _response_kb(day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚶 Уже еду", callback_data=f"cna:{CNA_COMING}:{day}")],
        [InlineKeyboardButton(text="😔 Не смогу прийти", callback_data=f"cna:{CNA_CANT}:{day}")],
        [InlineKeyboardButton(text="📍 Я на месте", callback_data=f"cna:{CNA_HERE}:{day}")],
    ])


async def _translated(telegram_id: int, text: str) -> str:
    """Тот же перевод, что у остальных ответов делегату — ленивый импорт (Pitfall
    циклического импорта на уровне модуля, см. докстринг `services/checkin_broadcast.py`)."""
    from handlers.reg_i18n import tr_text
    from services import i18n as i18n_service

    lang, tr_map = await i18n_service.context(telegram_id)
    return tr_text(text, lang, tr_map)


async def pending_count(*, city_scope=None) -> int:
    """Превью «Уйдёт N делегатам» для экрана подтверждения (handlers/admin_checkin.py)."""
    ids = await checkin_not_arrived_pending_ids(city_scope=city_scope)
    return len(ids)


async def send(*, city: str | None, city_scope=None) -> dict:
    """Отправляет шаблон СЕЙЧАС всем кандидатам города (пусто — все города, модуль выключен).
    `city` — СНИМОК для `checkin_not_arrived_text`/`event_city` строки (per_city резолвер),
    `city_scope` — дескриптор `cities.city_scope(city)`, тот же приём, что у остальных
    checkin-функций (вызывающий готовит оба, `db.py` не может импортировать `cities`)."""
    from cities import get_setting_typed_for_city
    from services import quiet_hours

    ids = await checkin_not_arrived_pending_ids(city_scope=city_scope)
    base_text = await get_setting_typed_for_city("checkin_not_arrived_text", city)
    now = msk_now()
    day = now.strftime("%Y-%m-%d")
    sent = queued = failed = 0
    for tid in ids:
        user = await get_user(tid)
        if user is None:
            continue
        # Идемпотентность СНАЧАЛА, не после отправки: двойной тап «Написать не пришедшим»,
        # пока первый вызов ещё отправляет, не должен взять того же человека второй раз.
        marked = await checkin_not_arrived_mark_sent(
            tid, user.get("event_city"), now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if not marked:
            continue
        text = await _translated(tid, base_text)
        kb = _response_kb(day)

        async def _sender(cid=tid, txt=text, markup=kb):
            await _sched._bot.send_message(cid, txt, reply_markup=markup)

        try:
            delivered = await quiet_hours.send_or_queue_text(now, tid, text, sender=_sender, reply_markup=kb)
        except Exception as e:
            logger.error(f"checkin_not_arrived.send: доставка {tid} упала: {e}")
            failed += 1
            continue
        if delivered:
            sent += 1
        else:
            queued += 1
        await asyncio.sleep(0.05)
    logger.info(
        f"checkin_not_arrived.send({city!r}): sent {sent}, queued {queued}, failed {failed} "
        f"of {len(ids)}"
    )
    return {"sent": sent, "queued": queued, "failed": failed, "total": len(ids)}


async def summary_text(*, city_scope=None) -> str:
    """«Едут N · Не смогут M · Уже на месте K · без ответа R» за СЕГОДНЯ — строка экрана
    «✅ Отметки на форуме» (handlers/admin_checkin.py). `0` по всем — ещё никому не слали
    шаблон сегодня, вызывающий сам решает, показывать ли строку вовсе."""
    s = await checkin_not_arrived_summary(city_scope=city_scope)
    return (
        f"Едут {s['coming']} · Не смогут {s['cant']} · Уже на месте {s['here']} · "
        f"без ответа {s['no_response']}"
    )
