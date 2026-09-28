"""29.09: «до кого из сотрудников бот не может достучаться».

Прод 19.09: часть модераторов заблокировала бота или ни разу не нажала /start — уведомления о
заявках до них не доходили, в логе копились ERROR, а менеджеры ничего не знали. Здесь:

- `is_unreachable_error` — ошибка доставки значит «человек недоступен» (заблокировал бота,
  удалил аккаунт, ни разу не писал боту), а не сетевой сбой;
- `note_undeliverable` / `note_delivered` — поставить / снять отметку в `staff_unreachable`;
  зовутся из циклов рассылки сотрудникам (`handlers.admin_caps.notify_by_capability`,
  `services.daily_digest`). Кому и в каком порядке слать, они НЕ решают — только помечают;
- `StaffReachMiddleware` — человек сам написал боту в личку -> отметка снимается;
- `first_warning_today` — один WARNING на человека в сутки вместо ERROR на каждую заявку.

Всё fail-soft: любой сбой отметки проглатывается (debug в лог) и никогда не долетает до цикла
рассылки. Процессный кэш отмеченных id (`_marked`) нужен, чтобы удачная доставка и каждое
входящее сообщение не ходили в БД: запрос — только когда человек реально отмечен. Кэш
привязан к `config.DB_PATH` (сменили БД — перечитали).
"""
import html
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from config import config
from database import db as _db
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

# Подстроки ответа Bot API в TelegramBadRequest, которые значат «этому человеку не написать».
_BAD_REQUEST_MARKERS = ("chat not found", "user not found", "peer_id_invalid", "user is deactivated")

_marked: set[int] | None = None
_marked_path: str | None = None
_warned_day: dict[int, str] = {}


def reset_state() -> None:
    """Для тестов: забыть кэш отметок и суточные WARNING."""
    global _marked, _marked_path
    _marked = None
    _marked_path = None
    _warned_day.clear()


def is_unreachable_error(exc: BaseException) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True  # заблокировал бота / удалил аккаунт / бот не может начать диалог
    if isinstance(exc, TelegramBadRequest):
        msg = str(getattr(exc, "message", "") or exc).lower()
        return any(marker in msg for marker in _BAD_REQUEST_MARKERS)
    return False


def _reason(exc: BaseException) -> str:
    return "blocked" if isinstance(exc, TelegramForbiddenError) else "chat_not_found"


async def _marked_ids() -> set[int]:
    global _marked, _marked_path
    if _marked is None or _marked_path != config.DB_PATH:
        _marked = set(await _db.list_staff_unreachable())
        _marked_path = config.DB_PATH
    return _marked


async def note_undeliverable(uid: int, exc: BaseException) -> None:
    """Поставить отметку. Уже отмеченного не трогаем (без записи в БД на каждую заявку)."""
    try:
        marked = await _marked_ids()
        if uid in marked:
            return
        await _db.mark_staff_unreachable(uid, _reason(exc))
        marked.add(uid)
    except Exception as e:
        logger.debug("staff_reach: failed to mark %s unreachable: %s", uid, e)


async def note_delivered(uid: int) -> None:
    """Снять отметку, если она есть. Без отметки — ни одного запроса к БД."""
    try:
        marked = await _marked_ids()
        if uid not in marked:
            return
        await _db.clear_staff_unreachable(uid)
        marked.discard(uid)
    except Exception as e:
        logger.debug("staff_reach: failed to clear mark for %s: %s", uid, e)


async def note_incoming(uid: int) -> None:
    """Человек сам написал боту — значит, бот снова может ему писать."""
    await note_delivered(uid)


def first_warning_today(uid: int) -> bool:
    """True ровно один раз на человека за московские сутки."""
    day = msk_now().strftime("%Y-%m-%d")
    if _warned_day.get(uid) == day:
        return False
    _warned_day[uid] = day
    return True


async def unreachable_marks() -> dict[int, dict]:
    """Текущие отметки для экранов; сбой -> пусто (экран не должен падать из-за подсказки)."""
    try:
        return await _db.list_staff_unreachable()
    except Exception as e:
        logger.debug("staff_reach: failed to list marks: %s", e)
        return {}


def mark_text(since: str | None) -> str:
    """Пометка для человека: «⚠️ не получает уведомления с 28.09 — …»."""
    when = ""
    if since and len(since) >= 10:
        when = f" с {since[8:10]}.{since[5:7]}"
    return f"⚠️ не получает уведомления{when} — заблокировал бота или не нажал /start"


def superadmin_lines(marks: dict[int, dict]) -> list[str]:
    """Строки про недоступных суперадминов (`config.ADMIN_IDS` нет в списке «Люди»)."""
    return [
        f"• Суперадмин {html.escape(str(uid))} — {mark_text(marks[uid].get('since'))}"
        for uid in config.ADMIN_IDS if uid in marks
    ]


class StaffReachMiddleware(BaseMiddleware):
    """Outer-middleware на `dp.update`: любое событие человека в ЛИЧКЕ с ботом снимает его
    отметку. Групповые чаты не считаются — писать в группу можно и с заблокированным ботом.
    Хендлер вызывается всегда, что бы ни случилось с отметкой."""

    async def __call__(self, handler, event, data):
        try:
            user = data.get("event_from_user")
            chat = data.get("event_chat")
            if user is not None and (chat is None or getattr(chat, "type", None) == "private"):
                await note_incoming(user.id)
        except Exception as e:
            logger.debug("staff_reach: middleware failed: %s", e)
        return await handler(event, data)
