"""Форум-ночь п.6 (D-25, идея №14 бэклога чек-ина): шаблон «Не пришёл» — одобренным текущего
сезона без отметки «Вход» СЕГОДНЯ (вход каждый день — вчерашний вход не в счёт) уходит вопрос «Мы тебя не видим на форуме, всё в порядке?» с тремя
кнопками ответа. Аудитория — `database.db.checkin_not_arrived_pending_ids`
(поле фильтра `checkin_entry`=`CHECKIN_NO` с `day`=`CHECKIN_DAY_TODAY`, тот же SQL, что использует общий мастер фильтра
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


def _response_kb(day: str, lang: str = "ru", tr_map: dict | None = None) -> InlineKeyboardMarkup:
    """Три кнопки ответа — на языке получателя. Ленивый импорт (Pitfall циклического импорта
    на уровне модуля, см. докстринг `services/checkin_broadcast.py`)."""
    from handlers.reg_i18n import tr_text

    m = tr_map or {}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr_text("🚶 Уже еду", lang, m), callback_data=f"cna:{CNA_COMING}:{day}")],
        [InlineKeyboardButton(text=tr_text("😔 Не смогу прийти", lang, m), callback_data=f"cna:{CNA_CANT}:{day}")],
        [InlineKeyboardButton(text=tr_text("📍 Я на месте", lang, m), callback_data=f"cna:{CNA_HERE}:{day}")],
    ])


async def pending_count(*, city_scope=None) -> int:
    """Превью «Уйдёт N делегатам» для экрана подтверждения (handlers/admin_checkin.py)."""
    ids = await checkin_not_arrived_pending_ids(city_scope=city_scope)
    return len(ids)


async def send(*, city: str | None, city_scope=None) -> dict:
    """Отправляет шаблон СЕЙЧАС всем кандидатам города (пусто — все города, модуль выключен).
    `city` — СНИМОК для `checkin_not_arrived_text`/`event_city` строки (per_city резолвер),
    `city_scope` — дескриптор `cities.city_scope(city)`, тот же приём, что у остальных
    checkin-функций (вызывающий готовит оба, `db.py` не может импортировать `cities`).

    Тихие часы делегата — НЕ ставим в очередь (в отличие от остальной семьи
    `send_or_queue_*`): шаблон спрашивает «мы тебя не видим ПРЯМО СЕЙЧАС», и если положить его
    в очередь `quiet_hours`, `flush_due` доставит его утром БЕЗ повторной проверки отметки —
    пришедший ночью/рано утром делегат получит «мы тебя не видим» уже после того, как отметился
    (`flush_due` не умеет перечитывать условие на момент доставки для `KIND_TEXT`, только для
    `KIND_APPLICATION_DECISION`). Поэтому такого делегата ПРОПУСКАЕМ целиком — и, ВАЖНО, НЕ
    зовём `checkin_not_arrived_mark_sent`: раз ему не отправили, идемпотентность «раз в день»
    не блокирует повторный тап «Написать не пришедшим» позже (после тихих часов) — придёт как в
    первый раз. Менеджеру считаем отдельно (`quiet`), экран подтверждения показывает «N сейчас в
    тихих часах — не отправлено, повторите позже»."""
    from core.cities import get_setting_typed_for_city
    from services import quiet_hours

    ids = await checkin_not_arrived_pending_ids(city_scope=city_scope)
    base_text = await get_setting_typed_for_city("checkin_not_arrived_text", city)
    now = msk_now()
    day = now.strftime("%Y-%m-%d")
    from handlers.reg_i18n import tr_text
    from services import i18n as i18n_service

    sent = quiet = failed = 0
    tr_maps: dict[str, dict] = {}
    for tid in ids:
        user = await get_user(tid)
        if user is None:
            continue
        if await quiet_hours.defer_until(now, tid) is not None:
            quiet += 1
            continue
        # Идемпотентность СНАЧАЛА, не после отправки: двойной тап «Написать не пришедшим»,
        # пока первый вызов ещё отправляет, не должен взять того же человека второй раз.
        marked = await checkin_not_arrived_mark_sent(
            tid, user.get("event_city"), now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if not marked:
            continue
        lang, tr_map = await i18n_service.context_cached(tid, tr_maps)
        text = tr_text(base_text, lang, tr_map)
        kb = _response_kb(day, lang, tr_map)
        try:
            await _sched._bot.send_message(tid, text, reply_markup=kb)
        except Exception as e:
            logger.error(f"checkin_not_arrived.send: доставка {tid} упала: {e}")
            failed += 1
            continue
        sent += 1
        await asyncio.sleep(0.05)
    logger.info(
        f"checkin_not_arrived.send({city!r}): sent {sent}, quiet {quiet}, failed {failed} "
        f"of {len(ids)}"
    )
    return {"sent": sent, "quiet": quiet, "failed": failed, "total": len(ids)}


def report_text(result: dict, expected: int | None = None) -> str:
    """Итог для менеджера после «📨 Написать не пришедшим». `expected` — сколько обещал экран
    подтверждения. Ушло меньше — объясняем, куда делась разница: ошибку доставки видно
    по счётчику `failed`, остальных между подтверждением и отправкой больше нет в списке —
    отметились на входе или получили вопрос с параллельного нажатия (различить нельзя)."""
    sent, failed, quiet = result["sent"], result["failed"], result["quiet"]
    if expected and sent < expected:
        text = f"✅ Ушло {sent} из {expected}."
        reasons = []
        gone = max(0, expected - sent - failed - quiet)
        if gone > 0:
            reasons.append(f"{gone} за это время отметились на входе или уже получили вопрос")
        if failed:
            reasons.append(f"{failed} не получили сообщение (ошибка доставки)")
        if reasons:
            text += " Остальные: " + "; ".join(reasons) + "."
    else:
        tail = f", не доставлено {failed}" if failed else ""
        text = f"✅ Отправлено {sent} делегатам{tail} из {result['total']}."
    if quiet:
        text += (
            f"\n🌙 {quiet} делегатов сейчас в тихих часах — им не отправлено, "
            "повторите позже."
        )
    return text


async def summary_text(*, city_scope=None) -> str:
    """«Едут N · Не смогут M · Уже на месте K · без ответа R» за СЕГОДНЯ — строка экрана
    «✅ Отметки на форуме» (handlers/admin_checkin.py). `0` по всем — ещё никому не слали
    шаблон сегодня, вызывающий сам решает, показывать ли строку вовсе."""
    s = await checkin_not_arrived_summary(city_scope=city_scope)
    return (
        f"Едут {s['coming']} · Не смогут {s['cant']} · Уже на месте {s['here']} · "
        f"без ответа {s['no_response']}"
    )
