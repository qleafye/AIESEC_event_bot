"""Квик 260927: автоочистка служебных уведомлений Telegram в чатах делегатов.

«X присоединился(ась) по ссылке», «X добавил(а) Y», «X покинул(а) группу», «закрепил(а)
сообщение» и подобное засоряют чат города. Менеджер отмечает галочками, какие типы удалять
(экран «🧹 Служебные сообщения в чате»); по умолчанию набор пуст — после выкатки ничего не
удаляется ни на одном стеке.

Границы:
- только привязанные чаты делегатов (`chat_tracking.bound_chats()`); чаты SOS/команды не
  трогаются — там команде как раз нужно видеть, кто вошёл;
- только закрытый набор служебных типов (CONTENT_TYPE_TO_CODE); сообщения людей — никогда;
- уведомление о создании темы (`forum_topic_created`) не удаляется: это корень темы, а не
  косметика;
- учёт вступлений/выходов (chat_members/chat_events) пишется ДО удаления — вызывающий
  хендлер (`handlers/group_chat.py`) зовёт очистку последней строкой.

Без права «Удаление сообщений» — одно предупреждение в лог на чат за процесс, чат помечается
`chat_bot_state.can_delete = 0`, дальше уведомления пропускаются без вызовов API. Флаг ставит
только явная проверка прав бота (`verify_rights`, getChatMember), а не ошибка удаления одного
сообщения. Право вернули — апдейт my_chat_member или плановая сверка админов
(`chat_tracking.refresh_all_chats`, работает и при выключенном учёте, если очистка включена)
перезаписывает состояние, очистка продолжается сама.

Нагрузка: на чат — не больше одного вызова API за `DELETE_PAUSE_SECONDS`, накопленное за паузу
уходит одним deleteMessages; на 429 — ждём, сколько просит Telegram, и повторяем. С задержкой —
строка в `chat_cleanup_queue`, её разбирает одна интервальная джоба (`drain_queue`), а не
отдельная джоба на каждое уведомление.

aiogram здесь не импортируется: ошибки Telegram распознаются по тексту (тот же приём, что
`chat_tracking._is_absent_error`), фейковые боты тестов бросают обычные Exception.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from database.db import (
    drop_chat_cleanup,
    due_chat_cleanup,
    enqueue_chat_cleanup,
    get_chat_bot_state,
    set_chat_bot_state,
)
from services import chat_tracking
from services.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

TYPES_KEY = "chat_cleanup_types"
DELAY_KEY = "chat_cleanup_delay_seconds"

# Код -> подпись для человека. Форма — как ждёт `options_ref` реестра (dict код -> подпись,
# порядок = порядок галочек на экране).
CLEANUP_TYPES: dict[str, str] = {
    "join": "👋 «Вступил(а) в группу» и «добавил(а) участника»",
    "leave": "🚪 «Вышел(а) из группы» и «удалил(а) участника»",
    "pin": "📌 «Закрепил(а) сообщение»",
    "chat_info": "🖼 Смена названия, фото, фона группы, автоудаления",
    "topics": "🗂 Изменения тем: переименование, закрытие, открытие",
    "boost": "🚀 Бусты группы",
    "video_chat": "🎥 Видеочаты: запланирован, начат, завершён",
}

# content_type сообщения Telegram -> код типа. forum_topic_created намеренно отсутствует
# (корень темы — не косметическое уведомление, удалять его нельзя).
_CONTENT_TYPES = {
    "join": ("new_chat_members",),
    "leave": ("left_chat_member",),
    "pin": ("pinned_message",),
    "chat_info": (
        "new_chat_title", "new_chat_photo", "delete_chat_photo",
        "message_auto_delete_timer_changed", "chat_background_set",
    ),
    "topics": (
        "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened",
        "general_forum_topic_hidden", "general_forum_topic_unhidden",
    ),
    "boost": ("boost_added",),
    "video_chat": (
        "video_chat_scheduled", "video_chat_started", "video_chat_ended",
        "video_chat_participants_invited",
    ),
}


def _known_content_types() -> set[str] | None:
    try:
        from aiogram.enums import ContentType  # только чтобы отбросить неизвестные имена
    except ImportError:  # pragma: no cover — aiogram в проекте есть всегда
        return None
    return {c.value for c in ContentType}


_KNOWN = _known_content_types()
CONTENT_TYPE_TO_CODE: dict[str, str] = {
    name: code
    for code, names in _CONTENT_TYPES.items()
    for name in names
    if _KNOWN is None or name in _KNOWN
}


# Ошибки Telegram, похожие на нехватку прав. Сами по себе чат НЕ выключают: по ним бот
# спрашивает свои права (getChatMember), и флаг «нет прав» ставит только этот ответ.
_NO_RIGHTS_MARKERS = ("NOT ENOUGH RIGHTS", "MESSAGE_DELETE_FORBIDDEN", "CHAT_ADMIN_REQUIRED")
# Проблема конкретного сообщения (уже удалено, старше 48 часов) — не прав бота.
_GONE_MARKERS = ("MESSAGE TO DELETE NOT FOUND", "CAN'T BE DELETED")

# Чаты, про которые уже предупредили в лог в этом процессе (warn once per chat).
_warned: set[int] = set()

# Скорость удаления. Массовое вступление по ссылке (ссылку рассылают в письме об одобрении)
# давало сотни одновременных deleteMessage, делящих глобальный лимит бота с самой рассылкой.
# Теперь на чат — не больше одного вызова API в DELETE_PAUSE_SECONDS: первое уведомление
# удаляется сразу, всё, что пришло за паузу, уходит одним deleteMessages (до BATCH_MAX id).
DELETE_PAUSE_SECONDS = 1.0
BATCH_MAX = 100
# 429 Too Many Requests: подождать сколько просит Telegram (не дольше RETRY_AFTER_CAP) и
# повторить, не больше RETRY_ATTEMPTS раз.
RETRY_ATTEMPTS = 2
RETRY_AFTER_CAP = 60
# Telegram даёт боту удалять сообщения моложе 48 часов; старше — из очереди просто убираем.
QUEUE_MAX_AGE = timedelta(hours=47)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


async def ticked_codes() -> list[str]:
    """Отмеченные менеджером типы (только известные коды; сентинел пустого набора отброшен)."""
    raw = await get_setting_typed(TYPES_KEY) or []
    return [code for code in raw if code in CLEANUP_TYPES]


async def _delay_seconds() -> int:
    try:
        value = int(await get_setting_typed(DELAY_KEY) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


async def _bound_ids() -> set[int]:
    return {b["chat_id"] for b in await chat_tracking.bound_chats()}


async def handle_service_message(bot, chat_id: int, message_id: int, code: str) -> None:
    """Удалить служебное уведомление, если его тип отмечен и чат — привязанный чат
    делегатов. С задержкой — строка в очереди БД (её разбирает `drain_queue`). Никогда не
    бросает."""
    try:
        if code not in await ticked_codes():
            return
        if chat_id not in await _bound_ids():
            return
        state = await get_chat_bot_state(chat_id)
        if state is not None and state.get("can_delete") == 0:
            return
        delay = await _delay_seconds()
        if delay > 0 and await _queue(chat_id, message_id, code, delay):
            return
        await delete_batched(bot, chat_id, [message_id])
    except Exception as e:
        logger.info("chat_cleanup: уведомление id=%s в чате id=%s не обработано: %s: %s",
                    message_id, chat_id, type(e).__name__, e)


async def _queue(chat_id: int, message_id: int, code: str, delay: int) -> bool:
    """Строка в очереди отложенного удаления (переживает рестарт, не трогает jobstore).
    Запись не удалась — False, вызывающий удаляет сразу."""
    try:
        due = (msk_now() + timedelta(seconds=delay)).strftime(_TS_FORMAT)
        await enqueue_chat_cleanup(chat_id, message_id, code, due)
        return True
    except Exception as e:
        logger.info("chat_cleanup: отложить удаление не вышло (%s: %s) — удаляю сразу",
                    type(e).__name__, e)
        return False


async def drain_queue(bot, now: datetime | None = None) -> int:
    """Разбор очереди отложенного удаления — цель одной интервальной джобы на весь бот.
    Перед удалением перепроверяется всё, что могло поменяться за задержку: галочка типа,
    привязка чата, право удалять. Возвращает, сколько уведомлений отправлено на удаление."""
    now = now or msk_now()
    rows = await due_chat_cleanup(now.strftime(_TS_FORMAT))
    if not rows:
        return 0
    ticked = set(await ticked_codes())
    bound = await _bound_ids()
    too_old = (now - QUEUE_MAX_AGE).strftime(_TS_FORMAT)
    by_chat: dict[int, list[dict]] = {}
    for row in rows:
        by_chat.setdefault(row["chat_id"], []).append(row)

    async def _one_chat(chat_id: int, items: list[dict]) -> int:
        keep = [
            r["message_id"] for r in items
            if r["code"] in ticked and chat_id in bound and str(r["created_at"]) >= too_old
        ]
        if keep:
            state = await get_chat_bot_state(chat_id)
            if state is not None and state.get("can_delete") == 0:
                keep = []
        try:
            if keep:
                await delete_batched(bot, chat_id, keep)
        finally:
            await drop_chat_cleanup(chat_id, [r["message_id"] for r in items])
        return len(keep)

    done = await asyncio.gather(
        *(_one_chat(chat_id, items) for chat_id, items in by_chat.items()),
        return_exceptions=True,
    )
    for result in done:
        if isinstance(result, Exception):
            logger.info("chat_cleanup.drain_queue: %s: %s", type(result).__name__, result)
    return sum(r for r in done if isinstance(r, int))


# ── Удаление: одна очередь на чат, пауза между вызовами, повтор после 429 ────────────────

class _ChatLane:
    """Очередь удаления одного чата внутри одного event loop. Первый пришедший становится
    «разносчиком»: удаляет накопленное, выдерживая паузу между вызовами API, остальные ждут
    свой future (обработчик апдейта не возвращается раньше, чем его уведомление обработано)."""

    def __init__(self, loop):
        self.loop = loop
        self.pending: list[tuple[int, asyncio.Future]] = []
        self.running = False
        self.last_call = 0.0


_lanes: dict[int, _ChatLane] = {}


async def delete_batched(bot, chat_id: int, message_ids) -> None:
    """Поставить id в очередь удаления чата и дождаться их обработки. Не бросает."""
    ids = [int(m) for m in message_ids]
    if not ids:
        return
    loop = asyncio.get_running_loop()
    lane = _lanes.get(chat_id)
    if lane is None or lane.loop is not loop:
        lane = _ChatLane(loop)
        _lanes[chat_id] = lane
    futures = []
    for mid in ids:
        fut = loop.create_future()
        lane.pending.append((mid, fut))
        futures.append(fut)
    if not lane.running:
        lane.running = True
        try:
            await _run_lane(bot, chat_id, lane)
        finally:
            lane.running = False
            for _mid, fut in lane.pending:  # страховка: разносчик ушёл с ошибкой
                if not fut.done():
                    fut.set_result(None)
            lane.pending.clear()
    await asyncio.gather(*futures, return_exceptions=True)


async def _run_lane(bot, chat_id: int, lane: _ChatLane) -> None:
    while lane.pending:
        wait = DELETE_PAUSE_SECONDS - (lane.loop.time() - lane.last_call)
        if lane.last_call and wait > 0:
            await asyncio.sleep(wait)
        take = lane.pending[:BATCH_MAX]
        del lane.pending[:BATCH_MAX]
        try:
            await _delete_ids(bot, chat_id, [mid for mid, _fut in take])
        except Exception as e:
            logger.info("chat_cleanup: пачка в чате id=%s не удалена: %s: %s",
                        chat_id, type(e).__name__, e)
        finally:
            lane.last_call = lane.loop.time()
            for _mid, fut in take:
                if not fut.done():
                    fut.set_result(None)


async def _delete_ids(bot, chat_id: int, ids: list[int]) -> None:
    many = getattr(bot, "delete_messages", None)
    if len(ids) > 1 and many is not None:
        error = await _with_retry(lambda: many(chat_id, ids))
        if error is not None:
            await _handle_error(bot, chat_id, ids, error)
        return
    for index, mid in enumerate(ids):
        if index:
            await asyncio.sleep(DELETE_PAUSE_SECONDS)
        error = await _with_retry(lambda mid=mid: bot.delete_message(chat_id, mid))
        if error is not None and await _handle_error(bot, chat_id, [mid], error):
            return  # прав нет — остальные id этой пачки не дёргаем


def _retry_after(exc: Exception) -> float | None:
    """Секунды из 429 Too Many Requests: атрибут TelegramRetryAfter.retry_after или текст."""
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)):
        return max(float(value), 0.0)
    text = str(exc).upper()
    if "TOO MANY REQUESTS" in text or "RETRY AFTER" in text:
        match = re.search(r"RETRY AFTER (\d+)", text)
        return float(match.group(1)) if match else 1.0
    return None


async def _with_retry(call) -> Exception | None:
    """None — вызов прошёл; иначе последняя ошибка (429 — после RETRY_ATTEMPTS повторов)."""
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            await call()
            return None
        except Exception as e:
            wait = _retry_after(e)
            if wait is None or attempt == RETRY_ATTEMPTS:
                return e
            logger.info("chat_cleanup: Telegram просит подождать %.0f с — повтор", wait)
            await asyncio.sleep(min(wait, RETRY_AFTER_CAP))
    return None  # pragma: no cover — цикл всегда выходит return'ом выше


async def _handle_error(bot, chat_id: int, ids: list[int], e: Exception) -> bool:
    """Разбор ошибки удаления. True — у бота нет прав (дальше в этом чате не пробуем)."""
    text = str(e).upper()
    if any(marker in text for marker in _GONE_MARKERS):
        return False  # кто-то удалил раньше — всё хорошо
    if any(marker in text for marker in _NO_RIGHTS_MARKERS):
        if await verify_rights(bot, chat_id) is False:
            return True
        logger.info("chat_cleanup: id=%s в чате id=%s не удалён (%s), но права у бота есть",
                    ids, chat_id, type(e).__name__)
        return False
    logger.info("chat_cleanup: не удалось удалить id=%s в чате id=%s: %s: %s",
                ids, chat_id, type(e).__name__, e)
    return False


async def verify_rights(bot, chat_id: int) -> bool | None:
    """Явная проверка: getChatMember самого бота -> `chat_bot_state`. True/False — может ли бот
    удалять сообщения; None — спросить не вышло (флаг не трогаем). Только этот ответ выключает
    очистку чата — одна неудачная попытка удалить сообщение её не выключает."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except Exception as e:
        logger.info("chat_cleanup: права бота в чате id=%s проверить не вышло: %s: %s",
                    chat_id, type(e).__name__, e)
        return None
    can_delete = chat_tracking.can_delete_from(member)
    await set_chat_bot_state(chat_id, getattr(member, "status", None), can_delete)
    if not can_delete and chat_id not in _warned:
        _warned.add(chat_id)
        logger.warning(
            "chat_cleanup: в чате id=%s у бота нет права «Удаление сообщений» — "
            "служебные уведомления не удаляются, пока права не вернут", chat_id,
        )
    return can_delete


async def _delete(bot, chat_id: int, message_id: int) -> None:
    """Одно уведомление через общую очередь чата (совместимость со старыми вызовами)."""
    await delete_batched(bot, chat_id, [message_id])


async def delete_service_message_job(chat_id: int, message_id: int) -> None:
    """Цель date-джоб, поставленных до перехода на очередь в БД (могли остаться в jobstore).
    Новые такие джобы не ставятся; оставшиеся доживают с теми же перепроверками, что у
    очереди: привязка чата и право удалять."""
    try:
        import services.scheduler as scheduler_module

        bot = scheduler_module.get_bot()
        if chat_id not in await _bound_ids():
            return
        state = await get_chat_bot_state(chat_id)
        if state is not None and state.get("can_delete") == 0:
            return
        await delete_batched(bot, chat_id, [message_id])
    except Exception as e:
        logger.info("chat_cleanup.delete_service_message_job: чат id=%s, id=%s: %s: %s",
                    chat_id, message_id, type(e).__name__, e)
