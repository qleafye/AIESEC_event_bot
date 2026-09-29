"""Ступени амбассадоров СкиллАп: одна точка проверки «пора ли выдать ступень».

Засчитываются только приглашённые, ПРОШЕДШИЕ ОТБОР (определения — докстринг
`database/amb_tiers_db.py`). Пороги, квота слотов разбора резюме, дедлайн и тексты — ключи
группы `game` реестра (`amb_*`). Главный тумблер `amb_qualified_program` выключен по
умолчанию: при `off` `check_tiers` выходит сразу, не читает и не пишет ни одной строки.

Почему не внутри `services.referrals.credit_for_approved`: та выходит раньше при
`ambassador_referral_coins = 0` (дефолт), а ступени от баллов не зависят. Поэтому проверку
зовут РЯДОМ с начислением, ленивым импортом, в каждом пути одобрения заявки:

- `services.applications.record_decision(..., effects_already_sent=True)` — одиночное
  одобрение в боте и вход на площадке (`services/onsite_reg.py`);
- `services.applications.flush_due_decisions` — одиночное одобрение в Mini App, только когда
  окно «Отменить» прошло (отменённое решение туда не доходит — ступени за него нет);
- `services.applications.claim_approve_all_with_credits` — «Принять всех» в боте и Mini App;
- `services/reg_finalize.py` — авто-одобрение на финале анкеты.

Сторож `tests/test_referral_credit_32.py::test_every_credit_call_site_also_checks_tiers`
валит сборку, если новый путь зовёт начисление без проверки ступеней.

Уведомление амбассадору ставится событием `amb_tier_reached` в `miniapp_outbox` — одним путём
для обоих процессов: веб-процесс Mini App сам писать в Telegram не может, а очередь разбирает
только бот (`services/miniapp_outbox.py` -> `services/amb_tiers_notify.py`). Ставится голым
`database.db.enqueue_miniapp_outbox`, не `miniapp.outbox.enqueue`: зависимость services ->
miniapp запрещена.

Модуль aiogram-free и fail-soft: сбой проверки ступеней логируется и никогда не отменяет уже
состоявшееся одобрение.
"""
from __future__ import annotations

import logging
from datetime import datetime

from database import amb_tiers_db
from database import db as _db
from services.timeutil import msk_now
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

TIER_EVENT_KIND = "amb_tier_reached"
DEADLINE_FORMAT = "%Y-%m-%d %H:%M"
_STAMP = "%Y-%m-%d %H:%M:%S"


async def program_on() -> bool:
    return await get_setting_typed("amb_qualified_program") == "on"


async def thresholds() -> tuple[int, int, int]:
    """Пороги ступеней 1/2/3 (int реестра: мусор и ≤0 уже дают дефолт)."""
    return (
        int(await get_setting_typed("amb_tier1_threshold")),
        int(await get_setting_typed("amb_tier2_threshold")),
        int(await get_setting_typed("amb_tier3_threshold")),
    )


async def current_season() -> str:
    return ((await _db.get_setting("event_season")) or "").strip()


async def deadline_passed(now: datetime | None = None) -> bool:
    """Прошёл ли дедлайн выдачи ступеней (МСК). Пусто = дедлайна нет. Значение, записанное
    в обход валидатора и не разбираемое, тоже = дедлайна нет (с предупреждением в лог):
    опечатка менеджера не должна лишать амбассадоров ступеней."""
    raw = ((await get_setting_typed("amb_count_deadline")) or "").strip()
    if not raw:
        return False
    try:
        deadline = datetime.strptime(raw, DEADLINE_FORMAT)
    except ValueError:
        logger.warning("amb_tiers: не разобрал amb_count_deadline — считаю, что дедлайна нет")
        return False
    return (now or msk_now()) > deadline


async def check_tiers(referrer_ids, *, notify: bool = True, now: datetime | None = None,
                      force: bool = False) -> list[dict]:
    """Выдаёт амбассадорам достигнутые ступени. Возвращает только НОВЫЕ:
    `[{"telegram_id", "tier", "o2o_status"}]`.

    `force=True` игнорирует тумблер (только для разового бэкафилла) — дедлайн действует всегда.
    `notify=False` — тихо: все новые ступени сразу помечаются уведомлёнными.

    Несколько ступеней за раз (0 -> 3 одним «Принять всех»): строки пишутся для каждой, а
    уведомление ставится только для старшей — младшие сразу помечаются уведомлёнными, иначе
    человек получил бы «до разбора резюме осталось 0» следом за «слот за тобой»."""
    if not force and not await program_on():
        return []
    if await deadline_passed(now):
        return []
    t1, t2, t3 = await thresholds()
    quota = int(await get_setting_typed("amb_o2o_quota"))
    season = await current_season()
    stamp = (now or msk_now()).strftime(_STAMP)

    result: list[dict] = []
    seen: set[int] = set()
    for raw_id in referrer_ids or ():
        try:
            rid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if rid in seen:
            continue
        seen.add(rid)
        try:
            referrer = await _db.get_user(rid)
            if not referrer or int(referrer.get("is_ambassador") or 0) != 1:
                continue
            counts = await amb_tiers_db.referral_counts(rid, season)
            qualified = counts["qualified"]
            reached = [t for t, th in ((1, t1), (2, t2), (3, t3)) if qualified >= th]
            if not reached:
                continue
            new_rows = await amb_tiers_db.claim_new_tiers(rid, reached, stamp, quota)
            if not new_rows:
                continue
            for row in new_rows:
                result.append({"telegram_id": rid, **row})
            new_tiers = [r["tier"] for r in new_rows]
            if not notify:
                await amb_tiers_db.mark_tiers_notified(rid, new_tiers, stamp)
                continue
            top = max(new_tiers)
            lower = [t for t in new_tiers if t != top]
            if lower:
                await amb_tiers_db.mark_tiers_notified(rid, lower, stamp)
            try:
                await _db.enqueue_miniapp_outbox(
                    TIER_EVENT_KIND,
                    {"telegram_id": rid, "tier": top, "left": max(t2 - qualified, 0)},
                    stamp,
                )
            except Exception:
                logger.warning(
                    "amb_tiers: уведомление о ступени %s не поставлено в очередь (tid=%s)",
                    top, rid, exc_info=True,
                )
        except Exception:
            logger.exception("amb_tiers: проверка ступеней не прошла (tid=%s)", rid)
    return result


async def check_tiers_for_invitees(invitee_ids) -> None:
    """Обёртка для путей одобрения: по id одобренных приглашённых находит их амбассадоров и
    зовёт `check_tiers`. Никогда не бросает. При выключенной программе — одно чтение
    настройки и выход, без запросов к пользователям."""
    try:
        if not await program_on():
            return
        referrers: list[int] = []
        for raw_id in invitee_ids or ():
            invitee = await _db.get_user(int(raw_id))
            if not invitee or not invitee.get("referrer_id"):
                continue
            rid = int(invitee["referrer_id"])
            if rid == int(raw_id) or rid in referrers:
                continue
            referrers.append(rid)
        if referrers:
            await check_tiers(referrers)
    except Exception:
        logger.exception("amb_tiers: check_tiers_for_invitees не прошла")
