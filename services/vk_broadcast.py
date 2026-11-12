"""Рассылка ВК отдельно от Telegram. Берём тех, кому сейчас пишем во ВК, не всех с номером."""
from __future__ import annotations

import asyncio
import logging

from services.vk.vk_api_client import send_vk_message

logger = logging.getLogger(__name__)

# 10 сообщений в секунду — пауза между вызовами, не после последнего.
_GAP_SECONDS = 0.1


async def list_vk_broadcast_ids() -> list[int]:
    """vk_id карточек с лентой ВК. Ник анкеты и номер без этой ленты в список не входят."""
    from database.db import _connect

    async with _connect() as db:
        async with db.execute(
            "SELECT vk_id FROM users "
            "WHERE active_messenger = 'vk' AND vk_id IS NOT NULL "
            "ORDER BY vk_id"
        ) as cursor:
            rows = await cursor.fetchall()
    return [int(row[0]) for row in rows]


async def broadcast_vk(
    text: str, photo_file_id: str | None, target_vk_ids: list[int],
) -> tuple[int, int]:
    """Текст по списку номеров ВК. Картинку Telegram во ВК не отправляем.

    Возвращает (доставлено, ошибок). Сбой одного номера не останавливает остальных.
    Пауза 0.1 с между вызовами. Текст письма в журнал не пишется.
    """
    ids: list[int] = []
    seen: set[int] = set()
    for raw in target_vk_ids:
        if raw is None:
            continue
        vk_id = int(raw)
        if vk_id in seen:
            continue
        seen.add(vk_id)
        ids.append(vk_id)
    if photo_file_id:
        logger.warning("VK broadcast refused a photo; nothing was sent")
        return (0, len(ids))
    body = (text or "").strip()
    if not body:
        logger.warning("VK broadcast refused an empty text; nothing was sent")
        return (0, len(ids))
    success = 0
    failed = 0
    for index, vk_id in enumerate(ids):
        try:
            if await send_vk_message(vk_id, body):
                success += 1
            else:
                failed += 1
        except Exception as exc:
            logger.warning("VK broadcast to %s failed: %s", vk_id, type(exc).__name__)
            failed += 1
        if index + 1 < len(ids):
            await asyncio.sleep(_GAP_SECONDS)
    return (success, failed)
