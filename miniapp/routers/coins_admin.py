"""Phase 19 (D-04 волна 2, D-07): менеджерские монеты вручную + журнал из Mini App.

Зеркало визарда `handlers/admin_gamification.py::coinsman_*` (импортировать нельзя — aiogram):
найти человека -> карточка с балансом -> сумма со знаком -> обязательная причина ->
подтверждение (на клиенте) -> `add_coins(source="manual", changed_by=менеджер)`. След в БД
байт-в-байт тот же, что у бота, поэтому «📜 Журнал монет» бота и журнал здесь — одна таблица.

Все маршруты — `require_cap("moderate_game")` + `require_section("coins")`.

Поиск получателя (T-19-47/49): `@username` — точное совпадение, число — `telegram_id`,
иначе часть имени через `database.db.search_users_by_name` (потолок 20, от 2 символов).
В ответ уходит только опознавательный минимум: id, имя, @username, город, баланс.
Привязанный к городу менеджер ищет и начисляет только в своём городе (T-19-46), как очередь
проверки — город ДЕЛЕГАТА против `Principal.city`.

После записи — `outbox coins_manual {user_id, delta}`: уведомление делегату и ребилд
Sheets делает бот (план 19-08), как после `coinsman_confirm`.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cities import cities_module_on, city_label, normalize_city
from database.db import (
    add_coins,
    count_manual_coin_entries,
    get_balance,
    get_user,
    get_user_by_username,
    list_manual_coin_entries,
    search_users_by_name,
)
from settings_schema import SETTINGS_SCHEMA, get_setting_typed

from miniapp.deps import Principal, require_cap, require_section
from miniapp.outbox import enqueue
from miniapp.routers.review import manager_scope

router = APIRouter()

SEARCH_MIN_CHARS = 2
SEARCH_LIMIT = 20
JOURNAL_LIMIT_DEFAULT = 20
JOURNAL_LIMIT_MAX = 50
# T-19-46: потолок модуля суммы — тот же, что у «своей суммы» в очереди проверки.
DELTA_MAX = 100_000
REASON_MAX = 500

# Те же слова, что у шагов визарда бота — менеджер их уже видел.
REASON_REQUIRED_TEXT = (
    "Причина обязательна: журнал монет должен отвечать на вопрос «кто, кому, за что». "
    "Напишите коротко, например: за помощь на стенде."
)
BAD_DELTA_TEXT = f"Сумма — целое число не ноль и не больше {DELTA_MAX}, например 5 или -5."
NOT_FOUND_TEXT = "Не нашёл такого человека среди зарегистрированных."
OUT_OF_SCOPE_TEXT = "Этот делегат из другого города — начислить ему можно только из его города."
SOURCE_LABELS = {"manual": "Вручную", "task": "За задание", None: "До обновления"}


# ── helpers ──────────────────────────────────────────────────────────────────────────────

def parse_amount_presets(raw: str | None) -> list[int]:
    """Копия `handlers/game_review_render.py::_parse_amount_presets` (модуль тянет aiogram):
    «5,10,20» -> [5, 10, 20]; всё, что не положительное целое, молча пропускается."""
    presets: list[int] = []
    for piece in str(raw or "").split(","):
        piece = piece.strip()
        if piece.isdigit() and int(piece) > 0 and int(piece) not in presets:
            presets.append(int(piece))
    return presets


async def _user_in_scope(p: Principal, user: dict) -> bool:
    """Город делегата против привязки менеджера (модуль выключен / нет привязки — всё)."""
    if not await cities_module_on() or p.city is None:
        return True
    return normalize_city(user.get("event_city")) == normalize_city(p.city)


async def _city_text(user: dict) -> str | None:
    if not await cities_module_on():
        return None
    return await city_label(normalize_city(user.get("event_city")))


def _display_name(user: dict | None, fallback) -> str:
    return str((user or {}).get("full_name") or (user or {}).get("username") or fallback)


async def _brief(user: dict) -> dict:
    """Опознавательный минимум (T-19-47): ни телефона, ни e-mail, ни вуза."""
    return {
        "telegram_id": user["telegram_id"],
        "name": _display_name(user, user["telegram_id"]),
        "username": user.get("username"),
        "city": await _city_text(user),
        "balance": await get_balance(user["telegram_id"]),
    }


def _parse_int(raw, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


# ── GET /app/api/admin/users/search ──────────────────────────────────────────────────────

@router.get("/app/api/admin/users/search")
async def users_search(
    q: str = "",
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("coins")),
) -> list[dict]:
    needle = (q or "").strip()
    if len(needle) < SEARCH_MIN_CHARS:
        return []

    direct: dict | None = None
    if needle.startswith("@"):
        direct = await get_user_by_username(needle)
    elif needle.isdigit():
        direct = await get_user(int(needle))
    if direct is not None:
        return [await _brief(direct)] if await _user_in_scope(p, direct) else []
    if needle.startswith("@"):
        return []

    rows = await search_users_by_name(needle, SEARCH_LIMIT, city_scope=await manager_scope(p))
    return [await _brief(row) for row in rows]


# ── POST /app/api/admin/coins ────────────────────────────────────────────────────────────

class CoinsIn(BaseModel):
    user_id: int
    delta: int
    reason: str = ""


@router.post("/app/api/admin/coins")
async def coins_manual(
    body: CoinsIn,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("coins")),
) -> dict:
    if body.delta == 0 or abs(body.delta) > DELTA_MAX:
        raise HTTPException(400, {"reason": "bad_delta", "text": BAD_DELTA_TEXT})
    reason = body.reason.strip()[:REASON_MAX]
    if not reason:
        raise HTTPException(400, {"reason": "reason_required", "text": REASON_REQUIRED_TEXT})
    user = await get_user(body.user_id)
    if user is None:
        raise HTTPException(404, {"reason": "not_found", "text": NOT_FOUND_TEXT})
    if not await _user_in_scope(p, user):
        raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})

    # Порядок как у coinsman_confirm: запись в журнал, потом всё остальное.
    await add_coins(body.user_id, body.delta, reason=reason, changed_by=p.telegram_id, source="manual")
    await enqueue("coins_manual", {"user_id": body.user_id, "delta": body.delta})
    balance = await get_balance(body.user_id)
    return {
        "ok": True,
        "user_id": body.user_id,
        "name": _display_name(user, body.user_id),
        "delta": body.delta,
        "balance": balance,
    }


# ── GET /app/api/admin/coins — журнал ────────────────────────────────────────────────────

def _when(raw) -> str:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(raw or "—")


@router.get("/app/api/admin/coins")
async def coins_journal(
    offset: str | None = None,
    limit: str | None = None,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("coins")),
) -> dict:
    off = _parse_int(offset, 0, 0, 10_000_000)
    lim = _parse_int(limit, JOURNAL_LIMIT_DEFAULT, 1, JOURNAL_LIMIT_MAX)
    total = await count_manual_coin_entries()
    rows = await list_manual_coin_entries(limit=lim, offset=off)

    changers: dict[int, str] = {}
    items = []
    for row in rows:
        changed_by = row.get("changed_by")
        if changed_by is not None and changed_by not in changers:
            changers[changed_by] = _display_name(await get_user(changed_by), changed_by)
        items.append({
            "id": row["id"],
            "when": _when(row.get("timestamp")),
            "user_id": row["user_id"],
            "recipient": _display_name(
                {"full_name": row.get("user_full_name"), "username": row.get("user_username")},
                row["user_id"],
            ),
            "delta": row.get("delta") or 0,
            "reason": row.get("reason") or "—",
            "changed_by": changed_by,
            "changed_by_name": changers.get(changed_by, "—"),
            "source_label": SOURCE_LABELS.get(row.get("source"), SOURCE_LABELS[None]),
        })
    return {
        "items": items,
        "total": total,
        "offset": off,
        "limit": lim,
        "empty_text": await get_setting_typed("miniapp_empty_admin_coins"),
    }


# ── GET /app/api/admin/coins/presets ─────────────────────────────────────────────────────

@router.get("/app/api/admin/coins/presets")
async def coins_presets(
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("coins")),
) -> dict:
    presets = parse_amount_presets(await get_setting_typed("coins_manual_amount_presets"))
    if not presets:
        presets = parse_amount_presets(SETTINGS_SCHEMA["coins_manual_amount_presets"]["default"])
    return {"presets": presets, "delta_max": DELTA_MAX, "reason_max": REASON_MAX}


__all__ = ["router", "parse_amount_presets"]
