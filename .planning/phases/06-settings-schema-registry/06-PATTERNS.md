# Phase 6: Settings-schema Registry - Pattern Map

**Mapped:** 2026-07-24
**Files analyzed:** 9 (1 new module, 6 modified consumers, tests extended)
**Analogs found:** 9 / 9 (registry module has no direct precedent — closest analog is the existing `SETTINGS_GROUPS`/`REG_FLOW` grouping-table shape it must generalize)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `settings_schema.py` (new) | config/registry module | CRUD (metadata lookup, no I/O) | `handlers/registration.py` `REG_FLOW`/`REG_CATEGORIES`/`REG_DEFAULTS`/`REG_LABELS` (combined shape) + `handlers/admin.py` `SETTINGS_GROUPS` | role-match (no single existing file *is* a per-key metadata dict; this generalizes two existing parallel tables) |
| `settings_schema.py::_parse_setting` (pure helper) | utility | transform | `services/reminders.py::_reminder_interval`/`_reminder_enabled`, `services/scheduler.py::_int_or_default`/`_parse_schedule_dt` | exact (same pure sync raw-string → typed-value shape) |
| `settings_schema.py::get_setting_typed` (async accessor) | service/accessor | CRUD (read-through) | `handlers/registration.py::_get_options` (raw-then-default-then-parse) | exact |
| `handlers/admin.py` `SETTINGS_FIELDS`/`SETTINGS_GROUPS`/`_SETTINGS_DISPLAY_DEFAULTS` → generated views | controller (module-level data + generator fns) | transform (registry → legacy shape) | `handlers/admin.py::_settings_group_keys`/`_settings_group_label`/`_settings_nav_groups` (existing leftover-safety generator pattern) | exact |
| `handlers/admin.py` `PHOTO_FIELDS`/`FILE_FIELDS` → registry `type: photo/file` | controller | transform | `handlers/admin.py:450-459` (current literal tables) + `build_settings_group_keyboard` upload-flow (lines 624-654) | exact |
| `handlers/admin.py::build_settings_keyboard` toggle buttons → registry `type: toggle` | controller | request-response | `handlers/admin.py::_is_question_on` (lines 2095-2097) + toggle idiom at 501/2097/2248 | exact |
| `handlers/registration.py::REG_DEFAULTS` → absorbed by registry | model/constants | CRUD (default lookup) | `settings_schema.py` new registry entries (`default` field per reg_q_* key) | exact (this IS the migration target) |
| `database/db.py::get_setting`/`set_setting`/`delete_setting` | model (raw I/O) | CRUD | unchanged — stays as-is, wrapped not replaced | exact (D-07: no change needed, just read for wrapping) |
| `services/reminders.py`, `services/scheduler.py`, `keyboards/builders.py` (REG-02 consumers) | service/utility | request-response / CRUD | each other (all three already do raw `get_setting` + local pure parse helper) | exact |
| `tests/test_settings_groups_c0x.py` (extend) | test | CRUD/transform verification | itself — existing `_admin_ready`/`FakeCallback`/`asyncio.run` scaffold | exact |

## Pattern Assignments

### `settings_schema.py` (new registry module)

**Analog 1 — grouping-table shape:** `handlers/admin.py:397-417` (`SETTINGS_GROUPS`)
**Analog 2 — key→default/label tables:** `handlers/registration.py:197-241` (`REG_DEFAULTS`), `:243-287` (`REG_LABELS`), `:88-135` (`REG_FLOW`)

**Existing group-table pattern to generalize into per-key dict** (`handlers/admin.py:397-417`):
```python
# Quick 260724-c0x: group→keys grouping (NOT a per-key metadata registry) so the settings
# landing screen can route into per-group sub-screens instead of dumping every field's value
# inline. Shape mirrors REG_CATEGORIES (handlers/registration.py) — (label, token, [keys]).
SETTINGS_GROUPS = [
    ("🎪 Событие/Медиа", "event", [
        "event_date", "event_time", "event_place_name", "event_place_address",
        "contact_person", "contact_vk", "contact_tg", "start_text", "event_name", "event_type",
    ]),
    ...
]
```

**Existing key→label/prompt table** (`handlers/admin.py:341-385`, `SETTINGS_FIELDS`):
```python
SETTINGS_FIELDS = [
    ("event_date", "🗓 Дата", "Введите дату форума"),
    ("event_time", "⌚ Время", "Введите время проведения"),
    ...
]
```

**Existing key→default table (target of absorption, D-06)** (`handlers/registration.py:197-241`):
```python
REG_DEFAULTS = {
    "reg_q_age": "on",
    "reg_q_vk": "on",            # ник в ВК (@username) — YL'26
    "reg_q_email": "off",
    ...
    "reg_q_resume": "off",
}
```

