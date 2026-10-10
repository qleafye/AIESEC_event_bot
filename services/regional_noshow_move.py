"""Трек «региональные форумы → Москва» (04.10): после последнего дня форума РЕГИОНАЛЬНОГО
города (СПб/Тюмень 03.10) одобренным делегатам текущего сезона, которых не видели ни на входе,
ни на сессиях форума, уходит предложение перенести заявку на форум города назначения
(`regional_noshow_target_city`, дефолт — `cities.default_city_code()`, НИКОГДА не хардкод msk).

Форма модуля byte-в-byte `services/forum_noshow_poll.py` — та же идемпотентность отправки
(`UNIQUE(telegram_id, season)`, `database.db.regional_noshow_move`), та же one-shot date-джоба на
город, та же очередь тихих часов, тот же fail-soft. Отличия:

  - аудитория ДОПОЛНИТЕЛЬНО минус те, кто в `forum_noshow_poll` ЭТОГО сезона выбрал одну из
    «не интересно»-причин (решение координатора 25.09: «Передумал(а)» И «Не смог(ла) по
    учёбе/работе», `database.db._forum_noshow_poll_not_interested_ids`) — предлагать перенос
    тому, кто прямо сказал «не интересно», незачем; остальные причины (далеко/забыл/другое)
    предложение получают как обычно;
  - порядок с опросом неявившихся: если ОБА тумблера включены — предложение уходит НЕ РАНЬШЕ,
    чем через `_POLL_OFFSET_HOURS` часов после расчётного времени отправки опроса (`_run_at_for`
    ниже). Простое и детерминированное правило вместо ожидания «ответил ли делегат на опрос» —
    неотвеченный опрос иначе блокировал бы предложение бесконечно; расстояние в часы гарантирует
    ровно то, что требуется («не слать одновременно»), без обращения к состоянию опроса другого
    делегата на момент отправки;
  - ответ делегата не одна кнопка, а мини-флоу «предложение -> подтверждение -> перенос»
    (`handlers/user_actions.py::rnm_accept/rnm_confirm/rnm_decline`) — сам перенос делает
    `services/city_move.py::move_user_city` (Phase 33), эта строка только фиксирует факт
    (`response`/`target_city`) в своей таблице; `rnm_confirm` ПЕРЕД самим переносом
    перепроверяет ВСЁ заново из БД (`revalidate_confirm`, ревью 🔴1) — состояние могло
    измениться между показом предложения и тапом «Да, перенести»;
  - менеджеру города НАЗНАЧЕНИЯ уходит АГРЕГИРОВАННАЯ сводка, не сообщение на каждого
    переехавшего — интервальная джоба `_notify_managers_job` (раз в `_NOTIFY_INTERVAL_MINUTES`
    минут) сканирует ещё не отправленные строки `response=RNM_MOVED`, группирует по городу
    назначения и шлёт одно сообщение держателям `moderate_reg` (`handlers.access.admin_caps.
    notify_by_capability`, city-scoped)."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, time, timedelta

from domain.cities import default_city_code, get_setting_typed_for_city, normalize_city
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

_JOB_PREFIX = "regional_noshow_move:"
_NOTIFY_JOB_ID = "regional_noshow_move:notify_managers"
_NOTIFY_INTERVAL_MINUTES = 30
_POLL_OFFSET_HOURS = 3  # см. докстринг модуля — расстояние от расчётного времени опроса

DEFAULT_TIME = "12:00"
# {city} — регион, где не получилось прийти; {target_city} — город назначения (`city_label`,
# название БЕЗ падежа — сама метка может быть произвольной строкой реестра, «в {target_city}»
# грамматически ломалось бы на части городов); {dates} — уже включает ведущий пробел и скобки
# (`_dates_label_for`), поэтому пустая дата не оставляет «висящего» текста в конце фразы.
DEFAULT_OFFER_TEXT = "Не получилось на форум в {city}? Приезжай на {event}: {target_city}{dates}"

STATUS_MODE_KEEP = "keep"
STATUS_MODE_TO_MODERATION = "to_moderation"


def job_id(city: str | None) -> str:
    return f"{_JOB_PREFIX}{city or 'all'}"


# ── Резолверы настроек ──────────────────────────────────────────────────────────────────────

async def enabled_for(city: str | None) -> bool:
    return await get_setting_typed_for_city("regional_noshow_offer_enabled", city) == "on"


async def _time_for(city: str | None) -> str:
    return await get_setting_typed_for_city("regional_noshow_offer_time", city) or DEFAULT_TIME


async def target_city_for(city: str | None) -> str:
    """Город назначения — `regional_noshow_target_city` этого (регионального) города, дефолт —
    `cities.default_city_code()` (главный/московский город реестра, НИКОГДА не хардкод "msk")."""
    raw = await get_setting_typed_for_city("regional_noshow_target_city", city)
    return raw or default_city_code()


async def move_status_for(city: str | None) -> str:
    raw = await get_setting_typed_for_city("regional_noshow_move_status", city)
    return raw if raw == STATUS_MODE_TO_MODERATION else STATUS_MODE_KEEP


async def _offer_text_for(city: str | None) -> str:
    return await get_setting_typed_for_city("regional_noshow_offer_text", city) or DEFAULT_OFFER_TEXT


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


# ── Дата последнего дня форума (своя копия — тот же приём, что sos.py/forum_day_menu.py/
# forum_day_report.py/forum_noshow_poll.py/staff_expiry.py, ни один из них не переиспользует
# чужой приватный хелпер) ────────────────────────────────────────────────────────────────────

async def _last_forum_day(city: str | None) -> date | None:
    from services.reject_rules import forum_date_for  # ленивый импорт — цикл-разрыв, тот же
    # приём, что у соседних модулей.

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


async def _dates_label_for(target_city: str) -> str:
    """« (30.10–31.10)» (или один день, если форум города назначения однодневный) — ведущий
    пробел и скобки НАРОЧНО часть возвращаемого значения (ревью 🔴2): плейсхолдер `{dates}`
    подставляется в конец фразы default-текста, и когда дата не задана функция отдаёт пустую
    строку — фраза остаётся грамматически целой, без «висящего» пробела/слова. Формат без года
    (`%d.%m`) — единообразно для обеих границ диапазона. Подставляется ПОСЛЕ перевода текста
    (докстринг модуля, RULES.md)."""
    from services.reject_rules import forum_date_for

    raw = await forum_date_for(target_city)
    if not raw:
        return ""
    raw = raw.strip()
    last_day = await _last_forum_day(target_city)
    try:
        start = datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return ""
    if last_day is None or last_day == start:
        label = start.strftime("%d.%m")
    else:
        label = f"{start.strftime('%d.%m')}–{last_day.strftime('%d.%m')}"
    return f" ({label})"


# ── Планирование ─────────────────────────────────────────────────────────────────────────────

async def _run_at_for(city: str | None) -> tuple[datetime | None, str | None]:
    last_day = await _last_forum_day(city)
    if last_day is None:
        return None, "no_date"
    target_day = last_day + timedelta(days=1)

    hh, mm = _parse_hhmm(await _time_for(city))
    run_at = datetime.combine(target_day, time(hh, mm))

    from services.forum_noshow_poll import enabled_for as poll_enabled_for
    if await poll_enabled_for(city):
        poll_hh, poll_mm = _parse_hhmm(await get_setting_typed_for_city("forum_noshow_poll_time", city))
        poll_run_at = datetime.combine(target_day, time(poll_hh, poll_mm))
        min_run_at = poll_run_at + timedelta(hours=_POLL_OFFSET_HOURS)
        if min_run_at > run_at:
            run_at = min_run_at

    return run_at, None


async def schedule_city_job(city: str | None) -> dict:
    """(Пере)ставить/снять one-shot джобу этого города — вызывается и `reconcile()` (старт
    бота), и сразу после правки настройки (`handlers/admin_settings.py::
    _reschedule_regional_noshow_move_if_relevant`)."""
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    jid = job_id(city)

    if not await enabled_for(city):
        cancel_city_job(city)
        return {"scheduled": False, "reason": "disabled"}

    run_at, reason = await _run_at_for(city)
    if run_at is None:
        cancel_city_job(city)
        return {"scheduled": False, "reason": reason}

    now = msk_now()
    if now.date() > run_at.date() + timedelta(days=2):
        # Целевой день давно прошёл (бот был выключен несколько дней) — не досылаем предложение
        # спустя долгое время, тот же баланс, что у forum_noshow_poll.schedule_city_job.
        cancel_city_job(city)
        return {"scheduled": False, "reason": "past"}
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
        logger.info(f"regional_noshow_move: job for city={city!r} skipped — город/предложение выключены")
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}
    return await send_offers(city)


def _cancel_stale_city_jobs(enabled_codes: set[str]) -> None:
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    for job in sched.get_jobs():
        if not job.id.startswith(_JOB_PREFIX) or job.id == _NOTIFY_JOB_ID:
            continue
        code = job.id[len(_JOB_PREFIX):]
        if code != "all" and code not in enabled_codes:
            try:
                sched.remove_job(job.id)
            except Exception:
                pass


def _reconcile_notify_job() -> None:
    from services.scheduler import get_scheduler

    sched = get_scheduler()
    sched.add_job(
        _notify_managers_job, "interval", minutes=_NOTIFY_INTERVAL_MINUTES,
        id=_NOTIFY_JOB_ID, replace_existing=True,
    )


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
                logger.error(f"regional_noshow_move.reconcile({code!r}) failed: {e}")
        if enabled_codes is not None:
            try:
                _cancel_stale_city_jobs(enabled_codes)
            except Exception as e:
                logger.error(f"regional_noshow_move.reconcile: stale sweep failed: {e}")
        try:
            _reconcile_notify_job()
        except Exception as e:
            logger.error(f"regional_noshow_move.reconcile: notify job failed: {e}")
    except Exception as e:
        logger.error(f"regional_noshow_move.reconcile failed: {e}")
    return touched


# ── Рассылка предложения ────────────────────────────────────────────────────────────────────

OFFER_BUTTON_KEYS = ("regional_noshow_accept_button_text", "regional_noshow_decline_button_text")


async def offer_button_labels() -> tuple[str, str]:
    """Подписи «Перенести»/«Нет, спасибо» из настроек — один раз на рассылку."""
    from domain.settings.ui_text_fields import ui_text

    return await ui_text(OFFER_BUTTON_KEYS[0]), await ui_text(OFFER_BUTTON_KEYS[1])


def offer_keyboard(labels: tuple[str, str] | None = None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from domain.settings.ui_text_fields import UI_TEXT_SCHEMA

    accept, decline = labels or tuple(UI_TEXT_SCHEMA[key]["default"] for key in OFFER_BUTTON_KEYS)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=accept, callback_data="rnm_accept")],
        [InlineKeyboardButton(text=decline, callback_data="rnm_decline")],
    ])


def _localized_offer_keyboard(lang: str, tr_map: dict, target_label: str,
                              labels: tuple[str, str] | None = None):
    """Перевод клавиатуры СНАЧАЛА, подстановка `{target_city}` в подпись кнопки ПОСЛЕ
    (RULES.md) — `offer_keyboard()` независимый шаблон на каждый вызов, ни он, ни результат
    `reg_i18n.tr_kb` (при `lang == "ru"` это ТОТ ЖЕ объект) не мутируются, чтобы общий для всех
    получателей города шаблон не потёк подстановкой одного делегата в подпись другого."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from handlers.i18n import reg_i18n

    kb = reg_i18n.tr_kb(offer_keyboard(labels), lang, tr_map)
    rows = [
        [
            InlineKeyboardButton(
                text=btn.text.replace("{target_city}", target_label), callback_data=btn.callback_data,
            )
            for btn in row
        ]
        for row in kb.inline_keyboard
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_offers(city: str | None) -> dict:
    """Отправляет предложение всем кандидатам региона (`city=None` — все города, модуль
    выключен). Троттлинг/мут/тихие часы — тот же приём, что `forum_noshow_poll.send_poll`."""
    from database.db import get_muted_today_ids, get_user, regional_noshow_move_mark_sent, regional_noshow_move_pending_ids
    from domain.settings.schema import get_setting_typed
    from services import quiet_hours
    import domain.cities as _cities

    bot = _bot()
    if bot is None:
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}

    season = (await get_setting_typed("event_season") or "").strip()
    scope = _cities.city_scope(city)
    targets = await regional_noshow_move_pending_ids(city_scope=scope)
    if not targets:
        return {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}

    target_city = await target_city_for(city)
    target_label = await _cities.city_label(target_city)
    raw_text = await _offer_text_for(city)
    dates_label = await _dates_label_for(target_city)
    from services.text_fill import event_name, fill_event

    event_title = await event_name()
    button_labels = await offer_button_labels()
    now = msk_now()
    muted = await get_muted_today_ids(now.strftime("%Y-%m-%d"))

    sent = queued = skipped_muted = failed = 0
    for tid in targets:
        if tid in muted:
            skipped_muted += 1
            continue
        user = await get_user(tid)
        if user is None:
            continue
        marked = await regional_noshow_move_mark_sent(
            tid, user.get("event_city"), season, now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if not marked:
            continue

        from handlers.i18n import reg_i18n
        from services import i18n as i18n_service

        lang, tr_map = await i18n_service.context(tid)
        source_city_label = await _cities.city_label(_cities.normalize_city(user.get("event_city")))
        text = reg_i18n.tr_text(raw_text, lang, tr_map)
        # Плейсхолдеры подставляются ПОСЛЕ перевода (RULES.md) — иначе EN-делегат увидел бы
        # русское название города/даты в переведённом тексте. Названия городов экранируются
        # (ревью 🟡3) — сообщение уходит с parse_mode=HTML по умолчанию бота, произвольный текст
        # реестра города иначе мог бы сломать разметку.
        text = (
            text.replace("{city}", html.escape(source_city_label))
            .replace("{target_city}", html.escape(target_label))
            .replace("{dates}", dates_label)
        )
        text = fill_event(text, event_title, lang, escape=True)
        tr_kb = _localized_offer_keyboard(lang, tr_map, target_label, button_labels)
        try:
            delivered_now = await quiet_hours.send_or_queue_text(
                now, tid, text,
                sender=lambda cid=tid, t=text, k=tr_kb: bot.send_message(cid, t, reply_markup=k),
                reply_markup=tr_kb,
            )
        except Exception as e:
            logger.warning(f"regional_noshow_move.send_offers: доставка {tid} упала: {e}")
            failed += 1
            continue
        if delivered_now:
            sent += 1
        else:
            queued += 1
        await asyncio.sleep(0.05)

    logger.info(
        f"regional_noshow_move.send_offers({city!r}): sent {sent}, queued {queued}, "
        f"muted {skipped_muted}, failed {failed} of {len(targets)}"
    )
    return {"sent": sent, "queued": queued, "muted": skipped_muted, "failed": failed, "total": len(targets)}


def _bot():
    try:
        import services.scheduler as scheduler_module
        return scheduler_module.get_bot()
    except Exception:
        return None


# ── Ответ делегата ───────────────────────────────────────────────────────────────────────────

async def get_state(telegram_id: int) -> dict | None:
    """Текущая строка предложения этого делегата в ТЕКУЩЕМ сезоне — `None`, если предложение
    не уходило вовсе (чужой/устаревший callback_data, вызывающий отвечает тихо)."""
    from database.db import regional_noshow_move_get

    season = await _current_season()
    return await regional_noshow_move_get(telegram_id, season)


async def record_decline(telegram_id: int) -> bool:
    from database.db import RNM_DECLINED, record_regional_noshow_move_response

    season = await _current_season()
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    return await record_regional_noshow_move_response(telegram_id, season, RNM_DECLINED, None, stamp)


async def _current_season() -> str:
    from domain.settings.schema import get_setting_typed

    return (await get_setting_typed("event_season") or "").strip()


# Ревью 🔴1: причины отказа `rnm_confirm` перед самим переносом — хендлер решает текст ответа
# делегату по одному из этих сентинелов, сама причина здесь не завязана на язык/формулировку.
RNM_REASON_ALREADY_ANSWERED = "already_answered"
RNM_REASON_WRONG_CITY = "wrong_city"
RNM_REASON_NO_USER = "no_user"
RNM_REASON_INELIGIBLE = "ineligible"


async def revalidate_confirm(telegram_id: int) -> dict:
    """Полная перепроверка ПЕРЕД самим переносом (`handlers/user_actions.py::
    regional_noshow_move_confirm`, ревью 🔴1) — состояние делегата могло измениться ПОСЛЕ
    отправки предложения и ДО тапа «Да, перенести»: заявку отклонили на модерации, отметили вход
    на форум, перевели вручную в другой город, сезон сменился, предложение уже отвечено. Каждая
    проверка — свежий запрос к БД, ничего не переиспользует из того, что читал `rnm_accept` на
    прошлом шаге.

    Возвращает `{"ok": bool, "reason": str | None, "state": dict | None,
    "source_city": str | None}`. `state is None` — строки предложения нет вовсе (чужой/
    устаревший `callback_data`) — хендлер отвечает тихо, без alert. `reason` — один из
    `RNM_REASON_*` выше, значим только при `ok=False` и `state is not None`."""
    from database.db import CHECKIN_ENTRY_POINT, get_user, list_checkins_for_user

    state = await get_state(telegram_id)
    if state is None:
        return {"ok": False, "reason": None, "state": None, "source_city": None}

    source_city = normalize_city(state.get("source_city"))
    if state.get("response") is not None:
        return {
            "ok": False, "reason": RNM_REASON_ALREADY_ANSWERED,
            "state": state, "source_city": source_city,
        }

    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "reason": RNM_REASON_NO_USER, "state": state, "source_city": source_city}

    if normalize_city(user.get("event_city")) != source_city:
        return {"ok": False, "reason": RNM_REASON_WRONG_CITY, "state": state, "source_city": source_city}

    if user.get("status") != "approved":
        return {"ok": False, "reason": RNM_REASON_INELIGIBLE, "state": state, "source_city": source_city}

    # Тот же гард «approved + текущий сезон», что `database.db._approved_current_season_frag`
    # (единая точка правды для аудитории `regional_noshow_move_pending_ids`): `season IS NULL`
    # у делегата — не блокируем, различаются только НЕСОВПАДАЮЩИЕ явные сезоны.
    season = await _current_season()
    user_season = (user.get("season") or "").strip()
    if season and user_season and user_season != season:
        return {"ok": False, "reason": RNM_REASON_INELIGIBLE, "state": state, "source_city": source_city}

    checkins = await list_checkins_for_user(telegram_id)
    if any(c.get("point") == CHECKIN_ENTRY_POINT for c in checkins):
        return {"ok": False, "reason": RNM_REASON_INELIGIBLE, "state": state, "source_city": source_city}

    return {"ok": True, "reason": None, "state": state, "source_city": source_city}


