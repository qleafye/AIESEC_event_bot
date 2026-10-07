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
- строки нет ни на вкладке города, ни на главном листе -> событие остаётся и пробуется реже:
  через 30 мин, 1, 2, 4 ч (потолок 6 ч). Строка появляется позже — анкету или одобрение на
  месте дописали после обрыва прокси, менеджер пересобрал лист, — и «Пришёл» встаёт без
  повторной отметки. Через MISSING_GIVE_UP_DAYS дней ожидания событие снимается с
  предупреждением в лог (ячейку тогда восстанавливает ручная пересборка листа);
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
    get_user,
    list_due_sheet_arrivals,
)
from secret_redact import redact_secrets
from services.timeutil import city_offset_hours, msk_now

logger = logging.getLogger(__name__)

BATCH_LIMIT = 2000
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 30 * 60
MISSING_BACKOFF_BASE_SECONDS = 30 * 60
MISSING_BACKOFF_MAX_SECONDS = 6 * 60 * 60
MISSING_GIVE_UP_DAYS = 7
MISSING_ERROR = "строки делегата нет в листе — жду, пока она появится"
_FMT = "%Y-%m-%d %H:%M:%S"


def arrival_cell_value(scanned_at: str | None, offset_hours: int = 0) -> str:
    """Значение ячейки «Пришёл» для людей: «2026-09-25 05:23:33» -> «25.09 05:23». Входа нет ->
    пусто; нераспознанная строка уходит как есть (лучше сырое время, чем пустая ячейка).
    `offset_hours` — смещение города делегата от МСК: в базе метка московская, в листе — по
    часам города («🕐 Часовой пояс»); 0 — как раньше."""
    if not scanned_at:
        return ""
    try:
        stamp = datetime.strptime(scanned_at.strip(), _FMT)
    except ValueError:
        return scanned_at
    if offset_hours:
        stamp += timedelta(hours=offset_hours)
    return stamp.strftime("%d.%m %H:%M")


async def city_offsets_by_user(telegram_ids) -> dict[int, int]:
    """{telegram_id: смещение города делегата}. Ни у одного города нет своего пояса (общий
    случай, дефолт МСК) -> пустой словарь БЕЗ чтения пользователей: лишних запросов к базе у
    массовой пересборки листа нет."""
    from cities import city_codes
    city_offsets = {code: await city_offset_hours(code) for code in city_codes()}
    if not any(city_offsets.values()):
        return {}
    out: dict[int, int] = {}
    for tid in telegram_ids:
        user = await get_user(tid)
        city = (user or {}).get("event_city")
        out[tid] = city_offsets.get(city) if city in city_offsets else await city_offset_hours(city)
    return out


def backoff_seconds(attempts: int) -> int:
    """Пауза после `attempts`-го сбоя подряд (1 -> 30 с, 2 -> 60 с, 3 -> 2 мин …, <= 30 мин)."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)


def missing_backoff_seconds(attempts: int) -> int:
    """Пауза, пока строки делегата нет в листе (1 -> 30 мин, 2 -> 1 ч, 3 -> 2 ч …, <= 6 ч)."""
    return min(MISSING_BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), MISSING_BACKOFF_MAX_SECONDS)


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
    oldest: dict[int, str] = {}
    for row in rows:
        tid = row["telegram_id"]
        upto[tid] = max(upto.get(tid, 0), row["id"])
        attempts[tid] = max(attempts[tid], row["attempts"])
        oldest[tid] = min(oldest.get(tid, row["created_at"]), row["created_at"])

    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        await drop_sheet_arrivals(upto)  # таблица не подключена — писать некуда
        return counts

    offsets = await city_offsets_by_user(upto)
    values = {
        tid: arrival_cell_value(await first_entry_scanned_at(tid), offsets.get(tid, 0)) for tid in upto
    }

    from services.sheets import write_arrivals_batch  # процесс бота; Mini App сюда не ходит

    try:
        result = await write_arrivals_batch(values)
    except Exception as e:
        result = {"written": set(), "missing": set(), "failed": {tid: str(e) for tid in upto}}

    give_up_before = (now - timedelta(days=MISSING_GIVE_UP_DAYS)).strftime(_FMT)
    missing = {tid for tid in result["missing"] if tid in upto}
    expired = {tid for tid in missing if oldest[tid] < give_up_before}
    done = {tid: upto[tid] for tid in (result["written"] & set(upto)) | expired}
    await drop_sheet_arrivals(done)
    for tid in expired:
        logger.warning(
            "sheet_arrival: telegram_id=%s так и не появился в листе за %s дн. — событие снято, "
            "«Пришёл» восстановит пересборка листа", tid, MISSING_GIVE_UP_DAYS,
        )
    waiting: dict[int, dict[int, int]] = defaultdict(dict)
    for tid in missing - expired:
        waiting[missing_backoff_seconds(attempts[tid] + 1)][tid] = upto[tid]
    for delay, part in waiting.items():
        await fail_sheet_arrivals(part, MISSING_ERROR, (now + timedelta(seconds=delay)).strftime(_FMT))
    if missing - expired:
        logger.warning(
            "sheet_arrival: %s делегатов нет в листе (ни вкладка города, ни главный лист) — "
            "«Пришёл» запишу, когда строка появится", len(missing - expired),
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
