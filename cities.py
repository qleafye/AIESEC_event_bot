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
from database.db import get_setting, set_setting, get_staff_city
from settings_schema import SETTINGS_SCHEMA, _parse_setting, get_setting_typed

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


async def tab_suffix(kind: str) -> str:
    """Quick 260815-3hw (TABS-01/02/03): admin-configurable tab-name suffix for a track kind
    ("short"/"party"/"incomplete"), read from the registry (city_tab_suffix__{kind}) with a
    fallback to the TAB_SUFFIX literal above. `kind == "main"` is not a registry key (the main
    tab has no suffix, a city's base name IS its main tab) — returns "" unconditionally, no
    registry lookup, no normalization.

    Normalization: admin input arrives already `.strip()`-ped (handlers/admin.py::
    settings_edit_value) — a manager typing «Акция» would otherwise produce «СПбАкция»
    (base + suffix concatenated with no separator). A non-empty result that doesn't already
    start with a space gets exactly one leading space added; TAB_SUFFIX's own literals already
    carry it (" Акция" etc.), so this is a no-op for the un-configured path."""
    if kind == "main":
        return ""
    configured = await get_setting_typed(f"city_tab_suffix__{kind}")
    value = configured if configured else TAB_SUFFIX.get(kind, "")
    if value and not value.startswith(" "):
        value = f" {value}"
    return value


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


# ── Phase 07.2 Plan 01 (CITY-02): city scope layer ───────────────────────────
#
# Filtering (city_scope) and access control (which cities an admin is ALLOWED to pick) are
# TWO DIFFERENT LAYERS. This module only builds the former. Phase 8 (staff/capabilities)
# plugs into the latter — it will narrow the set of codes `set_admin_city` accepts and that
# `admin_selected_city` may return, WITHOUT touching `city_scope` itself and WITHOUT touching
# a single SQL query in `database/db.py`. Nothing here binds a manager to a city; any admin
# may pick any city today (owner decision, 07.2-CONTEXT.md).

ADMIN_CITY_KEY_PREFIX = "admin_city__"


def city_scope(code: str | None) -> tuple[str, tuple[str, ...]] | None:
    """Pure, sync city-scope descriptor for `database.db._city_clause` — no DB access, no
    await, no knowledge of SQL. `database/db.py` cannot import this module (it would create
    an import cycle: `cities.py` already imports `database.db`), so the resolved scope is
    handed to db.py BY VALUE as this `(code, exclude)` tuple, never by importing the registry.

    `code is None` -> `None` ("no scope" — the caller wants every row, unfiltered; this is
    what makes module-off / "no city chosen" collapse to a byte-identical, unfiltered query).

    Otherwise the code is resolved through `normalize_city` (the SAME collapse the Sheets tabs
    already use), then:
      - resolved is the DEFAULT city -> `(resolved, tuple_of_every_other_known_code)`. The
        default city is deliberately described by EXCLUSION of the other known cities, not by
        equality, because it must also catch `event_city IS NULL` (never-migrated / pre-cities
        rows) and any unknown/garbage code — exactly `normalize_city`'s semantics. An equality
        match on the default code would silently drop every NULL row from "Moscow" and split
        the admin's view from what the Sheets tab already shows.
      - resolved is any OTHER known city -> `(resolved, ())` — plain equality, no exclusion.
    """
    if code is None:
        return None
    resolved = normalize_city(code)
    if is_default_city(resolved):
        others = tuple(c["code"] for c in CITIES if c["code"] != resolved)
        return (resolved, others)
    return (resolved, ())


def refresh_city_filter_spec(spec: list[dict]) -> list[dict] | None:
    """WR-02: re-resolve every `event_city` filter's `exclude` against the CURRENT registry.

    A scheduled broadcast stores its filter spec as JSON, and the `exclude` list inside it is
    a SNAPSHOT of "the other known city codes" taken at the moment the manager built the
    filter. `EVENT_CITIES` is an .env list that is edited between scheduling and sending — a
    city added afterwards is not in the frozen `exclude`, so the default-city condition
    (`event_city NOT IN (...)`) stops excluding it and its delegates leak into a broadcast
    that was addressed to another city.

    Returns a NEW spec with `exclude` recomputed from the live registry, or `None` when an
    `event_city` filter names a code the registry no longer knows. `None` means "refuse to
    send" — silently normalizing an unknown code would redirect the whole broadcast to the
    DEFAULT city (`normalize_city`'s fallback), which is worse than not sending at all.

    Non-`event_city` filters pass through untouched; a spec without city filters is returned
    unchanged in content.
    """
    out: list[dict] = []
    for f in spec:
        if not isinstance(f, dict) or f.get("field") != "event_city":
            out.append(f)
            continue
        value = f.get("value")
        if not value:
            out.append(f)  # empty value -> db._build_filter_clause fails it closed (WR-01)
            continue
        if get_city(value) is None:
            return None
        out.append({**f, "exclude": list(city_scope(value)[1])})
    return out


async def admin_selected_city(admin_id: int) -> str | None:
    """The one city an admin panel is currently scoped to, or `None` meaning "no scope,
    show everything" (module off — the SINGLE point where "module disabled" collapses to
    "nothing is filtered", mirroring `city_row_tab`'s escape hatch). With the module on and
    no stored choice yet, defaults to the default city code (never raw NULL — a screen must
    always have something to render).

    Phase 09.1 (C, ROLE-03): a manager bound to a city (`database.db.get_staff_city`, and not
    a bootstrap superadmin) always returns their bound city, ignoring any value previously
    saved to `bot_settings` for them — a choice made before the binding existed (or before it
    was set) must not silently keep acting once the person is locked to a city."""
    if not await cities_module_on():
        return None
    if admin_id not in config.ADMIN_IDS:  # D-12: bootstrap superadmins stay unrestricted
        bound = await get_staff_city(admin_id)
        if bound:
            return normalize_city(bound)
    raw = await get_setting(f"{ADMIN_CITY_KEY_PREFIX}{int(admin_id)}")
    return normalize_city(raw)


