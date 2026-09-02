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

Монеты через outbox НЕ ходят никогда: их начисляет сам `miniapp` в связке
`claim_submission -> add_coins` (план 19-05); `coins_manual` — только уведомление делегату.

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


__all__ = ["OUTBOX_KINDS", "enqueue"]
