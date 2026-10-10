"""Включить делегатский английский в тестовой БД — без фонового `bulk_seed()`.

`database.db.set_setting("delegate_lang_enabled", "on")` через `_maybe_enqueue_translation`
запускает `services.infra.background.spawn(bulk_seed())` — сотни записей в `translation_queue`,
каждая своим соединением и коммитом. Тестам «делегат на английском» очередь перевода не нужна,
а фоновая задача им вредит двумя способами:

- в том же `asyncio.run`, что и `i18n_form_manual.seed()` (ещё ~700 коммитов), два писателя
  десятки секунд делят блокировку записи SQLite; на загруженной машине одно ожидание
  перерастает `DB_BUSY_TIMEOUT_MS` (5 с) → `sqlite3.OperationalError: database is locked`;
- в отдельном `asyncio.run` задача успевает начать открывать соединение и отменяется на
  закрытии лупа — поток aiosqlite потом пишет в закрытый луп («RuntimeError: Event loop is
  closed» в `_connection_worker_thread`).

Настройка пишется тем же `set_setting` (аудит, снимок запроса — как в бою); подменяется только
фоновая корутина, и только на время вызова.
"""
from __future__ import annotations

import asyncio


async def enable_delegate_lang() -> None:
    from database import db
    from services.i18n import i18n_worker

    async def _skip_bulk_seed(lang: str = "en") -> int:
        return 0

    original = i18n_worker.bulk_seed
    i18n_worker.bulk_seed = _skip_bulk_seed
    try:
        await db.set_setting("delegate_lang_enabled", "on")
    finally:
        i18n_worker.bulk_seed = original
    await asyncio.sleep(0)  # даём пустой задаче завершиться в этом же лупе
