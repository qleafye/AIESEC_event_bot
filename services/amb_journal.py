"""Журнал зачётов приглашённых и единственная точка «приглашённого одобрили».

До журнала в путях одобрения (бот одиночное и площадка, оба «Принять всех», Mini App после
окна отмены, авто-одобрение) стояли три разных вызова подряд: начисление баллов, проверка
ступеней, выдача места собственной заявке амбассадора. Теперь все пути зовут одну
`on_invitees_approved` — она пишет журнал и сама вызывает остальное. Сторож в
`tests/test_referral_credit_32.py` (AST) не даёт вернуть прямые вызовы.

Строка журнала пишется приглашённому ЛЮБОГО делегата (с отметкой, был ли пригласивший
амбассадором), баллы — только если пригласивший active-амбассадор в момент одобрения, не
исключён и `ambassador_referral_coins` > 0. Повторный вызов баллы второй раз не начисляет:
идемпотентность держит PRIMARY KEY `referral_credits.invitee_id`.

Весь модуль fail-soft: одобрение уже записано до вызова, сбой журнала его не отменяет.
Зависимости — без aiogram: модуль тянут веб-процесс Mini App и бот.
"""
from __future__ import annotations

import logging

from database import amb_journal_db
from database import db as _db
from services.ambassador_waves import current_wave_for_city_raw, wave_eligible
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)


def _invitee_reason(invitee: dict) -> str:
    """«Приглашённый: Имя Фамилия» для истории монет амбассадора — начисление видно человеку,
    а не голой суммой."""
    name = (invitee.get("full_name") or "").strip() or "Без имени"
    return f"Приглашённый: {name}"


async def _is_excluded(invitee_id: int) -> bool:
    from database import amb_tiers_db
    return await amb_tiers_db.get_exclusion(invitee_id) is not None


async def _record_one(invitee_id: int, *, changed_by: int | None, source: str) -> dict | None:
    """Пишет строку журнала одному приглашённому. Возвращает данные начисления, если баллы
    действительно начислены сейчас, иначе None (строка при этом могла быть записана)."""
    try:
        invitee = await _db.get_user(int(invitee_id))
        if not invitee or invitee.get("status") != "approved":
            return None
        referrer_raw = invitee.get("referrer_id")
        if not referrer_raw:
            return None
        referrer_id = int(referrer_raw)
        if referrer_id == int(invitee_id):
            return None  # самореферал
        referrer = await _db.get_user(referrer_id)
        if not referrer:
            return None

        was_ambassador = int(referrer.get("is_ambassador") or 0) == 1
        excluded = await _is_excluded(int(invitee_id))
        coins = 0
        wave_id: int | None = None
        if was_ambassador and not excluded:
            coins = max(int(await get_setting_typed("ambassador_referral_coins") or 0), 0)
        if coins > 0:
            wave = await current_wave_for_city_raw(referrer.get("event_city"))
            if wave and wave_eligible(referrer, wave):
                wave_id = int(wave["id"])
        season = ((await _db.get_setting("event_season")) or "").strip() or None

        outcome = await amb_journal_db.record_approval(
            int(invitee_id), referrer_id, coins=coins, wave_id=wave_id, season=season,
            referrer_was_ambassador=was_ambassador, excluded=excluded,
            reason=_invitee_reason(invitee), source=source, changed_by=changed_by,
        )
        if outcome != "new" or coins <= 0:
            return None
        return {
            "referrer_id": referrer_id, "coins": coins, "wave_id": wave_id,
            "invitee_name": (invitee.get("full_name") or "").strip() or "Без имени",
        }
    except Exception:
        logger.exception("amb_journal: строка журнала не записана (invitee_id=%s)", invitee_id)
        return None


