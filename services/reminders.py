"""Phase 2 (APP-08, D-13/D-14): anti-storm periodic pending-application reminder.

A single asyncio background task started at bot startup pings the pending count once per
configurable interval — never one push per submission.

Квик 260919 (P3): получатели БОЛЬШЕ не «только config.ADMIN_IDS» — прод показал 7 менеджеров
reg_manager в `staff`, ни один не в ADMIN_IDS, и никто из них не получал вообще ничего. Теперь
это `capability_holders("moderate_reg")` (тот же D-13 примитив, что `reg_digest`/`game_digest`;
он сам кладёт ADMIN_IDS первыми и не дублирует).

Счётчик — ДВЕ ветки, не одна (owner correction поверх первой версии этого квика — тот же день):
* получатель ПРИВЯЗАН к городу (`staff.city` задан, не суперадмин — D-12: суперадмин никогда
  не сужается, даже если исторически несёт привязку) -> счётчик СВОЕГО города;
* любой другой (staff без города, ADMIN_IDS) -> ВСЕГДА общее число + разбивка по городам в
  одной строке. Первая версия вместо этого использовала `admin_selected_city` («что выбрано в
  шапке панели») для ВСЕХ — но на проде ни один менеджер не привязан к spb/tyumen, а
  непривязанные (и ADMIN_IDS) по умолчанию, без выбора, смотрят на дефолтный город (Москва,
  Phase 09.1/09.3) — заявки других городов не будили НИКОГО. Разбивка не зависит от шапки
  панели этого получателя вовсе — это фиксированный общий обзор, а не персональный фильтр.

Нулевой счётчик у получателя (или ноль в его городе) -> ему не шлём вовсе (незачем будить
пустым «0»). Разбивка не включает города с нулём; пуста (без скобок), если модуль городов
выключен или в реестре меньше двух городов — тогда сама разбивка не несёт новой информации
сверх общего числа. NULL/незнакомый `event_city` сворачивается в дефолтный город — та же логика,
что и «По городам» в `render_stats_text` (handlers/admin.py) для непустого total, второй копии
здесь не заводим.

Тихие часы (`services/quiet_hours.py`) здесь НАРОЧНО не применяются — тот модуль бережёт сон
ДЕЛЕГАТА, а не менеджера (см. его же докстринг и `services/daily_digest.py`, тот же вывод для
вечерней сводки менеджерам: соседей с тихими часами на manager-facing уведомлениях в проекте
нет — не изобретаем).
"""
import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from config import config
from database.db import get_city_counts, get_pending_count, get_setting, get_staff_city
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


async def _pending_breakdown_suffix(total: int) -> str:
    """`" (Город — N, ...)"` для непривязанного получателя — или `""`, если разбивка не несёт
    новой информации (модуль городов выключен, в реестре меньше двух городов, счётчик уже 0)
    или в ней нечего показывать (все города нулевые — не должно случаться при total > 0, но
    fail-soft на случай рассинхрона). Города с нулём в разбивку не попадают. NULL/незнакомый
    `event_city` сворачивается в дефолтный город тем же приёмом, что «По городам» у
    `render_stats_text` (handlers/admin.py) — db.py не может импортировать `cities`, поэтому
    свёртка всегда на стороне вызывающего."""
    from cities import CITIES, cities_module_on, city_label, normalize_city

    if total <= 0 or len(CITIES) <= 1 or not await cities_module_on():
        return ""
    rows = await get_city_counts()
    per_city: dict[str, int] = {}
    for raw_city, _total, pending, _approved in rows:
        code = normalize_city(raw_city)
        per_city[code] = per_city.get(code, 0) + (pending or 0)
    parts = []
    for c in CITIES:
        n = per_city.get(c["code"], 0)
        if n > 0:
            label = await city_label(c["code"])
            parts.append(f"{label} — {n}")
    if not parts:
        return ""
    return f" ({', '.join(parts)})"


async def _text_for_recipient(uid: int) -> str | None:
    """`None` — этому получателю сейчас нечего слать (его счётчик — 0). Иначе готовый текст.

    Owner correction (тот же день, квик 260919): ПРИВЯЗАННЫЙ к городу (`staff.city`, не
    суперадмин — D-12) получает счётчик СВОЕГО города; любой другой (staff без города,
    ADMIN_IDS) — ВСЕГДА общее число + разбивка по городам, независимо от того, что у него
    выбрано в шапке панели (`cities.admin_selected_city` здесь намеренно не читается)."""
    from cities import cities_module_on, city_label, city_scope, normalize_city

    bound = None
    if uid not in config.ADMIN_IDS and await cities_module_on():
        bound = await get_staff_city(uid)

    if bound:
        code = normalize_city(bound)
        count = await get_pending_count(city_scope=city_scope(code))
        if count <= 0:
            return None
        label = await city_label(code)
        return f"📋 Заявок в ожидании ({label}): {count}. Открой /admin → Заявки."

    count = await get_pending_count()
    if count <= 0:
        return None
    suffix = await _pending_breakdown_suffix(count)
    return f"📋 Заявок в ожидании: {count}{suffix}. Открой /admin → Заявки."


async def pending_reminder_loop(bot):
    """Forever: if enabled, ping every current `moderate_reg` holder (D-13 fan-out — ADMIN_IDS
    plus every staff `reg_manager`-family role, deduped, see module docstring), each with THEIR
    OWN text (`_text_for_recipient`), then sleep the configured interval. Fail-soft per
    iteration and per recipient send.

    Lazy import (module docstring precedent — `reg_digest`/`game_digest`/`daily_digest` all do
    the same): `handlers.admin_caps` imports back into `handlers`, and `main.py` imports this
    module at top level before `handlers` is guaranteed loaded."""
    from handlers.admin_caps import capability_holders

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
                        text = await _text_for_recipient(uid)
                        if text is None:
                            continue  # ничего не ждёт этого получателя — не будим зря
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