**Target shape (D-02, dict-by-key, plain dict — no dataclass per CONVENTIONS.md "no dataclasses for domain entities"):**
```python
SETTINGS_SCHEMA = {
    "event_date": {
        "type": "text", "group": "event", "label": "🗓 Дата",
        "prompt": "Введите дату форума", "default": None,
    },
    "reg_q_age": {
        "type": "toggle", "group": "reg_questions", "label": "🎂 Возраст",
        "prompt": None, "default": "on",
    },
    # photo/file entries — metadata only, passthrough parse (D-10):
    "program": {
        "type": "photo", "group": "event", "label": "📅 Программа",
        "prompt": "Отправьте фото программы (можно с подписью).", "default": None,
    },
}
```

---

### `settings_schema.py::_parse_setting(key, raw)` (pure sync helper, D-08)

**Analog 1 — int-type parse:** `services/reminders.py:22-28` (`_reminder_interval`)
```python
def _reminder_interval(raw: str | None) -> int:
    """Positive int seconds; None/empty/invalid/<=0 -> DEFAULT_INTERVAL."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return value if value > 0 else DEFAULT_INTERVAL
```

**Analog 2 — int-type parse (near-identical, second author):** `services/scheduler.py:34-42`
```python
# ── Pure helpers (no async, no DB — the unit-test surface) ────────────────────

def _int_or_default(raw, default: int) -> int:
    """Positive int or default. None/empty/garbage/<=0 -> default."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
```

**Analog 3 — date-type parse:** `services/scheduler.py:45-50`
```python
def _parse_schedule_dt(raw):
    """Parse admin datetime 'ДД.ММ.ГГГГ ЧЧ:ММ' -> datetime; None on bad input."""
    try:
        return datetime.strptime(raw.strip(), "%d.%m.%Y %H:%M")
    except (TypeError, ValueError, AttributeError):
        return None
```

**Analog 4 — list-type parse (newline / `;`-split):** `handlers/registration.py:188-195` (`_get_options`) and `keyboards/builders.py:56-64` (`get_source_kb`)
```python
async def _get_options(setting_key: str, defaults: list[str]) -> list[str]:
    """Admin-editable option list (newline text) with a hardcoded fallback."""
    raw = await get_setting(setting_key)
    if raw:
        items = [line.strip() for line in raw.splitlines() if line.strip()]
        if items:
            return items
    return list(defaults)
```
```python
async def get_source_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    custom = await get_setting("source_options")
    if custom:
        items = [line.strip() for line in custom.split("\n") if line.strip()]
    else:
        items = DEFAULT_SOURCE_OPTIONS
```

**Analog 5 — CANONICAL toggle parse idiom (byte-for-byte target for parse-equivalence test, D-15)** — three duplicated occurrences, all must reduce to the same `_parse_setting(key, raw)` output:
- `handlers/admin.py:501`:
```python
is_on = (v == "on") if v is not None else (REG_DEFAULTS.get(sk, "on") == "on")
```
- `handlers/admin.py:2095-2097` (`_is_question_on` — the one already extracted as a `_private` pure-ish async helper):
```python
async def _is_question_on(setting_key: str) -> bool:
    val = await get_setting(setting_key)
    return (val == "on") if val is not None else (REG_DEFAULTS.get(setting_key, "on") == "on")
```
- `handlers/admin.py:2247-2248`:
```python
val = await get_setting(setting_key)
current_on = (val == "on") if val is not None else (REG_DEFAULTS.get(setting_key, "on") == "on")
```

Semantics to preserve exactly in `_parse_setting`: `raw is not None` → `raw == "on"`; `raw is None` → fall back to the registry entry's `default` field compared to `"on"` (not always `True`/`"on"` — some toggles default `"off"`, e.g. `party_enabled`, `reg_q_email`). The **type-driven dispatch** (D-03) is new — no existing single function does `if type == "toggle": ... elif type == "int": ...` — this is synthesized from the parallel single-type helpers above, but the per-type parse bodies should be lifted verbatim from these analogs (int body from `_int_or_default`, date body from `_parse_schedule_dt`, list body from `_get_options`, toggle body from `_is_question_on`).

**Extraction convention this follows** (CONVENTIONS.md §Function Design):
> "When a piece of logic is independently testable (parsing, formatting, decision logic), it is deliberately extracted into a small `_private` function — this is the dominant refactoring pattern in this codebase."

---

### `settings_schema.py::get_setting_typed(key)` (async accessor, D-05/D-08)

