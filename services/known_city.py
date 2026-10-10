"""Квик 27.09: «какой город анкеты этого делегата уже известен» — одна цепочка для бота и
Mini App.

Прод YouLead (три открытых города) получал заявки с `users.event_city = NULL`: обходные входы
в анкету («Заново» на экране черновика после перезапуска бота, старая кнопка, продолжение
черновика без города, подача из приложения) начинали/подавали анкету, не зная города и не
спрашивая его. Эта функция отвечает только на вопрос «что уже известно», решение «спросить /
подставить / город закрыт» остаётся за `reg_engine.city_gate`.

Порядок звеньев — от самого свежего факта к самому старому:
1. черновик анкеты (`reg_drafts.event_city`) — текущая анкета;
2. строка незавершённой регистрации (`reg_started.event_city`, без окна свежести: сюда
   приходят только из уже идущей анкеты, а не с голого /start — там окно
   `reg_resume_ttl_hours` по-прежнему решает, спрашивать ли город заново);
3. заявка ЭТОГО сезона (`users.event_city`) — повторная подача отклонённого/одобренного;
4. последняя непустая запись воронки ЭТОГО сезона (`reg_events.event_city`).

Город прошлого сезона не подставляется (CONTEXT B: возвращенца спрашиваем заново). Код,
которого нет в справочнике городов, пропускается. Каждое звено fail-soft — сбой чтения
значит «не знаем», а не падение входа в анкету.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _known_code(code: str | None) -> str | None:
    if not code:
        return None
    from cities import all_cities
    return code if code in {c["code"] for c in all_cities()} else None


async def known_city(telegram_id: int) -> str | None:
    from database.db import (
        get_last_reg_event_city, get_reg_draft, get_reg_started_city, get_user,
    )
    from domain.settings.schema import get_setting_typed

    try:
        draft = await get_reg_draft(telegram_id)
        code = _known_code((draft or {}).get("event_city"))
        if code:
            return code
    except Exception as e:
        logger.error(f"known_city: draft lookup failed for {telegram_id}: {e}")

    try:
        code = _known_code(await get_reg_started_city(telegram_id))
        if code:
            return code
    except Exception as e:
        logger.error(f"known_city: reg_started lookup failed for {telegram_id}: {e}")

    try:
        season = (await get_setting_typed("event_season") or "").strip() or None
    except Exception as e:
        logger.error(f"known_city: event_season resolve failed for {telegram_id}: {e}")
        season = None

    try:
        import domain.regform.engine as reg_engine
        user = await get_user(telegram_id)
        if user and not reg_engine.is_past_season_row(user, season):
            code = _known_code(user.get("event_city"))
            if code:
                return code
    except Exception as e:
        logger.error(f"known_city: users lookup failed for {telegram_id}: {e}")

    try:
        return _known_code(await get_last_reg_event_city(telegram_id, season))
    except Exception as e:
        logger.error(f"known_city: reg_events lookup failed for {telegram_id}: {e}")
    return None
