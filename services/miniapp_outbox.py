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
)
from services.game_digest import notify_submission
from services.game_sync import request_resync

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
