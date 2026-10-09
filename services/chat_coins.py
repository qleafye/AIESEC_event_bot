"""«+N» в чате делегатов: гейм-менеджер отвечает на сообщение делегата «+5 за мем» — бот
начисляет монеты (источник `chat`), ставит реакцию на сообщение менеджера и пишет делегату
в личку тем же текстом, что у ручных монет (через тихие часы).

Решения (Кристина 28.09, владелец 08.10): право — только у держателя «🎮 Модерация
геймификации» (остальным бот молчит, сообщение — обычная реплика чата); минусов в чате нет —
снимать и отменять монеты только внутри бота; одно сообщение = одно начисление, второй «+N»
на то же сообщение не засчитывается, менеджеру об этом тихо в личку. В группу бот по-прежнему
ничего не пишет (правка 15.09) — только реакция.

Модуль aiogram-free по импортам: бот приходит параметром, сообщение читается по атрибутам.
"""
from __future__ import annotations

import html
import logging
import re

from database import chat_coins_db
from database.db import get_balance, get_user

logger = logging.getLogger(__name__)

# Больше разовой награды в чате не бывает: «+5000» — скорее опечатка, чем решение.
MAX_CHAT_AWARD = 1000

# «+5», «+ 5», «+5 за мем», «+10\nза помощь». Число — сразу после плюса, дальше — причина.
_AWARD_RE = re.compile(r"^\s*\+\s?(\d{1,6})(?!\d)[\s.,:;!—-]*(.*)$", re.DOTALL)

# Реакция на сообщение менеджера «принято». «✅» в Telegram реакцией поставить нельзя (нет в
# списке разрешённых); в чате может быть разрешена только часть реакций — пробуем по очереди.
CONFIRM_REACTIONS = ("👌", "👍", "🔥", "🏆")


def parse_award(text: str | None) -> tuple[int, str] | None:
    """(сумма, причина) или None, если сообщение — не «+N». Причина без хвостовых пробелов,
    переводы строк схлопнуты; может быть пустой."""
    m = _AWARD_RE.match(text or "")
    if not m:
        return None
    reason = " ".join(m.group(2).split())
    return int(m.group(1)), reason


def award_reason(reason: str) -> str:
    """То, что увидят делегат и журнал: «за мем — в чате» / «в чате»."""
    reason = reason.strip()
    return f"{reason} — в чате" if reason else "в чате"


def _display(user) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    return html.escape(getattr(user, "first_name", None) or "участник")


async def _dm(bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.info("chat_coins: личка id=%s не доставлена: %s", user_id, type(e).__name__)


async def _react(bot, chat_id: int, message_id: int) -> None:
    from aiogram.types import ReactionTypeEmoji

    for emoji in CONFIRM_REACTIONS:
        try:
            await bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji(emoji=emoji)])
            return
        except Exception as e:
            logger.info("chat_coins: реакция %s в чате %s не встала: %s", emoji, chat_id, type(e).__name__)


async def is_game_manager(telegram_id: int) -> bool:
    from handlers.admin_caps import resolve_capabilities

    return "moderate_game" in await resolve_capabilities(telegram_id)


async def try_handle(message, bot) -> bool:
    """True — сообщение было командой «+N» от гейм-менеджера и разобрано (начислено или
    менеджеру объяснено, почему нет). False — обычное сообщение, его считает учёт чата."""
    sender = getattr(message, "from_user", None)
    if sender is None or sender.is_bot:
        return False
    parsed = parse_award(getattr(message, "text", None))
    if parsed is None:
        return False
    from services.chat_tracking import bound_chats

    if not any(b["chat_id"] == message.chat.id for b in await bound_chats()):
        return False
    if not await is_game_manager(sender.id):
        return False
    amount, reason = parsed

    replied = getattr(message, "reply_to_message", None)
    target = getattr(replied, "from_user", None) if replied is not None else None
    is_topic_root = replied is not None and (
        getattr(replied, "forum_topic_created", None) is not None
        or (getattr(message, "is_topic_message", False)
            and getattr(replied, "message_id", None) == getattr(message, "message_thread_id", None))
    )
    if replied is None or is_topic_root:
        await _dm(bot, sender.id,
                  f"🪙 «+{amount}» не начислено: ответьте этим сообщением на сообщение делегата "
                  f"(свайп влево или «Ответить»), а не просто в чат.")
        return True
    if target is None or target.is_bot or getattr(replied, "sender_chat", None) is not None:
        await _dm(bot, sender.id,
                  f"🪙 «+{amount}» не начислено: это сообщение бота или канала — ответьте на "
                  f"сообщение самого делегата.")
        return True
    if target.id == sender.id:
        await _dm(bot, sender.id, f"🪙 «+{amount}» не начислено: себе начислить нельзя.")
        return True
    if amount <= 0 or amount > MAX_CHAT_AWARD:
        await _dm(bot, sender.id,
                  f"🪙 «+{amount}» не начислено: в чате можно начислить от 1 до {MAX_CHAT_AWARD}. "
                  f"Больше — через «🪙 Баллы вручную» в админке.")
        return True
    if await get_user(target.id) is None:
        await _dm(bot, sender.id,
                  f"🪙 «+{amount}» не начислено: {_display(target)} ещё не подавал(а) анкету в "
                  f"боте — баллы некуда записать. Попросите его(её) зарегистрироваться.")
        return True

    full_reason = award_reason(reason)
    ok, previous = await chat_coins_db.claim_chat_award(
        chat_id=message.chat.id, message_id=replied.message_id, user_id=target.id,
        amount=amount, reason=full_reason, awarded_by=sender.id,
        award_message_id=message.message_id,
    )
    if not ok:
        before = (previous or {}).get("amount")
        await _dm(bot, sender.id,
                  f"🪙 «+{amount}» не начислено: за это сообщение {_display(target)} уже "
                  f"начислено +{before} — одно сообщение = одно начисление. Поправить сумму можно "
                  f"в «🪙 Баллы вручную».")
        return True

    logger.info("chat_coins: +%s user=%s by=%s chat=%s", amount, target.id, sender.id, message.chat.id)
    await _react(bot, message.chat.id, message.message_id)
    try:
        from services.game_sync import request_resync

        request_resync()
    except Exception as e:
        logger.warning("chat_coins: пересборка вкладок геймы не запрошена: %s", type(e).__name__)
    from services.coins_notify import notify_manual_coins

    await notify_manual_coins(bot, target.id, amount, full_reason, await get_balance(target.id))
    return True
