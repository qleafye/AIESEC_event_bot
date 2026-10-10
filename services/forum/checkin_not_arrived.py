"""Форум-ночь п.6 (D-25, идея №14 бэклога чек-ина): шаблон «Не пришёл» — одобренным текущего
сезона без отметки «Вход» СЕГОДНЯ (вход каждый день — вчерашний вход не в счёт) уходит вопрос «Мы тебя не видим на форуме, всё в порядке?» с тремя
кнопками ответа. Аудитория — `database.db.checkin_not_arrived_pending_ids`
(поле фильтра `checkin_entry`=`CHECKIN_NO` с `day`=`CHECKIN_DAY_TODAY`, тот же SQL, что использует общий мастер фильтра
рассылки, `.planning/FORUM-CHECKIN.md` D-25).

ТОЛЬКО ручной запуск, БЕЗ авто-рассылки и БЕЗ джобы APScheduler (владелец не ответил, можно ли
доверять этому в CSV-режиме — регионы могут ещё догружать отметки файлами, часть пришедших
получила бы сообщение по ошибке; риск явно называется в подтверждении менеджеру,
`handlers/forum/admin_checkin.py`). Идемпотентно по (делегат, день) —
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
    checkin_not_arrived_unmark,
    checkin_not_arrived_pending_ids,
    checkin_not_arrived_summary,
    get_user,
)
from services import scheduler as _sched
from services.infra.timeutil import city_offset_hours, msk_now, shift_hours
from domain.settings.ui_text_fields import UI_TEXT_SCHEMA, ui_text

logger = logging.getLogger(__name__)

_BUTTON_KEYS = (
    (CNA_COMING, "checkin_not_arrived_coming_button_text"),
    (CNA_CANT, "checkin_not_arrived_cant_button_text"),
    (CNA_HERE, "checkin_not_arrived_here_button_text"),
)


async def button_labels() -> list[str]:
    """Подписи трёх кнопок из настроек — читаются один раз на рассылку."""
    return [await ui_text(key) for _, key in _BUTTON_KEYS]


def _response_kb(day: str, lang: str = "ru", tr_map: dict | None = None,
                 labels: list[str] | None = None) -> InlineKeyboardMarkup:
    """Три кнопки ответа — на языке получателя; `labels` — из `button_labels()`, без них
    подписи по умолчанию. Ленивый импорт (Pitfall циклического импорта на уровне модуля, см.
    докстринг `services/checkin_broadcast.py`)."""
    from handlers.i18n.reg_i18n import tr_text

    m = tr_map or {}
    labels = labels or [UI_TEXT_SCHEMA[key]["default"] for _, key in _BUTTON_KEYS]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr_text(label, lang, m), callback_data=f"cna:{code}:{day}")]
        for label, (code, _) in zip(labels, _BUTTON_KEYS)
    ])


async def pending_count(*, city_scope=None) -> int:
    """Превью «Уйдёт N делегатам» для экрана подтверждения (handlers/forum/admin_checkin.py)."""
    ids = await checkin_not_arrived_pending_ids(city_scope=city_scope)
    return len(ids)


async def is_forum_day(city: str | None, now) -> bool:
    """Сегодня (по календарю города — `now` вызывающий передаёт уже местным) — день форума города (окно `forum_date`..+`sos_active_days`)."""
    from services.sos import sos_active_window
    try:
        window = await sos_active_window(city)
    except Exception as e:  # noqa: BLE001 — без окна просто соблюдаем тихие часы
        logger.error(f"checkin_not_arrived.is_forum_day({city!r}): {e}")
        return False
    return window is not None and window[0] <= now.date() <= window[1]


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
    тихих часах — не отправлено, повторите позже».

    В ДЕНЬ ФОРУМА города тихие часы на этот шаблон не действуют. Окно тихих часов считается по
    Москве (дефолт до 09:00), а у Тюмени это до 11:00 по-местному — ровно время, когда вопрос
    «мы тебя не видим» и нужен. Отправка ручная и с подтверждением менеджера, а адресат
    зарегистрировался на форум, который идёт прямо сейчас, — это служебное сообщение (как QR,
    D-35), а не рассылка. В остальные дни тихие часы соблюдаются, как раньше."""
    from domain.cities import get_setting_typed_for_city
    from services import quiet_hours

    ids = await checkin_not_arrived_pending_ids(city_scope=city_scope)
    base_text = await get_setting_typed_for_city("checkin_not_arrived_text", city)
    now = msk_now()
    day = now.strftime("%Y-%m-%d")
    from handlers.i18n.reg_i18n import tr_text
    from services.i18n import i18n as i18n_service

    sent = quiet = failed = 0
    tr_maps: dict[str, dict] = {}
    labels = await button_labels()
    # День форума — по календарю города (Тюмень МСК+2 уже на следующих сутках в 22:00 МСК).
    forum_today = await is_forum_day(city, shift_hours(now, await city_offset_hours(city)))
    for tid in ids:
        user = await get_user(tid)
        if user is None:
            continue
        if not forum_today and await quiet_hours.defer_until(now, tid) is not None:
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
        kb = _response_kb(day, lang, tr_map, labels)
        # 429 — один ретрай внутри `_safe_send`. Временный сбой снимает отметку: иначе делегат
        # навсегда выпадал из повторного нажатия. Заблокировавший бота остаётся отмеченным —
        # повтор ему всё равно не дойдёт.
        permanent: list[int] = []

        async def _remember(cid):
            permanent.append(cid)

        ok = await _sched._safe_send(
            lambda cid: _sched._bot.send_message(cid, text, reply_markup=kb), tid,
            on_permanent_failure=_remember,
        )
        if not ok:
            if not permanent:
                await checkin_not_arrived_unmark(tid, day)
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
    «✅ Отметки на форуме» (handlers/forum/admin_checkin.py). `0` по всем — ещё никому не слали
    шаблон сегодня, вызывающий сам решает, показывать ли строку вовсе."""
    s = await checkin_not_arrived_summary(city_scope=city_scope)
    return (
        f"Едут {s['coming']} · Не смогут {s['cant']} · Уже на месте {s['here']} · "
        f"без ответа {s['no_response']}"
    )
