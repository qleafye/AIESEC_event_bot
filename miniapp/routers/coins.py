"""Phase 19 (D-07): монеты делегата — баланс, история, рейтинг. Только чтение.

Зеркало экрана «🪙 Мои монеты» бота (`handlers/user_actions.py`: `_balance_screen`,
`_balance_history_screen`, `render_leaderboard`) на тех же аксессорах. RU-подпись источника
операции — те же ключи реестра, что у бота (`balance_source_manual_label` /
`balance_source_task_label`). Рейтинг — топ-50 + блок «я»; имена те же, что публикует бот
(T-19-17), телефонов/e-mail в ответе нет.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from database.db import (
    count_coin_entries_for_user,
    get_balance,
    get_leaderboard,
    get_user_rank,
    list_coin_entries_for_user,
)
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_section
from miniapp.routers.tasks import parse_page

router = APIRouter()

LEADERBOARD_MAX = 50
PARTICIPANTS_CAP = 10_000  # тот же «масштаб-приемлемый» приём, что у бота (1000–1500 за сезон)


async def _participants() -> int:
    return len(await get_leaderboard(PARTICIPANTS_CAP))


def display_name(row: dict) -> str:
    """Как в `render_leaderboard` бота: ФИО -> username -> id."""
    return str(row.get("full_name") or row.get("username") or row.get("user_id"))


@router.get("/app/api/coins/balance")
async def balance(p: Principal = Depends(delegate_gate),
                  _: Principal = Depends(require_section("coins"))) -> dict:
    return {
        "balance": await get_balance(p.telegram_id),
        "rank": await get_user_rank(p.telegram_id),
        "participants": await _participants(),
    }


@router.get("/app/api/coins/history")
async def history(offset: str | None = None, limit: str | None = None,
                  p: Principal = Depends(delegate_gate),
                  _: Principal = Depends(require_section("coins"))) -> dict:
    off, lim = parse_page(offset, limit)
    total = await count_coin_entries_for_user(p.telegram_id)
    rows = await list_coin_entries_for_user(p.telegram_id, limit=lim, offset=off)
    manual_label = await get_setting_typed("balance_source_manual_label")
    task_label = await get_setting_typed("balance_source_task_label")
    items = []
    for row in rows:
        source = row.get("source")
        if source == "manual":
            source_label = manual_label
        elif source == "task":
            source_label = task_label
        else:
            source_label = "—"
        items.append({
            "delta": row.get("delta") or 0,
            "reason": row.get("reason"),
            "source": source,
            "source_label": source_label,
            "created_at": row.get("timestamp"),
        })
    return {
        "items": items, "total": total, "limit": lim, "offset": off,
        "empty_text": await get_setting_typed("balance_history_empty") if total == 0 else None,
    }


@router.get("/app/api/leaderboard")
async def leaderboard(limit: str | None = None, p: Principal = Depends(delegate_gate),
                      _: Principal = Depends(require_section("leaderboard"))) -> dict:
    _, lim = parse_page(0, limit, default_limit=LEADERBOARD_MAX, max_limit=LEADERBOARD_MAX)
    rows = await get_leaderboard(lim)
    items = [
        {
            "rank": i,
            "name": display_name(row),
            "balance": row.get("balance", 0),
            "is_me": row.get("user_id") == p.telegram_id,
        }
        for i, row in enumerate(rows, start=1)
    ]
    return {
        "items": items,
        "me": {"rank": await get_user_rank(p.telegram_id), "balance": await get_balance(p.telegram_id)},
        "total": await _participants(),
        "empty_text": await get_setting_typed("leaderboard_empty_text") if not rows else None,
    }
