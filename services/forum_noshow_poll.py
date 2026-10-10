"""Идея №23 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): опрос неявившихся
«почему не пришёл» — на СЛЕДУЮЩИЙ день после ПОСЛЕДНЕГО дня форума города, в настраиваемое
время (per_city, дефолт 12:00), одобренным делегатам текущего сезона этого города БЕЗ отметки
входа НИ В ОДИН день форума уходит один вопрос с пятью кнопками ответа.

Аудитория — СТРОГО: одобрен + `event_season` текущий + город + нет отметки входа
(`database.db.forum_noshow_poll_pending_ids`, тот же фильтр `checkin_entry`=`CHECKIN_NO`, что
`checkin_not_arrived_pending_ids` — единая точка правды, второй копии условия не заводится).

Идемпотентность — ОДИН опрос на делегата за `event_season`+город (`database.db.
forum_noshow_poll`, `UNIQUE(telegram_id, season)`); опрос НЕ повторяется, если бот был
перезапущен/джоба сработала снова в том же сезоне.

Тихие часы — В ОТЛИЧИЕ от `services/checkin_not_arrived.py` (то сообщение теряет смысл, если
доставить его с задержкой — «мы тебя не видим ПРЯМО СЕЙЧАС»), опрос «почему не пришёл» остаётся
верным независимо от момента доставки, поэтому используем ОЧЕРЕДЬ тихих часов
(`services.quiet_hours.send_or_queue_text`), а не молчаливый пропуск: у этой джобы нет ручной
кнопки повтора (в отличие от «Написать не пришедшим»), пропущенный делегат иначе не получил бы
опрос вовсе.

«🔕 Не присылать сегодня» опрос НЕ глушит (решение владельца 26.09): вопрос приходит всем
неявившимся, как служебное сообщение, — иначе причины неявки собираются неполными.

Троттлинг отправки — `asyncio.sleep(0.05)` между получателями, тот же приём, что
`services/checkin_not_arrived.py`/`services/checkin_volunteer_broadcast.py`.

Планирование — ОДНА one-shot date-джоба на город (тот же приём, что
`services/checkin_volunteer_broadcast.py`), без self-rescheduling (в отличие от
`services/forum_day_report.py` — опрос уходит РОВНО один раз в один день, не каждый день окна
форума)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from domain.cities import get_setting_typed_for_city
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

_JOB_PREFIX = "forum_noshow_poll:"
DEFAULT_TIME = "12:00"

DEFAULT_QUESTION = "Мы не видели тебя на площадке. Расскажешь, что помешало прийти?"
DEFAULT_OTHER_PROMPT = "Напиши своими словами, что помешало прийти."
DEFAULT_THANKS = "Спасибо, учтём!"

OPTION_KEYS = {
    "changed_mind": "forum_noshow_poll_option_changed_mind_text",
    "study_work": "forum_noshow_poll_option_study_work_text",
    "far": "forum_noshow_poll_option_far_text",
    "forgot": "forum_noshow_poll_option_forgot_text",
    "other": "forum_noshow_poll_option_other_text",
}
OPTION_DEFAULTS = {
    "changed_mind": "Передумал(а)",
    "study_work": "Не смог(ла) по учёбе/работе",
    "far": "Далеко ехать",
    "forgot": "Забыл(а)",
    "other": "Другое",
}


def job_id(city: str | None) -> str:
    return f"{_JOB_PREFIX}{city or 'all'}"


async def _last_forum_day(city: str | None) -> date | None:
    from services.reject_rules import forum_date_for  # ленивый импорт — цикл-разрыв, тот же
    # приём, что services/forum_day_report.py/services/sos.py.

    raw = await forum_date_for(city)
    if not raw:
        return None
    try:
        start = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None

    from services.sos import DEFAULT_ACTIVE_DAYS  # публичная константа, sos.py не правим

    raw_days = await get_setting_typed_for_city("sos_active_days", city)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = DEFAULT_ACTIVE_DAYS
    if days < 1:
        days = 1
    return start + timedelta(days=days - 1)


def _parse_hhmm(raw: str | None) -> tuple[int, int]:
    text = (raw or DEFAULT_TIME).strip()
    try:
        hh_s, mm_s = text.split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        return hh, mm
    except (ValueError, TypeError):
        hh_s, mm_s = DEFAULT_TIME.split(":")
        return int(hh_s), int(mm_s)


async def enabled_for(city: str | None) -> bool:
    return await get_setting_typed_for_city("forum_noshow_poll_enabled", city) == "on"


async def _time_for(city: str | None) -> str:
    return await get_setting_typed_for_city("forum_noshow_poll_time", city) or DEFAULT_TIME


async def schedule_city_job(city: str | None) -> dict:
    """(Пере)ставить/снять one-shot джобу этого города — вызывается и `reconcile()` (старт
    бота), и СРАЗУ после правки настройки (`handlers/settings/admin_settings.py::
    _reschedule_forum_noshow_poll_if_relevant`)."""
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    jid = job_id(city)

    if not await enabled_for(city):
        cancel_city_job(city)
        return {"scheduled": False, "reason": "disabled"}

    last_day = await _last_forum_day(city)
    if last_day is None:
        cancel_city_job(city)
        return {"scheduled": False, "reason": "no_date"}
    target_day = last_day + timedelta(days=1)

    now = msk_now()
    if now.date() > target_day + timedelta(days=2):
        # Целевой день давно прошёл (бот был выключен несколько дней) — не досылаем опрос
        # спустя долгое время, тот же баланс, что у session_feedback._CATCHUP_GRACE_HOURS.
        cancel_city_job(city)
        return {"scheduled": False, "reason": "past"}

    hh, mm = _parse_hhmm(await _time_for(city))
    run_at = datetime.combine(target_day, time(hh, mm))
    if run_at <= now:
        run_at = now + timedelta(minutes=1)  # догон — бот был выключен в момент отправки

    sched.add_job(
        _run_job, "date", run_date=run_at, args=[city], id=jid, replace_existing=True,
    )
    return {"scheduled": True, "run_at": run_at}


def cancel_city_job(city: str | None) -> None:
    from services.scheduler import get_scheduler

    try:
        get_scheduler().remove_job(job_id(city))
    except Exception:
        pass  # не стояла или уже сработала — оба случая ОК


async def _city_still_valid(city: str | None) -> bool:
    if city is not None:
        from domain.cities import cities_module_on, enabled_cities
        if await cities_module_on():
            codes = {c["code"] for c in await enabled_cities()}
            if city not in codes:
                return False
    return await enabled_for(city)


async def _run_job(city: str | None) -> dict:
    if not await _city_still_valid(city):
        logger.info(f"forum_noshow_poll: job for city={city!r} skipped — город/опрос выключены")
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}
    return await send_poll(city)


def _cancel_stale_city_jobs(enabled_codes: set[str]) -> None:
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    for job in sched.get_jobs():
        if not job.id.startswith(_JOB_PREFIX):
            continue
        code = job.id[len(_JOB_PREFIX):]
        if code != "all" and code not in enabled_codes:
            try:
                sched.remove_job(job.id)
            except Exception:
                pass


async def reconcile() -> list[str | None]:
    from domain.cities import cities_module_on, enabled_cities

    touched: list[str | None] = []
    try:
        if await cities_module_on():
            enabled_codes = {c["code"] for c in await enabled_cities()}
            codes = list(enabled_codes)
        else:
            enabled_codes = None
            codes = [None]
        for code in codes:
            try:
                await schedule_city_job(code)
                touched.append(code)
            except Exception as e:
                logger.error(f"forum_noshow_poll.reconcile({code!r}) failed: {e}")
        if enabled_codes is not None:
            try:
                _cancel_stale_city_jobs(enabled_codes)
            except Exception as e:
                logger.error(f"forum_noshow_poll.reconcile: stale sweep failed: {e}")
    except Exception as e:
        logger.error(f"forum_noshow_poll.reconcile failed: {e}")
    return touched


# ── Рассылка ───────────────────────────────────────────────────────────────────────────────

def poll_keyboard(labels: dict[str, str]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=labels[reason], callback_data=f"fnsp:{reason}")]
        for reason in OPTION_KEYS
    ])


async def _option_labels() -> dict[str, str]:
    from domain.settings.schema import get_setting_typed

    return {
        reason: (await get_setting_typed(key)) or OPTION_DEFAULTS[reason]
        for reason, key in OPTION_KEYS.items()
    }


async def send_poll(city: str | None) -> dict:
    """Отправляет опрос всем кандидатам города (`city=None` — все города, модуль выключен).
    Троттлинг/мут/тихие часы — докстринг модуля."""
    from database.db import forum_noshow_poll_mark_sent, forum_noshow_poll_pending_ids, get_user
    from domain.settings.schema import get_setting_typed
    from services import quiet_hours
    import domain.cities as _cities

    bot = _bot()
    if bot is None:
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}

    season = (await get_setting_typed("event_season") or "").strip()
    scope = _cities.city_scope(city)
    targets = await forum_noshow_poll_pending_ids(city_scope=scope)
    if not targets:
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}

    question = await get_setting_typed("forum_noshow_poll_question_text") or DEFAULT_QUESTION
    labels = await _option_labels()
    now = msk_now()

    sent = queued = skipped_muted = failed = 0  # skipped_muted остаётся 0: «🔕» опрос не глушит
    for tid in targets:
        user = await get_user(tid)
        if user is None:
            continue
        marked = await forum_noshow_poll_mark_sent(
            tid, user.get("event_city"), season, now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if not marked:
            continue

        from handlers.i18n import reg_i18n
        from services import i18n as i18n_service

        lang, tr_map = await i18n_service.context(tid)
        text = reg_i18n.tr_text(question, lang, tr_map)
        kb = reg_i18n.tr_kb(poll_keyboard(labels), lang, tr_map)
        try:
            delivered_now = await quiet_hours.send_or_queue_text(
                now, tid, text,
                sender=lambda cid=tid, t=text, k=kb: bot.send_message(cid, t, reply_markup=k),
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"forum_noshow_poll.send_poll: доставка {tid} упала: {e}")
            failed += 1
            continue
        if delivered_now:
            sent += 1
        else:
            queued += 1
        await asyncio.sleep(0.05)

    logger.info(
        f"forum_noshow_poll.send_poll({city!r}): sent {sent}, queued {queued}, "
        f"muted {skipped_muted}, failed {failed} of {len(targets)}"
    )
    return {"sent": sent, "queued": queued, "muted": skipped_muted, "failed": failed, "total": len(targets)}


def _bot():
    try:
        import services.scheduler as scheduler_module
        return scheduler_module.get_bot()
    except Exception:
        return None


# ── Ответ делегата ─────────────────────────────────────────────────────────────────────────

async def record_answer(telegram_id: int, reason: str, comment: str | None) -> bool:
    """`False` — делегат не получал опрос в текущем сезоне (чужой/устаревший callback_data) —
    вызывающий отвечает тихо, не пишет вслепую (тот же приём, что `services.session_feedback.
    record_rating`)."""
    from database.db import record_forum_noshow_poll_response
    from domain.settings.schema import get_setting_typed

    season = (await get_setting_typed("event_season") or "").strip()
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    return await record_forum_noshow_poll_response(telegram_id, season, reason, comment, stamp)


async def summary_text(*, city_scope=None) -> str:
    """«Ответили N из M: передумал 12, учёба 7…» — строка экрана менеджера (`handlers/
    admin_forum_functions.py`). `total_sent == 0` — вызывающий сам решает, показывать ли строку
    вовсе (тот же приём, что `services.checkin_not_arrived.summary_text`)."""
    from database.db import forum_noshow_poll_summary
    from domain.settings.schema import get_setting_typed

    season = (await get_setting_typed("event_season") or "").strip()
    s = await forum_noshow_poll_summary(season, city_scope=city_scope)
    by_reason = s["by_reason"]
    parts = ", ".join(
        f"{OPTION_DEFAULTS[r]} {by_reason.get(r, 0)}" for r in OPTION_KEYS
    )
    return f"Ответили {s['answered']} из {s['sent']}: {parts}"
