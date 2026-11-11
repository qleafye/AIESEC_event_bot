"""Догонялка несдатчиков анкеты с карточки опроса.

Не «всем approved»: только те, кому приглашение уже ушло, кто сегодня отметился на входе
и ещё не закончил. Город — из шапки менеджера, как у остальной аудитории опросов.
"""
from __future__ import annotations

import logging

from database.db import (
    CHECKIN_DAY_TODAY,
    CHECKIN_YES,
    count_and_list_filtered,
    list_poll_messages,
    notice_blocked_ids,
)
from database.survey_db import finished_survey_user_ids
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)


async def arrived_today_ids(*, city_scope=None) -> set[int]:
    filters: list[dict] = [
        {"field": "checkin_entry", "value": CHECKIN_YES, "day": CHECKIN_DAY_TODAY},
    ]
    if city_scope is not None:
        code, exclude = city_scope
        filters.append({"field": "event_city", "value": code, "exclude": list(exclude)})
    return set(await count_and_list_filtered(filters))


async def nudge_ids(poll_id: int, *, city_scope=None) -> list[int]:
    delivered = {int(r["chat_id"]) for r in await list_poll_messages(poll_id)}
    finished = await finished_survey_user_ids(poll_id)
    arrived = await arrived_today_ids(city_scope=city_scope)
    return sorted(delivered & arrived - finished)


async def send_nudge(bot, poll, user_ids: list[int]) -> tuple[int, int]:
    from services.comms.polls import _send_invite

    now = msk_now()
    sent = failed = 0
    finished = await finished_survey_user_ids(poll["id"])
    blocked = await notice_blocked_ids("reminders", now)
    for tid in user_ids:
        if tid in finished or tid in blocked:
            continue
        try:
            await _send_invite(bot, poll, tid, now)
            sent += 1
        except Exception as e:
            failed += 1
            logger.info("survey_nudge: не дошло id=%s poll=%s: %s", tid, poll.get("id"), e)
    return sent, failed
