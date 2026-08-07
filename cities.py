"""Event-city registry (Phase 07.1, CITY-01).

`event_city` is the CITY OF THE EVENT (Moscow / SPb / Tyumen) — a NEW, orthogonal field.
It must never be confused with the existing `city` registration question, which is the
DELEGATE's own home city. See .planning/phases/07.1-.../07.1-CONTEXT.md for the full
naming rationale — this is documented as the most expensive mistake this phase could make.

Design (mirrors settings_schema.py's one-directional dependency, D-01):
- Lives in its own top-level module — imports ONLY `config.config`,
  `database.db.get_setting` and `settings_schema.get_setting_typed`. No `handlers.*`
  import — settings_schema does not import cities, so there is no cycle.
- The city LIST + its DEFAULT LABELS/TAB BASES come from `.env` only (owner decision,
  2026-08-07). Per-city overrides (label/tab/enabled) live in `bot_settings`, resolved
  through this module's async accessors — one setting per city, not one setting per
  city*track combination (that would out-scale the number of cities).
- `normalize_city` is the SINGLE point in the whole project where "no city on record"
  becomes "Москва". Nothing else may hardcode that fallback, and nothing may write the
  resolved default back into storage (no backfill — see CONTEXT.md).
"""
from config import config
from database.db import get_setting
from settings_schema import get_setting_typed

CITY_SEP = ";"
FIELD_SEP = "|"

_MAX_CODE_LEN = 16


def parse_cities(raw: str) -> list[dict]:
    """Pure sync parser (no DB, no async — the unit-test surface).

    `raw` format: `code|label|tab_base;code|label|tab_base;...` — `tab_base` is optional
    (missing third field = ""). Entries are skipped (never raise) when: code is empty,
    code fails `code.isascii() and code.replace("_", "").isalnum()`, code is longer than
    16 chars, or label is empty. On a duplicate code the FIRST occurrence wins. Bad/empty
    input returns [].
    """
    result: list[dict] = []
    seen: set[str] = set()
    if not raw:
        return result
    for entry in raw.split(CITY_SEP):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split(FIELD_SEP)
        code = (fields[0] if len(fields) > 0 else "").strip()
        label = (fields[1] if len(fields) > 1 else "").strip()
        tab_base = (fields[2] if len(fields) > 2 else "").strip()
        if not code:
            continue
        if not (code.isascii() and code.replace("_", "").isalnum()):
            continue
        if len(code) > _MAX_CODE_LEN:
            continue
        if not label:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "label": label, "tab_base": tab_base})
    return result


# Computed once at import time — same pattern as SETTINGS_SCHEMA being a module-level dict.
CITIES = parse_cities(config.EVENT_CITIES)


def city_codes() -> list[str]:
    return [c["code"] for c in CITIES]


def get_city(code: str | None) -> dict | None:
    if not code:
        return None
    for c in CITIES:
        if c["code"] == code:
            return c
    return None


def default_city_code() -> str:
    """`config.EVENT_CITY_DEFAULT` if it names a known city; else the first parsed city;
    else the literal "msk" (last-resort fallback so the bot never crashes on a broken
    .env)."""
    configured = config.EVENT_CITY_DEFAULT
    if get_city(configured):
        return configured
    if CITIES:
        return CITIES[0]["code"]
    return "msk"


def normalize_city(code: str | None) -> str:
    """THE single point of "no city on record" -> Москва. A known code passes through
    unchanged; None/empty/unknown resolves to `default_city_code()`. Never writes back to
    storage — callers read this on every access, they never persist its result over a
    NULL `event_city`."""
    if get_city(code):
        return code
    return default_city_code()


def is_default_city(code: str | None) -> bool:
    return normalize_city(code) == default_city_code()


# Tab-name suffix per track, appended to a city's tab base (owner decision: "base city name
# + track suffix", not one setting per city*track combination). Moscow's tab_base is ""
# (legacy tabs untouched byte-for-byte); a new city's tab_base + these suffixes produce its
# tab names.
TAB_SUFFIX = {
    "main": "",
    "short": " Акция",
    "party": " Party",
    "incomplete": " Незавершённые",
}


async def is_city_enabled(code: str) -> bool:
    """A city is enabled by default (bot_settings absent = "on") — switched off point-wise
    from the admin panel (plan 04)."""
    return (await get_setting(f"city_enabled__{code}") or "on") == "on"


async def city_label(code: str) -> str:
    override = await get_setting(f"city_label__{code}")
    if override:
        return override
    city = get_city(code)
    if city:
        return city["label"]
    return code


async def city_tab_base(code: str) -> str:
    override = await get_setting(f"city_tab__{code}")
    if override:
        return override
    city = get_city(code)
    if city:
        return city["tab_base"]
    return ""


async def enabled_cities() -> list[dict]:
    out = []
    for c in CITIES:
        if await is_city_enabled(c["code"]):
            out.append(c)
    return out


async def cities_module_on() -> bool:
    """Master toggle (`event_city_enabled`, default "off" — plan 04 turns it on). Deliberately
    a SETTINGS_SCHEMA-registered enum key (not a dynamic per-city key), so it participates in
    the normal admin toggle rendering."""
    return await get_setting_typed("event_city_enabled") == "on"
