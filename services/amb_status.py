"""Правила входа в команду амбассадоров — одна точка для бота, Mini App и анкеты.

Пять поверхностей показа (вопрос анкеты в боте и Mini App, предложение после анкеты,
«Моя ссылка» в боте и в хабе приложения) спрашивают `offer_open()`, четыре точки действия
(ответ «да» в анкете, кнопка в боте, кнопка в приложении, возврат на «Моя ссылка») зовут
`request_join()`. Если каждая поверхность решит сама, они разъедутся — поэтому решения только
здесь.

Всё это — модуль «🤝 Отбор амбассадоров» (`amb_team_selection_enabled`, по умолчанию выключен).
Выключен — как было до модуля: вступление сразу по кнопке, без лимита и без кандидатов; статус
«кандидат» (в том числе перенесённый из ответа анкеты) и «отказано» ни на что не влияют, швы
одобрения ничего не делают, значения `amb_join_mode`/`amb_slots_limit` не читаются. Ступени
СкиллАп (`amb_qualified_program`) — отдельный тумблер, от этого не зависят.

Настройки события (группа `game` реестра):
- `amb_join_mode` — `instant` (по умолчанию, как раньше: сразу в команду) или `selection`
  (делегат становится кандидатом, в команду его берёт менеджер кнопкой «Взять» — `take`);
- `amb_slots_limit` — сколько мест («пакетов амбассадора»), 0 = без лимита (по умолчанию).

Место в лимите занимает только active-амбассадор с ОДОБРЕННОЙ собственной заявкой текущего
сезона и выдаётся только через `amb_status_db.try_claim_slot` (в `BEGIN IMMEDIATE`): при
вступлении, при `take` и при одобрении собственной заявки уже действующего амбассадора
(`on_applications_approved`). Без одобренной заявки человек в команде «без пакета» — со
ссылкой, заданиями, баллами и ступенями. Заявка перестала быть одобренной — место снимается
(`on_applications_unapproved`, хвосты ловит `reconcile_slots`); выданный пакет место держит.

`declined` (вежливо отказали) — терминал для самообслуживания в этом сезоне: предложения нет,
`request_join` ничего не пишет; вернуть может только менеджер через `take`.

Модуль aiogram-free. `offer_open` fail-open (сбой БД не прячет вопрос анкеты), действия
fail-soft там, где запись уже состоялась (ступени, место — логом, без исключения наружу).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from database import amb_status_db
from database import db as _db
from services.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

MODE_INSTANT = "instant"
MODE_SELECTION = "selection"

_STAMP = "%Y-%m-%d %H:%M:%S"

_ACTIVE = amb_status_db.STATUS_ACTIVE
_CANDIDATE = amb_status_db.STATUS_CANDIDATE
_LEFT = amb_status_db.STATUS_LEFT
_DECLINED = amb_status_db.STATUS_DECLINED
_NONE = amb_status_db.STATUS_NONE


@dataclass(frozen=True)
class JoinResult:
    """Итог просьбы о вступлении или «Взять». `outcome`:
    `active` / `already_active` / `candidate` / `already_candidate` / `declined` / `full` /
    `taken` / `no_user`. `slot` — выдано ли место в лимите этим действием."""
    outcome: str
    slot: bool = False


def _now() -> str:
    return msk_now().strftime(_STAMP)


# ── Настройки ────────────────────────────────────────────────────────────────────────────────

TOGGLE_KEY = "amb_team_selection_enabled"


async def selection_enabled() -> bool:
    """Включён ли модуль «🤝 Отбор амбассадоров». Всё, кроме явного `on`, — выключен."""
    return await get_setting_typed(TOGGLE_KEY) == "on"


async def join_mode() -> str:
    """`selection` или `instant`; всё прочее (мусор в обход валидатора) — `instant`. Модуль
    выключен — всегда `instant`, что бы ни лежало в настройке."""
    if not await selection_enabled():
        return MODE_INSTANT
    mode = await get_setting_typed("amb_join_mode")
    return MODE_SELECTION if mode == MODE_SELECTION else MODE_INSTANT


async def slots_limit() -> int:
    """Лимит мест, 0 — без лимита. Отрицательное и мусор — 0. Модуль выключен — 0."""
    if not await selection_enabled():
        return 0
    try:
        return max(0, int(await get_setting_typed("amb_slots_limit") or 0))
    except (TypeError, ValueError):
        return 0


async def slot_counter() -> tuple[int, int]:
    """(занято, лимит) — для счётчика «12 из 17» на экране менеджера."""
    return await amb_status_db.slots_taken(), await slots_limit()


async def slots_full() -> bool:
    """Лимит задан и набран. В БД ходит только при лимите > 0: вызывается из горячего пути
    анкеты, а у событий без лимита лишний COUNT на каждый шаг не нужен."""
    limit = await slots_limit()
    if limit <= 0:
        return False
    return await amb_status_db.slots_taken() >= limit


# ── Показывать ли предложение ────────────────────────────────────────────────────────────────

async def _offer_open(telegram_id: int | None) -> bool:
    if not await selection_enabled():
        return True  # как до модуля: предложение открыто всем
    if telegram_id is not None:
        st = await amb_status_db.get_status(int(telegram_id))
        status = (st or {}).get("status") or _NONE
        if status == _ACTIVE:
            # Действующий амбассадор: поверхность сама решает, что ему показать (ссылку,
            # прогресс) — лимит его не касается.
            return True
        if status == _DECLINED:
            return False
        if status == _CANDIDATE and await join_mode() == MODE_SELECTION:
            # Уже ждёт решения менеджера — повторно предлагать нечего.
            return False
    return not await slots_full()


async def offer_open(telegram_id: int | None = None) -> bool:
    """Показывать ли вопрос анкеты / кнопку «Хочу стать амбассадором». Без `telegram_id` —
    только по лимиту (анкета нового делегата). Fail-open: сбой чтения не должен прятать вопрос
    — лишний показ безопасен, `request_join` всё равно перепроверит."""
    try:
        return await _offer_open(telegram_id)
    except Exception:
        logger.exception("amb_status: offer_open не прошла (tid=%s) — показываю", telegram_id)
        return True


# ── Место в лимите ───────────────────────────────────────────────────────────────────────────

async def _try_slot(telegram_id: int) -> bool:
    """Выдать место, если человек active, его заявка одобрена в текущем сезоне и места есть.
    Fail-soft: статус уже записан, место доберёт следующее одобрение."""
    try:
        from services import amb_tiers
        return await amb_status_db.try_claim_slot(
            int(telegram_id), limit=await slots_limit(),
            season=await amb_tiers.current_season(), at=_now(),
        )
    except Exception:
        logger.exception("amb_status: выдача места не прошла (tid=%s)", telegram_id)
        return False


async def _credit_candidate_period(telegram_id: int, *, since: str, by: int) -> None:
    """Баллы за приглашённых, одобренных пока человек был кандидатом: тогда он ещё не был
    амбассадором, и журнал записал их без баллов. Так же догоняются ступени ниже. Fail-soft:
    человек уже в команде, сбой начисления это не отменяет."""
    try:
        from database import amb_journal_db
        coins = int(await get_setting_typed("ambassador_referral_coins") or 0)
        result = await amb_journal_db.credit_candidate_period(
            int(telegram_id), since=since, coins=coins, by=by, at=_now(),
            reason_tpl=await get_setting_typed("amb_referral_catchup_reason_text"),
        )
        if result["count"]:
            logger.info("amb_take_catchup tid=%s invitees=%s coins=%s", telegram_id,
                        result["count"], result["coins"])
    except Exception:
        logger.exception("amb_status: баллы за приглашённых до вступления не начислены (tid=%s)",
                         telegram_id)


async def _check_tiers(telegram_id: int) -> None:
    try:
        from services.amb_tiers import check_tiers_for_new_ambassador
        await check_tiers_for_new_ambassador(int(telegram_id))
    except Exception:
        logger.exception("amb_status: проверка ступеней при вступлении не прошла (tid=%s)",
                         telegram_id)


# ── Действия делегата ────────────────────────────────────────────────────────────────────────

async def _status(telegram_id: int) -> str | None:
    st = await amb_status_db.get_status(int(telegram_id))
    return None if st is None else st["status"]


async def request_join(telegram_id: int, *, source: str) -> JoinResult:
    """Делегат просится в команду (`source` — `form` / `button_bot` / `button_app` /
    `my_link`, только для лога). Всё перепроверяется здесь: устаревшая кнопка Telegram при
    набранном лимите или у отказанного ничего не пишет."""
    tid = int(telegram_id)
    result = await _request_join(tid)
    logger.info("amb_join tid=%s source=%s outcome=%s slot=%s", tid, source, result.outcome,
                result.slot)
    return result


async def _request_join(tid: int) -> JoinResult:
    status = await _status(tid)
    if status is None:
        return JoinResult("no_user")
    if not await selection_enabled():
        return await _join_legacy(tid, status)
    if status == _DECLINED:
        return JoinResult("declined")
    if status == _ACTIVE:
        return JoinResult("already_active")
    mode = await join_mode()
    if mode == MODE_SELECTION and status == _CANDIDATE:
        return JoinResult("already_candidate")
    if await slots_full():
        return JoinResult("full")

    if mode == MODE_INSTANT:
        if not await amb_status_db.set_status(tid, _ACTIVE, at=_now(),
                                              expect=(_NONE, _CANDIDATE, _LEFT)):
            return await _after_race(tid)
        slot = await _try_slot(tid)
        await _check_tiers(tid)
        return JoinResult("active", slot)

    if not await amb_status_db.set_status(tid, _CANDIDATE, at=_now(), expect=(_NONE, _LEFT)):
        return await _after_race(tid)
    return JoinResult("candidate")


async def _join_legacy(tid: int, status: str) -> JoinResult:
    """Модуль выключен — как до него: любой не-амбассадор (кандидат и «отказано» тоже) сразу
    в команде, мест нет. Ступени СкиллАп — своим тумблером, поэтому проверяются как всегда."""
    if status == _ACTIVE:
        return JoinResult("already_active")
    if not await amb_status_db.set_status(tid, _ACTIVE, at=_now(),
                                          expect=(_NONE, _CANDIDATE, _LEFT, _DECLINED)):
        return await _after_race(tid)
    await _check_tiers(tid)
    return JoinResult("active")


_RACE_OUTCOMES = {_ACTIVE: "already_active", _CANDIDATE: "already_candidate",
                  _DECLINED: "declined"}


async def _after_race(tid: int) -> JoinResult:
    """Условная запись не прошла — статус успел поменять второй тап или менеджер: отвечаем
    по тому, что теперь записано."""
    return JoinResult(_RACE_OUTCOMES.get(await _status(tid) or "", "no_user"))


async def leave(telegram_id: int) -> bool:
    """Амбассадор сам вышел из команды. Место освобождается, если пакет не выдан."""
    ok = await amb_status_db.set_status(int(telegram_id), _LEFT, at=_now(), expect=(_ACTIVE,))
    if ok:
        logger.info("amb_leave tid=%s", telegram_id)
    return ok


# ── Действия менеджера ───────────────────────────────────────────────────────────────────────

async def take(telegram_id: int, *, by: int) -> JoinResult:
    """«Взять» в команду: кандидата, отказанного, вышедшего или любого делегата с анкетой.
    Лимит не блокирует: сверх лимита человек становится active «без пакета» — пакетов всё
    равно не больше лимита. Сообщение человеку шлёт хендлер."""
    tid = int(telegram_id)
    if await _db.get_user(tid) is None:
        return JoinResult("no_user")
    before = await amb_status_db.get_status(tid) or {}
    if not await amb_status_db.set_status(tid, _ACTIVE, at=_now(), by=int(by),
                                          expect=(_NONE, _CANDIDATE, _DECLINED, _LEFT)):
        if await _status(tid) == _ACTIVE:
            return JoinResult("already_active")
        return JoinResult("no_user")
    slot = await _try_slot(tid)
    if before.get("status") == _CANDIDATE and before.get("status_at"):
        await _credit_candidate_period(tid, since=before["status_at"], by=int(by))
    await _check_tiers(tid)
    logger.info("amb_take admin=%s tid=%s slot=%s", by, tid, slot)
    return JoinResult("taken", slot)


async def remove(telegram_id: int, *, by: int) -> bool:
    """Менеджер вывел амбассадора из команды: тот же переход в `left`, что и сам вышел, но с
    отметкой, кто вывел. Выданный пакет место держит. False — человек не был в команде."""
    tid = int(telegram_id)
    ok = await amb_status_db.set_status(tid, _LEFT, at=_now(), by=int(by), expect=(_ACTIVE,))
    if ok:
        st = await amb_status_db.get_status(tid) or {}
        logger.info("admin=%s amb_remove tid=%s slot_kept=%s", by, tid,
                    st.get("slot_at") is not None)
    return ok


# ── Швы одобрения заявки ─────────────────────────────────────────────────────────────────────

async def _enabled_safe() -> bool:
    """Тумблер для швов одобрения: сбой чтения — «выключен» (шов ничего не делает, одобрение
    уже состоялось, хвосты доберёт сверка)."""
    try:
        return await selection_enabled()
    except Exception:
        logger.exception("amb_status: тумблер отбора не прочитан — шов пропущен")
        return False


async def on_applications_approved(telegram_ids) -> None:
    """Собственную заявку одобрили: действующему амбассадору без места — место, если есть.
    Не амбассадору — ничего. Модуль выключен — ничего. Никогда не бросает (одобрение уже
    состоялось)."""
    if not await _enabled_safe():
        return
    for raw in telegram_ids or ():
        try:
            st = await amb_status_db.get_status(int(raw))
            if st and st["status"] == _ACTIVE and not st["slot_at"]:
                await _try_slot(int(raw))
        except Exception:
            logger.exception("amb_status: место при одобрении не выдано (tid=%s)", raw)


async def on_applications_unapproved(telegram_ids) -> None:
    """Заявка перестала быть одобренной (отмена, отказ после одобрения): место без выданного
    пакета снимается, человек остаётся в команде «без пакета». Статус заявки перечитывается —
    повторное одобрение между событием и вызовом место не отнимет. Модуль выключен — ничего.
    Никогда не бросает."""
    if not await _enabled_safe():
        return
    try:
        from services import amb_tiers
        season = await amb_tiers.current_season()
    except Exception:
        logger.exception("amb_status: сезон не прочитан — место не снимаю")
        return
    for raw in telegram_ids or ():
        try:
            tid = int(raw)
            user = await _db.get_user(tid)
            if not user:
                continue
            if user.get("status") == "approved" and (user.get("season") or "") == season:
                continue
            if await amb_status_db.release_slot(tid):
                logger.info("amb_slot_release tid=%s reason=unapproved", tid)
        except Exception:
            logger.exception("amb_status: место при снятии одобрения не снято (tid=%s)", raw)


async def reconcile_slots() -> int:
    """Сверка: снять места у всех, чья заявка уже не одобрена в текущем сезоне (пропущенные
    швы). Возвращает, сколько мест снято. Модуль выключен — 0. Никогда не бросает."""
    if not await _enabled_safe():
        return 0
    released = 0
    try:
        from services import amb_tiers
        holders = await amb_status_db.unapproved_slot_holders(await amb_tiers.current_season())
    except Exception:
        logger.exception("amb_status: сверка мест не прочитала список")
        return 0
    for tid in holders:
        try:
            if await amb_status_db.release_slot(tid):
                released += 1
                logger.info("amb_slot_release tid=%s reason=reconcile", tid)
        except Exception:
            logger.exception("amb_status: сверка не сняла место (tid=%s)", tid)
    return released


# ── Состояние для «Моя ссылка» ───────────────────────────────────────────────────────────────

async def delegate_state(telegram_id: int) -> str:
    """Что показать делегату на «Моя ссылка»:
    `active_pack` — в команде с местом; `active_no_pack` — в команде без места;
    `candidate` — ждёт решения (режим отбора); `declined` — отказали в этом сезоне;
    `full` — места заняты; `open` — можно попроситься."""
    st = await amb_status_db.get_status(int(telegram_id)) or {}
    status = st.get("status") or _NONE
    if status == _ACTIVE:
        return "active_pack" if st.get("slot_at") else "active_no_pack"
    if not await selection_enabled():
        return "open"
    if status == _DECLINED:
        return "declined"
    if status == _CANDIDATE and await join_mode() == MODE_SELECTION:
        return "candidate"
    return "open" if await offer_open(telegram_id) else "full"