async def apply_move(telegram_id: int, *, source_city: str | None) -> dict:
    """Выполняет сам перенос (`services.city_move.move_user_city`) и фиксирует ответ строкой
    `response=RNM_MOVED`. Порядок НАРОЧНО такой (ревью 🟡4): СНАЧАЛА атомарный захват строки
    (`database.db.regional_noshow_move_claim`, `UPDATE ... WHERE response IS NULL`), ПОТОМ сам
    перенос — не наоборот. Если бы строка писалась ПОСЛЕ `move_user_city`, два одновременных
    тапа «Да, перенести» оба успевали бы пройти сам перенос (двойная запись в лист/БД/историю)
    прежде, чем решилось бы, кто из них первый.

    `report["claim_lost"] = True` — гонка проиграна (строку уже забрал другой вызов),
    `move_user_city` вообще не звался, повторного переноса не будет — хендлер отвечает так же,
    как «уже перенесено». Перенос технически не удался ПОСЛЕ захвата (`report["ok"] = False`,
    `claim_lost = False`) — строка ВОЗВРАЩАЕТСЯ в `response = NULL`
    (`regional_noshow_move_release_claim`), чтобы делегат мог повторить тап.

    `by_admin=0` — системный маркер (перенос инициирован делегатом по кнопке, не менеджером);
    `history_source="system:regional_offer"` (решение координатора 25.09) отличает эту запись
    `reg_answer_history` от ручного перевода менеджером карточкой (дефолт `move_user_city`,
    `source="admin"`)."""
    from database.db import regional_noshow_move_claim, regional_noshow_move_release_claim
    from services.city_move import move_user_city

    target_city = await target_city_for(source_city)
    status_mode = await move_status_for(source_city)
    season = await _current_season()
    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")

    claimed = await regional_noshow_move_claim(telegram_id, season, target_city, stamp)
    if not claimed:
        return {"ok": False, "error": None, "claim_lost": True}

    try:
        report = await move_user_city(
            telegram_id, target_city, status_mode=status_mode, by_admin=0, dry_run=False,
            history_source="system:regional_offer",
        )
    except Exception as e:  # noqa: BLE001 — захват обязан вернуться, иначе повтор невозможен
        logger.error("regional_noshow_move.apply_move: перенос %s упал: %s", telegram_id, e)
        report = {"ok": False, "error": str(e)}
    report["claim_lost"] = False
    if not report.get("ok"):
        await regional_noshow_move_release_claim(telegram_id, season)
    return report


