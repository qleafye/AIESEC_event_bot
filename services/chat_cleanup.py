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
`chat_bot_state.can_delete = 0`, дальше уведомления пропускаются без вызовов API. Право
вернули — апдейт my_chat_member перезаписывает состояние, очистка продолжается сама.

aiogram здесь не импортируется: ошибки Telegram распознаются по тексту (тот же приём, что
`chat_tracking._is_absent_error`), фейковые боты тестов бросают обычные Exception.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from database.db import get_chat_bot_state, set_chat_bot_state
from services import chat_tracking
from services.timeutil import msk_now
from settings_schema import get_setting_typed

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

# Ошибки Telegram «удалить нельзя» — права нет или сообщение старше 48 часов.
_NO_RIGHTS_MARKERS = (
    "NOT ENOUGH RIGHTS", "CAN'T BE DELETED", "MESSAGE_DELETE_FORBIDDEN", "CHAT_ADMIN_REQUIRED",
)
_GONE_MARKERS = ("MESSAGE TO DELETE NOT FOUND",)

# Чаты, про которые уже предупредили в лог в этом процессе (warn once per chat).
_warned: set[int] = set()


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


async def handle_service_message(bot, chat_id: int, message_id: int, code: str) -> None:
    """Удалить служебное уведомление, если его тип отмечен и чат — привязанный чат
    делегатов. Никогда не бросает."""
    try:
        if code not in await ticked_codes():
            return
        if not any(b["chat_id"] == chat_id for b in await chat_tracking.bound_chats()):
            return
        state = await get_chat_bot_state(chat_id)
        if state is not None and state.get("can_delete") == 0:
            return
        delay = await _delay_seconds()
        if delay > 0 and _schedule(chat_id, message_id, delay):
            return
        await _delete(bot, chat_id, message_id)
    except Exception as e:
        logger.info("chat_cleanup: уведомление id=%s в чате id=%s не обработано: %s: %s",
                    message_id, chat_id, type(e).__name__, e)


def _schedule(chat_id: int, message_id: int, delay: int) -> bool:
    """Персистентная date-джоба (переживает рестарт). Нет планировщика — False, вызывающий
    удаляет сразу."""
    try:
        from services.scheduler import get_scheduler

        get_scheduler().add_job(
            delete_service_message_job, "date", run_date=msk_now() + timedelta(seconds=delay),
            args=[chat_id, message_id], id=f"chatclean_{chat_id}_{message_id}",
            replace_existing=True,
        )
        return True
    except Exception as e:
        logger.info("chat_cleanup: отложить удаление не вышло (%s: %s) — удаляю сразу",
                    type(e).__name__, e)
        return False


async def _delete(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        text = str(e).upper()
        if any(marker in text for marker in _GONE_MARKERS):
            return  # кто-то удалил раньше — всё хорошо
        if any(marker in text for marker in _NO_RIGHTS_MARKERS):
            await set_chat_bot_state(chat_id, None, False)
            if chat_id not in _warned:
                _warned.add(chat_id)
                logger.warning(
                    "chat_cleanup: в чате id=%s у бота нет права «Удаление сообщений» — "
                    "служебные уведомления не удаляются, пока права не вернут", chat_id,
                )
            return
        logger.info("chat_cleanup: не удалось удалить id=%s в чате id=%s: %s: %s",
                    message_id, chat_id, type(e).__name__, e)


async def delete_service_message_job(chat_id: int, message_id: int) -> None:
    """Цель отложенной date-джобы: модульного уровня, аргументы — picklable int (APScheduler
    хранит ссылку `модуль:имя`). Права перепроверяются: их могли забрать за время задержки."""
    try:
        import services.scheduler as scheduler_module

        bot = scheduler_module.get_bot()
        state = await get_chat_bot_state(chat_id)
        if state is not None and state.get("can_delete") == 0:
            return
        await _delete(bot, chat_id, message_id)
    except Exception as e:
        logger.info("chat_cleanup.delete_service_message_job: чат id=%s, id=%s: %s: %s",
                    chat_id, message_id, type(e).__name__, e)
