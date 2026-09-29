"""Доставка уведомления о новой ступени амбассадора (событие `amb_tier_reached`).

Событие ставит `services.amb_tiers.check_tiers` в `miniapp_outbox` — из бот-процесса или из
веб-процесса Mini App; разбирает его только бот (`services/miniapp_outbox.py::_handle_row`).

Повтор безопасен: сначала атомарно ставится `notified_at`
(`database.amb_tiers_db.claim_tier_notification`, `UPDATE ... WHERE notified_at IS NULL`),
и только выигравший вызов шлёт сообщение. Временный сбой отправки снимает отметку и бросает
исключение — очередь сделает ретрай (до `MAX_ATTEMPTS`). Человек заблокировал бота —
отметка остаётся, ретраить бессмысленно.

Тексты — ключи реестра `amb_tier*_text`; в них только цифры и ступени, ни имени, ни ника,
ни статуса конкретного приглашённого. Перевод на язык амбассадора — `services.i18n`, тот же
приём, что у сообщения об одобрении на площадке (`services/onsite_reg.py`).

Модуль aiogram-free по импортам: бот приходит параметром, типы исключений aiogram читаются
лениво только при сбое. В логах — только id и номер ступени.
"""
from __future__ import annotations

import logging

from database import amb_tiers_db
from services.timeutil import msk_now
from settings_schema import SETTINGS_SCHEMA, get_setting_typed

logger = logging.getLogger(__name__)

_STAMP = "%Y-%m-%d %H:%M:%S"


def _text_key(tier: int, o2o_status: str | None) -> str | None:
    if tier == 1:
        return "amb_tier1_text"
    if tier == 2:
        return "amb_tier2_waitlist_text" if o2o_status == "waitlist" else "amb_tier2_granted_text"
    if tier == 3:
        return "amb_tier3_text"
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
            from services.i18n import context as i18n_context
            lang, tr_map = await i18n_context(telegram_id)
        except Exception:
            logger.exception("amb_tiers_notify: язык не определён (tid=%s)", telegram_id)
        from services.i18n import tr

        text = str(tr(template, lang, tr_map)).replace("{left}", str(max(int(left or 0), 0)))

        from services import quiet_hours

        await quiet_hours.send_or_queue_text(
            msk_now(), telegram_id, text,
            sender=lambda: bot.send_message(telegram_id, text, parse_mode="HTML"),
        )
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
