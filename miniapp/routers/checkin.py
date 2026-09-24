"""Phase 12 (FORUM-CHECKIN.md, D-08/D-12/D-13, идея №9): сканер отметки на форуме в Mini App.

Основной способ отметки на входе (D-08 — `Telegram.WebApp.showScanQrPopup`, непрерывный режим,
скан сразу ставит отметку). Право — `checkin` (капа заведена в `handlers/admin_caps.py`,
уже отражена в `dashboard/access.py::ALL_CAPABILITIES`, сторож `tests/test_dashboard_auth.py`
сравнивает оба списка), раздел-чекбокс `miniapp_section_checkin` (`SECTIONS` в
`miniapp/deps.py`) — обе проверки на КАЖДОЙ ручке, тот же приём, что у `admin_tasks.py`.

Городской скоуп — тот же приём, что `admin_tasks.py::bound_city`: суперадмин (ADMIN_IDS) не
ограничен НИКОГДА, модуль городов выключен -> ограничений нет, иначе -- `Principal.city`
(`staff.city`, привязка менеджера, НЕ бот-овский `admin_selected_city` — тот живёт в
aiogram-зависимом `handlers/admin_core.py`, сюда его импортировать нельзя). `/stats` строит ту
же разбивку по городам, что бот (`handlers/admin_checkin.py::_counter_line`) — те же
`count_checkins_by_point(city_scope=…)`/`count_approved_current_season(city_scope=…)`
(задача A2). Скан/ручная отметка (`/scan`/`/manual`) НЕ проверяют город делегата против
привязки менеджера — стойки не разложены по алфавиту/городу (D-15), волонтёр «Входа» отмечает
любого делегата с валидным QR, кто бы к нему ни подошёл.

QR не нашего события (`services.checkin.current_event_tag()` не совпал с меткой в самом QR) —
отдельный код `foreign_event`, ПРОВЕРЯЕТСЯ ПЕРВЫМ, до поиска делегата по токену: токен внутри
чужого QR искать в нашей БД бессмысленно и рискованно (совпадение токенов между независимыми
событиями не исключено при достаточном числе форумов на одном боте)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from cities import cities_module_on, city_label, city_scope, enabled_cities, normalize_city
from database.db import (
    count_approved_current_season,
    count_checkins_by_point,
    get_user,
    record_checkin,
)
from services.checkin import (
    DENIAL_REASON_TEXT,
    ENTRY_POINT,
    checkin_denial,
    current_event_tag,
    mark_arrived_in_sheet,
    parse_qr_payload,
    resolve_scanned_user,
)
from services.person_search import search_people

from miniapp.deps import Principal, require_cap, require_section

router = APIRouter()

_SEARCH_LIMIT = 20
_SECTION = "checkin"
_CAP = "checkin"


async def _bound_city(request: Request, p: Principal) -> str | None:
    """Тот же приём, что `admin_tasks.py::bound_city` — суперадмин не ограничен никогда,
    модуль городов выключен -> ограничений нет, иначе -- город, к которому привязан менеджер."""
    if p.telegram_id in (request.app.state.cfg.admin_ids or ()):
        return None
    if not await cities_module_on() or not p.city:
        return None
    return normalize_city(p.city)


def _person_fields(user: dict) -> dict:
    return {
        "telegram_id": user.get("telegram_id"),
        "full_name": user.get("full_name") or "—",
        "city": user.get("event_city") or None,
        "university": user.get("university") or None,
        "username": user.get("username") or None,
    }


class ScanBody(BaseModel):
    payload: str = ""
    point: str = ENTRY_POINT


@router.post("/app/api/checkin/scan")
async def checkin_scan(
    body: ScanBody,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    parsed = parse_qr_payload(body.payload)
    tag = parsed.get("tag") or ""
    if not tag or tag != await current_event_tag():
        return {
            "status": "foreign_event",
            "reason_text": DENIAL_REASON_TEXT["foreign_event"],
            "full_name": parsed.get("full_name") or None,
            "city": parsed.get("city") or None,
        }

    token = parsed.get("token")
    user, denial_code = await resolve_scanned_user(token)
    if denial_code is not None:
        return {
            "status": "not_found" if denial_code == "no_user" else "denied",
            "reason_text": DENIAL_REASON_TEXT.get(denial_code, denial_code),
            "full_name": (user or {}).get("full_name") or parsed.get("full_name") or None,
            "city": (user or {}).get("event_city") or parsed.get("city") or None,
        }

    status, scanned_at = await record_checkin(
        user["telegram_id"], body.point or ENTRY_POINT, source="miniapp", by_staff_id=p.telegram_id,
    )
    await mark_arrived_in_sheet(user["telegram_id"], status, scanned_at)
    return {"status": status, "scanned_at": scanned_at, **_person_fields(user)}


class ManualBody(BaseModel):
    telegram_id: int
    point: str = ENTRY_POINT


@router.post("/app/api/checkin/manual")
async def checkin_manual(
    body: ManualBody,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """D-11/D-12: делегат найден поиском (телефон сел/нет QR под рукой), не сканом — та же
    отметка, источник `manual` отличает её в журнале (будущее B1-31)."""
    user = await get_user(body.telegram_id)
    denial_code = await checkin_denial(user)
    if denial_code is not None:
        return {
            "status": "not_found" if denial_code == "no_user" else "denied",
            "reason_text": DENIAL_REASON_TEXT.get(denial_code, denial_code),
            "full_name": (user or {}).get("full_name") if user else None,
            "city": (user or {}).get("event_city") if user else None,
        }
    status, scanned_at = await record_checkin(
        user["telegram_id"], body.point or ENTRY_POINT, source="manual", by_staff_id=p.telegram_id,
    )
    await mark_arrived_in_sheet(user["telegram_id"], status, scanned_at)
    return {"status": status, "scanned_at": scanned_at, **_person_fields(user)}


@router.get("/app/api/checkin/search")
async def checkin_search(
    request: Request, q: str = "",
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """D-12/D-13: поиск по фамилии (общий `services.person_search.search_people`, ё=е) —
    одобренные текущего сезона первыми (`eligible`), город/@username/вуз в каждой строке
    различают тёзок. Городской скоуп — как у сканирования (см. докстринг модуля)."""
    bound = await _bound_city(request, p)
    scope = city_scope(bound) if bound else None
    found = await search_people(q, city_scope=scope, limit=_SEARCH_LIMIT)

    module_on = await cities_module_on()
    items = []
    for row in found:
        user = await get_user(row["user_id"]) if row["source"] == "users" else None
        denial_code = await checkin_denial(user) if user is not None else "no_user"
        raw_city = row.get("city")
        city_text = raw_city
        if raw_city and module_on:
            city_text = await city_label(normalize_city(raw_city))
        items.append({
            "telegram_id": row["user_id"],
            "full_name": row.get("full_name") or "—",
            "city": city_text,
            "university": row.get("university"),
            "username": row.get("username"),
            "eligible": denial_code is None,
            "reason_text": None if denial_code is None else DENIAL_REASON_TEXT.get(denial_code, denial_code),
        })
    # Одобренные текущего сезона (eligible) — первыми (D-12); внутри каждой группы порядок
    # `search_people` (алфавит по имени) сохраняется — сортировка Python стабильна.
    items.sort(key=lambda it: 0 if it["eligible"] else 1)
    return {"items": items}


@router.get("/app/api/checkin/stats")
async def checkin_stats(
    request: Request,
    p: Principal = Depends(require_cap(_CAP)),
    _: Principal = Depends(require_section(_SECTION)),
) -> dict:
    """A2 (FORUM-CHECKIN.md): та же построчная разбивка по городам, что бот
    (`handlers/admin_checkin.py::_counter_line`) — три ветки: менеджер с городом-привязкой
    видит только свой (`cities: null`), модуль городов выключен — общий счётчик байт-в-байт
    как раньше, иначе — построчно по городам с хотя бы одним одобренным + Итого."""
    bound = await _bound_city(request, p)
    if bound is not None:
        scope = city_scope(bound)
        approved = await count_approved_current_season(city_scope=scope)
        arrived = await count_checkins_by_point(ENTRY_POINT, city_scope=scope)
        return {"arrived": arrived, "approved": approved, "cities": None}

    if not await cities_module_on():
        approved = await count_approved_current_season()
        arrived = await count_checkins_by_point(ENTRY_POINT)
        return {"arrived": arrived, "approved": approved, "cities": None}

    cities_out = []
    total_arrived = 0
    total_approved = 0
    for c in await enabled_cities():
        code = c["code"]
        scope = city_scope(code)
        approved = await count_approved_current_season(city_scope=scope)
        if approved == 0:
            continue
        arrived = await count_checkins_by_point(ENTRY_POINT, city_scope=scope)
        cities_out.append({
            "code": code, "label": await city_label(code), "arrived": arrived, "approved": approved,
        })
        total_arrived += arrived
        total_approved += approved
    return {"arrived": total_arrived, "approved": total_approved, "cities": cities_out}


__all__ = ["router"]
