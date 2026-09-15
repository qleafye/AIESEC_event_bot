"""Уведомление делегата о ручном начислении/списании монет — ОДНА формулировка на все пути.

Переезд из `handlers/admin.py::_notify_manual_coins` (16.09). Причина переезда — находка
ревизии: ручные монеты, начисленные из Mini App (`miniapp/routers/coins_admin.py`), делегату
не приходили ВООБЩЕ. Веб пишет событие `coins_manual` в `miniapp_outbox`, а разборщик
(`services/miniapp_outbox.py`) до этого дня только просил пересборку вкладок геймы — про
уведомление там не было ни строки, тогда как путь из чата (мастер «🪙 Монеты» и `/coins`)
уведомлял через `services.quiet_hours`. Класть копию текста в разборщик значило бы завести
второй источник формулировки; поэтому функция живёт здесь, а оба пути её зовут.

`handlers/admin.py` реэкспортирует её под прежним именем `_notify_manual_coins` — все
существующие вызовы (`coinsman_confirm` в admin_gamification.py, `/coins`) и тесты
(`tests/test_coins_manual_260818.py`, `tests/test_quiet_hours_260904.py`) продолжают работать
без правок.

Модуль aiogram-free по импортам (бот приходит параметром, как в `services/polls.py`), но
СТОРОНА БОТА по вызову: `services.scheduler._now_moscow_naive` импортируется лениво, внутри
функции, и веб-процесс её не зовёт — он ставит событие в outbox, а разбирает его бот.
"""
from __future__ import annotations

import html as html_module
import logging

from settings_schema import get_setting_typed, SETTINGS_SCHEMA

logger = logging.getLogger(__name__)


# Phase 14 (GAME-09): shared by the button wizard (coinsman_confirm), /coins and the Mini App
# outbox -- one place builds the delegate-facing notification text, so the paths can never
# drift on wording. `.replace` per placeholder (not `.format`): a manager-edited template may
# carry a stray `{`/`}` and `.format` would raise on that, breaking the notification entirely.
# `{delta}` always carries an explicit sign (f"{delta:+d}") since the same template covers both
# credit and debit (CONTEXT.md B). `{reason}` (free text from a human) is HTML-escaped -- the
# bot sends with parse_mode="HTML".
async def notify_manual_coins(bot, user_id: int, delta: int, reason: str, balance: int) -> bool:
    """Returns True on successful delivery OR on being queued for the end of quiet hours
    (Quick 260904-dq1: for the caller this counts as success — the delegate WILL get the
    notification, just not right now), False on any failure (delegate blocked the bot,
    etc.) -- logged, never raised. The ledger write already happened before this is called
    (T-14-20): a failed notification must never be the reason an operation looks undone."""
    template = await get_setting_typed("coins_manual_notify_text")
    if not template:
        template = SETTINGS_SCHEMA["coins_manual_notify_text"]["default"]
    text = (
        str(template)
        .replace("{delta}", f"{delta:+d}")
        .replace("{reason}", html_module.escape(str(reason)))
        .replace("{balance}", str(balance))
    )
    try:
        from services import quiet_hours
        from services.scheduler import _now_moscow_naive
        sent_now = await quiet_hours.send_or_queue_text(
            _now_moscow_naive(), user_id, text,
            sender=lambda: bot.send_message(user_id, text, parse_mode="HTML"),
        )
        if not sent_now:
            logger.info(f"Manual coins notification for user {user_id} deferred to end of quiet hours")
        return True
    except Exception as e:
        logger.warning(f"Failed to notify user {user_id} of manual coins change: {e}", exc_info=True)
        return False