async def set_admin_city(admin_id: int, code: str) -> bool:
    """Persist an admin's city choice in `bot_settings` (survives bot restarts — FSM does
    not). `code` must be a member of the closed set `city_codes()` (same guard shape as
    `handlers.admin.city_toggle`) — an unknown code is rejected and NOTHING is written,
    returning False. `admin_id` is coerced to `int` so the settings key is never assembled
    from an arbitrary string.

    Phase 09.1 (C, ROLE-03): this is the "right", not just the "filter" (07.2's own comment
    above already earmarks this exact seam) -- a manager bound to a city (`get_staff_city`)
    can only ever pick THEIR city; a bootstrap superadmin (`config.ADMIN_IDS`, D-12) or an
    unbound manager (`city IS NULL`) keeps today's unrestricted behavior."""
    if code not in city_codes():
        return False
    if admin_id not in config.ADMIN_IDS:
        bound = await get_staff_city(admin_id)
        if bound and normalize_city(code) != normalize_city(bound):
            return False
    await set_setting(f"{ADMIN_CITY_KEY_PREFIX}{int(admin_id)}", code)
    return True


# ── Phase 09.2 (A): per-city переопределение настроек ────────────────────────────────
#
# Обобщение уже существующей семьи "override-ключ на город" (`city_label__{code}`,
# `city_enabled__{code}`, `city_tab__{code}`, `admin_city__{admin_id}`) на ЛЮБОЙ ключ
# `SETTINGS_SCHEMA`, помеченный `"per_city": True` — реестр остаётся единственным
# источником правды о том, что можно переопределить по городу (CONTEXT A).
#
# Литерал `city` в разделителе — не случайность: он нужен, чтобы городской override не
# столкнулся с трековыми суффиксами `__short`/`__party`, которые уже живут в реестре
# (`approve_text__party`, `city_tab_suffix__short`). `approve_text__city__spb` и
# `approve_text__party` — два разных, не пересекающихся ключа.

PER_CITY_SEP = "__city__"


def per_city_key(key: str, code: str) -> str | None:
    """ЕДИНСТВЕННАЯ точка сборки ключа per-city переопределения — и для чтения, и для
    записи. Возвращает `None`, если `code` не входит в закрытое множество `city_codes()`
    (T-092-01/V5: код города только из реестра, никогда произвольная строка). Любой
    писатель (админ-UI, планы 09.2-05/06) обязан ходить через эту функцию и отказывать на
    `None` — собрать ключ строкой напрямую запрещено."""
    if code not in city_codes():
        return None
    return f"{key}{PER_CITY_SEP}{code}"


def is_per_city(key: str) -> bool:
    """Реестр — источник правды: ключ переопределяем по городу тогда и только тогда,
    когда несёт `SETTINGS_SCHEMA[key]["per_city"] is True`."""
    return bool(SETTINGS_SCHEMA.get(key, {}).get("per_city"))


async def get_setting_for_city(key: str, city_code: str | None) -> str | None:
    """Резолвер с фолбэком на глобальное значение (CONTEXT A). Модуль городов выключен,
    ИЛИ `key` не `per_city`, ИЛИ `city_code is None` (город делегата ещё не известен) ->
    возвращается РОВНО `get_setting(key)` — байт-в-байт сегодняшнее поведение, тот же
    контракт "module off -> collapse to plain read", что у `admin_selected_city`
    (T-092-02: переопределение физически не может протечь, пока модуль выключен — все три
    ранних выхода читают глобальный ключ ДО первого обращения к городскому ключу)."""
    if not is_per_city(key):
        return await get_setting(key)
    if city_code is None:
        return await get_setting(key)
    if not await cities_module_on():
        return await get_setting(key)
    code = normalize_city(city_code)
    override_key = per_city_key(key, code)
    if override_key:
        raw = await get_setting(override_key)
        if raw:  # пустая строка = "переопределения нет" (CONTEXT A), не NULL и не ""
            return raw
    return await get_setting(key)


async def get_setting_typed_for_city(key: str, city_code: str | None):
    """Типизированный вариант `get_setting_for_city`: та же лестница ранних выходов, но
    фолбэк — `get_setting_typed(key)`, а найденный override пропускается через
    `_parse_setting(key, raw)` с БАЗОВЫМ ключом (метаданные типа лежат у базового ключа
    реестра, не у составного `{key}__city__{code}`)."""
    if not is_per_city(key):
        return await get_setting_typed(key)
    if city_code is None:
        return await get_setting_typed(key)
    if not await cities_module_on():
        return await get_setting_typed(key)
    code = normalize_city(city_code)
    override_key = per_city_key(key, code)
    if override_key:
        raw = await get_setting(override_key)
        if raw:
            return _parse_setting(key, raw)
    return await get_setting_typed(key)


async def city_override_codes(key: str) -> list[str]:
    """Коды городов (в порядке `CITIES`), у которых есть непустое переопределение `key`.
    `[]`, если `key` не `per_city` или модуль городов выключен. Используется админ-UI для
    пометок "✅ своё" / "— как везде" и строки "Переопределено для: …"."""
    if not is_per_city(key):
        return []
    if not await cities_module_on():
        return []
    out: list[str] = []
    for c in CITIES:
        code = c["code"]
        override_key = per_city_key(key, code)
        if override_key and await get_setting(override_key):
            out.append(code)
    return out
