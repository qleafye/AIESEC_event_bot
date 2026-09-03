"""Phase 19 (08, D-01/RESEARCH Pattern 7) — джоба бота: разбор `miniapp_outbox`.

Веб-процесс `miniapp` пишет побочные эффекты своих write-действий в таблицу
`miniapp_outbox` (план 19-04, `miniapp/outbox.py::enqueue`) вместо того, чтобы говорить с
Telegram или пересобирать таблицу самому — единственный писатель в Bot API и владелец
`services/game_sync.py::request_resync` debounce-таймера остаётся бот. Эта джоба —
единственное место, где очередь читается и разбирается.

Диспетчер по `kind` — закрытый набор (T-19-55): неизвестный `kind` НЕ исполняется (никогда
не eval/exec произвольного payload), а помечается ошибкой как любой другой сбой.

- `submission_created` -> `services.game_digest.notify_submission(bot, ...)` — тот же путь
  уведомления менеджеров, что и у сдачи из бота (режим «каждую сдачу»/дайджест решает сама
  функция).
- `submission_reviewed` / `task_changed` / `coins_manual` -> `services.game_sync.request_resync()`
  — debounced, синхронная, схлопывает пачку событий в один ребилд вкладок геймы. Делегата о
  решении по сдаче уведомляет сам Mini App (план 19-05) — повторно НЕ уведомляем.
- `reg_finalized` / `reg_edited` -> `services.reg_finalize.post_finalize(bot, telegram_id, mode,
  ...)` (Phase 21, 21-08) — тот же хвост (Sheets/уведомления менеджерам/приветствие при
  auto-approve), что и прямой вызов из чата (`handlers/registration.py::finalize_registration`).
  Payload несёт только `telegram_id` (T-21-08) — для `reg_edited` недостающие
  `changed_columns`/`remoderated`/`resubmitted` дочитывает `services.reg_finalize.
  derive_edit_facts` из уже записанной `reg_answer_history`/`users.status`.
- `reg_resume_upload` -> `services.reg_finalize.handle_resume_upload(bot, ...)` — резюме,
  загруженное в Mini App: Nextcloud + ячейка «Резюме (ссылка)» + копия делегату в чат (D-05).
- `application_decided` -> `services.application_effects.apply_decision_effects(bot,
  telegram_id, status, reason)` (Phase 23, план 23-04, D-06) — приветствие/отказ делегату
  по заявке отбора + лист, тот же хвост, что и прямой вызов из `handlers/admin_moderation.py`.
  Событие ставится не сразу: `miniapp/outbox.py::flush_application_decisions` переносит его
  из журнала `application_decisions` только после истечения окна отмены.
- `application_mass_approved` -> `services.application_effects.mass_approve_effects(bot,
  ids)` (D-07) — welcome-рассылка + один batch-sync листа для «Принять всех N»; у массового
  одобрения нет отмены, это событие ставится сразу в `miniapp/routers/applications.py`.

At-least-once, с ретраями (T-19-56): исключение -> `mark_miniapp_outbox_failed` (`attempts+1`,
текст ошибки), после `MAX_ATTEMPTS` попыток строка выводится из очереди (помечена обработанной)
с логом уровня error, чтобы одна битая строка не стопорила проход навсегда. Оба обработчика
идемпотентны: повторное уведомление менеджерам допустимо (то же самое уже происходит при
дайджест-режиме бота), повторная пересборка таблицы безвредна.

Монет в очереди нет и быть не может (начисление делает `miniapp` напрямую через `add_coins`,
план 19-05) — `drain` не вызывает `add_coins` ни при каком `kind`.

Логи содержат только идентификаторы строки/сдачи, никогда payload целиком (T-19-57 — payload
может нести ПД, например имя делегата).
"""
import logging
from datetime import datetime

from database.db import (
    list_unprocessed_miniapp_outbox,
    mark_miniapp_outbox_failed,
    mark_miniapp_outbox_processed,
    get_user,
)
from services.application_effects import apply_decision_effects, mass_approve_effects
from services.game_digest import notify_submission
from services.game_sync import request_resync
from services.reg_finalize import post_finalize, derive_edit_facts, handle_resume_upload

logger = logging.getLogger(__name__)

DRAIN_LIMIT = 50
MAX_ATTEMPTS = 5

# Закрытый набор kind -> "просто попросить ребилд" (T-19-55). submission_created обрабатывается
# отдельной веткой ниже (у неё другой обработчик, не request_resync).
_RESYNC_KINDS = frozenset({"submission_reviewed", "task_changed", "coins_manual"})


async def _handle_row(bot, kind: str, payload: dict) -> None:
    """Один вызов на строку. Бросает исключение на неизвестном `kind` или на сбое
    обработчика — `drain` решает, что делать с попыткой."""
    if kind == "submission_created":
        await notify_submission(
            bot,
            submission_id=payload.get("submission_id"),
            user_id=payload.get("user_id"),
            task_id=payload.get("task_id"),
            task_text=payload.get("task_text"),
            submitter_name=payload.get("submitter_name"),
        )
        return
    if kind in _RESYNC_KINDS:
        request_resync()
        return
    if kind in ("reg_finalized", "reg_edited"):
        telegram_id = payload.get("telegram_id")
        mode = "new" if kind == "reg_finalized" else "edit"
        changed_columns, remoderated, resubmitted = None, False, False
        if mode == "edit":
            full = await get_user(telegram_id) or {}
            changed_columns, remoderated, resubmitted = await derive_edit_facts(telegram_id, full)
        await post_finalize(
            bot, telegram_id, mode,
            changed_columns=changed_columns, remoderated=remoderated, resubmitted=resubmitted,
        )
        return
    if kind == "reg_resume_upload":
        await handle_resume_upload(
            bot, payload.get("telegram_id"), payload.get("file_id"), payload.get("filename")
        )
        return
    if kind == "application_decided":
        await apply_decision_effects(
            bot, payload.get("telegram_id"), payload.get("status"), payload.get("reason")
        )
        return
    if kind == "application_mass_approved":
        await mass_approve_effects(bot, payload.get("ids") or [])
        return
    raise ValueError(f"unknown miniapp_outbox kind: {kind!r}")


async def drain(bot) -> int:
    """Разбирает до `DRAIN_LIMIT` необработанных строк `miniapp_outbox`. Возвращает число
    строк, покинувших очередь (успешно обработанные + сдавшиеся после `MAX_ATTEMPTS`
    попыток). Пустая очередь -> 0, без единого вызова обработчиков."""
    rows = await list_unprocessed_miniapp_outbox(limit=DRAIN_LIMIT)
    done_ids: list[int] = []

    for row in rows:
        row_id = row["id"]
        kind = row.get("kind")
        try:
            await _handle_row(bot, kind, row.get("payload") or {})
        except Exception as e:
            # T-19-57: только id и kind в логе, никогда payload (может нести ПД).
            logger.error(f"miniapp_outbox: row {row_id} (kind={kind}) failed: {e}")
            await mark_miniapp_outbox_failed(row_id, str(e))
            attempts_now = (row.get("attempts") or 0) + 1
            if attempts_now >= MAX_ATTEMPTS:
                logger.error(
                    f"miniapp_outbox: row {row_id} (kind={kind}) exceeded {MAX_ATTEMPTS} "
                    "attempts — giving up, removing it from the queue"
                )
                done_ids.append(row_id)
            continue
        done_ids.append(row_id)

    if done_ids:
        await mark_miniapp_outbox_processed(done_ids, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return len(done_ids)
