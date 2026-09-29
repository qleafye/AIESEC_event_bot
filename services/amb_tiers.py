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

И отдельно — при вступлении в амбассадоры (`check_tiers_for_new_ambassador`: бот
`handlers/reg_ambassador.py`, возврат `handlers/user_actions.py::ambassador_join`, Mini App
`miniapp/routers/form.py::draft_ambassador`).

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


O2O_TIER = 2


def notify_tiers_for(new_tiers) -> list[int]:
    """О каких из новых ступеней сообщить, по возрастанию: старшая всегда, ступень разбора
    резюме — всегда, если она среди новых (её текст говорит, слот у человека или лист
    ожидания). Ступень 1 при прыжке выше не сообщается."""
    tiers = {int(t) for t in new_tiers}
    if not tiers:
        return []
    return sorted({max(tiers)} | ({O2O_TIER} & tiers))


async def check_tiers(referrer_ids, *, notify: bool = True, now: datetime | None = None,
                      force: bool = False) -> list[dict]:
    """Выдаёт амбассадорам достигнутые ступени. Возвращает только НОВЫЕ:
    `[{"telegram_id", "tier", "o2o_status"}]`.

    `force=True` игнорирует тумблер (только для разового бэкафилла) — дедлайн действует всегда.
    `notify=False` — тихо: все новые ступени сразу помечаются уведомлёнными.

    Несколько ступеней за раз (0 -> 3 одним «Принять всех», бэкафилл): строки пишутся для
    каждой, а уведомления — для старшей и, если среди новых есть ступень разбора резюме, ещё и
    для неё (слот или лист ожидания — это обещание, которого нет в тексте старшей ступени).
    Ступень 1 при прыжке помечается уведомлённой сразу: иначе человек получил бы «до разбора
    резюме осталось 0» следом за «слот за тобой». Так за раз не больше двух сообщений."""
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
            notify_tiers = notify_tiers_for(new_tiers)
            lower = [t for t in new_tiers if t not in notify_tiers]
            if lower:
                await amb_tiers_db.mark_tiers_notified(rid, lower, stamp)
            for tier in notify_tiers:
                try:
                    await _db.enqueue_miniapp_outbox(
                        TIER_EVENT_KIND,
                        {"telegram_id": rid, "tier": tier, "left": max(t2 - qualified, 0)},
                        stamp,
                    )
                except Exception:
                    logger.warning(
                        "amb_tiers: уведомление о ступени %s не поставлено в очередь (tid=%s)",
                        tier, rid, exc_info=True,
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


async def check_tiers_for_new_ambassador(telegram_id) -> None:
    """Человек только что стал амбассадором («Хочу свою ссылку» в боте или приложении,
    возврат кнопкой на «Моя ссылка»): ступени по приглашённым, одобренным ДО вступления, иначе
    пришли бы только со следующим одобрением — а его может и не быть. Никогда не бросает;
    при выключенной программе — одно чтение настройки."""
    try:
        if not await program_on():
            return
        await check_tiers([int(telegram_id)])
    except Exception:
        logger.exception("amb_tiers: проверка ступеней при вступлении не прошла (tid=%s)", telegram_id)


async def preview_backfill() -> list[dict]:
    """Предпросмотр разового пересчёта «кому какая ступень» — ничего не пишет.

    Список амбассадоров (сейчас `is_ambassador = 1`), которым положена хотя бы одна НОВАЯ
    ступень, в том порядке, в каком `tools/backfill_amb_tiers.py --apply` будет их
    обрабатывать: по времени, когда амбассадор набрал порог разбора резюме (одобрение
    `t2`-го прошедшего отбор; не дотянул — по порогу ступени 1), при равенстве — по id. Этот
    же порядок — порядок раздачи квоты, поэтому прогноз «выдан / лист ожидания» совпадает с
    тем, что запишет `--apply` (он зовёт тот же `check_tiers` по одному в этом порядке).

    Элемент: `{"telegram_id", "username", "qualified", "tiers": [{"tier", "o2o_status",
    "exists"}]}` — `exists=True` у ступеней, которые уже выданы. Дедлайн прошёл — `[]`."""
    if await deadline_passed():
        return []
    t1, t2, t3 = await thresholds()
    quota = int(await get_setting_typed("amb_o2o_quota"))
    season = await current_season()
    times = await amb_tiers_db.qualified_approval_times(season)
    existing: dict[int, dict[int, dict]] = {}
    for row in await amb_tiers_db.list_tiers():
        existing.setdefault(int(row["telegram_id"]), {})[int(row["tier"])] = row

    candidates = []
    for rid, approvals in times.items():
        qualified = len(approvals)
        if qualified < t1:
            continue
        user = await _db.get_user(rid)
        if not user or int(user.get("is_ambassador") or 0) != 1:
            continue
        reached = [t for t, th in ((1, t1), (2, t2), (3, t3)) if qualified >= th]
        have = existing.get(rid, {})
        if all(t in have for t in reached):
            continue
        key_idx = (t2 if qualified >= t2 else t1) - 1
        candidates.append((approvals[key_idx], rid, user, qualified, reached, have))
    candidates.sort(key=lambda c: (c[0], c[1]))

    granted = (await amb_tiers_db.o2o_summary())["granted"]
    result = []
    for _, rid, user, qualified, reached, have in candidates:
        tiers = []
        for tier in reached:
            if tier in have:
                tiers.append({"tier": tier, "o2o_status": have[tier].get("o2o_status"), "exists": True})
                continue
            o2o_status = None
            if tier == 2:
                o2o_status = "granted" if granted < quota else "waitlist"
                if o2o_status == "granted":
                    granted += 1
            tiers.append({"tier": tier, "o2o_status": o2o_status, "exists": False})
        result.append({
            "telegram_id": rid,
            "username": (user.get("username") or "").strip().lstrip("@"),
            "qualified": qualified,
            "tiers": tiers,
        })
    return result
