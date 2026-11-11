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
    get_poll,
    get_user,
    list_poll_messages,
    list_polls,
    notice_blocked_ids,
)
from database.survey_db import finished_survey_user_ids, mark_survey_nudged, survey_nudged_user_ids
from domain.cities import ALL_CITIES, admin_selected_city, city_scope
from services.infra.timeutil import msk_now

NEED_CAP = (
    "Чтобы напоминать несдатчикам, нужно право рассылки. "
    "Попросите того, кто выдаёт доступ, его включить."
)
EMPTY = "Несдатчиков среди пришедших сегодня нет."
ALREADY = "Этим людям уже напоминали. Второй раз не уйдёт."
CLOSED = "Анкета уже закрыта. Напомнить можно только открытую."
NOT_SURVEY = "Напомнить можно только анкету. Обычный опрос откройте в чате."
OTHER_CITY = (
    "Эта анкета другого города. Выберите город в шапке бота "
    "или откройте анкету из списка."
)

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


async def nudge_scope(admin_id: int | None):
    """Тот же охват, что шапка карточки опроса: «Все города» и выключенный модуль — без фильтра."""
    if admin_id is None:
        return None
    code = await admin_selected_city(admin_id)
    if code is None or code == ALL_CITIES:
        return None
    return city_scope(code)


def other_city(scope, poll: dict) -> bool:
    if scope is None or not poll.get("city"):
        return False
    return poll["city"] != scope[0]


def confirm_text(n: int, question: str) -> str:
    title = " ".join((question or "").split()) or "анкета"
    return (
        f"Напомнить {n} чел., кто сегодня отметился на входе и не закончил анкету «{title}»?\n\n"
        "Уйдёт то же приглашение с кнопкой. Кто уже сдал — не получит. "
        "Кому уже напоминали — второй раз не уйдёт."
    )


async def send_nudge(bot, poll, user_ids: list[int]) -> tuple[int, int]:
    from services.comms.polls import _send_invite

    now = msk_now()
    sent = failed = 0
    finished = await finished_survey_user_ids(poll["id"])
    already = await survey_nudged_user_ids(poll["id"])
    blocked = await notice_blocked_ids("reminders", now)
    for tid in user_ids:
        if tid in finished or tid in blocked or tid in already:
            continue
        try:
            await _send_invite(bot, poll, tid, now)
        except Exception as e:
            failed += 1
            logger.info("survey_nudge: не дошло id=%s poll=%s: %s", tid, poll.get("id"), e)
            continue
        await mark_survey_nudged(poll["id"], tid, now.strftime("%Y-%m-%d %H:%M:%S"))
        sent += 1
    return sent, failed


def _name(user: dict | None, tid: int) -> str:
    text = " ".join(((user or {}).get("full_name") or "").split())
    return text or "без имени"


async def screen(admin_id: int) -> dict:
    scope = await nudge_scope(admin_id)
    polls = await list_polls(statuses=("open",), city_scope=scope)
    surveys = []
    for poll in polls:
        if poll.get("kind") != "survey":
            continue
        ids = await nudge_ids(poll["id"], city_scope=scope)
        people = []
        for tid in ids:
            people.append({"id": tid, "name": _name(await get_user(tid), tid)})
        surveys.append({
            "id": int(poll["id"]),
            "question": poll.get("question") or "анкета",
            "count": len(people),
            "people": people,
        })
    if not surveys:
        text = "Открытых анкет нет. Текст напоминания задаётся в самой анкете, здесь он не пишется."
    else:
        text = "Пришедшие сегодня без сдачи. Не все одобренные."
    return {"text": text, "surveys": surveys}


async def invite_decision(admin_id: int, poll_id: int, *, confirm: bool) -> dict:
    poll = await get_poll(int(poll_id))
    if poll is None or poll.get("kind") != "survey":
        return {"ok": False, "text": NOT_SURVEY}
    if poll.get("status") != "open":
        return {"ok": False, "text": CLOSED}
    scope = await nudge_scope(admin_id)
    if other_city(scope, poll):
        return {"ok": False, "text": OTHER_CITY}
    ids = await nudge_ids(poll["id"], city_scope=scope)
    fresh = [tid for tid in ids if tid not in await survey_nudged_user_ids(poll["id"])]
    if not fresh:
        return {"ok": False, "text": ALREADY if ids else EMPTY}
    if not confirm:
        return {
            "ok": True,
            "need_confirm": True,
            "count": len(fresh),
            "text": confirm_text(len(fresh), poll.get("question") or ""),
        }
    return {
        "ok": True,
        "count": len(fresh),
        "text": "Напоминание поставлено в очередь. Второй раз тем же людям не уйдёт.",
        "enqueue": {"admin_id": int(admin_id), "poll_id": int(poll["id"])},
    }
