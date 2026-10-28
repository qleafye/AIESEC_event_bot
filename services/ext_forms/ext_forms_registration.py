"""Форма с назначением «📝 Регистрация»: ответ становится заявкой участника без анкеты бота.

Зачем: часть мероприятий собирает регистрацию в сторонней форме (Яндекс/Google). Каждый новый
ответ такой формы превращается в заявку `pending` (или сразу в одобренного — тумблер формы
«Одобрение»), человек анкету в боте не заполняет.

Человека бот узнаёт ТОЛЬКО по нику в Telegram из ответа и только среди тех, кто сам писал боту
(`users` / `reg_started`) или нажмёт /start позже (`try_registration_start`): форму заполнял кто
угодно, и вписанный чужой ник не должен создать заявку чужому человеку. Ник в `users` берётся
из Telegram, а не из текста формы. Ник за несколькими людьми — без действий ('ambiguous').
У того, кто уже подал анкету, ответ лишь привязывается: статус, анкета и источник не меняются.

Источник заявки — «Внешняя форма «<название>»» (`users.source`, `source_from_tag`) и
`reg_events.source_tag = 'ext_form'`: виден в «📈 Источники» и в фильтре рассылки.

В лог — только id формы, ответа и человека: ФИО, телефон и ник из формы — ПД.
"""
from __future__ import annotations

import html
import logging

from database import ext_forms_db as ef
from database.db import (
    add_user, approve_user_atomic, clear_reg_started, delete_reg_draft, get_reg_started_by_id,
    get_setting, get_user, record_reg_event, set_user_status, store_username,
    update_user_answers, username_needle,
)
from domain.settings.schema import get_setting_typed
from services.ext_forms.ext_forms_match import username_from_value
from services.ext_forms.ext_forms_purpose import SOURCE_TAG, before_purpose, purpose_of, source_label
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

_FMT = "%Y-%m-%d %H:%M:%S"
_DONE = ("created", "linked")
LINK_HOW_USERNAME = "username"


def _value_of(payload: list[dict], qkey: str | None) -> str | None:
    if not qkey:
        return None
    for it in payload or ():
        if isinstance(it, dict) and it.get("q") == qkey:
            value = it.get("value")
            text = str(value).strip() if value is not None else ""
            return text or None
    return None


def extract_fields(form: dict, payload: list[dict]) -> dict:
    """ФИО, телефон (как написан) и ник из ответа по вопросам, выбранным в настройках формы."""
    return {
        "full_name": _value_of(payload, form.get("reg_q_fullname")),
        "phone": _value_of(payload, form.get("key_phone_q")),
        "nick": username_from_value(_value_of(payload, form.get("key_username_q"))),
    }


async def _link_existing(tid: int, answer: dict) -> None:
    await ef.set_answer_reg(int(answer["id"]), "linked", tid)
    if answer.get("matched_telegram_id") is None:
        await ef.set_answer_match(int(answer["id"]), tid, LINK_HOW_USERNAME)


async def _send_pending(bot, tid: int) -> None:
    """Человеку — «заявка из формы получена» (ключ реестра ext_reg_pending_text) и меню."""
    from keyboards.builders import get_main_menu_kb
    from services.comms.text_fill import event_name, fill_event
    from services.i18n.i18n import context as i18n_context
    from services.i18n.i18n import tr
    from services.infra.telegram_send import send_with_retry

    template = await get_setting_typed("ext_reg_pending_text") or ""
    lang, tr_map = "ru", {}
    try:
        lang, tr_map = await i18n_context(tid)
    except Exception:
        logger.exception("ext_forms: язык человека не определён (tid=%s)", tid)
    text = fill_event(tr(template, lang, tr_map), await event_name(), lang, escape=True)
    kb = await get_main_menu_kb(tid)
    err = await send_with_retry(
        lambda: bot.send_message(tid, text, reply_markup=kb, parse_mode="HTML")
    )
    if err is not None:
        logger.warning("ext_forms: сообщение о заявке не доставлено (tid=%s): %s",
                       tid, type(err).__name__)


async def _cleanup_registration(bot, tid: int) -> None:
    """Снять черновик анкеты, отметку старта и незаконченную анкету в чате — анкета не нужна."""
    from services.delegations import delegations as dlg

    for step, coro in (("черновик анкеты", delete_reg_draft(tid)),
                       ("отметка старта", clear_reg_started(tid))):
        try:
            await coro
        except Exception:
            logger.exception("ext_forms: %s не снят(а) (tid=%s)", step, tid)
    if bot is not None:
        try:
            await dlg._reset_reg_fsm(bot, tid)
        except Exception:
            logger.exception("ext_forms: FSM анкеты не сброшен (tid=%s)", tid)


