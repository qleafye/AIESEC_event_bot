"""Разбор очереди записи «Пришёл» в Google-лист (нагрузочный прогон 25.09).

Отметка входа, её снятие и загрузка CSV пишут только в базу (`checkins`) и кладут событие в
`sheet_arrival_queue` (`database.db.enqueue_sheet_arrival`) — так же поступает Mini App, у
которого Google-кредов нет вовсе. Раньше ячейка писалась прямо в скане: Mini App её не писал
никогда, а бот на каждом первом скане до ответа волонтёру читал весь столбец id листа — при
двух сканах в секунду упор в квоту Sheets, CSV на 500 строк = 500 чтений подряд.

Здесь, в процессе бота, раз в 30 с (`services/scheduler.py`, джоба `sheet_arrival_drain`):
пачка созревших событий -> дубли схлопываются по делегату -> значение ячейки из базы (время
ПЕРВОГО входа за форум, для людей: «25.09 05:23»; входов не осталось — пусто) -> одно чтение
и один batch_update на вкладку (`services.sheets.write_arrivals_batch`). Значение всегда из базы, поэтому повтор
безвреден.

Исходы по делегату:
- записано -> события удаляются;
- строки нет ни на вкладке города, ни на главном листе -> события удаляются с предупреждением в
  лог: крутить их бессмысленно, строка сама не появится (следующая отметка поставит событие
  заново);
- сбой листа/сети -> события остаются, attempts + 1, следующий заход через 30 с, 1, 2, 4 …
  мин (потолок 30 мин), текст ошибки без секретов. Потолка попыток нет — событие не теряется.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from config import config
from database.db import (
    drop_sheet_arrivals,
    fail_sheet_arrivals,
    first_entry_scanned_at,
    list_due_sheet_arrivals,
)
from secret_redact import redact_secrets
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

BATCH_LIMIT = 2000
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 30 * 60
_FMT = "%Y-%m-%d %H:%M:%S"


def arrival_cell_value(scanned_at: str | None) -> str:
    """Значение ячейки «Пришёл» для людей: «2026-09-25 05:23:33» -> «25.09 05:23». Входа нет ->
    пусто; нераспознанная строка уходит как есть (лучше сырое время, чем пустая ячейка)."""
    if not scanned_at:
        return ""
    try:
        return datetime.strptime(scanned_at.strip(), _FMT).strftime("%d.%m %H:%M")
    except ValueError:
        return scanned_at


def backoff_seconds(attempts: int) -> int:
    """Пауза после `attempts`-го сбоя подряд (1 -> 30 с, 2 -> 60 с, 3 -> 2 мин …, <= 30 мин)."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)


async def drain() -> dict:
    """Один проход по очереди. Возвращает счётчики {"written", "missing", "failed"} по
    делегатам (для тестов и лога)."""
    counts = {"written": 0, "missing": 0, "failed": 0}
    now = msk_now()
    rows = await list_due_sheet_arrivals(now.strftime(_FMT), BATCH_LIMIT)
    if not rows:
        return counts

    upto: dict[int, int] = {}
    attempts: dict[int, int] = defaultdict(int)
    for row in rows:
        tid = row["telegram_id"]
        upto[tid] = max(upto.get(tid, 0), row["id"])
        attempts[tid] = max(attempts[tid], row["attempts"])

    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        await drop_sheet_arrivals(upto)  # таблица не подключена — писать некуда
        return counts

    values = {tid: arrival_cell_value(await first_entry_scanned_at(tid)) for tid in upto}

    from services.sheets import write_arrivals_batch  # процесс бота; Mini App сюда не ходит

    try:
        result = await write_arrivals_batch(values)
    except Exception as e:
        result = {"written": set(), "missing": set(), "failed": {tid: str(e) for tid in upto}}

    done = {tid: upto[tid] for tid in result["written"] | result["missing"] if tid in upto}
    await drop_sheet_arrivals(done)
    for tid in result["missing"]:
        logger.warning(
            "sheet_arrival: telegram_id=%s нет в листе (ни вкладка города, ни главный лист) — "
            "событие снято", tid,
        )

    groups: dict[tuple[int, str], dict[int, int]] = defaultdict(dict)
    for tid, err in result["failed"].items():
        if tid in upto:
            groups[(backoff_seconds(attempts[tid] + 1), redact_secrets(err))][tid] = upto[tid]
    for (delay, err), part in groups.items():
        await fail_sheet_arrivals(part, err, (now + timedelta(seconds=delay)).strftime(_FMT))
    if result["failed"]:
        logger.warning(
            "sheet_arrival: запись «Пришёл» не прошла для %s делегатов, повтор позже: %s",
            len(result["failed"]), next(iter(groups), (0, ""))[1],
        )

    counts.update(
        written=len(result["written"]), missing=len(result["missing"]), failed=len(result["failed"]),
    )
    return counts
