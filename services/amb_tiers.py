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
`handlers/reg/reg_ambassador.py`, возврат `handlers/user_actions.py::ambassador_join`, Mini App
`miniapp/routers/form.py::draft_ambassador`).

Сторож `tests/test_referral_credit_32.py::test_every_credit_call_site_also_checks_tiers`
валит сборку, если новый путь зовёт начисление без проверки ступеней.

Уведомление амбассадору ставится событием `amb_tier_reached` в `miniapp_outbox` — одним путём
для обоих процессов: веб-процесс Mini App сам писать в Telegram не может, а очередь разбирает
только бот (`services/infra/miniapp_outbox.py` -> `services/amb_tiers_notify.py`). Ставится голым
`database.db.enqueue_miniapp_outbox`, не `miniapp.outbox.enqueue`: зависимость services ->
miniapp запрещена.

Модуль aiogram-free и fail-soft: сбой проверки ступеней логируется и никогда не отменяет уже
состоявшееся одобрение.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from shared.amb_tier_keys import MAX_TIERS, tier_key
from database import amb_tiers_db
from database import db as _db
from services.infra.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

TIER_EVENT_KIND = "amb_tier_reached"
DEADLINE_FORMAT = "%Y-%m-%d %H:%M"
_STAMP = "%Y-%m-%d %H:%M:%S"


def can_earn_tiers(user: dict | None, season: str, *, require_approved: bool = True) -> bool:
    """Кто получает ступени: амбассадор (`is_ambassador = 1`) с ОДОБРЕННОЙ собственной заявкой
    ТЕКУЩЕГО сезона (решение владельца 30.09). Кнопку «Хочу свою ссылку» может нажать любой, но
    ступени откроются, только когда одобрят его самого, — иначе отклонённый или ещё не
    рассмотренный делегат собирал бы награды. Приглашённые при этом считаются за весь сезон,
    в том числе пришедшие до вступления.

    `require_approved=False` (галочка события `amb_tiers_require_approved`) снимает требование
    одобренной собственной заявки — остаётся только «амбассадор». Значение читает вызывающий
    (`require_approved_on()`), функция остаётся синхронной и чистой."""
    if not user or int(user.get("is_ambassador") or 0) != 1:
        return False
    if not require_approved:
        return True
    return user.get("status") == "approved" and (user.get("season") or "") == (season or "")


async def program_on() -> bool:
    return await get_setting_typed("amb_qualified_program") == "on"


async def require_approved_on() -> bool:
    """Галочка события «ступени только амбассадору с одобренной заявкой» (по умолчанию включена)."""
    return await get_setting_typed("amb_tiers_require_approved") != "off"


@dataclass(frozen=True)
class TierCfg:
    """Настройка одной ступени: порог, ключи текста/листа ожидания/«следующего шага» и квота
    (`None` — квота на этой ступени выключена)."""
    n: int
    threshold: int
    text_key: str
    quota: int | None
    waitlist_key: str
    next_key: str


async def tiers_count() -> int:
    raw = int(await get_setting_typed("amb_tiers_count"))
    return max(1, min(MAX_TIERS, raw))


async def tiers_config() -> list[TierCfg]:
    """Ступени 1..`amb_tiers_count` по возрастанию."""
    result: list[TierCfg] = []
    for n in range(1, await tiers_count() + 1):
        quota_on = await get_setting_typed(tier_key(n, "quota_on")) == "on"
        quota = int(await get_setting_typed(tier_key(n, "quota"))) if quota_on else None
        result.append(TierCfg(
            n=n,
            threshold=int(await get_setting_typed(tier_key(n, "threshold"))),
            text_key=tier_key(n, "text"),
            quota=quota,
            waitlist_key=tier_key(n, "waitlist"),
            next_key=tier_key(n, "next"),
        ))
    return result


