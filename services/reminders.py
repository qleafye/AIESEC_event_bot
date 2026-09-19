"""Phase 2 (APP-08, D-13/D-14): anti-storm periodic pending-application reminder.

A single asyncio background task started at bot startup pings the pending count once per
configurable interval — never one push per submission.

Квик 260919 (P3): получатели БОЛЬШЕ не «только config.ADMIN_IDS» — прод показал 7 менеджеров
reg_manager в `staff`, ни один не в ADMIN_IDS, и никто из них не получал вообще ничего. Теперь
это `capability_holders("moderate_reg")` (тот же D-13 примитив, что `reg_digest`/`game_digest`;
он сам кладёт ADMIN_IDS первыми и не дублирует), а счётчик у каждого получателя СВОЙ — ровно то
число, что он увидит, открыв «📋 Заявки»: тот же резолвер `handlers.admin_core._admin_city_view`
(город делegата → `staff.city`, если менеджер привязан, иначе его собственный выбор в шапке
панели). Нулевой счётчик у получателя -> ему не шлём вовсе (незачем будить пустым «0»).

Тихие часы (`services/quiet_hours.py`) здесь НАРОЧНО не применяются — тот модуль бережёт сон
ДЕЛЕГАТА, а не менеджера (см. его же докстринг и `services/daily_digest.py`, тот же вывод для
вечерней сводки менеджерам: соседей с тихими часами на manager-facing уведомлениях в проекте
нет — не изобретаем).
"""
import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from database.db import get_pending_count, get_setting
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1800  # seconds (30 min)

# Recipients we can no longer reach: they blocked the bot, or their chat is gone / their id is
# wrong (a deleted account answers "chat not found"). The reminder fires on a fixed interval
# forever, so such a recipient produces one identical ERROR per tick for as long as the bot runs
# (observed in production: 67 for a single admin id). Neither cause clears itself on our side
# — so we note it once, stop trying, and let a bot restart clear the set (the cheap way to
# re-test whether they unblocked / the id was fixed). Квик 260919: keyed by telegram_id, so it
# already covers any moderate_reg holder, not just config.ADMIN_IDS — name kept for git-blame
# continuity with the 260815/260816 incident this set was built to fix.
_blocked_admins: set[int] = set()

# Night review 260815 (review/services.md #15) — same class of bug as #1 in
# services/scheduler.py::_safe_send: "chat not found" arrives as HTTP 400
# (TelegramBadRequest), not 403, so it used to fall through to the generic `except` and never
# muted anyone. See decision D-01 of quick task 260816-44s: the WHOLE of TelegramBadRequest is
# treated as permanent on purpose — this loop sends a fixed text to one admin id, so a 400
# caused by the payload would repeat identically anyway, and we refuse to sniff `e.message`
# for Telegram's wording. Transient errors keep the generic branch and are retried next tick.
_PERMANENT_SEND_ERRORS = (TelegramForbiddenError, TelegramBadRequest)


def _reminder_enabled(raw: str | None) -> bool:
    """on/None -> True, off -> False, unknown -> True (default on)."""
    return raw != "off"


def _reminder_interval(raw: str | None) -> int:
    """Positive int seconds; None/empty/invalid/<=0 -> DEFAULT_INTERVAL.

    # REG-02: pending_reminder_loop no longer calls this directly (reads via
    # get_setting_typed instead) — retained as the parse oracle for
    # tests/test_reminders_phase2.py and tests/test_settings_consumers_phase6.py.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return value if value > 0 else DEFAULT_INTERVAL


async def pending_reminder_loop(bot):
    """Forever: if enabled, ping every current `moderate_reg` holder (D-13 fan-out — ADMIN_IDS
    plus every staff `reg_manager`-family role, deduped, see module docstring), each with THEIR
    OWN count, then sleep the configured interval. Fail-soft per iteration and per recipient
    send.

    Lazy imports (module docstring precedent — `reg_digest`/`game_digest`/`daily_digest` all do
    the same): `handlers.admin_caps`/`handlers.admin_core` import back into `handlers`, and
    `main.py` imports this module at top level before `handlers` is guaranteed loaded."""
    from handlers.admin_caps import capability_holders
    from handlers.admin_core import _admin_city_view

    while True:
        interval = DEFAULT_INTERVAL
        try:
            # REG-02: read through the registry accessor (byte-identical to
            # _reminder_interval, see tests/test_settings_consumers_phase6.py).
            interval = await get_setting_typed("pending_reminder_interval")
            if _reminder_enabled(await get_setting("pending_reminder_enabled")):
                for uid in await capability_holders("moderate_reg"):
                    if uid in _blocked_admins:
                        continue
                    try:
                        # WR-05 idiom (handlers/admin_core.py): one read resolves BOTH the
                        # scope and the label this recipient's own queue screen would show —
                        # a manager bound to a city gets that city's count, an unbound
                        # manager/superadmin gets the global one (or whatever they last picked
                        # in the panel header).
                        scope, label = await _admin_city_view(uid)
                        count = await get_pending_count(city_scope=scope)
                        if count <= 0:
                            continue  # ничего не ждёт этого получателя — не будим зря
                        suffix = f" ({label})" if label else ""
                        text = f"📋 Заявок в ожидании{suffix}: {count}. Открой /admin → Заявки."
                        await bot.send_message(uid, text)
                    except _PERMANENT_SEND_ERRORS as e:
                        _blocked_admins.add(uid)
                        logger.warning(
                            f"Pending reminder: recipient {uid} blocked the bot or "
                            f"their chat is unreachable — muting reminders for them "
                            f"until restart: {e}"
                        )
                    except Exception as e:
                        logger.error(f"Pending reminder: failed to notify recipient {uid}: {e}")
        except Exception as e:
            logger.error(f"Pending reminder loop iteration failed: {e}")
        await asyncio.sleep(interval)
