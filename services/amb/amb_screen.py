"""Состав делегатского экрана «Моя ссылка» — одна сводка для бота и хаба Mini App.

`delegate_view(tid)` отдаёт только данные и КЛЮЧИ реестра, не тексты: рендер, перевод и
подстановку делает поверхность. Каждая часть считается отдельно и fail-soft — сбой одной
(БД, волна, журнал) даёт `None` в этой части и запись в лог, остальные части остаются.

Модуль «🤝 Отбор амбассадоров» (`amb_team_selection_enabled`) выключен — экран как до модуля
(Юлид): ни строки статуса, ни баллов, ни места в волне; ссылка, прогресс ступеней и кнопки
живут как раньше. «Пакет» показывается только при лимите мест (`slots_limit() > 0`).

Модуль aiogram-free. Чужих данных здесь нет: только показатели самого делегата.
"""
from __future__ import annotations

import logging

from database import db as _db
from services.amb import amb_status
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

STATUS_PACK_KEY = "amb_status_pack_text"
STATUS_NO_PACK_KEY = "amb_status_no_pack_text"
STATUS_CANDIDATE_KEY = "amb_status_candidate_text"
STATUS_FULL_KEY = "amb_slots_full_text"


async def referral_points_sum(telegram_id: int) -> int:
    """Баллы за приглашённых по журналу: начисления минус снятия (`referral_reversal`)."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM coins WHERE user_id = ? "
            "AND source IN ('referral', 'referral_reversal')",
            (int(telegram_id),),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0] or 0)


def _status_key(state: str, limit: int) -> str | None:
    if state == "active_pack":
        return STATUS_PACK_KEY if limit > 0 else None
    if state == "active_no_pack":
        return STATUS_NO_PACK_KEY if limit > 0 else None
    if state == "candidate":
        return STATUS_CANDIDATE_KEY
    if state in ("full", "declined"):
        return STATUS_FULL_KEY
    return None


async def _state(telegram_id: int) -> str:
    try:
        return await amb_status.delegate_state(telegram_id)
    except Exception:
        logger.exception("amb_screen: статус не прочитан (tid=%s)", telegram_id)
        return "open"  # fail-open: кнопка вступления, `request_join` перепроверит


async def _points(telegram_id: int) -> int | None:
    try:
        total = await referral_points_sum(telegram_id)
        if total == 0 and int(await get_setting_typed("ambassador_referral_coins") or 0) == 0:
            return None
        return total
    except Exception:
        logger.exception("amb_screen: баллы за приглашённых не посчитаны (tid=%s)", telegram_id)
        return None


async def _wave_place(user: dict) -> dict | None:
    try:
        from services.amb import ambassador_waves as waves
        wave = await waves.current_wave_for_city_raw(user.get("event_city"))
        if not wave or not waves.wave_eligible(user, wave):
            return None
        own = (await waves.wave_rating_view(int(wave["id"]), int(user["telegram_id"])))["own"]
        if not own:
            return None
        return {"wave": waves.wave_number_label(wave), "number": wave.get("number"),
                "place": own["place"], "total": own["total"]}
    except Exception:
        logger.exception("amb_screen: место в волне не посчитано (tid=%s)", user.get("telegram_id"))
        return None


async def delegate_view(telegram_id: int, user: dict | None = None, need_state: bool = True) -> dict:
    """{"state", "status_key" | None, "referral_points" | None, "wave_place" | None,
    "is_ambassador"}. Модуль отбора выключен — строки статуса, баллов и волны пусты.

    `user` — уже прочитанная строка делегата (хаб Mini App её держит): экономит соединение.
    `need_state=False` — вызывающему не нужен `state`: при выключенном модуле статус в БД не
    читается вовсе (`state` = None), при включённом считается как обычно."""
    tid = int(telegram_id)
    if user is None:
        try:
            user = await _db.get_user(tid) or {}
        except Exception:
            logger.exception("amb_screen: делегат не прочитан (tid=%s)", tid)
            user = {}
    else:
        user = dict(user)
    is_amb = bool(user.get("is_ambassador"))
    view = {"state": None, "status_key": None, "referral_points": None,
            "wave_place": None, "is_ambassador": is_amb}
    try:
        enabled = await amb_status.selection_enabled()
    except Exception:
        logger.exception("amb_screen: строка статуса не собрана (tid=%s)", tid)
        enabled = False
    if enabled or need_state:
        view["state"] = await _state(tid)
    try:
        if not enabled:
            return view
        view["status_key"] = _status_key(view["state"], await amb_status.slots_limit())
    except Exception:
        logger.exception("amb_screen: строка статуса не собрана (tid=%s)", tid)
        return view
    if is_amb:
        view["referral_points"] = await _points(tid)
        user.setdefault("telegram_id", tid)
        view["wave_place"] = await _wave_place(user)
    return view
