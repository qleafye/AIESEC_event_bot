"""Экраны амбассадора в квалифицированной амбассадорке СкиллАп: прогресс цифрами и приватность.

Одна точка правды для бота (`handlers/user_actions.py`) и Mini App (`miniapp/routers/hub.py`,
`miniapp/routers/coins.py`) — поверхности зовут эти функции тонко, логики у себя не держат.
Модуль aiogram-free.

Перевод. Сервис сам не знает, чем переводит поверхность, поэтому принимает асинхронный колбэк
`tr_key(key) -> str`, который возвращает УЖЕ переведённый шаблон настройки (бот — через
`reg_i18n.tr_text`, Mini App — через `services.i18n.tr_setting`). Плейсхолдеры подставляются
здесь, после перевода; битый шаблон менеджера (лишняя `{…}`) не роняет экран — отдаётся как есть.

Прогресс (`render_progress`) — только агрегаты (всего / прошли отбор / сколько до следующей
награды), без статусов конкретных людей; видит его только амбассадор (`is_ambassador = 1`) и
только при включённой программе `amb_qualified_program`. «Прошли отбор» — то же число, что
у выдачи ступеней (`amb_tiers_db.referral_counts`), своего определения здесь нет. Счёт живой,
а не по таблице выданных ступеней: цифры на экране всегда совпадают с текущими статусами.

Скрытие имён (`amb_hide_invitee_names`). Маскируется при РЕНДЕРЕ, а не в БД: старые строки
`coins.reason` («Приглашённый: Имя») не переписываются — данные без владельца не трогаем, а
тумблер можно выключить обратно без потерь. `mask_referral_coin_rows` возвращает КОПИИ строк.
Номер N — порядок строки начисления среди referral-строк пользователя по `coins.id`: он один и
тот же на любой странице истории и в боте, и в Mini App (номер по позиции на странице «прыгал» бы).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from shared.amb_tier_keys import tier_key
from database import amb_tiers_db
from database import db as _db
from services import amb_tiers
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

TrKey = Callable[[str], Awaitable[str]]


def _fmt(template: str | None, **subs) -> str:
    template = template or ""
    try:
        return template.format(**subs)
    except (KeyError, IndexError, ValueError):
        logger.warning("amb_progress: не подставил значения в шаблон — отдаю как есть")
        return template


_LEGACY_KINDS = {2: "o2o", 3: "networking"}


async def progress_view(user_id: int) -> dict | None:
    """None, если программа выключена или человек не амбассадор. Иначе счётчики и цель:
    `next_tier` — ближайшая недостигнутая ступень с непустой подписью «сколько до неё»
    (пустая подпись ступень пропускает; `None` — все взяты), `n` — сколько ещё прошедших
    нужно до неё. `next_kind` оставлен для старых потребителей: "o2o" (ступень 2),
    "networking" (ступень 3), "tier{N}" для остальных, "done" — все взяты."""
    if not await amb_tiers.program_on():
        return None
    user = await _db.get_user(user_id)
    if not (user and user.get("is_ambassador")):
        return None
    counts = await amb_tiers_db.referral_counts(user_id, await amb_tiers.current_season())
    qualified = counts["qualified"]
    next_tier, n = None, 0
    for cfg in await amb_tiers.tiers_config():
        if qualified >= cfg.threshold:
            continue
        if not ((await get_setting_typed(cfg.next_key)) or "").strip():
            continue
        next_tier, n = cfg.n, cfg.threshold - qualified
        break
    next_kind = "done" if next_tier is None else _LEGACY_KINDS.get(next_tier, f"tier{next_tier}")
    return {**counts, "next_tier": next_tier, "next_kind": next_kind, "n": n}


async def render_progress(user_id: int, tr_key: TrKey) -> str | None:
    """Строка «По твоей ссылке: …» или None (программа выключена / не амбассадор)."""
    view = await progress_view(user_id)
    if view is None:
        return None
    key = "amb_next_step_done_text" if view["next_tier"] is None else tier_key(view["next_tier"], "next")
    next_step = _fmt(await tr_key(key), n=view["n"])
    return _fmt(
        await tr_key("amb_progress_text"),
        total=view["total"], qualified=view["qualified"], next_step=next_step,
    )


async def hide_names_on() -> bool:
    return await get_setting_typed("amb_hide_invitee_names") == "on"


async def render_invitee_counts(user_id: int, tr_key: TrKey) -> str:
    """«Мои приглашённые» без имён: всего / на рассмотрении / прошли отбор (текущий сезон)."""
    counts = await amb_tiers_db.referral_counts(user_id, await amb_tiers.current_season())
    return _fmt(
        await tr_key("amb_invitees_counts_text"),
        total=counts["total"], pending=counts["pending"], qualified=counts["qualified"],
    )


_MASKED_SOURCES = ("referral", "referral_reversal")


async def mask_referral_coin_rows(user_id: int, rows: list[dict], tr_key: TrKey) -> list[dict]:
    """При выключенном тумблере — `rows` как есть (тот же объект). При включённом — копии строк,
    у referral-строк `reason` = «Приглашённый №N»; прочие строки не меняются."""
    if not rows or not await hide_names_on():
        return rows
    if not any(row.get("source") in _MASKED_SOURCES for row in rows):
        return [dict(row) for row in rows]
    ordinals = await amb_tiers_db.referral_coin_ordinals(user_id)
    template = await tr_key("amb_invitee_masked_label_text")
    masked: list[dict] = []
    for row in rows:
        copy = dict(row)
        if copy.get("source") in _MASKED_SOURCES:
            # у обратной строки номера нет: она не входит в нумерацию начислений
            copy["reason"] = _fmt(template, n=ordinals.get(copy.get("id"), "—"))
        masked.append(copy)
    return masked
