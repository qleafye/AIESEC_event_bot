"""Единая точка приёма нормализованной анкеты внешней формы (Яндекс и Google)."""
from __future__ import annotations

import logging

from database import ext_forms_db as ef
from services.ext_forms_match import match_answer
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)


async def ingest_answer(
    form: dict, *, answer_id: str, answered_at: str | None, items: list[dict], raw: str | None,
) -> bool:
    """True = анкета новая, False = такой answer_id уже есть. Колонки регистрируются до вставки,
    чтобы зеркало в таблице видело позицию каждого вопроса."""
    await ef.upsert_columns(form["id"], [(i["q"], i["label"]) for i in items])
    tid, how = await match_answer(form, items)
    inserted = await ef.insert_answer(
        form_id=form["id"], answer_id=str(answer_id), answered_at=answered_at,
        received_at=msk_now().strftime("%Y-%m-%d %H:%M:%S"), payload=items, raw=raw,
        matched_telegram_id=tid, match_how=how,
    )
    if inserted:
        # Делегации вузов: отдельный try — сбой делегаций не должен ронять приём ответа
        # (ретрай очереди второй раз сюда не придёт: insert_answer вернёт False).
        try:
            from services.delegations import on_answer_available
            await on_answer_available(form["id"], str(answer_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("delegations: хук ответа %s формы %s: %s",
                           answer_id, form["id"], type(e).__name__)
    return inserted