**Analog — raw-then-default-then-parse async wrapper:** `handlers/registration.py:188-195` (`_get_options`, shown above) and the reminder loop call site `services/reminders.py:37-38`:
```python
interval = _reminder_interval(await get_setting("pending_reminder_interval"))
if _reminder_enabled(await get_setting("pending_reminder_enabled")):
```

**Target shape** (delegates to sync `_parse_setting`, mirrors `database/db.py:184-190` for the raw-read half):
```python
async def get_setting_typed(key: str):
    raw = await get_setting(key)  # from database.db — raw string I/O, unchanged (D-07)
    return _parse_setting(key, raw)
```

**Raw I/O being wrapped, unchanged** (`database/db.py:184-190`):
```python
async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
```

---

### `handlers/admin.py` — `SETTINGS_FIELDS`/`SETTINGS_GROUPS` as generated views (D-13/D-14)

**Analog — existing leftover-safety generator functions to reuse/extend** (`handlers/admin.py:420-448`):
```python
def _settings_group_keys(token: str) -> list[str]:
    """Keys for a given SETTINGS_GROUPS token, including leftover-safety: any SETTINGS_FIELDS
    key not placed in a declared group lands in the trailing «Прочие»/"misc" group so nothing
    is ever silently hidden (mirrors _categorized_question_keys leftover handling)."""
    for _, tok, keys in SETTINGS_GROUPS:
        if tok == token:
            return list(keys)
    if token == "misc":
        seen = {k for _, __, keys in SETTINGS_GROUPS for k in keys}
        return [k for k, _, _ in SETTINGS_FIELDS if k not in seen]
    return []


def _settings_group_label(token: str) -> str:
    for label, tok, _ in SETTINGS_GROUPS:
        if tok == token:
            return label
    if token == "misc":
        return "📦 Прочие"
    return token


def _settings_nav_groups() -> list[tuple[str, str]]:
    """(label, token) rows for the landing keyboard nav buttons — declared groups plus a
    trailing «Прочие» group ONLY if leftover keys exist."""
    rows = [(label, tok) for label, tok, _ in SETTINGS_GROUPS]
    if _settings_group_keys("misc"):
        rows.append(("📦 Прочие", "misc"))
    return rows
```

**Migration approach (D-13, coexistence invariant):** For the pilot `event` group, build `SETTINGS_FIELDS`/`SETTINGS_GROUPS` as a computed value at import time — e.g. `SETTINGS_FIELDS = [(k, v["label"], v["prompt"]) for k, v in SETTINGS_SCHEMA.items() if <migrated>] + <remaining literal tuples for unmigrated keys>`. Consumers (`render_settings_group_text`, `build_settings_group_keyboard`, `_settings_group_keys`) read the same module-level names unchanged (D-14) — no call-site rewrite needed for already-generic helpers.

---

### `handlers/admin.py` — `PHOTO_FIELDS`/`FILE_FIELDS` → registry `type: photo/file` (D-10)

**Current literal tables** (`handlers/admin.py:450-459`):
```python
PHOTO_FIELDS = [
    ("program", "📅 Программа", "Отправьте фото программы (можно с подписью)."),
    ("speakers", "🗣 Спикеры", "Отправьте одно фото со всеми спикерами (можно с подписью)."),
    ("start", "💬 Фото приветствия", "Отправьте фото для приветственного сообщения (/start)."),
    ("venue", "🏢 Площадка", "Отправьте фото площадки (можно с подписью)."),
]

FILE_FIELDS = [
    ("reg_bonus", "🎁 Бонус за регистрацию", "Отправьте файл или фото бонуса (можно с подписью)."),
]
```

**Consumer that must keep working unchanged (upload-flow stays special-cased per D-10)** (`handlers/admin.py:612-619`, `638-647`):
```python
if token == "event":
    for prefix, label, _ in PHOTO_FIELDS:
        photo = await get_setting(f"{prefix}_photo_file_id")
        lines.append(f"{label}: {'✅ загружена' if photo else '<i>— не задано</i>'}")
    for prefix, label, _ in FILE_FIELDS:
        photo = await get_setting(f"{prefix}_photo_file_id")
        doc = await get_setting(f"{prefix}_doc_file_id")
        lines.append(f"{label}: {'✅ загружен' if (photo or doc) else '<i>— не задано</i>'}")
```
Note the derived-key convention (`f"{prefix}_photo_file_id"` / `f"{prefix}_doc_file_id"`) — the registry entry's key is the *prefix* (`"program"`), not the actual `bot_settings` row key; `type: photo`/`type: file` in the registry documents this passthrough behavior but the upload/lookup mechanics stay exactly as above (D-10: "реестр описывает ключ, не заменяет upload-flow").

