"""Phase 23 (23-02, APP-TINDER-01) — хвост решения по заявке, которому физически нужен `bot`.

Перенесено из `handlers/admin_moderation.py` (Phase 23, план 23-02): тело `appr_approve`'s
эффект-хвоста + `appr_reject_reason`'s эффект-хвост -> `apply_decision_effects`; `_welcome_flipped`
+ хвост `appr_all_yes` -> `mass_approve_effects`.

Зачем модуль отдельный от `services/applications.py`: ядро отбора обязано остаться aiogram-free
(веб-процесс `miniapp/` не имеет права импортировать aiogram, `miniapp/deps.py`: «Модуль
aiogram-free»), а «отправить приветствие» и «отправить сообщение делегату» физически требуют
объекта бота — тот же разрез, что `services/reg_finalize.py::finalize_data`/`post_finalize`.

Зовёт эти две функции и чат (боту, напрямую после решения — `_spawn(apply_decision_effects(...))`
в `handlers/admin_moderation.py`), и (со следующего плана) джоба очереди событий веба, когда
истечёт окно отмены (D-06) — один и тот же журнал вызовов и текстов для обеих поверхностей.

Импорт `handlers.reg_schema` — ЛОКАЛЬНЫЙ внутри функции (тот же приём, что
`services/reg_finalize.py::post_finalize`): `handlers/admin_moderation.py` импортирует ИЗ этого
модуля на своём верхнем уровне, обратный модульный импорт дал бы цикл при загрузке пакета
`handlers`.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramRetryAfter

from reg_labels import STATUS_LABELS
from services.applications import reject_message_text
from services.sheets import bulk_update_status_in_sheet, update_status_in_sheet

logger = logging.getLogger(__name__)


async def apply_decision_effects(bot, telegram_id: int, status: str, reason: str | None = None) -> None:
    """Хвост одного решения по заявке. `approved`: приветствие (`approve_user`, ровно один раз
    — D-10) затем лист. `rejected`: сообщение делегату (`reject_message_text`, `parse_mode=HTML`)
    затем лист; сбой отправки — только в лог, решение НЕ откатывается (паритет с ботом)."""
    if status == "approved":
        from handlers.reg_schema import approve_user  # локальный импорт против цикла
        await approve_user(bot, telegram_id)  # welcome exactly once (D-10)
        await update_status_in_sheet(telegram_id, STATUS_LABELS["approved"])
    elif status == "rejected":
        text = await reject_message_text(reason)
        try:
            await bot.send_message(telegram_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify rejected user {telegram_id}: {e}")
        await update_status_in_sheet(telegram_id, STATUS_LABELS["rejected"])


async def mass_approve_effects(bot, ids: list) -> None:
    """Перенесённый `_welcome_flipped` (обработка `TelegramRetryAfter`, пауза 0.05 между
    отправками) + один `bulk_update_status_in_sheet`. Пустой список — выход без единого вызова."""
    if not ids:
        return
    for tid in ids:
        try:
            from handlers.reg_schema import approve_user  # локальный импорт против цикла
            await approve_user(bot, tid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                from handlers.reg_schema import approve_user  # локальный импорт против цикла
                await approve_user(bot, tid)
            except Exception as e2:
                logger.error(f"Mass-approve welcome retry failed for {tid}: {e2}")
        except Exception as e:
            logger.error(f"Mass-approve welcome failed for {tid}: {e}")
        await asyncio.sleep(0.05)
    await bulk_update_status_in_sheet({str(t): STATUS_LABELS["approved"] for t in ids})
