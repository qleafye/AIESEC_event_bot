"""Phase 27 (27-03, LANG-04/LANG-05) — воркер очереди перевода делегатской анкеты.

**Инвариант (T-27-02-05, зафиксирован здесь буквально, т.к. на нём держится схема
конкурентной записи):** `translation_queue` пишут ОБА процесса (бот через
`database.db.set_setting`, `miniapp` через веб-настройки — 27-CONTEXT.md A-04); а
`translations` (готовые переводы) — **ТОЛЬКО бот**, через этот модуль. В `miniapp` данный
воркер не импортируется и интервал-джоба для него не заводится (`services/scheduler.py`
живёт только в процессе бота).

Форма — копия отлаженного паттерна `services/miniapp_outbox.py::drain` (список необработанных
-> обработка построчно -> список «покинувших очередь» -> `attempts`/`last_error` для
неудачников). Разбор идёт пачками по `BATCH_SIZE` строк ЗА ОДИН СИНХРОННЫЙ вызов драйвера
внутри `asyncio.to_thread` — ct2-инференс CPU-bound C++, прямой вызов в event loop подвесил
бы long polling на десятки секунд (27-RESEARCH.md Pitfall 7).

Конвейер на строку (глоссарий — `services/i18n_glossary.py`, обязательные находки замера
27-01 + UAT 260906): `split_leading_symbols`/`split_trailing_symbols` (эмодзи-префикс и
-суффикс отдельно — движок не переживает голый эмодзи ни в начале, ни в конце строки) ->
`strip_gender_suffix` (русская скобочная гендерная приписка «(а)»/«(ла)» — у английского нет
рода, латиницей в переводе не нужна) -> `protect` (DNT-термины/HTML-тэги/плейсхолдеры
сентинелами) -> `driver.translate_batch` (в потоке) -> `apply` (восстановление + POST-замены,
`""` при потерянном/задвоенном сентинеле) -> приклеить эмодзи-префикс И -суффикс обратно.
Пустой результат (после `apply()` либо потому что движок реально вернул пустую/совпадающую с
исходником строку) — НЕ ошибка: пишется в `translations` как есть (короткие термины часто
совпадают дословно; битая разметка — легитимное состояние "failed" контракта
`database/db.py::translations.text`), строка покидает очередь.

Владелец (чекпоинт 27-01): «embedded с выгрузкой» — модель грузится только пока в очереди
есть что переводить и выгружается (`driver.unload()`) сразу, как только `drain()` её
опустошил. `get_driver()` (`services/i18n_engine.py`) не вызывается ВООБЩЕ, если после
вычитки очереди не осталось ни одной НЕ-ручной строки — пустая очередь не грузит модель и не
ходит по HTTP ни разу (проверено тестом `test_drain_empty_queue_never_calls_driver`)."""
from __future__ import annotations

import asyncio
import logging

from database.db import (
    bump_translation_attempt,
    drop_translation_queue,
    enqueue_translation,
    get_translation,
    list_pending_translations,
    list_translations,
    upsert_translation,
)
from services.i18n import src_hash
from services.i18n_engine import get_driver
from services.i18n_glossary import (
    apply,
    protect,
    split_leading_symbols,
    split_trailing_symbols,
    strip_gender_suffix,
)
from services.i18n_sources import corpus

logger = logging.getLogger(__name__)

BATCH_SIZE = 32
MAX_ATTEMPTS = 5