async def summary_text(*, city_scope=None) -> str:
    """«Предложено N, перенеслись M, отказались K» — строка экрана менеджера (`handlers.
    admin_forum_functions`)."""
    from database.db import regional_noshow_move_summary

    season = await _current_season()
    s = await regional_noshow_move_summary(season, city_scope=city_scope)
    return f"Предложено {s['offered']}, перенеслись {s['moved']}, отказались {s['declined']}"


# ── Сводка менеджеру города назначения ──────────────────────────────────────────────────────

async def _notify_managers_job() -> None:
    """Ревью 🟡5: строки помечаются `notified_at` ДО отправки, не после. Рестарт бота МЕЖДУ
    захватом и отправкой теряет одну сводку менеджеру (строки останутся `notified_at` не-NULL,
    но письмо не дошло) — противоположный порядок (пометить ПОСЛЕ успешной отправки) рисковал
    бы ДУБЛЁМ: рестарт между `notify_by_capability` и `regional_noshow_move_mark_notified` не
    потерял бы прогресс, а заставил бы следующий прогон джобы отправить ТУ ЖЕ сводку повторно.
    Потеря одной сводки менеджеру безопаснее дубля — менеджер сверяет переезды по счётчику
    экрана `_regional_noshow_cfg_text_kb` (`summary_text`), не только по уведомлениям."""
    from database.db import regional_noshow_move_mark_notified, regional_noshow_move_unnotified_moved
    from handlers.access.admin_caps import notify_by_capability
    import domain.cities as _cities

    bot = _bot()
    if bot is None:
        return
    rows = await regional_noshow_move_unnotified_moved()
    if not rows:
        return

    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    await regional_noshow_move_mark_notified([r["id"] for r in rows], now)

    by_target: dict[str | None, list[dict]] = {}
    for row in rows:
        by_target.setdefault(row.get("target_city"), []).append(row)

    for target_city, target_rows in by_target.items():
        by_source: dict[str | None, int] = {}
        for row in target_rows:
            source = row.get("source_city")
            by_source[source] = by_source.get(source, 0) + 1
        parts = []
        for source, count in by_source.items():
            label = html.escape(await _cities.city_label(_cities.normalize_city(source)))
            parts.append(f"из {label} — {count}")
        total = len(target_rows)
        text = f"🚌 Перенеслись {total} делегат(ов) регионального форума: {', '.join(parts)}."
        try:
            await notify_by_capability(bot, "moderate_reg", text, city=target_city)
        except Exception as e:
            logger.error(f"regional_noshow_move._notify_managers_job: notify({target_city!r}) failed: {e}")
            continue
