"""Quick 260910-okb (BC-01..06): фоновый прогон немедленной рассылки/отзыва.

09.09 менеджер отправил «Привет» на 951 человека одним касанием и не смог остановить: кнопка
«Отмена» чистила только FSM, а `for chat_id in users_ids` крутился внутри хендлера и никого не
слушал. Ни лога, ни message_id — отозвать было нечего.

Чистый прогон БЕЗ единого импорта хендлеров (`handlers/admin_broadcasts.py` импортирует этот
модуль — обратный импорт создал бы цикл). `_retry_delay` продублирован однострочно вместо
импорта из handlers (см. D-07 в handlers/admin_broadcasts.py) — по той же причине.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramRetryAfter

from database.db import (
    finish_broadcast,
    list_broadcast_messages,
    record_broadcast_delivery,
    set_broadcast_status,
)
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

# BC-06: Bot API отдаёт delete_message только 48 часов с момента отправки.
REVOKE_WINDOW_HOURS = 48

# Адресные флаги остановки — по одному набору на процесс, ключ = broadcast_id, так что
# request_stop(1) не трогает параллельный прогон с bid=2.
_stop: set[int] = set()

# Троттлинг on_progress: не чаще раза в эти N секунд ИЛИ каждые эти N итераций — что наступит
# раньше. Здесь, а не в хендлере: run_revoke тоже им пользуется.
_PROGRESS_MIN_INTERVAL_S = 3
_PROGRESS_EVERY_N = 25


def request_stop(broadcast_id: int) -> None:
    _stop.add(broadcast_id)


def is_stopped(broadcast_id: int) -> bool:
    return broadcast_id in _stop


def clear_stop(broadcast_id: int) -> None:
    _stop.discard(broadcast_id)


def can_revoke(started_at: str | None) -> bool:
    """True, если с started_at (тот же формат, что у остальных таблиц) прошло меньше
    REVOKE_WINDOW_HOURS. Пустое/нераспарсенное значение — False (fail-safe: лучше не
    показать кнопку удаления, чем дать её на неотзываемую рассылку)."""
    if not started_at:
        return False
    try:
        started = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return msk_now() - started < timedelta(hours=REVOKE_WINDOW_HOURS)


def _retry_delay(retry_after: int) -> int:
    """Продублировано из handlers/admin_broadcasts.py::_retry_delay (D-07) — этот модуль не
    импортирует хендлеры (иначе handlers -> services -> handlers)."""
    return retry_after + 1


async def run_broadcast(broadcast_id, chat_ids, send_one, on_progress=None, on_finish=None):
    """`send_one(chat_id)` — корутина, возвращающая СПИСОК message_id доставленных сообщений
    (одно для текста/фото, несколько для альбома). Каждый message_id пишется отдельной строкой
    в broadcast_deliveries. Один 429-ретрай на попытку; прочие исключения — blocked, цикл не
    падает. `is_stopped` проверяется в начале КАЖДОЙ итерации — стоп прерывает прогон на
    ближайшем шаге, адресно по broadcast_id."""
    total = len(chat_ids)
    delivered = 0
    blocked = 0
    stopped = False
    last_progress_ts = time.monotonic()

    for i, chat_id in enumerate(chat_ids):
        if is_stopped(broadcast_id):
            stopped = True
            break

        message_ids = None
        retried_ok = None
        try:
            message_ids = await send_one(chat_id)
            first_ok = True
        except TelegramRetryAfter as e:
            first_ok = False
            await asyncio.sleep(_retry_delay(e.retry_after))
            try:
                message_ids = await send_one(chat_id)
                retried_ok = True
            except Exception as e2:
                retried_ok = False
                # Квик 260915-twr (Task B2): раньше причина недоставки терялась полностью —
                # warning, не error: заблокировавший бота делегат — факт о человеке, не сбой
                # бота (тот же довод, что в докстринге services/scheduler.py::_safe_send).
                logger.warning(
                    "broadcast %s retry send failed for %s: %s: %s",
                    broadcast_id, chat_id, type(e2).__name__, e2,
                )
        except Exception as e:
            first_ok = False
            logger.warning(
                "broadcast %s send failed for %s: %s: %s",
                broadcast_id, chat_id, type(e).__name__, e,
            )

        if first_ok or retried_ok:
            delivered += 1
            for message_id in (message_ids or []):
                await record_broadcast_delivery(broadcast_id, chat_id, message_id)
        else:
            blocked += 1

        await asyncio.sleep(0.05)

        now = time.monotonic()
        if on_progress and (
            now - last_progress_ts >= _PROGRESS_MIN_INTERVAL_S or (i + 1) % _PROGRESS_EVERY_N == 0
        ):
            last_progress_ts = now
            try:
                await on_progress(delivered, blocked, total)
            except Exception:
                pass

    status = "stopped" if stopped else "done"
    await finish_broadcast(broadcast_id, status, delivered, blocked)
    clear_stop(broadcast_id)
    logger.info(
        "broadcast %s finished: status=%s delivered=%s blocked=%s",
        broadcast_id, status, delivered, blocked,
    )
    if on_finish:
        await on_finish(status, delivered, blocked)


async def run_revoke(bot, broadcast_id, on_progress=None, on_finish=None):
    """Удаляет у получателей всё, что записано в broadcast_deliveries для этой рассылки.
    Любая ошибка удаления — «не удалось», цикл не падает. Тот же стоп-флаг/троттлинг/сон, что
    у run_broadcast."""
    pairs = await list_broadcast_messages(broadcast_id)
    total = len(pairs)
    deleted = 0
    failed = 0
    last_progress_ts = time.monotonic()

    for i, (chat_id, message_id) in enumerate(pairs):
        if is_stopped(broadcast_id):
            break

        ok = False
        try:
            await bot.delete_message(chat_id, message_id)
            ok = True
        except TelegramRetryAfter as e:
            await asyncio.sleep(_retry_delay(e.retry_after))
            try:
                await bot.delete_message(chat_id, message_id)
                ok = True
            except Exception:
                ok = False
        except Exception:
            ok = False

        if ok:
            deleted += 1
        else:
            failed += 1

        await asyncio.sleep(0.05)

        now = time.monotonic()
        if on_progress and (
            now - last_progress_ts >= _PROGRESS_MIN_INTERVAL_S or (i + 1) % _PROGRESS_EVERY_N == 0
        ):
            last_progress_ts = now
            try:
                await on_progress(deleted, failed, total)
            except Exception:
                pass

    await set_broadcast_status(broadcast_id, "revoked")
    clear_stop(broadcast_id)
    logger.info("broadcast %s revoke finished: deleted=%s failed=%s", broadcast_id, deleted, failed)
    if on_finish:
        await on_finish(deleted, failed)