---

### `handlers/admin.py::build_settings_keyboard` toggle buttons → registry `type: toggle` (D-12)

**Current hardcoded toggle-button block** (`handlers/admin.py:521-590`), representative excerpt:
```python
async def build_settings_keyboard():
    reg_mode = await get_setting("registration_mode") or "short"
    toggle_text = "📝 Регистрация: ⚡ Краткая → 📋 Полная" if reg_mode == "short" else "📝 Регистрация: 📋 Полная → ⚡ Краткая"
    ...
    party_enabled = await get_setting("party_enabled") or "off"
    party_toggle_text = ("🎉 Трек вечеринки: ❌ Выкл → ✅ Вкл" if party_enabled != "on"
                         else "🎉 Трек вечеринки: ✅ Вкл → ❌ Выкл")
    ...
    buttons = [
        [InlineKeyboardButton(text=toggle_text, callback_data="settings_toggle_reg")],
        ...
        [InlineKeyboardButton(text=party_toggle_text, callback_data="toggle_party_enabled")],
    ]
```
This is bespoke per-toggle callback-data (`settings_toggle_reg`, `toggle_party_enabled`, etc.) — NOT a uniform `reg_q_toggle:<key>` pattern. The registry's `type: toggle` metadata (D-09) documents these keys for the "one file describes everything" goal, but D-12 explicitly defers the toggle-button-generation wave to *after* the text-group pilot proves the pattern — do not attempt to collapse these bespoke callback-data strings into a single generated loop in the same pass as REG-01/pilot; that is a separate wave per D-12/D-14 discretion note.

**Simpler, uniform toggle pattern already used for `reg_q_*` keys (better analog for a FUTURE generated-toggle-button loop):** `handlers/registration.py` question-keyboard building (`reg_q_toggle:<key>` callback prefix, parsed via `.split(":", 1)[1]` — see `handlers/admin.py:2245`) is the more uniform shape to imitate once the toggle wave starts, rather than the one-off `settings_toggle_*`/`toggle_*` strings above.

---

### `handlers/registration.py::REG_DEFAULTS` absorption (D-06)

**Current table to be deleted, replaced by `default` field per entry in `SETTINGS_SCHEMA`** (`handlers/registration.py:197-241`, excerpt shown above under registry section). All three read-sites (`admin.py:501`, `admin.py:2097` `_is_question_on`, `admin.py:2248`) currently do `REG_DEFAULTS.get(sk, "on")` — these become `SETTINGS_SCHEMA[sk]["default"]` (or route through `get_setting_typed(sk)` directly, collapsing the whole idiom to one call). **Existing tests that assert on `REG_DEFAULTS` directly** (`tests/test_registration_phase4.py`, `tests/test_registration_phase5.py` per canonical_refs) must stay green — if `REG_DEFAULTS` is fully removed rather than kept as a computed re-export, grep those test files for `REG_DEFAULTS` before deleting the name.

---

### Test files — extend `tests/test_settings_groups_c0x.py` (D-17)

**Analog — existing test scaffold to extend, not replace** (`tests/test_settings_groups_c0x.py:1-73`):
```python
"""...pytest-asyncio is unavailable in this env (see tests/test_db_phase5.py) — every async
helper is driven via asyncio.run() and config.DB_PATH points at a tmp_path file.
"""
import asyncio
from config import config
from database import db
from handlers import admin as admin_mod

ADMIN_ID = 900002

def _admin_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_groups_c0x.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]

# ── Task 1: coverage — every SETTINGS_FIELDS key lands in exactly one group (or leftover) ──

def test_settings_groups_cover_every_field_key():
    grouped_keys = [k for _, __, keys in admin_mod.SETTINGS_GROUPS for k in keys]
    all_keys = [k for k, _, _ in admin_mod.SETTINGS_FIELDS]
    leftover = admin_mod._settings_group_keys("misc")

    assert len(grouped_keys) == len(set(grouped_keys))
    for key in all_keys:
        assert key in grouped_keys or key in leftover, f"{key} missing from all groups"
    for key in grouped_keys + leftover:
        assert key in all_keys
```
**New tests to add, following this exact scaffold style (plain function, no pytest-asyncio, tmp_path + `_admin_ready`):**
- `test_registry_coverage_*` (D-17): every `SETTINGS_SCHEMA` key has exactly one `group`, `type` in the allowed taxonomy (D-04), `default` parses via `_parse_setting` without raising.
- `test_parse_equivalence_*` (D-15): pure, no `tmp_path`/DB needed — table-driven `(key, raw_input, expected)` asserting `_parse_setting(key, raw) == <old manual parse result>` for a matrix including `None`/empty/garbage, modeled on how `_reminder_interval`/`_int_or_default` are unit-tested elsewhere (no DB fixture — pure function test).
- `test_render_settings_snapshot_*` (D-16): capture `render_settings_text()`/`build_settings_keyboard()` output before/after migration for the `event` group pilot, reusing `_admin_ready`/`FakeCallback` from this same file.