async def _create_application(tid: int, form: dict, answer: dict, fields: dict, *,
                              tg_username: str | None, bot) -> dict:
    """Заявка участника по ответу формы. Ответ сначала занимается ('created'): при сбое до
    создания строки users он возвращается в прежнее состояние и исключение поднимается."""
    import domain.regform.engine as reg_engine
    from domain.cities import default_city_code
    from services.delegations import delegations as dlg

    row_id = int(answer["id"])
    prev_state = answer.get("reg_state")
    if not await ef.claim_answer_reg(row_id, tid):
        return {"already": True}
    season = (await get_setting("event_season") or "").strip() or None
    city = form.get("reg_city") or default_city_code()
    try:
        started = await get_reg_started_by_id(tid)
        real = (started or {}).get("username") or tg_username
        username = store_username("@" + username_needle(real)) if username_needle(real) else "-"
        data = reg_engine.with_defaults({
            "full_name": fields.get("full_name") or "-",
            "phone": fields.get("phone") or "-",
            "source": source_label(form),
            "event_city": city,
            "season": season,
            "participant_type": "full",
        })
        data["telegram_id"] = tid
        data["username"] = username
        data["registration_date"] = msk_now().strftime(_FMT)
        await add_user(data)
    except Exception:
        try:
            await ef.set_answer_reg(row_id, prev_state, None)
        except Exception:
            logger.exception("ext_forms: ответ %s не возвращён после сбоя", row_id)
        raise
    await update_user_answers(tid, {"source_from_tag": 1}, allowed_columns=["source_from_tag"])
    await set_user_status(tid, "pending")
    try:
        await record_reg_event(tid, "form_completed", event_city=city, season=season,
                               source_tag=SOURCE_TAG)
    except Exception:
        logger.exception("ext_forms: событие воронки не записано (tid=%s)", tid)
    if answer.get("matched_telegram_id") is None:
        await ef.set_answer_match(row_id, tid, LINK_HOW_USERNAME)

    resolved_bot = None
    try:
        resolved_bot = dlg._resolve_bot(bot)
    except Exception:
        logger.exception("ext_forms: бот не инициализирован (tid=%s)", tid)
    await _cleanup_registration(resolved_bot, tid)
    if resolved_bot is None:
        return {"created": True, "approve": None}

    if (form.get("reg_approve") or "queue") == "auto":
        await _auto_approve(resolved_bot, tid)
        return {"created": True, "approve": "auto"}
    try:
        from services.reg_digest import notify_application
        admin_text = (
            f"📝 Новая заявка из внешней формы «{html.escape(str(form.get('title') or ''))}»: "
            f"{html.escape(fields.get('full_name') or '-')}, "
            f"@{html.escape(username_needle(real) or '-')}"
        )
        await notify_application(resolved_bot, telegram_id=tid, admin_text=admin_text,
                                 city_raw=city, is_new=True)
    except Exception:
        logger.exception("ext_forms: менеджеры не уведомлены о заявке (tid=%s)", tid)
    try:
        await _send_pending(resolved_bot, tid)
    except Exception:
        logger.exception("ext_forms: сообщение о заявке не отправлено (tid=%s)", tid)
    return {"created": True, "approve": "queue"}


async def _auto_approve(bot, tid: int) -> None:
    """Тот же путь, что автоодобрение на подаче анкеты: журнал решений и обычное приветствие
    (при включённой оплате оно откроет оплату), учёт доставки."""
    from services.applications.application_effects import auto_approve_effects
    from services.applications.applications import record_decision
    from services.applications.reject_journal import AUTO_DECIDED_BY

    if not await approve_user_atomic(tid):
        return
    try:
        await record_decision(tid, "approved", None, AUTO_DECIDED_BY, msk_now(),
                              effects_already_sent=True)
    except Exception:
        logger.exception("ext_forms: журнал решений не записан (tid=%s)", tid)
    await auto_approve_effects(bot, tid)


async def on_answer_available(form_id: int, answer_id: str, *, reason: str = "ingest") -> dict:
    """Хук приёма ответа: для формы «Регистрация» — заявка, привязка или ожидание /start.
    Для форм с другим назначением — no-op. Возвращает только коды и id."""
    from services.delegations.delegations import find_person

    form = await ef.get_form(int(form_id))
    if not form or await purpose_of(form) != "registration":
        return {"skipped": "not_registration_form"}
    answer = await ef.get_answer(int(form_id), str(answer_id))
    if not answer:
        return {"skipped": "no_answer"}
    if before_purpose(form, answer):
        return {"skipped": "before_purpose"}
    if answer.get("reg_state") in _DONE:
        return {"already": True}
    fields = extract_fields(form, answer.get("payload") or [])
    nick = fields["nick"]
    if not nick:
        await ef.set_answer_reg(int(answer["id"]), "no_nick", None)
        return {"state": "no_nick"}
    tid, where = await find_person(nick)
    if where == "ambiguous":
        await ef.set_answer_reg(int(answer["id"]), "ambiguous", None)
        return {"state": "ambiguous"}
    if where == "users":
        await _link_existing(tid, answer)
        return {"state": "linked", "tid": tid}
    if where == "reg_started":
        res = await _create_application(tid, form, answer, fields, tg_username=None, bot=None)
        return {"state": "created", "tid": tid, **res}
    # Ждём /start: ник в нижнем регистре — ключ поиска list_waiting_registration_answers.
    await ef.set_answer_needles([(int(answer["id"]), nick.lower())])
    await ef.set_answer_reg(int(answer["id"]), "waiting", None)
    return {"state": "waiting"}


async def try_registration_start(message, state, bot) -> bool:
    """Поздний вход из `cmd_start`: ответ формы «Регистрация» с ником человека ждёт его /start.
    Промах — False без побочных эффектов. Уже есть анкета — ответы привязываются, False
    (обычный /start продолжается). Иначе по первому ответу создаётся заявка — True."""
    from_user = getattr(message, "from_user", None)
    needle = username_needle(getattr(from_user, "username", None))
    if needle is None:
        return False
    rows = await ef.list_waiting_registration_answers(needle.lower())
    if not rows:
        return False
    tid = from_user.id
    if await get_user(tid):
        for answer in rows:
            await _link_existing(tid, answer)
        return False
    created = False
    for answer in rows:
        form = await ef.get_form(int(answer["form_id"]))
        if form is None:
            continue
        if created:
            await _link_existing(tid, answer)
            continue
        fields = extract_fields(form, answer.get("payload") or [])
        res = await _create_application(tid, form, answer, fields,
                                        tg_username=getattr(from_user, "username", None), bot=bot)
        created = bool(res.get("created"))
    return created
