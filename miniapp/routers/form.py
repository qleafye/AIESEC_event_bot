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

Phase 30 (30-02, A2-03): то же правило действует и у ручки поиска по справочнику (`reg_suggest`
ниже) — НИКОГДА `q` (поисковый текст делегата), только длина/факт запроса (T-30-06).

Веб-процесс не ходит в Telegram Bot API/Sheets сам (D-01 фазы 19): `submit`/резюме ставят
события в `miniapp.outbox`, их разбирает `services/miniapp_outbox.py` в боте
(`post_finalize`/`handle_resume_upload`, план 21-08). Единственное исключение — мгновенный
ответ делегату в ЕГО ЖЕ чат через `telegram_api.send_message` (тот же приём, что
`miniapp/routers/review.py::_notify_delegate`) — не эффект над чужими данными, а копия того,
что уже отдано в ответе HTTP.

Phase 27 (27-04, LANG-02/LANG-06): `_draft_response`/`draft_patch` — вторая (после чата бота,
план 27-05) воронка вывода делегатского текста. `services.i18n.context()` грузит `(lang,
tr_map)` РОВНО ОДИН раз на запрос (не по разу на текст — см. докстринг `_draft_response`);
`reg_engine` о языках не знает (A-03, 27-CONTEXT.md) — перевод прогоняется НАД уже собранной
спекой (`prompt`/`help`/`label`/`options`), контракт JSON не меняется. `draft_patch`
канонизирует английскую подпись варианта в русский канон ДО `reg_engine.validate_answer`
(`_canonicalize_answer`, T-27-04-01) — иначе она молча уехала бы в `users`/Google-таблицу.
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
    set_reg_draft_surface,
    update_user_answers,
    upsert_reg_draft,
    set_user_lang,
)
from settings_schema import get_setting_typed
from services import i18n, reg_edit_policy
from services.consent import outstanding_consents
from services.lookup import search_lookup, top_chips
from services.reg_finalize import finalize_data, resolve_delegate_text
from services.reg_handoff import SURFACE_APP, SURFACE_BOT, draft_holder

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
    `_start_registration_flow` бота (план 21-09, обновлено quick 260904-3vm D15): анкета
    ТЕКУЩЕГО сезона реально подана (непустой `registration_date`) -> `edit`, иначе (новичок /
    ещё не подавал / отклонён / прошлый сезон) -> `new` с префиллом (D-07)."""
    draft = await get_reg_draft(telegram_id)
    user_row = await get_user(telegram_id)
    season = (await get_setting("event_season") or "").strip() or None
    is_returning = reg_engine.is_returning_row(user_row, season)

    if draft:
        kind = draft["kind"]
        answers = draft["answers"] or {}
        meta = draft["meta"] or {}
        participant_type = draft.get("participant_type")
        event_city = draft.get("event_city")
        version = draft["version"]
        step = draft.get("step")
    else:
        # Quick 260904-3vm (D15): экран «Изменение анкеты» читает `users`, а черновик лежит в
        # `reg_drafts` — показать его делегату, который анкету ещё не подавал, значит показать
        # пустой экран поверх реально заполненного черновика. `has_submitted_anketa` — тот же
        # признак («подана» = непустой `registration_date`), что и у бота.
        kind = "edit" if reg_engine.has_submitted_anketa(user_row, season) else "new"
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

    # Quick 260906-4rg: веб задавал вопрос «Источник» делегату, который уже пришёл по
    # рекламной метке (deep-link `src_*`) — бот тот же вопрос не задаёт. Правило пропуска шага
    # одно на оба клиента (`reg_engine.enabled_steps:348`), но живёт на признаке `_source_from_tag`
    # в `answers` — веб этот ключ не подмешивал. `meta["source_from_tag"]` (без `_`) — то же
    # самое поле под другим именем: `upsert_reg_draft` вырезает ключи с ведущим `_` при записи,
    # поэтому бот пишет маркер в meta без подчёркивания, а читает движок — с ним. Второй
    # источник признака — уже поданная анкета (`users.source_from_tag`), но ТОЛЬКО при
    # kind == "edit": у возвращенца (kind == "new", is_returning) `user_row` — от ПРОШЛОГО
    # сезона, и его пометка не означает метку в ТЕКУЩЕЙ сессии (бот в новой сессии без метки
    # вопрос задаёт — иначе разъедемся с ним).
    from_tag = bool(meta.get("source_from_tag")) or (
        kind == "edit" and bool(user_row) and bool(user_row.get("source_from_tag"))
    )
    if from_tag:
        answers = {**answers, "_source_from_tag": True}

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
        # Quick 260904-3vm (эстафета): кто владеет черновиком ПРЯМО СЕЙЧАС — "bot" | "app" |
        # None (ничей: пустой черновик, или уже отправляется). Питает и плиту GET-ответа
        # (`_draft_response["handoff"]`), и 409 в PATCH.
        "holder": draft_holder(draft),
        # Quick 260904-3vm (D16): трек, разрешённый ТЕМ ЖЕ правилом, что `reg_engine.form_spec`
        # теперь применяет сама (resolve_track с пустым кандидатом). `participant_type` НЕ
        # заменяется этим значением нигде рядом — он гейтит 409 already_set/скрытие развилки,
        # смешивать эти две роли нельзя (см. комментарий у D-27 выше).
        "effective_track": participant_type or await reg_engine.resolve_track(None, event_city),
    }


async def _pre_items(
    pre_tokens: list[str], prior_city: str | None = None, prior_track: str | None = None,
    lang: str = "ru", tr_map: dict[str, str] | None = None,
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
    JS не менялся.

    Phase 27 (27-04, LANG-02/LANG-09): `lang`/`tr_map` (одна карта на запрос, `_draft_response`
    её и загружает) переводят `text`/`options[].label` вилок города и трека — `options[].code`
    остаётся русским кодом всегда (PATCH шлёт `code`, не подпись, канонизация тут не нужна,
    см. `reg_engine.validate_city_choice`/`validate_track_choice`). Карточка согласия — ИСКЛЮЧЕНИЕ
    (LANG-09): `label`/`button_text` не переводятся машиной — ручной английский текст живёт в
    отдельном плане (27-06), пока его нет — делегат видит русский тем же fail-soft, что и
    everywhere else в проекте."""
    tr_map = tr_map or {}
    if not pre_tokens:
        return []
    items: list[dict] = []
    consent_tokens = [t for t in pre_tokens if t.startswith("consent:")]
    consent_labels = dict([(k, lbl) for lbl, k in await reg_engine.consent_entries()]) if consent_tokens else {}
    button_text = await get_setting_typed("consent_button_text") if consent_tokens else None
    for token in pre_tokens:
        if token.startswith("consent:"):
            # LANG-09: НЕ переводится машиной -- ни здесь, ни где-либо ещё в этом плане.
            key = token.split(":", 1)[1]
            items.append({
                "type": "consent",
                "key": key,
                "label": consent_labels.get(key, key),
                "pdf_file_id": await get_setting(f"consent_pdf_{key}"),
                "button_text": button_text,
            })
        elif token == "city_fork":
            text = await get_setting_typed("city_fork_text")
            options = await reg_engine.city_fork_options()
            if lang != "ru":
                text = i18n.tr(text, lang, tr_map)
                options = [{**o, "label": i18n.tr(o["label"], lang, tr_map)} for o in options]
            items.append({
                "type": "city_fork", "field": "event_city",
                "text": text, "options": options, "value": prior_city,
            })
        elif token == "party_fork":
            text = await get_setting_typed("party_fork_text")
            options = await reg_engine.party_track_options()
            if lang != "ru":
                text = i18n.tr(text, lang, tr_map)
                options = [{**o, "label": i18n.tr(o["label"], lang, tr_map)} for o in options]
            items.append({
                "type": "party_fork", "field": "participant_type",
                "text": text, "options": options, "value": prior_track,
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


async def _edit_gate(ctx: dict) -> tuple[bool, str | None]:
    """Квик 260911-w2m: можно ли делегату сейчас записать/отправить правку УЖЕ ПОДАННОЙ
    анкеты — тонкая обёртка над `services.reg_edit_policy.edit_gate`.

    Первичная подача (`ctx["kind"] != "edit"`) НЕ гейтится и не платит лишним чтением
    реестра — `(True, None)` немедленно. Проверка дублирует признак `kind`, а не полагается
    только на статус пользователя ВНУТРИ `edit_gate`, потому что `kind` может прийти из
    строки `reg_drafts` (черновик правки, заведённый ДО того, как менеджер закрыл правку) —
    источник правды один и тот же (`reg_engine.has_submitted_anketa` внутри `edit_gate`),
    здесь просто короткое замыкание для самого частого случая (новая анкета)."""
    if ctx["kind"] != "edit":
        return True, None
    return await reg_edit_policy.edit_gate(ctx["user_row"])


def _continue_deeplink(bot_username: str | None) -> str | None:
    """D-17: «Продолжить в чате» — deep-link строится на сервере, тем же приёмом, что
    построение deep-link в `miniapp/routers/profile.py`; фронт не собирает `t.me/...` строкой
    сам — сторож `test_only_external_url_is_telegram_web_app_sdk` запрещает внешние
    URL-литералы в JS вне SDK Telegram."""
    return f"https://t.me/{bot_username}?start=continue" if bot_username else None


async def _draft_response(telegram_id: int, ctx: dict | None = None, *, bot_username: str | None = None) -> dict:
    ctx = ctx or await _load_context(telegram_id)
    # Phase 27 (27-04, LANG-02): ОДНА загрузка карты переводов на запрос (не по разу на каждый
    # шаг/текст — form_spec резолвит ~43 шага, наивная врезка удвоила бы число чтений реестра
    # на рендер формы, см. 27-04-PLAN.md). `"ask"` (язык ещё не выбран, делегат не отвечал на
    # экран выбора — Mini App его не показывает, это поверхность бота) трактуется как "ru" —
    # тот же фоллбэк, что `services.i18n.context` уже применяет к `tr_map` для этого случая.
    lang, tr_map = await i18n.context(telegram_id)
    lang = lang if lang in ("ru", "en") else "ru"
    closed = ctx["kind"] == "new" and await _registration_closed(ctx["event_city"])
    edit_can_edit, edit_closed_text = await _edit_gate(ctx)
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
        # Квик 260912-l53 (задача 3): мёртвый ключ контракта убран — факт сохранённого резюме
        # несут `spec.values`/`spec.display` (квик 260911-2kb, пункт 4), отдельный булев флаг
        # фронт не читал ни разу (`grep has_prior_resume miniapp/static/js/` пуст). Функция
        # `reg_engine.has_prior_resume` не трогается — на ней чат-рекол бота и фильтр рассылки.
        step_spec_row["locked"] = ctx["kind"] == "edit" and step_spec_row["key"] in reg_engine.EDIT_LOCKED_STEPS
        # Phase 27 (27-04, LANG-02): перевод НАД уже собранной спекой — reg_engine сам о
        # языках не знает (A-03, 27-CONTEXT.md). Контракт не меняется: `options` остаётся
        # `list[str]` (те же строки, что options() и так уже вернула для этого шага, просто
        # прогнанные через tr() — второй поход в options() через option_pairs() не нужен).
        if lang != "ru":
            step_spec_row["prompt"] = i18n.tr(step_spec_row["prompt"], lang, tr_map)
            step_spec_row["help"] = i18n.tr(step_spec_row["help"], lang, tr_map)
            step_spec_row["label"] = i18n.tr(step_spec_row["label"], lang, tr_map)
            if step_spec_row["options"] is not None:
                step_spec_row["options"] = [
                    i18n.tr(opt, lang, tr_map) for opt in step_spec_row["options"]
                ]
            # Phase 30 (30-03, A2-08, задача 4): v2_texts/option_hints — тот же прогон, что
            # остальные текстовые поля спеки выше; иначе английская анкета показывала бы
            # русские тексты новых типов (select/lookup/multi/link) поверх переведённого
            # prompt/help/label/options.
            if step_spec_row.get("v2_texts"):
                step_spec_row["v2_texts"] = {
                    key: i18n.tr(text, lang, tr_map)
                    for key, text in step_spec_row["v2_texts"].items()
                }
            if step_spec_row.get("option_hints"):
                step_spec_row["option_hints"] = {
                    value: i18n.tr(text, lang, tr_map)
                    for value, text in step_spec_row["option_hints"].items()
                }
    user_row = ctx["user_row"]
    show_progress = await get_setting_typed("reg_show_progress") == "on"
    # Quick 260904-3vm (эстафета): плита «анкета сейчас в чате» — ТОЛЬКО когда держит бот;
    # владение приложением (или «ничей») ответ не отмечает — экран рисует обычный мастер/обзор.
    handoff = None
    if ctx["holder"] == SURFACE_BOT:
        handoff = {
            "held_by": "bot",
            "text": await get_setting_typed("reg_form_held_by_bot_text"),
            "takeover_text": await get_setting_typed("reg_form_takeover_cta_text"),
            "continue_text": await get_setting_typed("reg_form_continue_in_chat_text"),
            "deeplink": _continue_deeplink(bot_username),
        }
    return {
        "exists": ctx["draft"] is not None,
        "kind": ctx["kind"],
        "handoff": handoff,
        "step": ctx["step"],
        "version": ctx["version"],
        "pre": spec["pre"],
        "pre_items": await _pre_items(
            spec["pre"], ctx.get("prior_city"), ctx.get("prior_track"), lang, tr_map,
        ),
        "steps": spec["steps"],
        "progress": spec["progress"],
        "closed": closed,
        "closed_text": (
            await get_setting_typed_for_city("reg_form_closed_text", ctx["event_city"]) if closed else None
        ),
        # Квик 260911-w2m: отдельная пара «правка выключена» — своя причина и свой текст,
        # намеренно НЕ смешана с `closed`/`closed_text` выше (тот гейт закрыт сторожами
        # плана 21-10 и означает совсем другое — «регистрация закрыта режимом города»).
        "edit_closed": not edit_can_edit,
        "edit_closed_text": edit_closed_text,
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
        # Phase 23.1 (UI-REDESIGN-04): подписи экрана мастера — надзаголовок списка
        # вопросов, «…и ещё N впереди», пометка сохранения черновика, кнопки дальше/назад.
        "questions_eyebrow": await get_setting_typed("reg_form_questions_eyebrow"),
        "more_questions_text": await get_setting_typed("reg_form_more_questions_text"),
        "draft_saved_text": await get_setting_typed("reg_form_draft_saved_text"),
        "next_cta_text": await get_setting_typed("reg_form_next_cta_text"),
        "back_cta_text": await get_setting_typed("reg_form_back_cta_text"),
        "updated_in_chat_badge_text": await get_setting_typed("reg_form_updated_in_chat_badge_text"),
        "conflict_text": await get_setting_typed("reg_form_conflict_text"),
        "consent_required_text": await get_setting_typed("reg_form_consent_required_text"),
        "show_progress": show_progress,
        # UAT 21-12 находка 2: подпись «N из M» — шаблон из реестра, не литерал JS (D-25);
        # {step}/{total} подставляет фронт `.replace` (Pitfall 11), номер шага меняется без
        # похода на сервер. Только когда тумблер включён — незачем гонять текст, который
        # экран не покажет.
        "progress_text": (await get_setting_typed("reg_form_progress_text")) if show_progress else None,
        "continue_deeplink": _continue_deeplink(bot_username),
        # D9 (quick 260904-de4): фолбэк на загрузку резюме файлом, когда сервер не отдал
        # человеческий текст своим (`bad_type`/`too_large` дают его сами — см.
        # `submissions.py::_upload_resume`); отдельный литерал в JS не заводим.
        "resume_upload_error_text": await get_setting_typed("reg_form_resume_upload_error_text"),
        # D13: подпись кнопки «Поделиться номером» на шаге телефона — из реестра, не литерал JS.
        "share_contact_text": await get_setting_typed("reg_form_share_contact_text"),
        # Phase 30 (30-05, задача 4, A2-08): группа «Язык анкеты» в поповере настроек шапки
        # видна только при включённом модуле (фаза 27) — `lang` здесь ТОТ ЖЕ, что уже
        # резолвлен выше для перевода текстов спеки, второго похода в `i18n.context` не нужно.
        "lang_module_enabled": await get_setting_typed("delegate_lang_enabled") == "on",
        "lang": lang,
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
    # Квик 260912-l53: СПИСОК КЛЮЧЕЙ ШАГОВ (не колонок) — «×» на дропзоне резюме и её
    # аналоги. Отдельное поле, а не `null` в `answers`: `null` уже занят под «Пропустить» для
    # `_NULL_SKIP_STEPS`, а для обязательного шага (например «resume») пустой ответ обязан
    # ловить валидацию, а не молча очищаться — «очистить» и «прислал пустой ввод» это разные
    # намерения. Клиент называет шаг, набор колонок для очистки разворачивает сервер
    # (`reg_engine.columns_for_step`) — клиент имён колонок не знает.
    clear: list[str] = Field(default_factory=list)


# (имя поля PATCH, валидатор движка) — порядок важен: трек резолвится с учётом города.
_PRE_CHOICE_VALIDATORS = (
    ("event_city", reg_engine.validate_city_choice),
    ("participant_type", reg_engine.validate_track_choice),
)


def _unwrap_other(raw: Any) -> Any:
    """Pitfall 10: веб шлёт `{"other": "текст"}` для choice-шагов с `other_allowed` вместо
    литерала «Другое» — движок про эту обёртку не знает, распаковка целиком на роутере.

    UAT 21-12 находка 5: та же история для шага «Резюме» — дропзона (`miniapp/static/js/
    form.js::fileControl`) шлёт текстовый ответ как `{"text": "..."}` (переключатель
    «ответить текстом» вместо загрузки файла), а `reg_engine.validate_answer("resume", ...)`
    ждёт голую строку и падает на `.strip()` словаря. Обёртка на этом же роутере, не в
    движке (T-21-05: движок не знает про формы JS)."""
    if isinstance(raw, dict):
        if "other" in raw:
            return raw["other"]
        if "text" in raw:
            return raw["text"]
    return raw


async def _canonicalize_answer(step_key: str, raw: Any, lang: str, tr_map: dict[str, str]) -> Any:
    """Phase 27 (27-04, LANG-06, T-27-04-01): английская подпись варианта -> русский канон —
    ОБЯЗАТЕЛЬНО до `validate_answer`. Шаги `_CHOICE_STEPS`/`_BESPOKE_CHOICE`/`_MEMBERSHIP_STEPS`
    принимают любой текст и сохранили бы английскую подпись молча (без ошибки, но с
    расползанием базы и Google-таблицы на два языка). `option_pairs` на шаге без вариантов
    отдаёт `[]` -> `canonical_option` тривиально `None` -> `raw` без изменений (свободный
    текст/шаги без выбора не трогаются). Multi-шаги (список) канонизируются поэлементно —
    тождественно на каноне, если элемент и так канон (см. докстринг задачи 3 плана)."""
    pairs = await reg_engine.option_pairs(step_key, lang, tr_map)
    if not pairs:
        return raw
    if isinstance(raw, list):
        return [reg_engine.canonical_option(pairs, item) or item for item in raw]
    if isinstance(raw, str):
        canon = reg_engine.canonical_option(pairs, raw)
        return canon if canon is not None else raw
    return raw


@router.patch("/app/api/reg/draft")
async def draft_patch(
    body: DraftPatch,
    request: Request,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    ctx = await _load_context(p.telegram_id)
    # Quick 260904-3vm (эстафета): ПЕРВЫМ делом — владение. Бот держит анкету -> приложение не
    # пишет вообще ничего, даже до валидации/проверки «регистрация закрыта».
    if ctx["holder"] == SURFACE_BOT:
        raise HTTPException(409, {
            "reason": "held_by_bot",
            "text": await get_setting_typed("reg_form_held_by_bot_text"),
        })
    # Квик 260911-w2m: чужая поверхность (выше) -> правка выключена (здесь) -> регистрация
    # закрыта (ниже) — именно в этом порядке. 409, не 403 (Р-3 плана): `api.js` красит ЛЮБОЙ
    # 403 экраном «Нет доступа» поверх уже отрисованного, а `errorText()` уже достаёт текст
    # из `payload.text` для 409 — новых веток в JS заводить не нужно.
    edit_can_edit, edit_closed_text = await _edit_gate(ctx)
    if not edit_can_edit:
        raise HTTPException(409, {"reason": "edit_closed", "text": edit_closed_text})
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
    # Quick 260904-3vm (D16): ctx["effective_track"] уже резолвит промо-short так же, как
    # reg_engine.form_spec — иначе PATCH на пустом черновике мог бы провалидировать ответ по
    # "full" (дефолт до резолва), пока GET того же черновика уже отдаёт короткий набор шагов.
    effective_track = pre_patch.get("participant_type") or ctx["effective_track"]

    # Phase 27 (27-04, LANG-06, T-27-04-01): канонизация ДО validate_answer — см. докстринг
    # _canonicalize_answer. lang=="ru" (module off/делегат ещё не выбрал английский) пропускает
    # саму загрузку карты переводов через тот же ноль-чтений фоллбэк, что _draft_response —
    # PATCH при выключенном модуле не получает ни одного нового похода в БД.
    answer_lang, answer_tr_map = await i18n.context(p.telegram_id)
    answer_lang = answer_lang if answer_lang in ("ru", "en") else "ru"

    # Phase 28 (28-05, SU-04, T-28-05-01, deviation Rule 3): выбор ветки развилки резюме
    # (`resume_type`) — закрытый словарь из трёх токенов, тот же контракт, что
    # `regfork:file|link|mini` в боте (handlers/reg_resume_fork.py). Обрабатывается ОТДЕЛЬНО
    # от общего цикла ниже: `resume_type` НЕ REG_FLOW-шаг и не проходит через
    # `reg_engine.column_to_step`/`validate_answer` — попади он в общий цикл, схлопнулся бы в
    # 400 bad_field, как любая незнакомая колонка.
    resume_type_patch = body.answers.pop("resume_type", None)
    if resume_type_patch is not None and resume_type_patch not in ("file", "link", "mini"):
        raise HTTPException(400, {"reason": "bad_field", "field": "resume_type"})

    # Квик 260912-l53: `clear` называет ШАГ, набор колонок для очистки — единственный источник
    # правды `reg_engine.columns_for_step` (тот же, которым живут `_sync_draft_in/out` и
    # `step_spec` в чате бота). Неизвестный/пустой шаг -> 400 bad_field, как и незнакомая
    # колонка в `answers` выше. `body.step` этой веткой не трогается: «×» шаг анкеты не
    # двигает, `resume_type` (выбранная ветка развилки) при очистке не сбрасывается —
    # делегат остаётся в ветке «файл», чтобы приложить другой.
    cleared_columns: list[str] = []
    for step_key in body.clear:
        cols = reg_engine.columns_for_step(step_key)
        if not cols:
            raise HTTPException(400, {"reason": "bad_field", "field": step_key})
        for col in cols:
            if col not in cleared_columns:
                cleared_columns.append(col)

    step_patch: dict[str, Any] = {}
    for column, raw in body.answers.items():
        step_key = reg_engine.column_to_step(column)
        if step_key is None:
            raise HTTPException(400, {"reason": "bad_field", "field": column})
        unwrapped = _unwrap_other(raw)
        if answer_lang != "ru":
            unwrapped = await _canonicalize_answer(step_key, unwrapped, answer_lang, answer_tr_map)
        # Phase 28 (28-03, SU-02, T-28-03-01): второй барьер лимита мультивыбора — веб-PATCH
        # не проходит через `process_multi_toggle` (бот), поэтому здесь этот лимит и есть
        # единственный реальный гейт (клиентский дизейбл в form.js — только подсказка).
        max_select = None
        limit_error_text = None
        if reg_engine.REG_STEP_TYPES.get(step_key) == "multi":
            max_select = await reg_engine.multi_max(step_key)
            if max_select is not None:
                limit_error_text = await get_setting_typed("reg_multi_limit_error_text")
        value, err = reg_engine.validate_answer(
            step_key, unwrapped, participant_type=effective_track,
            max_select=max_select, limit_error_text=limit_error_text,
        )
        if err:
            errors[column] = err
        else:
            step_patch[step_key] = value
    if errors:
        raise HTTPException(400, {"reason": "invalid", "errors": errors})

    field_versions = ctx["meta"].get("field_versions", {})
    # Очистка — такая же правка поля, как ответ: гонку с чатом (кто-то параллельно принёс
    # резюме в бота, пока делегат жал «×» в приложении) обязана показывать тоже она.
    touched_columns = [reg_engine.STEP_TO_COLUMN.get(sk, sk) for sk in step_patch] + cleared_columns
    conflicts = reg_engine.conflicts(field_versions, body.version, touched_columns)

    # Phase 28 (28-01, SU-03): множество «учусь» — реестровое, пусто = прежнее правило.
    edu_studying_set = await reg_engine.studying_statuses()
    new_answers = reg_engine.apply_answers(
        ctx["answers"], step_patch, studying_statuses=edu_studying_set,
    )
    # Phase 28 (28-05, SU-04): `resume_type` кладётся В ОБХОД apply_answers (не REG_FLOW-шаг,
    # нет своей колонки/правила APPLY_GOLDEN) — сразу в merged-словарь, ДО расчёта delta, чтобы
    # он попал и в сохранённый патч, и в `enabled_now` ниже (условие resume_link/mini_* читает
    # именно это поле, reg_engine.enabled_steps).
    if resume_type_patch is not None:
        new_answers["resume_type"] = resume_type_patch
    # Квик 260912-l53: обнуляем ВЕСЬ набор колонок шага в объединённых ответах — до расчёта
    # `delta`, чтобы дальнейший код (spec/`enabled_now`) видел уже очищенное состояние.
    for col in cleared_columns:
        new_answers[col] = None
    # УАТ 10-11.09 (квик 260911-2kb, пункт 5): живой баг — первый PATCH из приложения по уже
    # поданной анкете (например `uploadResume` бутстрапит черновик `{version: 0, answers: {}}`)
    # обнулял анкету делегату. `_load_context` подставляет снимок ответов из `users`
    # (`answers_from_user_row`) ТОЛЬКО в ветке «строки `reg_drafts` нет вовсе» — `ctx["answers"]`
    # уже несёт этот снимок. Но раньше `delta` считалась ПРОТИВ него всегда, а
    # `upsert_reg_draft` на INSERT кладёт в `answers` НОВОЙ строки ровно `delta` (существующей
    # строки для слияния ещё нет) — снимок из `users` терялся, оставался только что отвеченный
    # шаг. База при этом была цела (`finalize_data` в режиме `edit` достраивает полный набор из
    # `users`), но делегат видел пустую анкету. Когда строка создаётся ЭТИМ запросом
    # (`ctx["draft"] is None`), базой для дельты берём ПУСТОЙ словарь — тогда весь снимок
    # (уже подмешанный в `new_answers` через `ctx["answers"]` выше) уезжает в `patch` и
    # персистится в новой строке. Когда строка уже есть — поведение прежнее байт-в-байт.
    baseline = ctx["answers"] if ctx["draft"] else {}
    delta = {col: val for col, val in new_answers.items() if baseline.get(col) != val}
    # Пункт 5 objective (квик 260912-l53): у делегата без строки `reg_drafts` `baseline` для
    # очищаемой колонки часто вообще НЕ содержит ключ (снимок из `users` без файла резюме) —
    # «не было ключа -> стало None» даёт `baseline.get(col) != val` == False, и очистка не
    # попала бы в `delta`, то есть не персистировалась бы. Кладём принудительно, после
    # расчёта дельты, а не полагаемся на сравнение со снимком.
    for col in cleared_columns:
        delta[col] = None

    # Quick 260904-3vm (D2): контракт `reg_drafts.step` = шаг, который ЕЩЁ НЕ ОТВЕЧЕН — ровно
    # то, что штампует бот последним действием хода (registration.py::_stamp_reg_step штампует
    # ЗАДАВАЕМЫЙ вопрос), и то, что читают reg_resume.py::resume_from_draft и
    # form.js::stepIndexFromKey. `body.step`, наоборот, приходит как ТОЛЬКО ЧТО ОТВЕЧЕННЫЙ шаг
    # (form.js::goNext) — переводим один в другой здесь, один раз, а не в каждом читателе.
    step_to_store = body.step
    if body.step:
        enabled_now = await reg_engine.enabled_steps({**new_answers, "participant_type": effective_track})
        if body.step in enabled_now:
            idx = enabled_now.index(body.step)
            if idx + 1 < len(enabled_now):
                step_to_store = enabled_now[idx + 1]
            else:
                # UAT 07.09 (T-d6t-04): отвеченный шаг был последним включённым — «не отвечено
                # ничего» это МАРКЕР, а не последний отвеченный вопрос. Иначе бот, читающий
                # тот же столбец, переспрашивает уже отвеченное («✍️ Продолжить в чате»).
                step_to_store = reg_engine.STEP_DONE

    # Quick 260904-3vm (эстафета): "ничей" черновик (holder is None — пуст или только что
    # создаётся) занимается МОЛЧА приложением; уже занятый приложением — как раньше, без событий.
    silent_takeover = ctx["holder"] is None
    await upsert_reg_draft(
        p.telegram_id,
        kind=ctx["kind"],
        participant_type=effective_track,
        event_city=pre_patch.get("event_city") or ctx["event_city"],
        step=step_to_store,
        patch=delta,
        source="miniapp",
        active_surface=SURFACE_APP if silent_takeover else None,
    )
    if silent_takeover:
        # FSM бота могла остаться в анкете с прошлого захода (делегат начал в чате, ушёл, а
        # потом открыл пустой черновик в приложении) — сбрасываем на всякий случай.
        await enqueue("reg_fsm_reset", {"telegram_id": p.telegram_id, "reason": "takeover"})
    # T-21-08: в лог — только имена полей/колонок, значения не пишутся.
    logger.info(
        "reg draft patch telegram_id=%s step=%s base_version=%s pre=%s columns=%s cleared=%s",
        p.telegram_id, step_to_store, body.version, sorted(pre_patch), sorted(delta.keys()),
        sorted(cleared_columns),
    )
    resp = await _draft_response(p.telegram_id, bot_username=request.app.state.cfg.bot_username)
    resp["conflicts"] = conflicts
    return resp


# ── POST /app/api/reg/draft/takeover, /release ──────────────────────────────────────────────

@router.post("/app/api/reg/draft/takeover")
async def draft_takeover(
    request: Request,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    """Кнопка «Забрать сюда» на плите «анкета сейчас в чате» — явный захват владения
    приложением. Черновика может не быть вовсе (мастер просто откроется пустым) — маршрут
    всё равно 200. Всегда ставит РОВНО одно событие сброса FSM бота, даже если владение уже
    было у приложения (идемпотентный тап — лишний сброс FSM безвреден)."""
    await set_reg_draft_surface(p.telegram_id, SURFACE_APP)
    await enqueue("reg_fsm_reset", {"telegram_id": p.telegram_id, "reason": "takeover"})
    return await _draft_response(p.telegram_id, bot_username=request.app.state.cfg.bot_username)


@router.post("/app/api/reg/draft/release")
async def draft_release(
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    """Кнопка «Продолжить в чате» — отдаёт владение ПЕРЕД тем, как приложение откроет
    deep-link, иначе гвард бота (handlers/reg_handoff.py) отбил бы делегата же его собственным
    вводом. Без outbox: бот и так заберёт анкету по deep-link `?start=continue` через
    существующий экран «Продолжить / Заново» (D-17/D-18)."""
    await set_reg_draft_surface(p.telegram_id, SURFACE_BOT)
    return {"ok": True}


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
    # Quick 260907-4ai: в вебе разметки кнопки на сервере нет — снимок берём из той же
    # настройки, которой отрисована форма (см. строку ~189 этого же файла).
    raw_button = await get_setting_typed("consent_button_text") or "Согласен(-на)"
    await record_user_consent(p.telegram_id, key, raw_button=raw_button)  # idempotent (INSERT OR IGNORE)
    return {"ok": True, "key": key}


# ── POST /app/api/reg/lang (план 30-05, задача 4, A2-07) ─────────────────────────────────
#
# Настройки в шапке мастера: группа «Язык анкеты» видна только при включённом модуле
# (`delegate_lang_enabled`, фаза 27), переключение зовёт ТОТ ЖЕ `set_user_lang`, что бот
# (`handlers/reg_lang.py`) — второй точки записи `users.lang` не заводим. `form_gate`, не
# `delegate_gate` — тот же гейт, что у остальных ручек анкеты (незарегистрированный делегат
# посреди мастера тоже может переключить язык).

class LangPatch(BaseModel):
    lang: str


@router.post("/app/api/reg/lang")
async def set_lang(
    body: LangPatch,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    if await get_setting_typed("delegate_lang_enabled") != "on":
        raise HTTPException(403, {"reason": "lang_module_off"})
    if body.lang not in ("ru", "en"):
        raise HTTPException(400, {"reason": "bad_field", "field": "lang"})
    await set_user_lang(p.telegram_id, body.lang)
    return {"lang": body.lang}


# ── POST /app/api/reg/draft/submit ───────────────────────────────────────────────────────

@router.post("/app/api/reg/draft/submit")
async def draft_submit(
    request: Request,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    # Квик 260911-w2m: ПЕРВОЕ действие функции — до согласий, обязательно до
    # `claim_reg_draft` (захваченный при отказе черновик остался бы залоченным). Маршрут не
    # собирал `ctx` сам до этого квика — один лишний проход по `_load_context` (тому же
    # источнику правды, что GET/PATCH), не вторая копия правила.
    ctx = await _load_context(p.telegram_id)
    edit_can_edit, edit_closed_text = await _edit_gate(ctx)
    if not edit_can_edit:
        raise HTTPException(409, {"reason": "edit_closed", "text": edit_closed_text})

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
        # UAT 21-12 находка 1 (round 2): claim ничего не нашёл по ДВУМ разным причинам —
        # строки `reg_drafts` вообще нет (обзор правки не PATCH-ил ни одного поля перед
        # отправкой, D-26) или строка есть, но уже отправляется (гонка с чатом, T-21-02).
        # Раньше обе ветки отвечали одним и тем же вводящим в заблуждение `already_submitting`
        # — делегат читал «уже отправляется», хотя правка просто не сохранилась.
        if await get_reg_draft(p.telegram_id) is None:
            raise HTTPException(409, {
                "reason": "no_draft",
                "text": await get_setting_typed("reg_form_no_draft_text"),
            })
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
    # Quick 260904-3vm (эстафета): второй слой к гварду бота (handlers/reg_handoff.py) — гвард
    # закрывает окно до 30 с, пока эта джоба очереди не проснулась. Молча — приложение уже
    # показало делегату экран «Заявка принята», второе уведомление не нужно.
    await enqueue("reg_fsm_reset", {"telegram_id": p.telegram_id, "reason": "submitted"})
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

    response = {
        "mode": result["mode"], "status": result["status"], "heading": heading, "body": body,
        # Квик 12.09 (UI-аудит, пункт 2): подпись кнопки выхода на терминальном экране —
        # раньше это была одна иконка check без текста и без aria-label (accessibility BLOCKER).
        "home_cta": await get_setting_typed("miniapp_form_complete_home_cta_text"),
    }
    # Phase 28 (28-06, SU-07, D-09): паритет с чатом бота — блок-предложение реф-ссылки на том
    # же терминальном экране «Заявка принята», только при mode == "new" и включённом тумблере
    # (дефолт off, D-06). Ссылка сама НЕ строится здесь — только тексты; сервером выдаётся
    # отдельным эндпоинтом POST /app/api/reg/ambassador по тапу «Хочу свою ссылку».
    if result["mode"] == "new" and await get_setting_typed("reg_offer_ref_link") == "on":
        response["ambassador"] = {
            "heading": await get_setting_typed_for_city(
                "miniapp_form_ambassador_offer_heading_text", event_city,
            ),
            "body": await get_setting_typed_for_city(
                "miniapp_form_ambassador_offer_body_text", event_city,
            ),
            "cta": await get_setting_typed("miniapp_form_ambassador_cta_text"),
            "later": await get_setting_typed("miniapp_form_ambassador_later_text"),
        }
    return response


# ── POST /app/api/reg/ambassador ─────────────────────────────────────────────────────────

@router.post("/app/api/reg/ambassador")
async def draft_ambassador(
    request: Request,
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    """«Хочу свою ссылку» (SU-07, D-09) — паритет с ботовским `regamb:want`. Пишет ТОЛЬКО
    `is_ambassador` СВОЕЙ строки автора запроса (T-28-06-03: `allowed_columns` из одной
    колонки, `telegram_id` — из подписанного initData, не из тела запроса). Ссылка строится
    сервером (`ref_code` = `telegram_id`, OQ-3) — фронт её не собирает и не может подделать."""
    await update_user_answers(p.telegram_id, {"is_ambassador": 1}, allowed_columns=["is_ambassador"])
    bot_username = request.app.state.cfg.bot_username
    link = f"https://t.me/{bot_username}?start=amb_{p.telegram_id}" if bot_username else None
    return {
        "link": link,
        "heading": await get_setting_typed("miniapp_form_ambassador_link_heading_text"),
        "copy_button": await get_setting_typed("miniapp_form_ambassador_copy_button_text"),
        "copied_toast": await get_setting_typed("miniapp_form_ambassador_copied_toast_text"),
    }


# ── Поиск по справочнику ВУЗ/город (A2-03) ──────────────────────────────────────────────────

# Phase 30 (30-02, A2-03): шаг -> вид справочника `services.lookup` (закрытый словарь
# "university"/"city"). Кроме этих двух шагов у `step_type_v2` в этой фазе типа `lookup`
# нет (30-01-SUMMARY.md) — карта заведомо покрывает всё множество lookup-шагов сегодня, расти
# ей вместе с `_STEP_TYPE_V2_OVERRIDES` в `reg_engine.py`, если появится третий.
_STEP_TO_LOOKUP_KIND = {"university": "university", "city": "city"}

# Короче двух символов — «поиск» по одной букве не сигнал, а лишняя нагрузка на БД на каждое
# нажатие клавиши (T-30-04); чипы всё равно отдаются.
_SUGGEST_MIN_QUERY_LEN = 2
_SUGGEST_CHIPS_LIMIT = 8
_SUGGEST_RESULTS_LIMIT = 10


@router.get("/app/api/reg/suggest")
async def reg_suggest(
    step: str = "",
    q: str = "",
    p: Principal = Depends(form_gate),
    _: Principal = Depends(require_section("form")),
) -> dict:
    """Поиск по справочнику ВУЗ/город для типа шага `lookup` (A2-03, задача 4 плана 30-02) —
    рендер экрана делает план 30-03, здесь только данные. Судья формата — `services.lookup`
    (нормализация/ранжирование/чипы), этот роутер не содержит собственных правил сравнения
    строк — та же дисциплина, что у `validate_answer` выше (T-21-05).

    `step` — ключ шага анкеты (не `kind` напрямую: фронт знает шаг, `kind` — закрытый словарь
    `services.lookup`). Шаг ОБЯЗАН быть `reg_engine.step_type_v2(step) == "lookup"` — иначе
    (неизвестный шаг, шаг другого типа, делегат на устаревшей версии клиента после того, как
    менеджер выключил тумблер) отдаётся пустой ответ, НЕ 500 (T-30-04, тот же fail-soft
    принцип, что у `_STEP_TO_LOOKUP_KIND.get`).

    `q` обрезается по `reg_engine.MAX_LEN_DEFAULT` (T-30-04, DoS длинным `q`); короче
    `_SUGGEST_MIN_QUERY_LEN` символов — только чипы, без похода в `search_lookup` вовсе.
    `other_allowed` — из `reg_engine._OTHER_ALLOWED_STEPS` (та же атрибутная модель списка,
    что у сегодняшних choice-шагов с «Другое»; план 30-07 заведёт отдельный экран атрибутов).
    """
    step_key = (step or "").strip()
    kind = _STEP_TO_LOOKUP_KIND.get(step_key)
    if kind is None or reg_engine.step_type_v2(step_key) != "lookup":
        return {"chips": [], "results": [], "other_allowed": False}

    query_text = (q or "")[: reg_engine.MAX_LEN_DEFAULT]
    other_allowed = step_key in reg_engine._OTHER_ALLOWED_STEPS

    # `event_city` зарезервирован контрактом `top_chips` (`services/lookup.py`) на будущую
    # city-scoped политику чипов — сегодня не читается функцией, поэтому здесь не тратим
    # лишний поход в БД за городом делегата ради параметра, который пока ни на что не влияет.
    chips = await top_chips(kind, None, limit=_SUGGEST_CHIPS_LIMIT)
    results: list[dict] = []
    if len(query_text.strip()) >= _SUGGEST_MIN_QUERY_LEN:
        results = await search_lookup(kind, query_text, limit=_SUGGEST_RESULTS_LIMIT)

    return {"chips": chips, "results": results, "other_allowed": other_allowed}


__all__ = ["router", "DraftPatch"]
