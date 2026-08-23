"""Phase 19 (D-04 волна 2): «📊 Статистика геймы» для Mini App — только агрегаты.

`GET /app/api/stats/game` отдаёт результат `database.db.get_game_stats()` — той же и
ЕДИНСТВЕННОЙ агрегирующей функции, что стоит за экраном 9 бота
(`handlers/admin_gamification.py::show_game_stats`), поэтому числа совпадают до единицы.
RU-подписи категорий — из корневого `game_labels.category_label` (реестр), порядок —
фиксированный `GAME_CATEGORIES`, как у `render_category_bars`; нулевые категории тоже
отдаются (фронт сам решает, рисовать ли полосу).

Правило дашборда D-17 распространяется сюда (T-19-32): в ответе нет ни одного поля с
персональными данными — ни имён, ни username, ни телефонов, ни telegram_id.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from database.db import GAME_CATEGORIES, get_game_stats
from game_labels import category_label

from miniapp.deps import Principal, require_cap, require_section

router = APIRouter()


@router.get("/app/api/stats/game")
async def game_stats(
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("stats")),
) -> dict:
    stats = await get_game_stats()
    by_category = stats.get("by_category") or {}
    return {
        "participants": stats["participants"],
        "submissions": {
            "pending": stats["pending"],
            "approved": stats["approved"],
            "rejected": stats["rejected"],
        },
        "by_category": [
            {"code": code, "label": await category_label(code), "count": int(by_category.get(code, 0))}
            for code in GAME_CATEGORIES
        ],
    }
