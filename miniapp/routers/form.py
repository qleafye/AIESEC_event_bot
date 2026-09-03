"""Phase 21 (21-10, FORM-SYNC-03/04/05): HTTP-контракт анкеты Mini App — прочитать черновик со
спекой шагов (`reg_engine.form_spec`), записать ответы пофилевым слиянием (`version` +
`conflicts`), подписать согласия, отправить анкету. Один движок для чата бота и приложения
(T-21-05) — этот роутер не содержит НИ ОДНОЙ собственной проверки формата ввода: судья —
`reg_engine.validate_answer`, финал — `services.reg_finalize.finalize_data`/`post_finalize`
(план 21-08), те же функции, что зовёт бот.

Права (RESEARCH § «Права / безопасность формы», T-21-01/T-21-19): `telegram_id` — только из
подписанного initData (`form_gate`, не `delegate_gate` — Pitfall 9: незарегистрированный
делегат с черновиком `kind='new'` обязан пройти), allowlist колонок PATCH —
`reg_engine.column_to_step`, ни один из маршрутов не принимает чужой `telegram_id` ни в пути,
ни в теле. Логи — только `telegram_id`/`step`/`version`/коды ошибок, НИКОГДА `answers` (T-21-08).

Веб-процесс не ходит в Telegram Bot API/Sheets сам (D-01 фазы 19): `submit`/резюме ставят
события в `miniapp.outbox`, их разбирает `services/miniapp_outbox.py` в боте
(`post_finalize`/`handle_resume_upload`, план 21-08). Единственное исключение — мгновенный
ответ делегату в ЕГО ЖЕ чат через `telegram_api.send_message` (тот же приём, что
`miniapp/routers/review.py::_notify_delegate`) — не эффект над чужими данными, а копия того,
что уже отдано в ответе HTTP.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import reg_engine
from cities import cities_module_on, get_setting_typed_for_city, is_city_enabled
from database.db import (
    claim_reg_draft,
    get_reg_draft,
    get_setting,
    get_user,
    get_user_consents,
    record_user_consent,
    upsert_reg_draft,
)
from settings_schema import get_setting_typed
from services.consent import outstanding_consents
from services.reg_finalize import finalize_data, resolve_delegate_text

from miniapp import telegram_api
from miniapp.deps import Principal, form_gate, require_section
from miniapp.outbox import enqueue
from miniapp.telegram_api import TelegramApiError

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Контекст черновика: общий для GET/PATCH/submit ──────────────────────────────────────────

async def _load_context(telegram_id: int) -> dict:
    """Один общий разбор «где сейчас черновик» — GET/PATCH обязаны видеть одно и то же
    состояние, иначе PATCH мог бы записать поверх track/city, которых GET не знал (T-21-19).
    Черновика может не быть вовсе — тогда `kind` выводится ТЕМ ЖЕ правилом, что
    `_start_registration_flow` бота (план 21-09): есть незакрытая строка `users` текущего
    сезона -> `edit`, иначе (новичок / отклонён / прошлый сезон) -> `new` с префиллом (D-07)."""
    draft = await get_reg_draft(telegram_id)
    user_row = await get_user(telegram_id)
    is_returning = reg_engine.is_returning_row(user_row, (await get_setting("event_season") or "").strip() or None)

    if draft:
        kind = draft["kind"]
        answers = draft["answers"] or {}
        meta = draft["meta"] or {}
        participant_type = draft.get("participant_type")
        event_city = draft.get("event_city")
        version = draft["version"]
        step = draft.get("step")
    else:
        kind = "edit" if (user_row and not is_returning) else "new"
        # UAT 21-12 находка 4: правка уже поданной анкеты (D-26) без строки `reg_drafts` —
        # обзор обязан подставить текущие ответы из `users` тем же приёмом, что чат-recall
        # (reg_engine.prior_answers_for/STEP_TO_COLUMN), иначе form_spec видит answers={} и
        # каждое поле рисуется «Не заполнено», хотя users содержит реальные значения.
        answers = reg_engine.answers_from_user_row(user_row) if kind == "edit" else {}
        meta = {}
        # D-27: город/трек ПРОШЛОГО сезона у возвращенца (kind='new') сюда не переносятся —
        # так же, как rereg_start/`_city_fork_then_continue` бота никогда не подставляет
        # `user.event_city` в `effective_city`, а спрашивает город/трек заново (CONTEXT B:
        # «переспрашиваем всегда»). Иначе PATCH ловил бы 409 already_set на пустом месте, а
        # `should_show_city_fork` считал бы город уже известным и прятал развилку.
        participant_type = user_row.get("participant_type") if (kind == "edit" and user_row) else None
        event_city = user_row.get("event_city") if (kind == "edit" and user_row) else None
        version = 0
        step = None

    # Pitfall 5: prior — ТОЛЬКО на лету из users, никогда не персистится в reg_drafts.
    prior = reg_engine.prior_answers_for(user_row) if (kind == "new" and is_returning) else {}
    # D-27: прошлые город/трек возвращенца — ТОЛЬКО для предзаполнения пикера развилки
    # (`_pre_items`), не для `ctx["event_city"]`/`ctx["participant_type"]` (те решают
    # «уже известно» и гейтят PATCH 409 already_set — см. комментарий выше).
    prior_city = user_row.get("event_city") if (kind == "new" and is_returning and user_row) else None
    prior_track = user_row.get("participant_type") if (kind == "new" and is_returning and user_row) else None

    return {
        "draft": draft,
        "user_row": user_row,
        "kind": kind,
        "answers": answers,
        "meta": meta,
        "participant_type": participant_type,
        "event_city": event_city,
        "version": version,
        "step": step,
        "prior": prior,
        "prior_city": prior_city,
        "prior_track": prior_track,
    }


async def _pre_items(
    pre_tokens: list[str], prior_city: str | None = None, prior_track: str | None = None,
) -> list[dict]:
    """Данные для рендера pre-flow экранов мастера (план 21-11): согласия — карточка с
    названием/PDF/подписью чекбокса; вилки города/трека — интерактивные пикеры: `field` —
    имя поля PATCH (`event_city`/`participant_type`), `options` — варианты с сервера (те же
    города и те же подписи, что в клавиатурах бота), выбор уходит `PATCH /app/api/reg/draft`
    в те же валидаторы, что у тапа в боте (`reg_engine.validate_city_choice`/
    `validate_track_choice`). Deep-link приоритетен: вилка приходит только когда значение
    ещё не задано (`pre_flow`), уже заданное отбивается 409 `already_set`. Экран не содержит
    своих текстов и не знает имён полей — только то, что вернула эта функция.

    D-27: `prior_city`/`prior_track` — прошлый город/трек возвращенца (`_load_context`,
    только когда `kind == "new" and is_returning` — тот же признак, что открывает `prior` для
    шагов анкеты); кладутся в `value`, который пикер уже умеет предвыбирать (`drawFork` в
    `miniapp/static/js/screens/form.js` ищет `options.find(o => o.code === item.value)`),
    JS не менялся."""
    if not pre_tokens:
        return []
    items: list[dict] = []
    consent_tokens = [t for t in pre_tokens if t.startswith("consent:")]
    consent_labels = dict([(k, lbl) for lbl, k in await reg_engine.consent_entries()]) if consent_tokens else {}
    button_text = await get_setting_typed("consent_button_text") if consent_tokens else None
    for token in pre_tokens:
        if token.startswith("consent:"):
            key = token.split(":", 1)[1]
            items.append({
                "type": "consent",
                "key": key,
                "label": consent_labels.get(key, key),
                "pdf_file_id": await get_setting(f"consent_pdf_{key}"),
                "button_text": button_text,
            })
        elif token == "city_fork":
            items.append({
                "type": "city_fork", "field": "event_city",
                "text": await get_setting_typed("city_fork_text"),
                "options": await reg_engine.city_fork_options(), "value": prior_city,
            })
        elif token == "party_fork":
            items.append({
                "type": "party_fork", "field": "participant_type",
                "text": await get_setting_typed("party_fork_text"),
                "options": await reg_engine.party_track_options(), "value": prior_track,
            })
    return items


async def _registration_closed(event_city: str | None) -> bool:
    """D-11: «регистрация закрыта режимом города» — единственный существующий в боте сигнал
    для конкретного города (общего тумблера «закрыть регистрацию совсем» бот сегодня не имеет;
    появится — эта функция станет его первой проверкой). Единственный/безгородской ивент
    (`cities_module_on() == False`) никогда не «закрыт» этой проверкой — паритет с ботом, где
    такое событие сегодня тоже нельзя закрыть переключателем города."""
    if not event_city or not await cities_module_on():
        return False
    return not await is_city_enabled(event_city)


def _continue_deeplink(bot_username: str | None) -> str | None:
    """D-17: «Продолжить в чате» — deep-link строится на сервере, тем же приёмом, что
    построение deep-link в `miniapp/routers/profile.py`; фронт не собирает `t.me/...` строкой
    сам — сторож `test_only_external_url_is_telegram_web_app_sdk` запрещает внешние
    URL-литералы в JS вне SDK Telegram."""
    return f"https://t.me/{bot_username}?start=continue" if bot_username else None


async def _draft_response(telegram_id: int, ctx: dict | None = None, *, bot_username: str | None = None) -> dict:
    ctx = ctx or await _load_context(telegram_id)
    closed = ctx["kind"] == "new" and await _registration_closed(ctx["event_city"])
    # UAT 21-12 находка 1: мастер переспрашивал согласие на КАЖДОЕ открытие, даже секунды
    # после подписи в чате той же сессией. `outstanding_consents` — тот же фильтр версий, что
    # уже использует гейт пересогласия (services/consent.py) — подпись старой редакции ИЛИ
    # без подписи вовсе остаётся «pending» (экран нужен), подпись текущей редакции гасит
    # экран. Submit-гейт (D-23, ниже по файлу) не меняется — сверяет ЛЮБУЮ версию, это
    # отдельный жёсткий чек, не UI-удобство.
    pending_consents = [key for _label, key in await outstanding_consents(
        telegram_id, await reg_engine.consent_entries(),
    )]
    spec = await reg_engine.form_spec(
        ctx["answers"], ctx["participant_type"], ctx["event_city"], prior=ctx["prior"],
        pending_consent_keys=pending_consents,
    )
    # D-13: город/трек/согласия не меняются при правке — «locked» решает сервер по спеке
    # (движок знает список REG_FLOW-шагов), а не экран по названию колонки (Task 2
    # acceptance: JS не содержит литералов "city"/"participant_type").
    for step_spec_row in spec["steps"]:
        if step_spec_row["key"] == "resume":
            step_spec_row["has_prior_resume"] = reg_engine.has_prior_resume(ctx["user_row"])
        step_spec_row["locked"] = ctx["kind"] == "edit" and step_spec_row["key"] in reg_engine.EDIT_LOCKED_STEPS
    user_row = ctx["user_row"]
    return {
        "exists": ctx["draft"] is not None,
        "kind": ctx["kind"],
        "step": ctx["step"],
        "version": ctx["version"],
        "pre": spec["pre"],
        "pre_items": await _pre_items(spec["pre"], ctx.get("prior_city"), ctx.get("prior_track")),
        "steps": spec["steps"],
        "progress": spec["progress"],
        "closed": closed,
        "closed_text": (
            await get_setting_typed_for_city("reg_form_closed_text", ctx["event_city"]) if closed else None
        ),
        "prior_badge_text": (
            await get_setting_typed("reg_form_prior_answer_badge_text") if ctx["prior"] else None
        ),
        # Task 2 (обзор правки, D-26): статус нужен для баннера отклонённой заявки и для
        # решения, какой заголовок покажет submit (в edit-режиме статус не приходит нигде
        # больше — GET /app/api/reg/draft не отдавал его до этого плана).
        "status": (user_row.get("status") or "approved") if (ctx["kind"] == "edit" and user_row) else None,
        "rejected_banner_text": (
            await get_setting_typed_for_city("reg_form_rejected_banner_text", ctx["event_city"])
            if (ctx["kind"] == "edit" and user_row and user_row.get("status") == "rejected") else None
        ),
        "not_set_text": await get_setting_typed("reg_form_not_set_text"),
        "submit_cta_text": await get_setting_typed(
            "reg_form_edit_submit_cta_text" if ctx["kind"] == "edit" else "reg_form_submit_cta_text",
        ),
        "cancel_changes_text": await get_setting_typed("reg_form_cancel_changes_text"),
        "cancel_changes_confirm_text": await get_setting_typed("reg_form_cancel_changes_confirm_text"),
        "continue_in_chat_text": await get_setting_typed("reg_form_continue_in_chat_text"),
        "updated_in_chat_badge_text": await get_setting_typed("reg_form_updated_in_chat_badge_text"),
        "conflict_text": await get_setting_typed("reg_form_conflict_text"),
        "consent_required_text": await get_setting_typed("reg_form_consent_required_text"),
        "show_progress": await get_setting_typed("reg_show_progress") == "on",
        "continue_deeplink": _continue_deeplink(bot_username),
    }


# ── GET /app/api/reg/draft ───────────────────────────────────────────────────────────────

@router.get("/app/api/reg/draft")
async def draft_get(
    request: Request,
    p: Principal = Depends(form_gate), _: Principal = Depends(require_section("form")),
) -> dict:
    return await _draft_response(p.telegram_id, bot_username=request.app.state.cfg.bot_username)


# ── PATCH /app/api/reg/draft ─────────────────────────────────────────────────────────────

class DraftPatch(BaseModel):
    version: int
    answers: dict[str, Any] = Field(default_factory=dict)
    step: str | None = None
    # Pre-flow выбор (gap closure, D-01): город/трек из пикеров мастера. Проверяются теми же
    # валидаторами, что тап по развилке в боте; уже заданное значение -> 409 already_set.
    event_city: str | None = None
    participant_type: str | None = None


# (имя поля PATCH, валидатор движка) — порядок важен: трек резолвится с учётом города.
_PRE_CHOICE_VALIDATORS = (
    ("event_city", reg_engine.validate_city_choice),
    ("participant_type", reg_engine.validate_track_choice),
)


def _unwrap_other(raw: Any) -> Any:
    """Pitfall 10: веб шлёт `{"other": "текст"}` для choice-шагов с `other_allowed` вместо
    литерала «Другое» — движок про эту обёртку не знает, распаковка целиком на роутере."""
    if isinstance(raw, dict) and "other" in raw:
        return raw["other"]
    return raw


@router.patch("/app/api/reg/draft")
async def draft_patch(
    body: DraftPatch,
    request: Request,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    ctx = await _load_context(p.telegram_id)
    if ctx["kind"] == "new" and await _registration_closed(ctx["event_city"]):
        raise HTTPException(403, {
            "reason": "registration_closed",
            "text": await get_setting_typed_for_city("reg_form_closed_text", ctx["event_city"]),
        })

    errors: dict[str, str] = {}
    # Город/трек из пикеров pre-flow. T-21-32/D-13: deep-link и правка поданной анкеты
    # приоритетны — приложение не может перебить уже зафиксированное значение (409).
    pre_patch: dict[str, str] = {}
    for field, validator in _PRE_CHOICE_VALIDATORS:
        raw = getattr(body, field)
        if raw is None:
            continue
        if ctx["kind"] == "edit" or ctx[field] is not None:
            raise HTTPException(409, {"reason": "already_set", "field": field})
        if field == "participant_type":
            value, err = await validator(raw, pre_patch.get("event_city") or ctx["event_city"])
        else:
            value, err = await validator(raw)
        if err:
            errors[field] = err
        else:
            pre_patch[field] = value
    effective_track = pre_patch.get("participant_type") or ctx["participant_type"]

    step_patch: dict[str, Any] = {}
    for column, raw in body.answers.items():
        step_key = reg_engine.column_to_step(column)
        if step_key is None:
            raise HTTPException(400, {"reason": "bad_field", "field": column})
        value, err = reg_engine.validate_answer(
            step_key, _unwrap_other(raw), participant_type=effective_track,
        )
        if err:
            errors[column] = err
        else:
            step_patch[step_key] = value
    if errors:
        raise HTTPException(400, {"reason": "invalid", "errors": errors})

    field_versions = ctx["meta"].get("field_versions", {})
    touched_columns = [reg_engine.STEP_TO_COLUMN.get(sk, sk) for sk in step_patch]
    conflicts = reg_engine.conflicts(field_versions, body.version, touched_columns)

    new_answers = reg_engine.apply_answers(ctx["answers"], step_patch)
    delta = {col: val for col, val in new_answers.items() if ctx["answers"].get(col) != val}

    await upsert_reg_draft(
        p.telegram_id,
        kind=ctx["kind"],
        participant_type=effective_track,
        event_city=pre_patch.get("event_city") or ctx["event_city"],
        step=body.step,
        patch=delta,
        source="miniapp",
    )
    # T-21-08: в лог — только имена полей/колонок, значения не пишутся.
    logger.info(
        "reg draft patch telegram_id=%s step=%s base_version=%s pre=%s columns=%s",
        p.telegram_id, body.step, body.version, sorted(pre_patch), sorted(delta.keys()),
    )
    resp = await _draft_response(p.telegram_id, bot_username=request.app.state.cfg.bot_username)
    resp["conflicts"] = conflicts
    return resp


# ── POST /app/api/reg/consent/{key} ──────────────────────────────────────────────────────

@router.post("/app/api/reg/consent/{key}")
async def draft_consent(
    key: str,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    valid_keys = {consent_key for _label, consent_key in await reg_engine.consent_entries()}
    if key not in valid_keys:
        raise HTTPException(400, {"reason": "bad_key"})
    await record_user_consent(p.telegram_id, key)  # idempotent (INSERT OR IGNORE)
    return {"ok": True, "key": key}


# ── POST /app/api/reg/draft/submit ───────────────────────────────────────────────────────

@router.post("/app/api/reg/draft/submit")
async def draft_submit(
    request: Request,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    # T-21-05/D-23: серверная проверка обязательна — скрытия кнопки на фронте недостаточно.
    consent_steps = await reg_engine.get_consent_steps()
    required_keys = [step_key.split(":", 1)[1] for step_key in consent_steps]
    if required_keys:
        signed = set(await get_user_consents(p.telegram_id))
        missing = [key for key in required_keys if key not in signed]
        if missing:
            raise HTTPException(409, {
                "reason": "consent_required",
                "keys": missing,
                "text": await get_setting_typed("reg_form_consent_required_text"),
            })

    # T-21-02: claim перед финалом — второй submit (гонка с чатом) получает 409, не вторую запись.
    draft = await claim_reg_draft(p.telegram_id)
    if draft is None:
        raise HTTPException(409, {"reason": "already_submitting"})

    try:
        result = await finalize_data(p.telegram_id, p.username, draft)
    except Exception:
        # finalize_data сама освобождает claim (release_reg_draft) перед пробросом исключения.
        logger.exception("reg draft submit: finalize_data failed telegram_id=%s", p.telegram_id)
        raise HTTPException(500, {"reason": "server_error"})

    event_city = draft.get("event_city")
    if result["mode"] == "new":
        heading = await get_setting_typed_for_city("reg_form_complete_heading_text", event_city)
        body = await get_setting_typed_for_city("reg_form_complete_body_text", event_city)
    else:
        heading = await resolve_delegate_text(
            result["mode"],
            remoderated=result["remoderated"],
            resubmitted=result["resubmitted"],
            event_city=event_city,
        )
        body = None

    kind_event = "reg_finalized" if result["mode"] == "new" else "reg_edited"
    await enqueue(kind_event, {"telegram_id": p.telegram_id})
    logger.info(
        "reg draft submit telegram_id=%s mode=%s status=%s", p.telegram_id, result["mode"], result["status"],
    )

    chat_text = heading if not body else f"{heading}\n{body}"
    if chat_text:
        try:
            await telegram_api.send_message(request.app.state.cfg, p.telegram_id, chat_text)
        except TelegramApiError as exc:
            logger.warning(
                "reg draft submit: chat notify failed telegram_id=%s (%s)", p.telegram_id, exc.reason,
            )

    return {"mode": result["mode"], "status": result["status"], "heading": heading, "body": body}


__all__ = ["router", "DraftPatch"]