async def thresholds() -> tuple[int, int, int]:
    """Пороги ступеней 1/2/3 (int реестра: мусор и ≤0 уже дают дефолт). Совместимость для
    вызовов, которым нужны ровно три порога."""
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
    опечатка менеджера не должна лишать амбассадоров ступеней.

    Минута дедлайна входит целиком: «23:59» значит «до 23:59:59 включительно» — так это
    понимает менеджер, а `msk_now()` с секундами иначе закрывал бы приём в 23:59:00."""
    raw = ((await get_setting_typed("amb_count_deadline")) or "").strip()
    if not raw:
        return False
    try:
        deadline = datetime.strptime(raw, DEADLINE_FORMAT)
    except ValueError:
        logger.warning("amb_tiers: не разобрал amb_count_deadline — считаю, что дедлайна нет")
        return False
    return (now or msk_now()) >= deadline + timedelta(minutes=1)


O2O_TIER = 2


def notify_tiers_for(new_tiers, quota_tiers=None) -> list[int]:
    """О каких из новых ступеней сообщить, по возрастанию: старшая всегда, каждая ступень с
    квотой — всегда, если она среди новых (её текст говорит, слот у человека или лист
    ожидания). Остальные младшие ступени при прыжке выше не сообщаются. `quota_tiers` по
    умолчанию — ступень 2 (прежнее поведение для вызовов без конфигурации)."""
    tiers = {int(t) for t in new_tiers}
    if not tiers:
        return []
    quoted = {O2O_TIER} if quota_tiers is None else {int(t) for t in quota_tiers}
    return sorted({max(tiers)} | (quoted & tiers))


def _left_to_next(cfg: list[TierCfg], qualified: int) -> int:
    """`{left}` уведомления: сколько прошедших отбор нужно до «главной» награды. Если на
    событии есть ступень с квотой — это её порог (прежнее поведение: «до разбора резюме»;
    0, когда порог взят). Без квот — порог ближайшей недостигнутой ступени (0 — все взяты)."""
    quoted = [c for c in cfg if c.quota is not None]
    if quoted:
        return max(quoted[0].threshold - qualified, 0)
    pending = [c.threshold for c in cfg if c.threshold > qualified]
    return (min(pending) - qualified) if pending else 0


async def check_tiers(referrer_ids, *, notify: bool = True, now: datetime | None = None,
                      force: bool = False) -> list[dict]:
    """Выдаёт амбассадорам достигнутые ступени. Возвращает только НОВЫЕ:
    `[{"telegram_id", "tier", "o2o_status"}]`.

    `force=True` игнорирует тумблер (только для разового бэкафилла) — дедлайн действует всегда.
    `notify=False` — тихо: все новые ступени сразу помечаются уведомлёнными.

    Несколько ступеней за раз (0 -> 3 одним «Принять всех», бэкафилл): строки пишутся для
    каждой, а уведомления — для старшей и, если среди новых есть ступень разбора резюме, ещё и
    для неё (слот или лист ожидания — это обещание, которого нет в тексте старшей ступени).
    Так «старшая + каждая ступень с квотой среди новых»; остальные младшие ступени
    помечаются уведомлёнными сразу, без сообщения: иначе человек получил бы «до следующей
    осталось 0» следом за «слот за тобой»."""
    if not force and not await program_on():
        return []
    if await deadline_passed(now):
        return []
    cfg = await tiers_config()
    quotas = {c.n: c.quota for c in cfg if c.quota is not None}
    require_approved = await require_approved_on()
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
            if not can_earn_tiers(referrer, season, require_approved=require_approved):
                continue
            counts = await amb_tiers_db.referral_counts(rid, season)
            qualified = counts["qualified"]
            reached = [c.n for c in cfg if qualified >= c.threshold]
            if not reached:
                continue
            new_rows = await amb_tiers_db.claim_new_tiers(rid, reached, stamp, quotas, season=season)
            if not new_rows:
                continue
            for row in new_rows:
                result.append({"telegram_id": rid, **row})
            new_tiers = [r["tier"] for r in new_rows]
            if not notify:
                await amb_tiers_db.mark_tiers_notified(rid, new_tiers, stamp)
                continue
            notify_tiers = notify_tiers_for(new_tiers, quotas)
            lower = [t for t in new_tiers if t not in notify_tiers]
            if lower:
                await amb_tiers_db.mark_tiers_notified(rid, lower, stamp)
            for tier in notify_tiers:
                try:
                    await _db.enqueue_miniapp_outbox(
                        TIER_EVENT_KIND,
                        {"telegram_id": rid, "tier": tier, "left": _left_to_next(cfg, qualified)},
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
    """Обёртка для путей одобрения: по id одобренных находит их амбассадоров (и самих
    одобренных, если они амбассадоры) и зовёт `check_tiers`. Никогда не бросает. При выключенной программе — одно чтение
    настройки и выход, без запросов к пользователям."""
    try:
        if not await program_on():
            return
        referrers: list[int] = []
        for raw_id in invitee_ids or ():
            invitee = await _db.get_user(int(raw_id))
            if not invitee:
                continue
            # Одобрили самого амбассадора — его люди могли пройти отбор раньше него.
            if int(invitee.get("is_ambassador") or 0) == 1 and int(raw_id) not in referrers:
                referrers.append(int(raw_id))
            if not invitee.get("referrer_id"):
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


async def revoke_tier(telegram_id: int, tier: int, *, by: int | None) -> bool:
    """Ручное «снять ступень» (решение менеджера, например за накрутку): строка ступени
    удаляется, выданное место квоты освобождается. Человеку ничего не шлётся. `False` — такой
    ступени у него уже нет (повторное нажатие).

    Снятие «липкое»: в той же транзакции пишется метка `amb_tier_revocations` (сезон
    текущий), и автоматика (`check_tiers`, сверка, бэкафилл) не выдаёт эту ступень и все
    старшие, пока менеджер не вернёт её кнопкой (`unrevoke_tier`). Старшие блокируются
    тоже: снятую за накрутку ступень не должна «заменять» следующая."""
    season = await current_season()
    row = await amb_tiers_db.revoke_tier_sticky(
        int(telegram_id), int(tier), season, by=by, at=msk_now().strftime(_STAMP))
    if row is None:
        return False
    logger.info("admin=%s amb_tier_revoke tid=%s tier=%s status=%s",
                by, int(telegram_id), int(tier), row.get("o2o_status"))
    return True


async def unrevoke_tier(telegram_id: int, tier: int, *, by: int | None) -> list[dict]:
    """«Вернуть ступень»: снимает метку и сразу прогоняет проверку ступеней человека.
    Возвращает выданные сейчас ступени (пусто — метки уже не было или порога не хватает).
    Уведомление уходит обычным путём."""
    season = await current_season()
    if not await amb_tiers_db.remove_revocation(int(telegram_id), int(tier), season):
        return []
    logger.info("admin=%s amb_tier_unrevoke tid=%s tier=%s", by, int(telegram_id), int(tier))
    return await check_tiers([int(telegram_id)])


async def promote_waitlist(tier: int, *, by: int | None) -> str:
    """Ручная выдача освободившегося места ступени первому из листа ожидания (автопродвижения
    нет). Возвращает `"promoted:<telegram_id>"`, `"no_slot"` (места нет), `"empty"` (лист пуст)
    или `"no_quota"` (у ступени нет квоты или её нет в лестнице). После повышения в очередь
    ставится событие `amb_tier_reached`: доставка возьмёт текст ступени по статусу `granted`."""
    tier = int(tier)
    cfg = next((c for c in await tiers_config() if c.n == tier), None)
    if cfg is None or cfg.quota is None:
        return "no_quota"
    stamp = msk_now().strftime(_STAMP)
    promoted = await amb_tiers_db.promote_first_waitlisted(tier, cfg.quota, at=stamp)
    if promoted is None:
        return "no_slot" if await amb_tiers_db.waitlist_count(tier) else "empty"
    logger.info("admin=%s amb_tier_promote tid=%s tier=%s", by, promoted, tier)
    try:
        counts = await amb_tiers_db.referral_counts(promoted, await current_season())
        await _db.enqueue_miniapp_outbox(
            TIER_EVENT_KIND,
            {"telegram_id": promoted, "tier": tier,
             "left": _left_to_next(await tiers_config(), counts["qualified"])},
            stamp,
        )
    except Exception:
        # Сверка раз в 10 минут заново поставит уведомление: notified_at пуст.
        logger.warning("amb_tiers: уведомление о выданном месте не поставлено (tid=%s)",
                       promoted, exc_info=True)
    return f"promoted:{promoted}"


STALE_NOTIFY_MINUTES = 10


async def reconcile_tiers() -> dict:
    """Периодическая сверка (раз в 10 минут, второй шаг джобы сверки журнала). Никогда не бросает.

    1. `check_tiers(notify=True)` по действующим амбассадорам с приглашёнными — дозаписывает
       ступени, которые пропустил живой путь (упавший процесс, одобрение в обход).
    2. Ступени без `notified_at`, достигнутые больше 10 минут назад, заново ставятся в
       очередь, если необработанного события про них нет (событие могло потеряться). Очередь
       и `claim_tier_notification` держат exactly-once: повторное событие второго
       сообщения не даёт.

    Возвращает `{"granted": сколько новых ступеней, "requeued": сколько уведомлений
    поставлено заново}`."""
    result = {"granted": 0, "requeued": 0}
    try:
        if not await program_on():
            return result
        season = await current_season()
        ids = await amb_tiers_db.active_ambassadors_with_invitees(season)
        if ids:
            result["granted"] = len(await check_tiers(ids, notify=True))
        stamp_now = msk_now()
        older = (stamp_now - timedelta(minutes=STALE_NOTIFY_MINUTES)).strftime(_STAMP)
        cfg = await tiers_config()
        for row in await amb_tiers_db.stale_unnotified(older):
            try:
                rid, tier = int(row["telegram_id"]), int(row["tier"])
                if await amb_tiers_db.has_pending_tier_event(TIER_EVENT_KIND, rid, tier):
                    continue
                counts = await amb_tiers_db.referral_counts(rid, season)
                await _db.enqueue_miniapp_outbox(
                    TIER_EVENT_KIND,
                    {"telegram_id": rid, "tier": tier, "left": _left_to_next(cfg, counts["qualified"])},
                    stamp_now.strftime(_STAMP),
                )
                result["requeued"] += 1
            except Exception:
                logger.warning("amb_tiers: сверка не поставила уведомление (tid=%s)",
                               row.get("telegram_id"), exc_info=True)
    except Exception:
        logger.exception("amb_tiers: сверка ступеней не прошла")
    return result


async def preview_backfill() -> list[dict]:
    """Предпросмотр разового пересчёта «кому какая ступень» — ничего не пишет.

    Список амбассадоров (сейчас `is_ambassador = 1`), которым положена хотя бы одна НОВАЯ
    ступень, в том порядке, в каком `tools/backfill_amb_tiers.py --apply` будет их
    обрабатывать: по времени, когда амбассадор набрал порог первой достигнутой ступени с
    квотой (одобрение N-го прошедшего отбор; квот нет или не дотянул — по порогу первой
    ступени), при равенстве — по id. Этот же порядок — порядок раздачи квоты, поэтому прогноз
    «выдан / лист ожидания» совпадает с тем, что запишет `--apply` (он зовёт тот же
    `check_tiers` по одному в этом порядке).

    Элемент: `{"telegram_id", "username", "qualified", "tiers": [{"tier", "o2o_status",
    "exists"}]}` — `exists=True` у ступеней, которые уже выданы. Дедлайн прошёл — `[]`."""
    if await deadline_passed():
        return []
    cfg = await tiers_config()
    quotas = {c.n: c.quota for c in cfg if c.quota is not None}
    require_approved = await require_approved_on()
    season = await current_season()
    times = await amb_tiers_db.qualified_approval_times(season)
    existing: dict[int, dict[int, dict]] = {}
    for row in await amb_tiers_db.list_tiers():
        existing.setdefault(int(row["telegram_id"]), {})[int(row["tier"])] = row

    floors = await amb_tiers_db.revoked_floors(season)
    first_threshold = cfg[0].threshold
    candidates = []
    for rid, approvals in times.items():
        qualified = len(approvals)
        if qualified < first_threshold:
            continue
        user = await _db.get_user(rid)
        if not can_earn_tiers(user, season, require_approved=require_approved):
            continue
        floor = floors.get(rid)
        reached = [c.n for c in cfg if qualified >= c.threshold and (floor is None or c.n < floor)]
        have = existing.get(rid, {})
        if all(t in have for t in reached):
            continue
        quoted = [c for c in cfg if c.n in quotas and qualified >= c.threshold]
        key_threshold = quoted[0].threshold if quoted else first_threshold
        candidates.append((approvals[key_threshold - 1], rid, user, qualified, reached, have))
    candidates.sort(key=lambda c: (c[0], c[1]))

    summary = await amb_tiers_db.quota_summary()
    granted = {t: summary.get(t, {}).get("granted", 0) for t in quotas}
    result = []
    for _, rid, user, qualified, reached, have in candidates:
        tiers = []
        for tier in reached:
            if tier in have:
                tiers.append({"tier": tier, "o2o_status": have[tier].get("o2o_status"), "exists": True})
                continue
            status = None
            if tier in quotas:
                status = "granted" if granted[tier] < quotas[tier] else "waitlist"
                if status == "granted":
                    granted[tier] += 1
            tiers.append({"tier": tier, "o2o_status": status, "exists": False})
        result.append({
            "telegram_id": rid,
            "username": (user.get("username") or "").strip().lstrip("@"),
            "qualified": qualified,
            "tiers": tiers,
        })
    return result