---

## Shared Patterns

### Raw setting I/O — stays untouched, wrapped not replaced (D-07)
**Source:** `database/db.py:184-205`
**Apply to:** `settings_schema.py::get_setting_typed` only; all existing raw `get_setting`/`set_setting` call sites in `handlers/admin.py` (writes) remain untouched.
```python
async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
```

### Toggle on/off resolution idiom (must be byte-identical post-migration, D-15)
**Source:** `handlers/admin.py:501`, `:2095-2097`, `:2247-2248` (three duplicated instances)
**Apply to:** `_parse_setting`'s `toggle` branch; parse-equivalence test in `tests/test_settings_groups_c0x.py`.
```python
is_on = (v == "on") if v is not None else (REG_DEFAULTS.get(sk, "on") == "on")
```

### Pure helper extraction for testable parsing (CONVENTIONS.md §Function Design, D-08)
**Source:** `services/reminders.py:17-28`, `services/scheduler.py:34-50`
**Apply to:** every `_parse_setting` type-branch body (int/date/list/toggle) — lift these bodies verbatim rather than reinventing parse logic.
```python
def _int_or_default(raw, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
```

### Leftover-safety generator pattern (D-13, "nothing silently hidden")
**Source:** `handlers/admin.py:420-448` (`_settings_group_keys`/`_settings_group_label`/`_settings_nav_groups`)
**Apply to:** the registry-driven replacement for `SETTINGS_FIELDS`/`SETTINGS_GROUPS` computed views — any key present in `SETTINGS_SCHEMA` but not yet assigned to a migrated group must still surface via the existing `misc`/"📦 Прочие" fallback, not disappear.

### Lazy (function-local) import to break cycles
**Source:** `keyboards/builders.py:27-34`
**Apply to:** only if a reverse-dependency cycle actually appears when wiring `settings_schema.py` into `handlers/registration.py`/`handlers/admin.py` (per CONTEXT.md, the new module is designed with zero upstream deps, so this should not be needed — but if `registration.py` needs to import something from `admin.py` that isn't already imported, use this pattern rather than a module-level import).
```python
if telegram_id is not None:
    try:
        from handlers.payment import should_offer_receipt_upload
        if await should_offer_receipt_upload(telegram_id):
            kb.button(text="💳 Оплата")
    except Exception:
        pass
```

### Named-import convention for `database.db` functions
**Source:** `handlers/registration.py:16`
**Apply to:** any new file importing `get_setting`/`set_setting` — add the name to an existing import line rather than `import database.db as db`.
```python
from database.db import add_user, get_user, get_setting, set_setting, mark_reg_started, clear_reg_started, set_reg_step, set_user_subscribed, set_user_status, record_user_consent, get_user_consents, get_reg_started_track, _csv_safe
```

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| `settings_schema.py` type-driven dispatch (`if type == "toggle": ... elif type == "int": ...`) | utility | transform | No existing function dispatches parse behavior by a `type` tag across multiple setting kinds in one place — today each consumer hand-rolls its own parse per key. This is genuinely new structure; compose it from the per-type analogs listed above rather than searching further. |
| Registry-generated toggle-button loop (future wave, D-12) | controller | request-response | `build_settings_keyboard`'s toggle buttons use bespoke one-off callback-data strings (`settings_toggle_reg`, `toggle_party_enabled`, ...), not a uniform `type:key` loop — no existing code generates this section from a table. Closest partial-match is the `reg_q_toggle:<key>` uniform-prefix pattern in `handlers/registration.py`/`admin.py:2239-2258`, listed above as a discretionary reference for whenever the toggle wave starts. |

## Metadata

**Analog search scope:** `handlers/admin.py`, `handlers/registration.py`, `database/db.py`, `services/reminders.py`, `services/scheduler.py`, `keyboards/builders.py`, `tests/test_settings_groups_c0x.py`
**Files scanned:** 7 source files (targeted line-ranges only, no full-file reads on `admin.py`/`registration.py` — both are 2000+/3000+ lines), 1 test file (full read, 163 lines)
**Pattern extraction date:** 2026-07-24
