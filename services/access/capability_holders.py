"""Кто сейчас держит право: список telegram_id, с необязательным сужением по городу.

Тело перенесено из handlers.access.admin_caps без изменения веток.
Хендлер реэкспортирует ту же функцию.
"""
from __future__ import annotations

from config import config
from database.db import get_staff_city, get_staff_ids_by_role
from domain.cities import cities_module_on, normalize_city
from domain.settings.schema import get_setting_typed
from services.access.role_registry import ROLES, role_caps_key, role_enabled_key


async def capability_holders(cap: str, *, city: str | None = None) -> list[int]:
    """Every telegram_id currently entitled to `cap`, order-preserving de-duped (D-08: a
    person holding two roles that both grant `cap` is listed once). Bootstrap admins (D-12)
    always come first -- they hold every capability regardless of `staff`/registry state --
    followed by staff members of each enabled (D-10) role whose role_caps_* includes `cap`.
    Deliberately mirrors resolve_capabilities()'s own role-iteration shape rather than calling
    it per-candidate: resolve_capabilities would need every staff id up front to iterate over,
    which is exactly what this function is computing.

    Phase 09.2 (D, CITY-06): `city` is an OPTIONAL narrowing applied AFTER the list above is
    built -- it never changes who is a holder, only who among the holders is addressed this
    time. `city=None`, or the cities module being off, means "no filter", byte-identical to
    the pre-09.2 return value (regression contract). With a real `city`, `normalize_city` is
    applied to BOTH sides of the comparison (the requested city and each holder's
    `get_staff_city` binding) so a stray label/garbage binding still resolves to a known code
    the same way `admin_selected_city` does. A superadmin (`config.ADMIN_IDS`) is always kept
    regardless of binding (D-12); a holder with no binding at all (`get_staff_city` empty) is
    also always kept -- "all cities" is the pre-09.1 default for everyone (09.1 C).

    Two-layer fallback (RESEARCH Pitfall 4) -- both live INSIDE this primitive, not at a call
    site: (a) nobody holds `cap` at all -> `notify_by_capability` falls back to
    `config.ADMIN_IDS` (existing, below); (b) somebody holds `cap` but the city filter leaves
    NOBODY -> this function falls back to the unfiltered `holders` list, right here. Layer (b)
    must live here rather than in the caller, or `notify_by_capability`'s returned
    `sent_count` (the delegate-question UX branch depends on it) would stop reflecting reality
    the moment a filtered list goes empty."""
    holders: list[int] = []
    seen: set[int] = set()
    for admin_id in config.ADMIN_IDS:
        if admin_id not in seen:
            holders.append(admin_id)
            seen.add(admin_id)
    for role in ROLES:
        if await get_setting_typed(role_enabled_key(role)) != "on":
            continue  # D-10: role switched off entirely -> no holders from it
        role_caps = await get_setting_typed(role_caps_key(role)) or []
        if cap not in role_caps:
            continue
        for uid in await get_staff_ids_by_role(role):
            if uid not in seen:
                holders.append(uid)
                seen.add(uid)
    if city is None or not await cities_module_on():
        return holders
    target = normalize_city(city)
    filtered: list[int] = []
    for uid in holders:
        if uid in config.ADMIN_IDS:  # D-12: superadmins never filtered out
            filtered.append(uid)
            continue
        bound = await get_staff_city(uid)
        if not bound or normalize_city(bound) == target:
            filtered.append(uid)
    if not filtered:
        return holders  # layer (b): city filter emptied the list -> never drop the message
    return filtered
