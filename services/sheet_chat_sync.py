"""Разбор очереди записи «В чате» в Google-лист (29.09).

Вход и выход из чата (`database.db.upsert_chat_member`), одобрение делегата
(`handlers.reg_schema.approve_user`) и сверка состава (`services.chat_tracking.refresh_chat`)
пишут только в базу и кладут событие в `sheet_chat_queue`. Искать строку делегата по всем
вкладкам на каждое событие чата нельзя — это сотни чтений подряд и упор в квоту Sheets (429).

Здесь, в процессе бота, раз в 60 с (`services/scheduler.py`, джоба `sheet_chat_drain`): пачка
созревших событий -> дубли схлопываются по делегату -> значение ячейки из базы одним проходом
(`chat_tracking.chat_cell_values`: «да» / «нет» / «не проверено» / «-») -> одно чтение и один
batch_update на вкладку (`services.sheets.write_column_batch`). Значение всегда из базы,
поэтому повтор безвреден.

Исходы по делегату — как у очереди «Пришёл» (`services/sheet_arrival_sync.py`):
- записано -> события удаляются;
- строки нет (или на листе ещё нет колонки «В чате» — не пересобран) -> события удаляются,
  в лог одна сводная строка: строка сама не появится, следующая пересборка/событие догонит;
- сбой листа/сети -> события остаются, attempts + 1, backoff 30 с … 30 мин, ошибка без секретов.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from config import config
from database.db import drop_sheet_chat, fail_sheet_chat, list_due_sheet_chat
from core.secret_redact import redact_secrets
from services.sheet_arrival_sync import backoff_seconds
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

BATCH_LIMIT = 2000
_FMT = "%Y-%m-%d %H:%M:%S"


async def drain() -> dict:
    """Один проход по очереди. Возвращает счётчики {"written", "missing", "failed"} по
    делегатам (для тестов и лога)."""
    counts = {"written": 0, "missing": 0, "failed": 0}
    now = msk_now()
    rows = await list_due_sheet_chat(now.strftime(_FMT), BATCH_LIMIT)
    if not rows:
        return counts

    upto: dict[int, int] = {}
    attempts: dict[int, int] = defaultdict(int)
    for row in rows:
        tid = row["telegram_id"]
        upto[tid] = max(upto.get(tid, 0), row["id"])
        attempts[tid] = max(attempts[tid], row["attempts"])

    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        await drop_sheet_chat(upto)  # таблица не подключена — писать некуда
        return counts

    from services.chat_tracking import chat_cell_values
    from services.sheets import CHAT_HEADER, write_column_batch

    try:
        values = await chat_cell_values(list(upto))
        result = await write_column_batch(CHAT_HEADER, values)
    except Exception as e:
        result = {"written": set(), "missing": set(), "failed": {tid: str(e) for tid in upto}}

    done = {tid: upto[tid] for tid in result["written"] | result["missing"] if tid in upto}
    await drop_sheet_chat(done)
    if result["missing"]:
        logger.warning(
            "sheet_chat: %s делегатов нет в листе или на вкладке нет колонки «В чате» — "
            "события сняты (пример: %s)", len(result["missing"]), sorted(result["missing"])[:5],
        )

    groups: dict[tuple[int, str], dict[int, int]] = defaultdict(dict)
    for tid, err in result["failed"].items():
        if tid in upto:
            groups[(backoff_seconds(attempts[tid] + 1), redact_secrets(err))][tid] = upto[tid]
    for (delay, err), part in groups.items():
        await fail_sheet_chat(part, err, (now + timedelta(seconds=delay)).strftime(_FMT))
    if result["failed"]:
        logger.warning(
            "sheet_chat: запись «В чате» не прошла для %s делегатов, повтор позже: %s",
            len(result["failed"]), next(iter(groups), (0, ""))[1],
        )

    counts.update(
        written=len(result["written"]), missing=len(result["missing"]), failed=len(result["failed"]),
    )
    return counts
