"""Чек-лист менеджера: файл или текст после одобрения.

Пустые настройки ничего не шлют и флаг не ставят. Сбой отправки флаг не ставит.
Повтор при уже ушедшем чек-листе молчит. Список вещей в коде не задан.
"""
from __future__ import annotations

import html
import logging

from database.db import checklist_was_sent, get_setting, mark_checklist_sent
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)


async def send_manager_checklist(bot, telegram_id: int, *, quiet_now=None) -> None:
    """Отдаёт то, что задал менеджер. Не поднимает исключение и не трогает письмо о решении."""
    try:
        if await checklist_was_sent(telegram_id):
            return
        doc = (await get_setting("checklist_doc_file_id") or "").strip()
        photo = (await get_setting("checklist_photo_file_id") or "").strip()
        text = (await get_setting_typed("checklist_text") or "").strip()
        if doc:
            method, file_id = "send_document", doc
        elif photo:
            method, file_id = "send_photo", photo
        else:
            method, file_id = None, ""
        if not file_id and not text:
            return
        if file_id:
            await _emit_file(bot, telegram_id, method, file_id, quiet_now)
        if text:
            await _emit_text(bot, telegram_id, text, quiet_now)
        await mark_checklist_sent(telegram_id)
    except Exception:
        logger.exception("checklist: не ушло %s", telegram_id)


async def _emit_file(bot, telegram_id: int, method: str, file_id: str, quiet_now) -> None:
    async def _send():
        await getattr(bot, method)(telegram_id, file_id)

    if quiet_now is None:
        await _send()
        return
    from services.comms import quiet_hours
    await quiet_hours.send_or_queue_media(
        quiet_now, telegram_id, sender=_send, method=method, file_id=file_id,
        caption=None, parse_mode=None,
    )


async def _emit_text(bot, telegram_id: int, text: str, quiet_now) -> None:
    safe = html.escape(text)

    async def _send():
        await bot.send_message(telegram_id, safe)

    if quiet_now is None:
        await _send()
        return
    from services.comms import quiet_hours
    await quiet_hours.send_or_queue_text(
        quiet_now, telegram_id, safe, sender=_send, parse_mode="HTML",
    )
