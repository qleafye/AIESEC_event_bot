"""Доставка уведомления о новой ступени амбассадора (событие `amb_tier_reached`).

Событие ставит `services.amb.amb_tiers.check_tiers` в `miniapp_outbox` — из бот-процесса или из
веб-процесса Mini App; разбирает его только бот (`services/infra/miniapp_outbox.py::_handle_row`).

Повтор безопасен: сначала атомарно ставится `notified_at`
(`database.amb_tiers_db.claim_tier_notification`, `UPDATE ... WHERE notified_at IS NULL`),
и только выигравший вызов шлёт сообщение. Временный сбой отправки снимает отметку и бросает
исключение — очередь сделает ретрай (до `MAX_ATTEMPTS`). Человек заблокировал бота —
отметка остаётся, ретраить бессмысленно. Битая HTML-разметка в тексте менеджера («can't parse
entities») — тоже не временный сбой: пять повторов дали бы ту же ошибку, поэтому сообщение
сразу уходит вторым запросом без форматирования (теги вырезаны), в лог — предупреждение.
Ограничение: сообщение, отложенное тихими часами, отправляет общая очередь тихих часов, и там
этого отката нет.

Тексты — ключи реестра `amb_tier*_text`; в них только цифры и ступени, ни имени, ни ника,
ни статуса конкретного приглашённого. Перевод на язык амбассадора — `services.i18n.i18n`, тот же
приём, что у сообщения об одобрении на площадке (`services/forum/onsite_reg.py`).

Модуль aiogram-free по импортам: бот приходит параметром, типы исключений aiogram читаются
лениво только при сбое. В логах — только id и номер ступени.
"""
from __future__ import annotations

import html
import logging
import re

from shared.amb_tier_keys import tier_key
from database import amb_tiers_db
from services.infra.timeutil import msk_now
from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed

logger = logging.getLogger(__name__)

_STAMP = "%Y-%m-%d %H:%M:%S"
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")


def _text_key(tier: int, o2o_status: str | None) -> str | None:
    """Ключ текста по номеру ступени; статус 'waitlist' — текст листа ожидания."""
    try:
        return tier_key(tier, "waitlist" if o2o_status == "waitlist" else "text")
    except ValueError:
        return None


def _is_permanent(exc: Exception) -> bool:
    """Бот заблокирован / чата нет — повтор ничего не изменит."""
    try:
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    except Exception:  # noqa: BLE001 — без aiogram любой сбой считаем временным
        return False
    if isinstance(exc, TelegramForbiddenError):
        return True
    return isinstance(exc, TelegramBadRequest) and "chat not found" in str(exc).lower()


def _is_bad_markup(exc: Exception) -> bool:
    """Telegram не разобрал HTML текста — повтор с тем же текстом ничего не изменит."""
    try:
        from aiogram.exceptions import TelegramBadRequest
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, TelegramBadRequest) and "can't parse entities" in str(exc).lower()


def _plain(text: str) -> str:
    """Текст без HTML: теги вырезаны, сущности (&lt; и т.п.) раскрыты."""
    return html.unescape(_TAG_RE.sub("", text))


async def deliver_tier_notification(bot, telegram_id: int, tier: int, left: int | None) -> bool:
    """`True` — отправлено (или отложено до конца тихих часов), `False` — уже отправлялось
    раньше, ступени нет или человек заблокировал бота. Временный сбой — исключение."""
    telegram_id, tier = int(telegram_id), int(tier)
    row = await amb_tiers_db.claim_tier_notification(telegram_id, tier, msk_now().strftime(_STAMP))
    if row is None:
        return False
    key = _text_key(tier, row.get("o2o_status"))
    if key is None:
        logger.warning("amb_tiers_notify: неизвестная ступень %s (tid=%s)", tier, telegram_id)
        return False
    try:
        template = await get_setting_typed(key) or SETTINGS_SCHEMA[key]["default"]
        lang, tr_map = "ru", {}
        try:
            from services.i18n.i18n import context as i18n_context
            lang, tr_map = await i18n_context(telegram_id)
        except Exception:
            logger.exception("amb_tiers_notify: язык не определён (tid=%s)", telegram_id)
        from services.i18n.i18n import tr

        text = str(tr(template, lang, tr_map)).replace("{left}", str(max(int(left or 0), 0)))

        from services.comms import quiet_hours

        async def send():
            try:
                return await bot.send_message(telegram_id, text, parse_mode="HTML")
            except Exception as exc:
                if not _is_bad_markup(exc):
                    raise
                logger.warning(
                    "amb_tiers_notify: в тексте %s битая разметка — отправляю без форматирования "
                    "(tid=%s)", key, telegram_id,
                )
                return await bot.send_message(telegram_id, _plain(text), parse_mode=None)

        await quiet_hours.send_or_queue_text(msk_now(), telegram_id, text, sender=send)
        return True
    except Exception as exc:
        if _is_permanent(exc):
            logger.warning(
                "amb_tiers_notify: ступень %s не доставлена, бот недоступен (tid=%s)",
                tier, telegram_id,
            )
            return False
        await amb_tiers_db.release_tier_notification(telegram_id, tier)
        raise
