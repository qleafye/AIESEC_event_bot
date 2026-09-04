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
import logging
import os
from datetime import datetime

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
)
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)


async def finalize_data(telegram_id: int, username: str | None, draft: dict) -> dict:
    """Синхронная (в смысле «сразу», не «эффекты потом») часть финала — вызывается ПОСЛЕ
    `database.db.claim_reg_draft`. `draft` — строка `reg_drafts` (или псевдо-черновик,
    собранный вызывающим из FSM-данных чата, пока бот сам не пишет в `reg_drafts`).

    Возвращает `{"status", "mode", "changed_columns", "remoderated", "resubmitted",
    "resume_file_id", "resume_file_name"}` — этого достаточно и боту, и (в будущем) роутеру
    Mini App, чтобы решить, какой текст показать и что передать в `post_finalize`."""
    mode = draft.get("kind") or "new"
    raw_answers = draft.get("answers") or {}

    changed_columns: list[str] = []
    remoderated = False
    resubmitted = False
    status = None
    answers = raw_answers

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
        else:
            answers = reg_engine.with_defaults(raw_answers)
            data = dict(answers)
            data["telegram_id"] = telegram_id
            data["username"] = username or "-"
            data["registration_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
                if prior:
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
    }


def _resume_file_stem(full: dict, telegram_id: int) -> str:
    from handlers.registration import _resume_file_stem as _stem
    return _stem({**full, "telegram_id": telegram_id})


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
        _sheet_dispatch, append_to_named_sheet, is_subscribed, _normalize_channel_ref,
        city_row_tab, approve_user, notify_by_capability,
    )
    from services.nextcloud import upload_resume, upload_text_resume
    from services.sheets import update_row_by_id

    resume_url = None
    if resume_file_id:
        try:
            stem = _resume_file_stem(await get_user(telegram_id) or {}, telegram_id)
            ext = os.path.splitext(resume_file_name or "")[1]
            resume_url = await asyncio.wait_for(
                upload_resume(bot, resume_file_id, f"{stem}{ext}"), timeout=20
            )
        except Exception as e:
            logger.error(f"Nextcloud resume upload failed for {telegram_id}: {e}")
    elif resume_text:
        try:
            stem = _resume_file_stem(await get_user(telegram_id) or {}, telegram_id)
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
            row = await row_fn(full)
            if mode == "new":
                tab = await city_row_tab(full.get("event_city"), full.get("participant_type"))
                if tab is None:
                    await append_fn(row)
                else:
                    await append_to_named_sheet(tab, row)
            else:
                tab = await _resolve_update_tab(full.get("event_city"), full.get("participant_type"))
                found = await update_row_by_id(tab, telegram_id, row)
                if not found:
                    if tab is None:
                        await append_fn(row)
                    else:
                        await append_to_named_sheet(tab, row)
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
        await notify_by_capability(
            bot, "moderate_reg", admin_text, parse_mode="HTML", city=full.get("event_city")
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


async def handle_resume_upload(bot, telegram_id: int, file_id: str, filename: str | None) -> None:
    """kind=`reg_resume_upload` (D-05, Pattern 5): резюме, прикреплённое в Mini App — Nextcloud,
    запись `resume_url` узким UPDATE, обновление ячейки «Резюме (ссылка)» той же
    `update_row_by_id`, копия файла делегату в чат (подпись — `miniapp_upload_caption_resume`,
    тот же приём, что и у копии сдачи геймы, план 19-05)."""
    from services.nextcloud import upload_resume
    from services.sheets import update_row_by_id

    full = await get_user(telegram_id) or {}
    try:
        stem = _resume_file_stem(full, telegram_id)
        ext = os.path.splitext(filename or "")[1]
        url = await asyncio.wait_for(upload_resume(bot, file_id, f"{stem}{ext}"), timeout=20)
    except Exception as e:
        logger.error(f"Nextcloud resume upload (miniapp) failed for {telegram_id}: {e}")
        url = None

    if url:
        await update_user_answers(telegram_id, {"resume_url": url}, allowed_columns=["resume_url"])
        try:
            from handlers.registration import _sheet_dispatch

            row_fn, _append_fn = _sheet_dispatch(full.get("participant_type"))
            row = await row_fn({**full, "resume_url": url})
            tab = await _resolve_update_tab(full.get("event_city"), full.get("participant_type"))
            await update_row_by_id(tab, telegram_id, row)
        except Exception as e:
            logger.error(f"Failed to update resume cell for {telegram_id}: {e}")

    try:
        caption = await get_setting("miniapp_upload_caption_resume") or "\U0001f4ce Резюме получено"
        await bot.send_document(telegram_id, file_id, caption=caption)
    except Exception as e:
        logger.error(f"Failed to forward resume copy to {telegram_id}: {e}")


__all__ = [
    "finalize_data", "post_finalize", "resolve_delegate_text",
    "derive_edit_facts", "handle_resume_upload",
]
