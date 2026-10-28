"""Состояние делегаций у внешней формы и предупреждение «форма подключена, делегации выключены».

Зачем: владелец подключил форму делегаций в «📝 Внешние формы» и считал, что делегации
работают, — а модуль включается отдельно на экране «🏫 Делегации», и бот отправил делегатам
автоотказ. Теперь карточка формы показывает состояние делегаций, а менеджерам заявок через
сутки «полувключённого» состояния приходит одно предупреждение.

«Относится к делегациям» — форма выбрана как форма делегаций или среди её вопросов есть
вопрос про вуз. «Полувключена» — форма активна, относится к делегациям, а делегации не
включены ни для какой формы. Предупреждение: одно сообщение на все созревшие формы, глобально
не чаще раза в сутки, по одной форме — однократно (до выхода из состояния), не в тихие часы,
тумблер `delegation_off_alert`.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from database import ext_forms_db as ef
from domain.settings.schema import get_setting_typed
from handlers.access.admin_caps import notify_by_capability
from services.comms.quiet_hours import is_quiet, window_for_city
from services.delegations import delegations as dlg
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

_FMT = "%Y-%m-%d %H:%M:%S"
_DAY = timedelta(hours=24)


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], _FMT)
    except ValueError:
        return None


async def _context() -> dict:
    """Общее для всех форм за один проход: выбранная форма, включены ли делегации, тумблер."""
    return {
        "dfid": await dlg.delegation_form_id(),
        "armed": await dlg.is_armed(),
        "alert_on": await get_setting_typed("delegation_off_alert") == "on",
    }


async def _has_university_question(form_id: int) -> bool:
    cols = await ef.list_columns(form_id)
    guess = dlg.guess_delegation_questions([(c["qkey"], c.get("label") or "") for c in cols])
    return guess.get("university") is not None


async def _state(form: dict, ctx: dict, now: datetime) -> dict:
    fid = form["id"]
    dfid, armed = ctx["dfid"], ctx["armed"]
    relevant = (dfid is not None and fid == dfid) or await _has_university_question(fid)
    armed_here = armed and dfid == fid
    armed_other_title = None
    if armed and dfid != fid:
        other = await ef.get_form(dfid)
        armed_other_title = (other or {}).get("title") or "без названия"
    half_on = form.get("status") == "active" and relevant and not armed
    seen = _parse(form.get("dlg_off_seen_at"))
    stale_off = bool(half_on and ctx["alert_on"] and seen is not None and now - seen >= _DAY)
    return {
        "relevant": relevant, "armed_here": armed_here,
        "armed_other_title": armed_other_title, "half_on": half_on, "stale_off": stale_off,
    }


async def form_delegation_state(form: dict) -> dict:
    """{relevant, armed_here, armed_other_title, half_on, stale_off} для карточки формы."""
    return await _state(form, await _context(), msk_now())


async def _in_quiet_hours(now) -> bool:
    window = await window_for_city(None)
    if window is None:
        return False
    return is_quiet(now, window[0], window[1])


def _alert_text(titles: list[str]) -> str:
    names = ", ".join(f"«{html.escape(t)}»" for t in titles)
    if len(titles) == 1:
        lead = f"Форма {names} подключена, в ней есть вопрос про вуз"
    else:
        lead = f"Формы {names} подключены, в них есть вопрос про вуз"
    return (
        "⚠️ <b>Делегации выключены больше суток</b>\n\n"
        f"{lead}, но бот не одобряет делегатов и не пишет им.\n\n"
        "Включить: /admin → «📋 Заявки» → «🏫 Делегации» → «✅ Включить делегации». "
        "Если форма не про делегации — там же выключите «🔔 Напоминать о выключенных делегациях»."
    )


async def alert_off_delegations(bot) -> int:
    """Один проход фоновой проверки; возвращает число отправленных сообщений (0 или 1)."""
    ctx = await _context()
    if not ctx["alert_on"]:
        return 0
    now = msk_now()
    stamp = now.strftime(_FMT)
    ripe: list[dict] = []
    recently_alerted = False
    for form in await ef.list_forms():
        try:
            st = await _state(form, ctx, now)
            seen, alerted = form.get("dlg_off_seen_at"), form.get("dlg_off_alerted_at")
            if not st["half_on"]:
                if seen or alerted:
                    await ef.set_form_dlg_off(form["id"], None, None)
                continue
            if not seen:
                await ef.set_form_dlg_off(form["id"], stamp, alerted)
                continue
            alerted_dt = _parse(alerted)
            if alerted_dt is not None:
                if now - alerted_dt < _DAY:
                    recently_alerted = True
                continue
            seen_dt = _parse(seen)
            if seen_dt is not None and now - seen_dt >= _DAY:
                ripe.append(form)
        except Exception as e:
            logger.error(f"delegations off alert: форма {form.get('id')} не проверена: {e}")
    if not ripe or recently_alerted:
        return 0
    if await _in_quiet_hours(now):
        return 0
    titles = [f.get("title") or "без названия" for f in ripe]
    try:
        await notify_by_capability(bot, "moderate_reg", _alert_text(titles), parse_mode="HTML")
    except Exception as e:
        logger.error(f"delegations off alert: предупреждение не отправлено: {e}")
        return 0
    for form in ripe:
        await ef.set_form_dlg_off(form["id"], form.get("dlg_off_seen_at"), stamp)
    return 1
