"""Phase 21 (21-08, FORM-SYNC-02/04, Pattern 4 «данные синхронно, эффекты асинхронно») —
общий финал анкеты для бота и Mini App.

До этого плана `handlers/registration.py::finalize_registration` был единственной реализацией
сохранения анкеты. Mini App без общего финала получила бы вторую реализацию — с другими
дефолтами, другим расчётом статуса и вторым (append-only) путём в Google Sheets — то есть
ИМЕННО тот «второй движок», которого фаза 21 избегает (RESEARCH «Anti-Patterns to Avoid»).

Разрез на два шага:

- `finalize_data(telegram_id, username, draft)` — ТОЛЬКО данные: `users`/`reg_answer_history`,
  статус, `reg_drafts`. Вызывается ПОСЛЕ успешного `database.db.claim_reg_draft` (проигравший
  двойной финал получает `None` от claim и вообще не зовёт эту функцию — T-21-02). Любое
  исключение внутри освобождает claim (`release_reg_draft`) и пробрасывается наверх — иначе
  черновик навсегда остался бы «в отправке» (T-21-24).
- `post_finalize(bot, telegram_id, mode, ...)` — эффекты, которые обязаны идти через бота:
  Nextcloud (резюме), Google Sheets (append для новой анкеты, `update_row_by_id` для правки),
  уведомление менеджерам по capability, приветственный скрипт при auto-approve. Зовут её ОБА
  вызывающих с одинаковым журналом Sheets/уведомлений: бот — сразу после `finalize_data`
  (`handlers/registration.py::finalize_registration`), джоба очереди — по kind
  `reg_finalized`/`reg_edited` (`services/miniapp_outbox.py::_handle_row`), когда Mini App
  поставит эти события (планы 21-09..21-11).

Текст-подтверждение делегату В ЧАТЕ (`reg_complete_text` и его правочные аналоги) отправляет
САМА `finalize_registration` — она держит живой `message` (правильный чат, тот же тап «Всё
верно»), а не только `telegram_id`. Если бы это же делал `post_finalize`, прямой вызов из бота
отправил бы делегату сообщение ДВАЖДЫ. `resolve_delegate_text` ниже — общая точка правды для
режима `edit` (заголовки D-10/D-12), которую использует и бот, и (когда появится) веб-финал
Mini App; режим `new` продолжает резолвиться самой `finalize_registration` — так сохраняется её
собственный try/except-фоллбэк на глобальный `reg_complete_text` при сбое резолвера.

Модуль без телеграм-фреймворка, кроме одного нетипизированного параметра `bot` у
`post_finalize` (сам файл не импортирует этот фреймворк ни разу — грепается тестом плана).
Вызовы `handlers.registration`/`handlers.reg_schema`/`handlers.admin_caps` внутри
`post_finalize` — ЛОКАЛЬНЫЕ (внутри функции):
`handlers/registration.py` импортирует `finalize_data`/`post_finalize` из ЭТОГО модуля на своём
верхнем уровне, поэтому обратный импорт на уровне модуля дал бы цикл при загрузке пакета
`handlers` (тот же приём уже используют `handlers/reg_schema.py::approve_user`/
`incomplete_city_batches` — «function-body-local import» дословно оттуда).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from datetime import datetime, timedelta

import reg_engine
from reg_labels import REG_LABELS
from cities import get_setting_for_city
from config import config
from database.db import (
    add_user,
    get_user,
    get_setting,
    set_user_status,
    set_user_subscribed,
    clear_reg_started,
    reset_payment_for_new_season,
    record_reg_event,
    update_user_answers,
    record_answer_history,
    mark_user_edited,
    delete_reg_draft,
    release_reg_draft,
    get_resume_upload_backlog,
    settings_snapshot,
)
from settings_schema import get_setting_typed
from services.timeutil import msk_now

logger = logging.getLogger(__name__)


async def _resume_field_patch(resume_type_val, resume_link_val) -> dict:
    """Phase 28 (28-04, SU-04): пересчитывает `resume_type`/`link_verified` для узкого UPDATE
    после `add_user` (эталон — `source_from_tag` выше, `resume_url` в `post_finalize` ниже) —
    большой INSERT не трогаем (RESEARCH Anti-Pattern). `link_verified` — ВСЕГДА серверный
    расчёт по актуальному вайтлисту (T-28-04-01): присланное клиентом значение НИКОГДА не
    участвует, эта функция его даже не принимает на вход."""
    patch: dict = {}
    if resume_type_val not in (None, ""):
        patch["resume_type"] = resume_type_val
    if resume_link_val not in (None, "", "-"):
        whitelist = await reg_engine.resume_link_whitelist()
        _, verified, _ = reg_engine.validate_resume_link(resume_link_val, whitelist)
        patch["link_verified"] = 1 if verified else 0
    elif resume_type_val == "link":
        # Развилка привела на R2b, но ссылки почему-то нет (edit стёр её пустым патчем) —
        # признак «проверено» не может пережить саму ссылку.
        patch["link_verified"] = 0
    return patch


async def _score_patch(answers: dict) -> dict:
    """Phase 28 (28-07, SU-08, A-04, OQ-4): безусловный пересчёт балла — узкий UPDATE после
    `add_user` (new) и в ветке `edit`, тот же приём, что `_resume_field_patch` выше. Выключенный
    `reg_scoring_enabled` -> `{}` СРАЗУ (T-28-07-05): ни одного похода в реестр за правилами,
    ни одной записи — на других событиях (D-06 default off) ничего не меняется. Сбой скоринга
    не имеет права потерять заявку (T-28-07-03/принцип season-резолва выше в этом файле) —
    свой try/except с логом, возврат `{}` (заявка сохраняется без балла)."""
    if not await get_setting_typed("reg_scoring_enabled"):
        return {}
    try:
        rules = await reg_engine.scoring_rules()
        score, is_it_3plus = reg_engine.compute_score(answers, rules)
        return {"score": score, "is_it_3plus": int(is_it_3plus)}
    except Exception as e:
        logger.error(f"Scoring failed, application preserved without score: {e}")
        return {}


def _auto_rule_label(rule_id, rules_by_id: dict) -> str:
    """Человеческая подпись сработавшего правила для колонки «Детали»/уведомления менеджеру —
    своё имя правила (D-10), если менеджер его задал, иначе первые слова текста отказа. Ни id,
    ни код условия сюда не попадают (CLAUDE.md «бот для людей»)."""
    rule = rules_by_id.get(rule_id) or {}
    name = str(rule.get("name") or "").strip()
    if name:
        return name
    text = str(rule.get("reject_text") or "").strip()
    if not text:
        return "без описания"
    words = text.split()
    short = " ".join(words[:6])
    return short + "…" if len(words) > 6 else short


async def _auto_reject_patch(telegram_id: int, answers: dict, status: str) -> dict:
    """Phase 31 (31-06, D-01..D-32): оценка правил автоотказа — рядом с `_score_patch`, по её
    образцу. Гейт первым действием: `active_rules` вернул пустой список (общий рубильник
    `reject_rules_enabled` выключен или для этого города/трека нет активных правил) -> `{}`
    СРАЗУ, ни одного лишнего похода в базу.

    Вызывается ПОСЛЕ `decide_status`/`set_user_status` в ветке новой заявки и ПОСЛЕ того, как
    `add_user` уже сохранил ВСЕ ответы делегата (D-14 фазы «Правила автоотказа»: заявка
    принимается ЦЕЛИКОМ — ни один вопрос анкеты не пропускается, делегат доходит до конца — и
    только ПОТОМ правило может её отклонить); в ветке `edit` — после полного пересчёта текущих
    ответов (план 31-06, задача 3).

    Весь блок — собственный try/except с `logger.error`: сбой оценки правил (сеть, битые
    условия правила, что угодно) не имеет права потерять заявку — анкета сохраняется обычным
    путём, та же дисциплина, что у `_score_patch`/резолва season выше в этом файле.

    Возвращает пустой словарь при отсутствии срабатывания, иначе словарь с колонками для
    узкого UPDATE (`auto_reject_rule_ids`, `auto_rejected_at`, `flagged_rule_ids`,
    `auto_rule_note`, и `rejected_at` — только когда сработал отказ) плюс хвостовые ключи для
    вызывающего (`status_override`, `reject_rule_ids`, `reject_texts`), которые в narrow UPDATE
    не идут."""
    from services.reject_rules import active_rules, forum_date_for

    try:
        rules = await active_rules(
            event_city=answers.get("event_city"),
            participant_type=answers.get("participant_type"),
        )
        if not rules:
            return {}

        forum_date = await forum_date_for(answers.get("event_city"))
        result = reg_engine.evaluate_reject_rules(
            answers, rules,
            birth_date=answers.get("birth_date"), forum_date=forum_date,
        )
        reject_rule_ids = result["reject_rule_ids"]
        reject_texts = result["reject_texts"]
        flag_rule_ids = result["flag_rule_ids"]
        if not reject_rule_ids and not flag_rule_ids:
            return {}

        rules_by_id = {r.get("id"): r for r in rules}
        now_stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
        if reject_rule_ids:
            label = _auto_rule_label(reject_rule_ids[0], rules_by_id)
            note = f"🤖 Автоотказ {msk_now().strftime('%d.%m')} (правило: {label})"
        else:
            label = _auto_rule_label(flag_rule_ids[0], rules_by_id)
            note = f"⚠️ Помечена правилом: {label}"

        patch: dict = {
            "auto_reject_rule_ids": (
                json.dumps(reject_rule_ids, ensure_ascii=False) if reject_rule_ids else None
            ),
            "auto_rejected_at": now_stamp if reject_rule_ids else None,
            "flagged_rule_ids": (
                json.dumps(flag_rule_ids, ensure_ascii=False) if flag_rule_ids else None
            ),
            "auto_rule_note": note,
            "status_override": result["status_override"],
            "reject_rule_ids": reject_rule_ids,
            "reject_texts": reject_texts,
            "flag_rule_ids": flag_rule_ids,
        }
        if reject_rule_ids:
            patch["rejected_at"] = now_stamp
        return patch
    except Exception as e:
        logger.error(
            f"Auto-reject rules evaluation failed for {telegram_id}, application preserved "
            f"without auto-reject: {e}"
        )
        return {}


async def finalize_data(telegram_id: int, username: str | None, draft: dict) -> dict:
    """Синхронная (в смысле «сразу», не «эффекты потом») часть финала — вызывается ПОСЛЕ
    `database.db.claim_reg_draft`. `draft` — строка `reg_drafts` (или псевдо-черновик,
    собранный вызывающим из FSM-данных чата, пока бот сам не пишет в `reg_drafts`).

    Возвращает `{"status", "mode", "changed_columns", "remoderated", "resubmitted",
    "resume_file_id", "resume_file_name", "auto_rejected", "flagged_rule_ids"}` — этого
    достаточно и боту, и (в будущем) роутеру Mini App, чтобы решить, какой текст показать и
    что передать в `post_finalize`. Два последних ключа (Phase 31, 31-06) — хвостовые,
    существующие вызывающие их просто не читают.

    Perf (замер 260917): десяток последовательных get_setting/get_setting_typed (season,
    scoring, registration_mode, full/short/party_approval и т.д.) — ни одного сетевого
    вызова и ни одного create_task в этой функции (только `add_user`/`update_user_answers`/
    учёт истории — свои соединения, снимок их не трогает), поэтому снимок безопасно
    накрывает функцию целиком. `post_finalize` (Sheets/Nextcloud/уведомления — сеть) снимком
    НЕ оборачивается — там держать его открытым поперёк секунд сетевого ожидания уже не
    стоит той крохи, что он экономит."""
    async with settings_snapshot():
        return await _finalize_data_impl(telegram_id, username, draft)


async def _finalize_data_impl(telegram_id: int, username: str | None, draft: dict) -> dict:
    mode = draft.get("kind") or "new"
    raw_answers = draft.get("answers") or {}

    # Хотфикс 06.09 (с b460826 обёртка передаёт СЮДА реальный черновик `reg_drafts`, а не
    # FSM-словарь): город и трек анкеты живут в КОЛОНКАХ черновика (`event_city`,
    # `participant_type`), а признак «пришёл по деп-линку» (`source`/`referrer_id`) — в
    # `draft["meta"]` (`handlers/registration.py::_start_registration_flow`, `meta_patch`).
    # Ни то ни другое не попадает в `draft["answers"]` — до b460826 всё это лежало прямо в
    # FSM `data`, которую обёртка и передавала сюда как "answers". Подмешиваем ПОД реальные
    # ответы (те, что делегат заполнил сам, — например руками ответил на «Источник» — обязаны
    # победить черновиковые/деп-линковые значения), иначе `event_city` уходит в users как NULL,
    # а `with_defaults` подставляет «Вопрос не задавался» вместо настоящей метки кампании.
    # Пседво-черновик (fallback из FSM, см. `finalize_registration`) этих ключей на верхнем
    # уровне не имеет — `draft.get(...)` вернёт `None`, слияние станет no-op.
    draft_level: dict = {}
    for col in ("event_city", "participant_type"):
        if draft.get(col):
            draft_level[col] = draft[col]
    meta = draft.get("meta") or {}
    for key in ("source", "referrer_id"):
        if meta.get(key) not in (None, ""):
            draft_level[key] = meta[key]
    raw_answers = {**draft_level, **raw_answers}

    changed_columns: list[str] = []
    remoderated = False
    resubmitted = False
    status = None
    answers = raw_answers
    # Phase 31 (31-06): хвостовые ключи возвращаемого словаря — существующие вызывающие их
    # просто не читают (byte-compat), план 31-08/31-11 читает через finalize_data напрямую.
    auto_rejected = False
    flagged_rule_ids_out: list = []

    try:
        if mode == "edit":
            # T-21-19: `raw_answers` из правки МОЖЕТ быть partial patch'ем (только точечно
            # тронутые поля, D-26 "касание поля открывает шаг мастера только для него") — если
            # звать reg_engine.diff/with_defaults прямо на нём, каждое НЕтронутое поле анкеты
            # (отсутствующее в patch) выглядело бы как «изменено на пусто». Поэтому сперва
            # достраиваем ПОЛНЫЙ набор текущих ответов из уже сохранённой строки users, и
            # только патч побеждает поверх него (D-19, per-field LWW) — diff() после этого
            # видит изменившимся ровно то, что реально изменилось.
            old = await get_user(telegram_id) or {}
            status = old.get("status")
            base = {col: old.get(col) for col in reg_engine.answer_columns()}
            answers = {**base, **raw_answers}
            changes = reg_engine.diff(old, answers)
            changed_columns = [c["column"] for c in changes]
            source = draft.get("updated_by") or "bot"
            season = old.get("season")

            if changes:
                patch = {c["column"]: answers.get(c["column"]) for c in changes}
                await record_answer_history(telegram_id, changes, source, season)
                await update_user_answers(
                    telegram_id, patch, allowed_columns=reg_engine.answer_columns()
                )
                await mark_user_edited(telegram_id, source)

                if status == "rejected":
                    # D-10: повторная подача отклонённой анкеты -> pending, отдельная запись
                    # истории {"column": "status", ...}, которую admin_moderation.py (21-07,
                    # _edit_badges_for) уже умеет распознавать как признак «🔁 Повторная подача».
                    resubmitted = True
                    status = "pending"
                    await record_answer_history(
                        telegram_id,
                        [{"column": "status", "old": "rejected", "new": "pending"}],
                        source, season,
                    )
                    await set_user_status(telegram_id, status)
                elif await get_setting_typed("toggle_reg_edit_remoderation") == "on":
                    # D-12: тумблер «Изменённая анкета — снова на модерацию».
                    remoderated = True
                    status = "pending"
                    await set_user_status(telegram_id, status)
            # Пустой diff (D-14): истории нет, edited_at не выставлен, статус не трогаем —
            # правка без фактических изменений — не событие.

            # Phase 28 (28-04, SU-04): resume_type/link_verified — производные поля, не
            # входят в `answer_columns()`/`diff()` (комментарий 28-01: сознательно НЕ в
            # add_user INSERT), поэтому пересчитываются здесь отдельно, а не через `patch`
            # выше. Гейт по присутствию ключа в самом патче правки (не в `changes`) — правка,
            # не тронувшая развилку резюме, не должна слать лишний UPDATE (дух D-14).
            if "resume_type" in raw_answers or "resume_link" in raw_answers:
                resume_type_val = raw_answers.get("resume_type", old.get("resume_type"))
                resume_link_val = answers.get("resume_link")
                resume_patch = await _resume_field_patch(resume_type_val, resume_link_val)
                if resume_patch:
                    await update_user_answers(
                        telegram_id, resume_patch,
                        allowed_columns=["resume_type", "link_verified"],
                    )

            # Phase 28 (28-07, SU-08, OQ-4): пересчёт БЕЗУСЛОВНЫЙ (не только когда diff задел
            # влияющие поля) — поле не должно протухать при частичном патче. Запись — только
            # если значения реально изменились (D-14: «правка без изменений» остаётся
            # не-событием, тот же принцип, что у diff() выше).
            score_patch = await _score_patch(answers)
            if score_patch and (
                score_patch["score"] != old.get("score")
                or score_patch["is_it_3plus"] != old.get("is_it_3plus")
            ):
                await update_user_answers(
                    telegram_id, score_patch, allowed_columns=["score", "is_it_3plus"]
                )
        else:
            answers = reg_engine.with_defaults(raw_answers)
            data = dict(answers)
            data["telegram_id"] = telegram_id
            data["username"] = username or "-"
            data["registration_date"] = msk_now().strftime("%Y-%m-%d %H:%M:%S")
            # Phase 5 (D-01): a flow that never saw a party link writes the default explicitly.
            data.setdefault("participant_type", "full")

            # Phase 07.3 (RET-01/RET-02): season/prev_season resolved BEFORE add_user, in its
            # own fail-soft try/except -- a season-resolve error must never lose an
            # already-collected application (T-073-04-07). `_prior_answers` is deliberately
            # NEVER stored in reg_drafts (RESEARCH anti-pattern) -- it only ever arrives here
            # via the chat wrapper's FSM-sourced pseudo-draft; a Mini App draft simply won't
            # have it, so `prior` is `{}` and this whole block is a no-op for that surface.
            prior = raw_answers.get("_prior_answers") or {}
            season = None
            try:
                season = (await get_setting("event_season") or "").strip() or None
                data["season"] = season
                # Квик 260914-k74 (LEAK-01): снимок со сводки («Изменить») несёт маркер
                # `_from_confirm` — это ТЕКУЩИЕ ответы этой же анкеты, а не прошлый сезон.
                # Без этой проверки делегат, просто поправивший поле на сводке, получал
                # «🔁 Повторный: был(а) на прошлом событии» в карточке модерации
                # (handlers/admin_moderation.py, services/applications.py) — настоящий
                # возвращенец (rereg_start / ?start=edit / admin_rereg) маркера не несёт,
                # для него ветка не меняется.
                if prior and not prior.get("_from_confirm"):
                    data["prev_season"] = (prior.get("season") or "").strip() or "legacy"
            except Exception as e:
                logger.error(f"Season resolve failed for {telegram_id}: {e}")
                season = None
                data["season"] = None

            # Ночное ревью, находка #4: единственный неогороженный await во всей финализации —
            # падение здесь обязано быть видимым (пробрасывается в общий except ниже), а не
            # тихо поглощённым, иначе делегат уверен, что зарегистрировался, а строки нет.
            await add_user(data)

            # Quick 260904-aup (D5, «Источник»): узкий UPDATE — тот же приём, которым ниже по
            # этой же функции post_finalize дописывает `resume_url` (не разрастание большого
            # INSERT в add_user). `draft["meta"]` — единственное место, где ещё жив признак
            # «источник — из деп-линка», к моменту delete_reg_draft ниже он исчезнет.
            if (draft.get("meta") or {}).get("source_from_tag"):
                await update_user_answers(
                    telegram_id, {"source_from_tag": 1}, allowed_columns=["source_from_tag"]
                )

            # Phase 28 (28-04, SU-04): resume_type/link_verified — узкий UPDATE, тот же приём.
            resume_patch = await _resume_field_patch(
                data.get("resume_type"), data.get("resume_link")
            )
            if resume_patch:
                await update_user_answers(
                    telegram_id, resume_patch, allowed_columns=["resume_type", "link_verified"]
                )

            # Phase 28 (28-07, SU-08, OQ-4): узкий UPDATE, тот же приём, что resume_type/
            # link_verified/source_from_tag выше — большой INSERT в add_user не трогаем
            # (RESEARCH Anti-Pattern).
            score_patch = await _score_patch(data)
            if score_patch:
                await update_user_answers(
                    telegram_id, score_patch, allowed_columns=["score", "is_it_3plus"]
                )

            reg_mode = await get_setting_typed("registration_mode")
            full_setting = await get_setting_typed("full_approval")
            short_setting = await get_setting_typed("short_approval")
            party_setting = await get_setting_typed("party_approval")
            status = reg_engine.decide_status(
                reg_mode, full_setting, short_setting,
                participant_type=data.get("participant_type", "full"),
                party_setting=party_setting,
            )
            try:
                await set_user_status(telegram_id, status)
            except Exception as e:
                logger.error(f"Failed to set status for {telegram_id}: {e}")

            # Phase 31 (31-06, D-14 фазы): оценка правил автоотказа — ПОСЛЕ decide_status/
            # set_user_status и после того, как add_user уже сохранил ВСЕ ответы делегата
            # (заявка принимается ЦЕЛИКОМ, ни один вопрос анкеты не пропускается) — и только
            # теперь правило может её отклонить. Данные — синхронно, здесь; эффекты делегату/
            # менеджеру/лист — в post_finalize (Pattern 4 «данные синхронно, эффекты async»).
            auto_patch = await _auto_reject_patch(telegram_id, data, status)
            if auto_patch:
                column_patch = {
                    "auto_reject_rule_ids": auto_patch["auto_reject_rule_ids"],
                    "auto_rejected_at": auto_patch["auto_rejected_at"],
                    "flagged_rule_ids": auto_patch["flagged_rule_ids"],
                    "auto_rule_note": auto_patch["auto_rule_note"],
                }
                if "rejected_at" in auto_patch:
                    column_patch["rejected_at"] = auto_patch["rejected_at"]
                await update_user_answers(
                    telegram_id, column_patch,
                    allowed_columns=["auto_reject_rule_ids", "auto_rejected_at", "flagged_rule_ids", "auto_rule_note", "rejected_at"],
                )
                flagged_rule_ids_out = auto_patch["flag_rule_ids"]
                if auto_patch["status_override"] == "rejected":
                    # Пометка (flag) статус НЕ меняет — заявка остаётся на обычной модерации
                    # с бейджем (auto_rule_note уже записан узким UPDATE выше).
                    status = "rejected"
                    auto_rejected = True
                    await set_user_status(telegram_id, status)
                    from services.reject_journal import record_auto_reject
                    await record_auto_reject(
                        telegram_id, auto_patch["reject_rule_ids"], auto_patch["reject_texts"],
                    )

            try:
                await record_reg_event(
                    telegram_id, "form_completed",
                    event_city=data.get("event_city"), season=season,
                )
            except Exception as e:
                logger.error(f"record_reg_event(form_completed) failed for {telegram_id}: {e}")

            try:
                await clear_reg_started(telegram_id)
            except Exception as e:
                logger.error(f"Failed to clear reg_started for {telegram_id}: {e}")

            if prior and season and (prior.get("season") or "").strip() != season:
                try:
                    await reset_payment_for_new_season(telegram_id)
                except Exception as e:
                    logger.error(f"reset_payment_for_new_season failed for {telegram_id}: {e}")

        await delete_reg_draft(telegram_id)
    except Exception as e:
        logger.warning(f"finalize_data failed for {telegram_id}, releasing draft claim: {e}")
        await release_reg_draft(telegram_id)
        raise

    return {
        "status": status,
        "mode": mode,
        "changed_columns": changed_columns,
        "remoderated": remoderated,
        "resubmitted": resubmitted,
        "resume_file_id": answers.get("resume_file_id"),
        "resume_file_name": answers.get("resume_file_name"),
        # Phase 31 (31-06): хвостовые ключи — существующие вызывающие их просто не читают.
        "auto_rejected": auto_rejected,
        "flagged_rule_ids": flagged_rule_ids_out,
    }


def _resume_file_stem(full: dict, telegram_id: int, mode: str = "full") -> str:
    from handlers.registration import _resume_file_stem as _stem
    return _stem({**full, "telegram_id": telegram_id}, mode=mode)


async def _resume_filename_mode() -> str:
    """Phase 28 (28-09, SU-10, Pitfall 4): реестровый тумблер `resume_filename_short_mode`
    (enum on/off, дефолт "off" — прежнее имя байт-в-байт) -> режим `_resume_file_stem`.
    Читается ЗДЕСЬ (async-вызывающий), не самой `_resume_file_stem` — она остаётся чистой
    sync-функцией без обращений к БД."""
    return "id" if await get_setting_typed("resume_filename_short_mode") == "on" else "full"


def _column_label(column: str) -> str:
    step = reg_engine.column_to_step(column)
    if step:
        label = REG_LABELS.get(f"reg_q_{step}")
        if label:
            return label
    if column in ("resume_file_id", "resume_file_name", "resume_text"):
        return REG_LABELS.get("reg_q_resume", "Резюме")
    return column


def _edited_note(changed_columns: list[str], edited_at: str | None) -> str:
    """D-16: строка для колонки «Детали» — «✏️ Изменена дд.мм (поля: ...)», человеческие
    подписи полей из REG_LABELS (через reg_engine.column_to_step), без новых колонок листа."""
    date_part = ""
    if edited_at:
        try:
            date_part = datetime.strptime(str(edited_at), "%Y-%m-%d %H:%M:%S").strftime("%d.%m")
        except ValueError:
            date_part = str(edited_at)
    note = "✏️ Изменена"
    if date_part:
        note += f" {date_part}"
    labels = ", ".join(_column_label(c) for c in changed_columns)
    if labels:
        note += f" (поля: {labels})"
    return note


def _new_admin_text(full: dict, status: str) -> str:
    """Дословный перенос текста уведомления менеджерам о НОВОЙ заявке из прежней
    `finalize_registration` (тот же формат, та же экранировка)."""
    safe_name = html.escape(str(full.get("full_name") or "-"))
    safe_username = html.escape(str(full.get("username") or "-"))
    admin_text = (
        f"\U0001f195 <b>Новая регистрация!</b>\n"
        f"\U0001f464 {safe_name} ({safe_username})"
    )
    if status == "pending":
        admin_text += "\n⏳ Ожидает одобрения (/admin → Заявки)"
    if full.get("local_committee") and full["local_committee"] != "-":
        admin_text += f"\n\U0001f3e2 {html.escape(str(full['local_committee']))}"
    if full.get("position") and full["position"] != "-":
        admin_text += f"\n\U0001f454 {html.escape(str(full['position']))}"
    if full.get("age"):
        admin_text += f"\n\U0001f382 {full['age']}"
    safe_source = html.escape(str(full.get("source") or "-"))
    if safe_source != "-":
        admin_text += f"\n\U0001f4dd {safe_source}"
    return admin_text


def _edit_admin_text(full: dict, resubmitted: bool) -> str:
    """D-10/D-12: уведомление менеджерам «как о новой заявке» при повторной подаче
    отклонённой анкеты или при включённой повторной модерации. Обычная правка (ни то ни
    другое) уведомления не вызывает вовсе (D-14) — эта функция для неё не зовётся."""
    safe_name = html.escape(str(full.get("full_name") or "-"))
    safe_username = html.escape(str(full.get("username") or "-"))
    heading = (
        "\U0001f501 <b>Повторная подача анкеты!</b>" if resubmitted
        else "✏️ <b>Анкета изменена — снова на модерации</b>"
    )
    return f"{heading}\n\U0001f464 {safe_name} ({safe_username})"


async def _resolve_update_tab(event_city: str | None, participant_type: str | None) -> str | None:
    """Имя вкладки для `update_row_by_id` — тот же маршрут, что и append при регистрации
    (`city_row_tab`), с фоллбэком на party/short вкладку ПО УМОЛЧАНИЮ, когда `city_row_tab`
    вернул `None` (в append-пути `None` там означает «используй дефолтный аппендер трека»,
    а не «главный лист» — для party/short это РАЗНЫЕ вкладки, не главный лист)."""
    from handlers.registration import PARTY_SHEET_TAB_DEFAULT, SHORT_SHEET_TAB_DEFAULT, city_row_tab

    tab = await city_row_tab(event_city, participant_type)
    if tab is not None:
        return tab
    if reg_engine._is_party_track(participant_type):
        return await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT
    if reg_engine._is_short_track(participant_type):
        return await get_setting("short_sheet_tab") or SHORT_SHEET_TAB_DEFAULT
    return None


async def resolve_delegate_text(
    mode: str, *, remoderated: bool = False, resubmitted: bool = False,
    event_city: str | None = None,
) -> str | None:
    """Текст-подтверждение делегату для режима `edit` (D-10/D-12) — общая точка правды для
    чата и будущего веб-финала Mini App. Режим `new` возвращает `None` намеренно:
    `finalize_registration` резолвит `reg_complete_text` сама (сохраняет свой собственный
    try/except-фоллбэк на глобальный текст при сбое резолвера — дублировать его здесь незачем
    и рискованно расходиться с ним)."""
    if mode != "edit":
        return None
    if resubmitted:
        key, default = "reg_form_resubmit_heading_text", "Заявка отправлена заново"
    elif remoderated:
        key, default = "reg_form_edited_pending_heading_text", "Анкета снова на проверке"
    else:
        key, default = "reg_form_edited_heading_text", "Изменения отправлены"
    try:
        return await get_setting_for_city(key, event_city) or default
    except Exception as e:
        logger.error(f"resolve_delegate_text({key}) failed: {e}")
        return default


async def post_finalize(
    bot,
    telegram_id: int,
    mode: str,
    *,
    changed_columns: list | None = None,
    remoderated: bool = False,
    resubmitted: bool = False,
    resume_file_id: str | None = None,
    resume_file_name: str | None = None,
    resume_text: str | None = None,
) -> None:
    """Хвост финала: Nextcloud (резюме) -> Sheets (append для `new`, `update_row_by_id` для
    `edit`) -> уведомление менеджерам по capability -> приветственный скрипт при auto-approve.
    Зовётся и ботом напрямую (сразу после `finalize_data`), и джобой очереди по kind
    `reg_finalized`/`reg_edited` (`services/miniapp_outbox.py`) — один и тот же журнал вызовов
    Sheets/уведомлений для обеих поверхностей (T-21-02, Task 3 acceptance)."""
    from handlers.registration import (
        _sheet_dispatch, _sheet_headers_fn, append_to_named_sheet, is_subscribed,
        _normalize_channel_ref, city_row_tab, approve_user,
    )
    from handlers.reg_schema import sheet_city_code
    from services.nextcloud import upload_resume, upload_text_resume
    from services.sheets import update_row_by_id

    resume_url = None
    if resume_file_id:
        try:
            stem_mode = await _resume_filename_mode()
            stem = _resume_file_stem(await get_user(telegram_id) or {}, telegram_id, mode=stem_mode)
            ext = os.path.splitext(resume_file_name or "")[1]
            resume_url = await asyncio.wait_for(
                upload_resume(bot, resume_file_id, f"{stem}{ext}"), timeout=20
            )
        except Exception as e:
            logger.error(f"Nextcloud resume upload failed for {telegram_id}: {e}")
    elif resume_text:
        try:
            stem_mode = await _resume_filename_mode()
            stem = _resume_file_stem(await get_user(telegram_id) or {}, telegram_id, mode=stem_mode)
            resume_url = await asyncio.wait_for(
                upload_text_resume(resume_text, f"{stem}.txt"), timeout=20
            )
        except Exception as e:
            logger.error(f"Nextcloud text resume upload failed for {telegram_id}: {e}")
    if resume_url:
        await update_user_answers(telegram_id, {"resume_url": resume_url}, allowed_columns=["resume_url"])

    full = await get_user(telegram_id) or {}
    if mode == "edit" and changed_columns:
        full = dict(full)
        full["_edited_note"] = _edited_note(changed_columns, full.get("edited_at"))

    # D-14 / plan behavior #3: правка без фактических изменений не трогает Sheets вовсе — не
    # только не пишет диф, но и не делает лишний update-запрос впустую.
    touches_sheet = mode == "new" or bool(changed_columns)
    if touches_sheet:
        try:
            row_fn, append_fn = _sheet_dispatch(full.get("participant_type"))
            # Phase 25 (CITYQ-03): резолвим город ОДИН раз на путь (не по разу на ветку
            # new/edit) — sheet_city_code и city_row_tab/_resolve_update_tab построены на
            # одних и тех же трёх ранних выходах (инвариант зафиксирован докстрингом
            # sheet_city_code), сверять их результат друг с другом не нужно.
            city = await sheet_city_code(full.get("event_city"))
            row = await row_fn(full, city)
            # Квик 260914-k74 (T2): заголовки для именованной вкладки — один раз на путь, в
            # отдельном try/except (вычисление заголовков не имеет права ломать аппенд строки).
            # append_fn(row) заголовки не принимает — их для своего трека считают сами
            # append_to_party_sheet/append_to_short_sheet (шаг 2 плана).
            try:
                headers = await _sheet_headers_fn(full.get("participant_type"))(city)
            except Exception as e:
                logger.warning(f"Failed to compute named sheet headers for {telegram_id}: {e}")
                headers = None
            # Инцидент 13.09: mode="new" описывает ПУТЬ анкеты (обычная регистрация ИЛИ
            # повторная подача — отклонённый после /start, делегат прошлого сезона
            # is_returning_row, человек после /delete_user), а НЕ факт отсутствия строки в
            # таблице. Старый код аппендил всегда для mode="new" — три источника повторной
            # подачи копили дубли по ID (в листе МСК до 5 на делегата). Теперь наличие строки
            # спрашиваем у самой таблицы: сначала update_row_by_id в ту же вкладку, куда ушёл
            # бы append (_resolve_update_tab повторяет маршрут city_row_tab), и только если
            # строки там нет — append. Если старая строка делегата лежит на главном листе, а
            # он теперь в городе с вкладкой, update_row_by_id найдёт её фоллбэком на главном
            # листе и обновит на месте — строка остаётся там, где была, второй не появляется.
            update_tab = await _resolve_update_tab(full.get("event_city"), full.get("participant_type"))
            found = await update_row_by_id(update_tab, telegram_id, row)
            if not found:
                if mode == "new":
                    tab = await city_row_tab(full.get("event_city"), full.get("participant_type"))
                    if tab is None:
                        await append_fn(row)
                    else:
                        await append_to_named_sheet(tab, row, headers)
                else:
                    if update_tab is None:
                        await append_fn(row)
                    else:
                        await append_to_named_sheet(update_tab, row, headers)
        except Exception as e:
            logger.error(f"Failed to write sheet row for {telegram_id}: {e}")

    status = full.get("status")
    if mode == "new":
        notify_admins = status == "approved" or (
            status == "pending" and await get_setting_typed("pending_notify_mode") == "instant"
        )
        admin_text = _new_admin_text(full, status) if notify_admins else None
    else:
        # D-14: обычная правка НЕ уведомляет менеджеров — только пометка в карточке/«Детали».
        notify_admins = remoderated or resubmitted
        admin_text = _edit_admin_text(full, resubmitted) if notify_admins else None

    if config.ADMIN_IDS and notify_admins and admin_text:
        # Квик 260916: ЕДИНСТВЕННАЯ дверь уведомления менеджерам о поданной анкете — и для
        # чата, и для Mini App (её submit приезжает сюда же через miniapp_outbox). Режим
        # «каждую отдельно / пачкой» и маршрутизация по городу живут в services/reg_digest.py;
        # прямой notify_by_capability отсюда убран намеренно — иначе настройка режима молча
        # перестала бы действовать на одном из двух путей. `is_new` отделяет НОВУЮ заявку
        # (может уйти пачкой) от правки/переподачи (всегда сразу: это не «новая заявка», и
        # менеджеру нужен текст «что изменилось», а не строчка в счётчике).
        from services.reg_digest import notify_application  # локальный импорт, как соседи
        await notify_application(
            bot, telegram_id=telegram_id, admin_text=admin_text,
            city_raw=full.get("event_city"), is_new=(mode == "new"),
        )

    # HG-01: subscription flag persisted AFTER the row definitely exists (fail-soft + fail-open).
    try:
        sub_channel = _normalize_channel_ref(await get_setting("contact_tg"))
        if sub_channel is not None:
            sub_result = await is_subscribed(bot, sub_channel, telegram_id)
            if sub_result is not None:
                await set_user_subscribed(telegram_id, sub_result)
    except Exception as e:
        logger.warning(f"Subscription persist skipped for {telegram_id}: {e}")

    if mode == "new" and status == "approved":
        # Quick 260904-3vm (E2): статус решён ЗДЕСЬ, сразу на подаче — значит модерации не
        # было (иначе status был бы "pending", а approve_user на одобрение позвал бы отдельный
        # путь менеджера — services/applications.py/admin_moderation.py). Делегат читает
        # «заявка принята», а не «прошёл отбор» — отбора не было. Покрывает и чат, и Mini App
        # (submit из приложения приходит сюда же через outbox reg_finalized).
        await approve_user(bot, telegram_id, auto_approved=True)


async def derive_edit_facts(telegram_id: int, full: dict) -> tuple[list, bool, bool]:
    """(changed_columns, remoderated, resubmitted) для `kind=reg_edited`, разобранного из
    очереди (T-21-08: payload несёт только `telegram_id`, НЕ ответы анкеты — эти факты уже
    записаны `finalize_data` в `reg_answer_history`/`users.status`, здесь их только читаем).

    `finalize_data` пишет РОВНО ОДНУ запись истории для обычной правки (поля) и ДВЕ для
    повторной подачи отклонённой (поля, затем отдельно маркер `{"column": "status", "old":
    "rejected", ...}` — тот же приём, что уже умеет распознавать `admin_moderation.py::
    _edit_badges_for`, план 21-07). `remoderated` не пишется отдельным маркером — выводится
    из того, что статус стал `pending` при непустом diff и это НЕ повторная подача."""
    from database.db import get_answer_history

    history = await get_answer_history(telegram_id, limit=2)
    changed_columns: list[str] = []
    resubmitted = False
    for row in history:
        changes = row.get("changes") or []
        if any(c.get("column") == "status" and c.get("old") == "rejected" for c in changes):
            resubmitted = True
            continue
        if not changed_columns:
            changed_columns = [c["column"] for c in changes if c.get("column")]
    remoderated = (not resubmitted) and full.get("status") == "pending" and bool(changed_columns)
    return changed_columns, remoderated, resubmitted


async def _apply_resume_url(telegram_id: int, full: dict, url: str | None) -> None:
    """Общий хвост записи ссылки на резюме: узкий UPDATE `resume_url` в `users` + обновление
    ячейки «Резюме (ссылка)» в Google Sheets той же `update_row_by_id`. Используется и
    моментальной догрузкой из Mini App (`handle_resume_upload`), и фоновой джобой повтора
    (`retry_pending_resume_uploads`), и очисткой дропзоны резюме в Mini App
    (`miniapp/routers/form.py::draft_patch`, квик 260915-4mv, `url=None`) — единый путь,
    чтобы поведение (какая ячейка листа обновляется, какая вкладка резолвится) не расходилось
    между вызывающими. `url=None` пишет `NULL` в `users.resume_url` и «-» в ячейку листа
    (`row_fn` уже умеет рендерить пустую ссылку) — файл в Некстклауде при этом НЕ удаляется,
    ссылка просто перестаёт где-либо отображаться (гигиена хранилища — отдельная тема).
    Сбой Sheets — `logger.error`, без проброса: ссылка в БД уже обновлена, лист догонит
    «Синхронизацией» (та же дисциплина, что и раньше в `handle_resume_upload`)."""
    from handlers.registration import _sheet_dispatch
    from handlers.reg_schema import sheet_city_code
    from services.sheets import update_row_by_id

    await update_user_answers(telegram_id, {"resume_url": url}, allowed_columns=["resume_url"])
    try:
        row_fn, _append_fn = _sheet_dispatch(full.get("participant_type"))
        city = await sheet_city_code(full.get("event_city"))
        row = await row_fn({**full, "resume_url": url}, city)
        tab = await _resolve_update_tab(full.get("event_city"), full.get("participant_type"))
        await update_row_by_id(tab, telegram_id, row)
    except Exception as e:
        logger.error(f"Failed to update resume cell for {telegram_id}: {e}")


async def handle_resume_upload(bot, telegram_id: int, file_id: str, filename: str | None) -> None:
    """kind=`reg_resume_upload` (D-05, Pattern 5): резюме, прикреплённое в Mini App — Nextcloud,
    запись `resume_url` узким UPDATE, обновление ячейки «Резюме (ссылка)» той же
    `update_row_by_id` (через `_apply_resume_url`), копия файла делегату в чат (подпись —
    `miniapp_upload_caption_resume`, тот же приём, что и у копии сдачи геймы, план 19-05)."""
    from services.nextcloud import upload_resume

    full = await get_user(telegram_id) or {}
    try:
        stem_mode = await _resume_filename_mode()
        stem = _resume_file_stem(full, telegram_id, mode=stem_mode)
        ext = os.path.splitext(filename or "")[1]
        url = await asyncio.wait_for(upload_resume(bot, file_id, f"{stem}{ext}"), timeout=20)
    except Exception as e:
        logger.error(f"Nextcloud resume upload (miniapp) failed for {telegram_id}: {e}")
        url = None

    if url:
        await _apply_resume_url(telegram_id, full, url)

    try:
        caption = await get_setting("miniapp_upload_caption_resume") or "\U0001f4ce Резюме получено"
        await bot.send_document(telegram_id, file_id, caption=caption)
    except Exception as e:
        logger.error(f"Failed to forward resume copy to {telegram_id}: {e}")


# Quick 260907-4ai (P0 SkillUp5): строки, чей файл Telegram уже не отдаёт («file not found» /
# «wrong file_id»), помечаются здесь на время жизни ПРОЦЕССА, чтобы очередь ретрая не крутилась
# вечно — перезапуск бота сбрасывает пометку намеренно (файл мог вернуться, а не только
# отвалиться навсегда).
_resume_retry_dead: set[int] = set()

# Финал грузит резюме под `asyncio.wait_for(timeout=20)` — строка моложе двух минут может быть
# ещё «в полёте» там; трогать её ретраем рано (двойная параллельная загрузка одного файла).
_RESUME_RETRY_MIN_AGE_MINUTES = 2


async def retry_pending_resume_uploads(bot, limit: int = 20) -> int:
    """Interval-джоба (services/scheduler.py::resume_upload_retry_job): резюме, не улетевшее в
    Nextcloud на финале (облако лежало/таймаут) — догружается сюда без участия делегата и
    менеджера. Делегату НИЧЕГО не шлём (в отличие от `handle_resume_upload` — там копия файла
    в чат уместна, потому что делегат только что сам её прислал; здесь же файл был прислан
    давно, повторное сообщение было бы для него неожиданным).

    Гейт ДО любого обращения к БД и боту — на стендах без настроенного Nextcloud (например
    тестовый стенд) тик джобы не стоит ни одного запроса."""
    from services.nextcloud import _resume_max_bytes, is_configured, upload_resume, upload_text_resume

    if not is_configured():
        return 0

    cutoff = (
        msk_now() - timedelta(minutes=_RESUME_RETRY_MIN_AGE_MINUTES)
    ).strftime("%Y-%m-%d %H:%M:%S")
    rows = await get_resume_upload_backlog(cutoff, limit)
    if not rows:
        return 0
    # Тумблер не меняется посреди одного тика джобы — читаем один раз на прогон, тот же
    # приём, что `kb`/`text` в nudge_incomplete_registrations (services/scheduler.py).
    stem_mode = await _resume_filename_mode()

    done = 0
    for row in rows:
        tid = row["telegram_id"]
        if tid in _resume_retry_dead:
            continue
        try:
            stem = _resume_file_stem(row, tid, mode=stem_mode)
            url = None
            file_id = row.get("resume_file_id")
            if file_id:
                try:
                    tg_file = await bot.get_file(file_id)
                except Exception as e:
                    text = str(e).lower()
                    if "file not found" in text or "wrong file_id" in text:
                        logger.warning(f"resume_upload_retry: файл делегата {tid} больше недоступен в Telegram")
                        _resume_retry_dead.add(tid)
                        continue
                    raise
                # Файл больше лимита облако не примет никогда (upload_resume вернёт None
                # каждый тик) — помечаем, как и недоступный файл, чтобы не шуметь в логах
                # каждые N минут одной и той же строкой.
                size = getattr(tg_file, "file_size", None)
                if size is not None and size > _resume_max_bytes():
                    logger.warning(f"resume_upload_retry: файл делегата {tid} больше лимита размера, пропускаем")
                    _resume_retry_dead.add(tid)
                    continue
                ext = os.path.splitext(tg_file.file_path or "")[1] or ".pdf"
                url = await asyncio.wait_for(upload_resume(bot, file_id, f"{stem}{ext}"), timeout=20)
            elif row.get("resume_text"):
                url = await asyncio.wait_for(
                    upload_text_resume(row["resume_text"], f"{stem}.txt"), timeout=20
                )

            if url:
                await _apply_resume_url(tid, row, url)
                logger.info(f"resume_upload_retry: резюме {tid} догружено")
                done += 1
            else:
                logger.warning(f"resume_upload_retry: выгрузка резюме {tid} не удалась, повторим позже")
        except Exception as e:
            logger.error(f"resume_upload_retry: строка {tid} упала: {e}")
        await asyncio.sleep(0.05)

    return done


__all__ = [
    "finalize_data", "post_finalize", "resolve_delegate_text",
    "derive_edit_facts", "handle_resume_upload", "retry_pending_resume_uploads",
]