async def on_invitees_approved(invitee_ids, *, changed_by: int | None = None,
                               source: str = "approval") -> dict:
    """Единая точка «приглашённых одобрили»: журнал + баллы, затем место собственной заявке
    амбассадора и ступени. Сводка для менеджера: `{"credited", "coins", "ambassadors"}`."""
    ids = [int(i) for i in (invitee_ids or ())]
    credited = 0
    coins_total = 0
    ambassadors: set[int] = set()
    for invitee_id in ids:
        result = await _record_one(invitee_id, changed_by=changed_by, source=source)
        if result:
            credited += 1
            coins_total += result["coins"]
            ambassadors.add(result["referrer_id"])
    if ids:
        try:
            from services.amb_status import on_applications_approved
            await on_applications_approved(ids)
        except Exception:
            logger.exception("amb_journal: выдача места не прошла")
        try:
            from services.amb_tiers import check_tiers_for_invitees
            await check_tiers_for_invitees(ids)
        except Exception:
            logger.exception("amb_journal: проверка ступеней не прошла")
    return {"credited": credited, "coins": coins_total, "ambassadors": len(ambassadors)}


def _stamp(delta_hours: int = 0) -> str:
    from datetime import timedelta
    from services.timeutil import msk_now
    return (msk_now() - timedelta(hours=delta_hours)).strftime("%Y-%m-%d %H:%M:%S")


async def sync_revocations(invitee_ids=None) -> dict:
    """Отзыв зачёта у потерявших одобрение и снятие отзыва у одобренных снова. Баллы и
    выданные ступени не трогает. Fail-soft."""
    revoked = cleared = 0
    try:
        revoked = await amb_journal_db.mark_revoked(invitee_ids, at=_stamp())
        cleared = await amb_journal_db.clear_revoked(invitee_ids)
        if revoked or cleared:
            logger.info("amb_journal: отозвано %s, возвращено %s", revoked, cleared)
    except Exception:
        logger.exception("amb_journal: синхронизация отзывов не прошла")
    return {"revoked": revoked, "cleared": cleared}


RECONCILE_WINDOW_HOURS = 72


async def reconcile() -> dict:
    """Периодическая сверка: отзывы по факту статуса, недостающие строки журнала за последние
    72 часа (source 'reconcile') и места амбассадоров. Каждый шаг в своём try, не бросает."""
    result: dict = {"revoked": 0, "cleared": 0, "added": 0, "slots_released": 0}
    result.update(await sync_revocations())
    try:
        season = ((await _db.get_setting("event_season")) or "").strip() or None
        missing = await amb_journal_db.missing_recent(_stamp(RECONCILE_WINDOW_HOURS), season)
        if missing:
            await on_invitees_approved(missing, source="reconcile")
            result["added"] = len(missing)
            logger.info("amb_journal: сверка дописала %s строк", len(missing))
    except Exception:
        logger.exception("amb_journal: сверка не дописала недостающие строки")
    try:
        from services import amb_status
        result["slots_released"] = await amb_status.reconcile_slots()
    except Exception:
        logger.exception("amb_journal: сверка мест не прошла")
    return result


async def _name_of(invitee_id: int) -> str:
    user = await _db.get_user(int(invitee_id))
    return ((user or {}).get("full_name") or "").strip() or "Без имени"


def _fill(template: str, name: str) -> str:
    return (template or "").replace("{name}", name)


async def exclude(invitee_id: int, *, by: int | None, reason: str) -> bool:
    """Исключение за накрутку: причина, отметка в журнале, баллы списываются обратной строкой
    монет. Ступени не трогает («Снять ступень» — отдельная кнопка). False — уже исключён."""
    name = await _name_of(invitee_id)
    template = await get_setting_typed("amb_referral_reversal_reason_text")
    result = await amb_journal_db.exclude_atomic(
        int(invitee_id), reason=reason, by=by, at=_stamp(),
        reversal_reason=_fill(template, name),
    )
    if result is None:
        return False
    logger.info("admin=%s amb_exclude invitee=%s referrer=%s coins=%s",
                by, invitee_id, result["referrer_id"], result["coins"])
    return True


async def unexclude(invitee_id: int, *, by: int | None) -> bool:
    """Возврат в зачёт: баллы возвращаются новой строкой. False — уже возвращён."""
    name = await _name_of(invitee_id)
    template = await get_setting_typed("amb_referral_restore_reason_text")
    result = await amb_journal_db.unexclude_atomic(
        int(invitee_id), by=by, at=_stamp(), restore_reason=_fill(template, name),
    )
    if result is None:
        return False
    logger.info("admin=%s amb_unexclude invitee=%s referrer=%s coins=%s",
                by, invitee_id, result["referrer_id"], result["coins"])
    return True