async def drain(limit_batches: int = 1) -> int:
    """Разбирает до `limit_batches` пачек по `BATCH_SIZE` строк из очереди перевода на
    английский. Возвращает число строк, покинувших очередь (переведённые + отброшенные как
    `manual` + сдавшиеся). Пустая очередь на первом же чтении -> `0`, БЕЗ единого вызова
    `get_driver()`/драйвера — выключенный модуль/простой не стоят ни модели, ни HTTP-хопа."""
    total_done = 0
    driver = None

    for _ in range(max(1, limit_batches)):
        pending = await list_pending_translations("en", limit=BATCH_SIZE, max_attempts=MAX_ATTEMPTS)
        if not pending:
            break

        # LANG-05: строка, которую менеджер уже правил вручную, убирается из очереди и
        # машинному переводу не подвергается вовсе — даже если попала туда повторным
        # массовым пресетом ПОСЛЕ ручной правки.
        rows, manual_ids = [], []
        for row in pending:
            existing = await get_translation("en", row["src_hash"])
            if existing and existing.get("manual"):
                manual_ids.append(row["id"])
            else:
                rows.append(row)
        if manual_ids:
            await drop_translation_queue(manual_ids)
            total_done += len(manual_ids)
        if not rows:
            continue

        if driver is None:
            driver = await get_driver()

        prepared = []
        for row in rows:
            prefix, rest = split_leading_symbols(row["src_text"])
            body, suffix = split_trailing_symbols(rest)
            body = strip_gender_suffix(body)
            protected, mapping = protect(body)
            prepared.append({
                "row": row, "prefix": prefix, "suffix": suffix, "body": body,
                "mapping": mapping, "protected": protected,
            })

        try:
            translated = await asyncio.to_thread(
                driver.translate_batch, [p["protected"] for p in prepared],
            )
        except Exception as exc:  # noqa: BLE001 — сбой драйвера: строки остаются в очереди
            logger.error("i18n_worker.drain: сбой драйвера перевода (%s)", exc)
            for p in prepared:
                await bump_translation_attempt(p["row"]["id"], str(exc))
            continue

        if len(translated) != len(prepared):
            logger.error(
                "i18n_worker.drain: длина ответа драйвера (%d) не совпала с запросом (%d)",
                len(translated), len(prepared),
            )
            for p in prepared:
                await bump_translation_attempt(p["row"]["id"], "driver length mismatch")
            continue

        done_ids = []
        for p, text_en in zip(prepared, translated):
            final_body = apply(p["body"], text_en or "", p["mapping"])
            # Пустой перевод (движок отдал "" сам ИЛИ apply() отбросил битую разметку) — не
            # ошибка: пишем "" как есть, без эмодзи-префикса/суффикса (нечего к ним приклеивать).
            final_text = (p["prefix"] + final_body + p["suffix"]) if final_body else final_body
            row = p["row"]
            await upsert_translation(
                "en", row["src_hash"], row["src_text"], final_text,
                manual=0, origin_key=row.get("origin_key"),
            )
            done_ids.append(row["id"])
        await drop_translation_queue(done_ids)
        total_done += len(done_ids)

        # Уступка event loop между батчами (long polling не должен голодать, если
        # limit_batches > 1 разбирает несколько пачек за один вызов).
        await asyncio.sleep(0)

    if driver is not None:
        # Owner-decision 27-01: модель держим в памяти РОВНО пока есть что переводить.
        still_pending = await list_pending_translations("en", limit=1, max_attempts=MAX_ATTEMPTS)
        if not still_pending:
            driver.unload()

    return total_done


async def bulk_seed(lang: str = "en") -> int:
    """Ставит в очередь ВЕСЬ корпус делегатских текстов анкеты (`services/i18n_sources.py
    ::corpus()` — единственный перечислитель, второго списка источников в проекте нет) при
    включении модуля (`delegate_lang_enabled` -> "on", врезка в `database.db.set_setting`).
    Строки, для которых уже есть ручная правка менеджера (`manual=1`), в очередь не ставятся —
    LANG-05 действует и здесь, не только в `drain()`. `INSERT OR IGNORE` в `enqueue_translation`
    (`UNIQUE(lang, src_hash)`) сам не даёт повторному вызову наплодить дублей. Возвращает
    число реально поставленных строк (не считая пропущенных manual/уже стоящих в очереди)."""
    items = await corpus()
    queued = 0
    for origin_key, text in items:
        text_hash = src_hash(text)
        existing = await get_translation(lang, text_hash)
        if existing and existing.get("manual"):
            continue
        row_id = await enqueue_translation(lang, text_hash, text, origin_key=origin_key)
        if row_id is not None:
            queued += 1
    return queued


async def progress(lang: str = "en") -> dict:
    """Сводка состояния корпуса для сообщения менеджеру и экрана правки (план 27-06):
    `{"total", "done", "manual", "failed", "pending"}`. `done` — машинный перевод получен и
    не является ручной правкой (`total` минус остальные три категории, взаимоисключающие по
    контракту `translations.text`/`manual` в `database/db.py`)."""
    _, total = await list_translations(lang, limit=1, state=None)
    _, manual = await list_translations(lang, limit=1, state="manual")
    _, pending = await list_translations(lang, limit=1, state="pending")
    _, failed = await list_translations(lang, limit=1, state="failed")
    done = total - manual - pending - failed
    return {"total": total, "done": done, "manual": manual, "failed": failed, "pending": pending}
