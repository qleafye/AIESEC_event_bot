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


async def progress_view(user_id: int) -> dict | None:
    """None, если программа выключена или человек не амбассадор. Иначе счётчики и следующая
    ступень: `next_kind` = "o2o" | "networking" | "done", `n` — сколько ещё прошедших нужно."""
    if not await amb_tiers.program_on():
        return None
    user = await _db.get_user(user_id)
    if not (user and user.get("is_ambassador")):
        return None
    counts = await amb_tiers_db.referral_counts(user_id, await amb_tiers.current_season())
    _t1, t2, t3 = await amb_tiers.thresholds()
    qualified = counts["qualified"]
    if qualified < t2:
        next_kind, n = "o2o", t2 - qualified
    elif qualified < t3:
        next_kind, n = "networking", t3 - qualified
    else:
        next_kind, n = "done", 0
    return {**counts, "next_kind": next_kind, "n": n}


_NEXT_STEP_KEYS = {
    "o2o": "amb_next_step_o2o_text",
    "networking": "amb_next_step_networking_text",
    "done": "amb_next_step_done_text",
}


async def render_progress(user_id: int, tr_key: TrKey) -> str | None:
    """Строка «По твоей ссылке: …» или None (программа выключена / не амбассадор)."""
    view = await progress_view(user_id)
    if view is None:
        return None
    next_step = _fmt(await tr_key(_NEXT_STEP_KEYS[view["next_kind"]]), n=view["n"])
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


async def mask_referral_coin_rows(user_id: int, rows: list[dict], tr_key: TrKey) -> list[dict]:
    """При выключенном тумблере — `rows` как есть (тот же объект). При включённом — копии строк,
    у referral-строк `reason` = «Приглашённый №N»; прочие строки не меняются."""
    if not rows or not await hide_names_on():
        return rows
    if not any(row.get("source") == "referral" for row in rows):
        return [dict(row) for row in rows]
    ordinals = await amb_tiers_db.referral_coin_ordinals(user_id)
    template = await tr_key("amb_invitee_masked_label_text")
    masked: list[dict] = []
    for row in rows:
        copy = dict(row)
        if copy.get("source") == "referral":
            copy["reason"] = _fmt(template, n=ordinals.get(copy.get("id"), "—"))
        masked.append(copy)
    return masked
