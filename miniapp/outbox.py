"""Phase 19 (D-01): постановка побочных эффектов Mini App в `miniapp_outbox`.

Веб-процесс пишет в БД только то, что делегат/менеджер сделал; всё, что требует бота
(уведомление менеджерам о сдаче, уведомление делегату о проверке, дайджест, Sheets-ребилд),
ставится сюда и подбирается джобой бота at-least-once (план 19-08).

Контракт видов событий (payload — JSON-словарь; сторожевой тест сверяет этот список):

    submission_created   {submission_id, user_id, task_id, task_text, submitter_name}
    submission_reviewed  {submission_id, user_id, status, coins}
    task_changed         {task_id}
    coins_manual         {user_id, delta}
    reg_finalized        {telegram_id}
    reg_edited           {telegram_id}
    reg_resume_upload    {telegram_id, file_id, filename}
    application_decided        {telegram_id, status, reason}
    application_mass_approved  {ids}

Монеты через outbox НЕ ходят никогда: их начисляет сам `miniapp` в связке
`claim_submission -> add_coins` (план 19-05); `coins_manual` — только уведомление делегату.

Phase 23 (23-04, APP-TINDER-02, D-06/D-07): `application_decided`/`application_mass_approved`
— хвост решения по заявке отбора (`services/applications.py`, план 23-02). Веб-процесс НЕ
шлёт приветствие/отказ делегату сам: `approve_user` (welcome + меню + реквизиты) — aiogram-
путь бота, должен остаться единственным местом, где оно отправляется РОВНО один раз (D-10).
`application_decided` ставится не сразу — `flush_application_decisions` ниже переносит его
из журнала `application_decisions` в эту очередь, только когда истекло окно отмены (D-06,
менеджер уже не может нажать «Отменить»). `application_mass_approved` — исключение: у
массового одобрения нет отмены (D-07), эффект ставится сразу вызывающим кодом
(`miniapp/routers/applications.py`), не через flush.

Phase 21 (21-08, FORM-SYNC-02/04/07, D-05/D-06): `reg_finalized`/`reg_edited` — Mini App
поставила `finalize_data` (данные уже записаны узким UPDATE/add_user); бот разбирает их
`services.reg_finalize.post_finalize` — тем же путём, что и прямой вызов из чата (Sheets/
уведомления менеджерам). Payload сознательно несёт только `telegram_id` — ответы анкеты (ПД)
в очередь не попадают (T-21-08), бот перечитывает текущее состояние из `users` сам.
`reg_resume_upload` — резюме, загруженное в Mini App (D-05): бот кладёт файл в Nextcloud и
шлёт копию в чат, `miniapp` сама с Telegram Bot API/Nextcloud не говорит (D-01).

Fail-soft: таблицу создаёт `database.db.init_db` (схемой владеет ТОЛЬКО бот, здесь
миграций нет и быть не может). Если бот ещё старой версии и таблицы нет — `enqueue`
логирует предупреждение и возвращает None: сама сдача уже сохранена, уведомление догонит
после обновления бота.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import aiosqlite

from database.db import enqueue_miniapp_outbox

logger = logging.getLogger(__name__)

OUTBOX_KINDS = frozenset({
    "submission_created",
    "submission_reviewed",
    "task_changed",
    "coins_manual",
    "reg_finalized",
    "reg_edited",
    "reg_resume_upload",
    "application_decided",
    "application_mass_approved",
})


async def enqueue(kind: str, payload: dict) -> int | None:
    """`id` новой строки или None (таблицы нет / БД недоступна — залогировано)."""
    if kind not in OUTBOX_KINDS:
        raise ValueError(f"unknown miniapp outbox kind: {kind!r}")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        return await enqueue_miniapp_outbox(kind, dict(payload), created_at)
    except aiosqlite.Error as exc:
        logger.warning("miniapp outbox: событие %s не поставлено (%s)", kind, exc)
        return None


async def flush_application_decisions(now: datetime) -> int:
    """D-06: подметает просроченные решения по заявкам (`services.applications.
    flush_due_decisions`, план 23-02) и ставит их эффект в очередь как `application_decided`.

    `flush_due_decisions` зовёт свой колбэк СИНХРОННО и без await (его тестовый двойник в
    `tests/test_applications_service.py` — обычная функция, не корутина) — переданная сюда
    `enqueue` асинхронна, поэтому колбэк только СОБИРАЕТ созданные ею корутины, а не
    исполняет их сам; `asyncio.gather` ниже реально их исполняет, ПОСЛЕ того как обход
    завершился. Ни одна корутина не остаётся неисполненной висящим объектом.

    `kind`, который получает колбэк, — это `application_decisions.decision` ('approved' |
    'rejected'), А НЕ вид outbox: этот адаптер и есть перевод одного в другое —
    `services/applications.py` намеренно ничего не знает про имя `application_decided`
    (докстринг `flush_due_decisions`: модуль остаётся свободен от `miniapp.outbox`).

    Бот (`services/scheduler.py::miniapp_outbox_drain_job`) НЕ импортирует этот модуль —
    зависимость `miniapp -> services/database` однонаправленная — и держит маленький
    дубль этого же адаптера на голых `database.db.enqueue_miniapp_outbox` (see его докстринг)."""
    from services.applications import flush_due_decisions  # локально: applications.py не
    # знает про miniapp вовсе, здесь достаточно узнать про него ОДНОЙ функции при вызове.

    pending: list = []

    def _collect(decision: str, row: dict) -> None:
        pending.append(enqueue("application_decided", {
            "telegram_id": row["telegram_id"],
            "status": row.get("decision", decision),
            "reason": row.get("reason"),
        }))

    count = await flush_due_decisions(now, _collect)
    if pending:
        await asyncio.gather(*pending)
    return count


__all__ = ["OUTBOX_KINDS", "enqueue", "flush_application_decisions"]
