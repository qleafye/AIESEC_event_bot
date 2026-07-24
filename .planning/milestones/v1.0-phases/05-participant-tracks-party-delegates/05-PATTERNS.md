# Phase 5: Participant Tracks (Party Delegates) - Pattern Map

**Mapped:** 2026-07-20
**Files analyzed:** 5 modified (no new files required — every Phase 5 mechanic extends an
existing module; the 8 canonical mechanics from the mapper prompt are annotated inline below)
**Analogs found:** 5 / 5 (all in-repo; brownfield — every mechanic has a same-repo precedent,
several inside the same file that will be modified)

> No RESEARCH.md for this phase — everything below builds on Phase 1–4 patterns already in
> production. Prefer copying these exact shapes over inventing new ones.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `database/db.py` (modify) | model/migration | CRUD | itself — `_ensure_column` (Phase 1–4 blocks), `add_user` ON CONFLICT | exact (self) |
| `handlers/registration.py` (modify) | handler (FSM) | request-response | itself — `_is_step_enabled`/`_prompt`/`_get_enabled_steps`/`_extract_source_tag`/`REG_PRESETS`/`_decide_status`/`SHEET_COLUMNS` | exact (self) |
| `handlers/payment.py` (modify) | handler (FSM) | request-response | itself — `_parse_options` (2-field → 3-field, optional trailing field) | exact (self) |
| `handlers/admin.py` (modify) | handler | request-response | itself — questions toggle screen, `_toggle_module_setting`/`_toggle_approval_setting`, `REG_PRESETS` apply handler, `_PICKER_FIELDS`, application card | exact (self) |
| `services/sheets.py` (modify) | service (Sheets I/O) | file-I/O (Google Sheets API) | itself — `_get_sheet`/`append_to_sheet`/`ensure_sheet_header` (main tab) generalized to a second cached tab, alongside `sync_named_worksheet` (full-overwrite precedent) | role-match (needs generalization — see detail below) |

No new files are needed. `handlers/states.py` needs **no new states** — D-09 explicitly rules out
new `REG_FLOW`/FSM step keys; the party fork question and track switcher are callback/admin-only
mechanics, not new `Registration.*` FSM states.

---

## Pattern Assignments

### `database/db.py` — additive migrations (D-01, D-02)

**Analog:** itself — `_ensure_column` (db.py:31), Phase 1–4 migration call blocks (db.py:65–175), `add_user` (db.py:203), `mark_reg_started`/`clear_reg_started` (db.py:555/568).

**D-01 — `users.participant_type` column.** Copy the exact call style used for every prior
additive column (db.py:76, 146–150 etc.), placed in the migration block before `db.commit()`:
```python
# Phase 5 (TRACK-01, D-01): participant track — 'full' | 'party_overnight' | 'party_noovernight'.
# DEFAULT 'full' means every one of the ~590 live rows lands on the existing behavior with
# zero data loss, exactly like the Phase-1 status='approved' default (db.py:76).
await _ensure_column(db, "users", "participant_type", "TEXT DEFAULT 'full'")
```

**D-02 — `reg_started.participant_type` column.** `reg_started` already gets additive columns
this way (`nudged_at`, `last_step` — db.py:125/128). Do the same for the track, so a repeat
`/start` with no deep-link parameter can recover the track from the still-open `reg_started` row
before it is cleared by `finalize_registration`:
```python
# Phase 5 (D-02): track must survive a repeat /start with no deep-link param. reg_started is
# the ONLY persistent (non-FSM) store alive between flow-start and finalize (mark_reg_started
# writes it, clear_reg_started deletes it at db.py:568/1876-in-registration.py) — MemoryStorage
# does not survive a restart, so this is not optional.
await _ensure_column(db, "reg_started", "participant_type", "TEXT")
```

**`mark_reg_started` — extend to accept + persist the track** (copy shape at db.py:555–565, the
`ON CONFLICT DO UPDATE` idiom is already there — just widen the column list):
```python
async def mark_reg_started(telegram_id: int, username: str | None, participant_type: str | None = None):
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute('''
            INSERT INTO reg_started (telegram_id, username, started_at, participant_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                started_at=excluded.started_at,
                participant_type=COALESCE(excluded.participant_type, reg_started.participant_type)
        ''', (telegram_id, username, started_at, participant_type))
        await db.commit()
```
The `COALESCE(excluded.x, table.x)` guard is the exact idiom `add_user` already uses for
`resume_file_id`/`resume_text`/`resume_url`/`receipt_file_id` (db.py:253–255, 268) — a repeat
`/start` with **no** deep-link arg calls this with `participant_type=None` and must NOT clobber
the track recorded on the first call.

**Add a `get_reg_started_track(telegram_id)` reader** — model on any single-row lookup
(`get_user`, db.py:345–354):
```python
async def get_reg_started_track(telegram_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT participant_type FROM reg_started WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
```

**`add_user` — add `participant_type` to the INSERT column list, VALUES tuple, and
`ON CONFLICT DO UPDATE SET`** exactly like every other Phase 4 column was added (db.py:224,
283, 339 — three edit sites per column, same pattern each time):
```python
# in the INSERT column list (db.py:209-224):
..., bed_sharing, bed_partner, participant_type
# in ON CONFLICT DO UPDATE SET (db.py:227-283):
participant_type=excluded.participant_type,
# in the VALUES tuple (db.py:284-342):
data.get('participant_type', 'full'),
```

---

### `handlers/registration.py` — the core of Phase 5

**Analog:** itself throughout. Every mechanic below is a documented extension of an existing
function in this exact file — no new architecture, no new file.

#### Mechanic 1 (mapper prompt) — per-track setting override with global fallback (D-04, D-05)

**Analog:** `_is_step_enabled` (registration.py:335–339) and `_prompt` (registration.py:329–332)
are the two single resolution points this phase must extend to tri-state + per-track wording.

Current 2-state resolver:
```python
async def _is_step_enabled(setting_key: str) -> bool:
    val = await get_setting(setting_key)
    if val is None:
        return REG_DEFAULTS.get(setting_key, "on") == "on"
    return val == "on"
```

**D-04 tri-state resolver for the party track** — same shape, but reads the `__party`-suffixed
key FIRST and falls back to the existing global resolution when absent (inherit):
```python
def _is_party_track(participant_type: str | None) -> bool:
    return participant_type in ("party_overnight", "party_noovernight")


async def _is_step_enabled_for_track(setting_key: str, participant_type: str | None) -> bool:
    """D-03/D-04: __party is ONE namespace for both party sub-tracks (no per-subtrack split).
    Absent override key -> inherit the existing global _is_step_enabled result; present -> its
    explicit on/off wins. `full` track is byte-identical to today (never reads __party)."""
    if _is_party_track(participant_type):
        override = await get_setting(f"{setting_key}__party")
        if override is not None:
            return override == "on"
    return await _is_step_enabled(setting_key)
```
The admin toggle (handlers/admin.py, see below) must render three states from the SAME
`get_setting(f"{setting_key}__party")` read: `None` → "➕ Наследует", `"on"` → "✅ Вкл",
`"off"` → "❌ Выкл" — key-absence IS the inherit state (Claude's Discretion item resolved this
way to reuse `get_setting`/`set_setting`/`delete_setting` verbatim with zero schema change).

**D-05 wording override — extend `_prompt`, the single resolution point:**
```python
async def _prompt(step_key: str, default: str, participant_type: str | None = None) -> str:
    """Editable question wording. Phase 5 (D-05): party track checks reg_prompt_<key>__party
    first, else falls through to the existing global reg_prompt_<key>, else `default`."""
    if _is_party_track(participant_type):
        override = await get_setting(f"reg_prompt_{step_key}__party")
        if override:
            return override
    return await get_setting(f"reg_prompt_{step_key}") or default
```
Every call site in `_ask_step` (registration.py:437–658) currently reads
`await _prompt('age', 'default text')` with two args — the third `participant_type` arg needs a
default of `None` so every existing call site keeps working unchanged; only call sites that need
per-track wording pass `data.get("participant_type")` explicitly (`_ask_step` already has
`state`/`data` available via its caller, or thread it through `_ask_step`'s own signature the
same way `step`/`total` are threaded today).

#### Mechanic 2 — conditional step skipping from an earlier answer (D-08)

**Analog:** `_get_enabled_steps` (registration.py:348–383) already hosts six conditional-skip
rules of exactly this shape — this is rule #7, not a new mechanism:
```python
async def _get_enabled_steps(data: dict) -> list[str]:
    enabled = []
    edu_conditional = (await get_setting("edu_conditional") or "on") == "on"
    studying = str(data.get("education_status", "")).startswith("Да")
    participant_type = data.get("participant_type", "full")           # Phase 5
    for step_key, setting_key, *_rest in REG_FLOW:
        if not await _is_step_enabled_for_track(setting_key, participant_type):   # was _is_step_enabled
            continue
        if step_key == "informal_day" and data.get("attendance_format") == "Online":
            continue
        if step_key == "source" and data.get("_source_from_tag"):
            continue
        if step_key == "housing" and "arrival" in data and data.get("arrival") != "Заранее":
            continue
        if step_key == "bed_partner" and not str(data.get("bed_sharing", "")).startswith("Да"):
            continue
        # Phase 5 (D-08): housing/bed_sharing/bed_partner reuse the SAME steps, gated to the
        # overnight sub-track only. No new step keys, no new DB/sheet columns (D-08, D-09).
        if step_key in ("housing", "bed_sharing", "bed_partner") and participant_type != "party_overnight" \
                and participant_type not in (None, "full"):
            # party_noovernight (or any future non-full, non-overnight track) never sees these.
            continue
        if edu_conditional and step_key == "university" and not studying:
            continue
        ...  # remaining existing rules unchanged
        enabled.append(step_key)
    return enabled
```
Note: for `full` track, the existing `housing` gate on `arrival` (line 365) still applies
unchanged — the new rule only *adds* a party-specific exclusion, it never loosens the full-track
behavior. Order the new check so it does not fire for `full`/`None` (only excludes party tracks
that are NOT `party_overnight`).

#### Mechanic 3 — deep-link `/start` param sets state + suppresses a later question (D-10)

**Analog:** `_extract_source_tag` (registration.py:721–727) + its consumption in
`_start_registration_flow` (registration.py:983–996, the `_source_from_tag` skip flag) is the
exact precedent — "deep-link value is authoritative, don't ask the question that could
overwrite it."

```python
# Mirror _extract_referrer_id (registration.py:690-702) / _extract_source_tag (721-727):
# same "strip, match a literal prefix, return the mapped value or None" shape.
_PARTY_TAG_MAP = {"party_over": "party_overnight", "party_noover": "party_noovernight"}


def _extract_party_track(command_args: str | None) -> str | None:
    if not command_args:
        return None
    return _PARTY_TAG_MAP.get(command_args.strip())
```
`cmd_start` (registration.py:1074–1146) already resolves `referrer_id` and `source_tag` from
the SAME `command.args` string in sequence (lines 1120–1121) — add the party-track parse as a
third, independent extraction right beside them (a numeric arg is still `referrer_id`, `src_*`
is still a source tag — `_extract_party_track` only matches the two literal party tokens, so all
three extractors are mutually exclusive by construction, per D-10's explicit requirement):
```python
referrer_id = _extract_referrer_id(args, user_id)
source_tag = _extract_source_tag(args)
party_track = _extract_party_track(args)          # Phase 5 (D-10)
```

**Suppressing the fork question once the deep link is authoritative** — copy the
`_source_from_tag` flag pattern verbatim (registration.py:986, 996): store a
`_track_from_link` marker in FSM state at flow start; `_get_enabled_steps`'s (future) fork-question
rule checks it the same way the `source` skip rule checks `_source_from_tag` (registration.py:361):
```python
if step_key == "party_fork" and data.get("_track_from_link"):
    continue   # deep-link already set the track authoritatively — never let the user override it
```

#### Mechanic 4 — bulk preset button writing many `bot_settings` keys (D-07)

**Analog:** `REG_PRESETS` (registration.py:281–302) + its handler in admin.py
(`_apply_event_preset`, admin.py:2040–2048) is the exact shape to clone for the "🎉 Party" preset.

```python
REG_PRESETS = {
    "forum": { ... },   # unchanged
    "conf": { ... },    # unchanged
    "party": {
        "label": "🎉 Party",
        # Phase 5 (D-07): writes ONLY __party-suffixed keys — never touches the global reg_q_*
        # keys a live full-registration flow is reading. Applying this preset cannot disturb
        # an in-progress full delegate's questions (mirrors D-15's "new capability defaults
        # OFF until an admin deliberately acts" posture from Phase 4).
        "on": ["age", "phone", "vk", "city", "allergies", "food_pref"],   # step_keys, not setting_keys
    },
}
```
The apply function differs from `_apply_event_preset` in ONE way — it writes `f"reg_q_{step}__party"`
keys (never bare `reg_q_*`), and it does NOT touch `payment_enabled` (party pricing is a separate
concern, D-16/D-17, not a module on/off flag):
```python
async def _apply_party_preset() -> None:
    """D-07: bulk-write __party overrides only. Every step in REG_FLOW gets an explicit
    __party on/off, so re-tapping the preset is deterministic regardless of prior manual
    overrides — same determinism guarantee as _apply_event_preset (admin.py:2040-2048)."""
    on_set = set(REG_PRESETS["party"]["on"])
    for step_key, setting_key, *_rest in REG_FLOW:
        await set_setting(f"{setting_key}__party", "on" if step_key in on_set else "off")
```

#### Mechanic — per-form approval routing (D-13, D-14, D-15)

**Analog:** `_decide_status` (registration.py:64–68) already routes on form type to a per-form
moderation setting — this is a third branch, not a new mechanism:
```python
def _decide_status(reg_mode: str, full_setting: str, short_setting: str,
                    participant_type: str = "full", party_setting: str | None = None) -> str:
    """Phase 5 (D-13): party tracks resolve status from party_approval, completely
    independent of full_approval/short_approval — a party track never falls through to the
    reg_mode branch below it."""
    if participant_type in ("party_overnight", "party_noovernight"):
        setting = party_setting or "manual"
        return "pending" if setting == "manual" else "approved"
    setting = full_setting if reg_mode == "full" else short_setting
    return "pending" if setting == "manual" else "approved"
```
`finalize_registration` (registration.py:1880–1891) is the single call site — thread
`data.get("participant_type", "full")` and `await get_setting("party_approval")` through
alongside the existing `full_setting`/`short_setting` reads (registration.py:1883–1886).

**D-14 — party applications share the SAME tinder queue** (no new query, no new admin screen):
`get_pending_users`/`get_pending_count` (db.py:666–684) already select on `status='pending'`
across ALL rows — a party row with `status='pending'` is picked up automatically. Only
`_render_application_card` (admin.py:2246–2277) needs one new conditional line (see admin.py
section below) — do NOT build a second queue.

**D-15 — per-track approve text**, mirror `_prompt`'s two-key resolution
(`send_completion_and_bonus`, registration.py:1753–1774, reads `approve_text` at line 1759):
```python
async def _approve_text_for(participant_type: str) -> str:
    if _is_party_track(participant_type):
        override = await get_setting("approve_text__party")
        if override:
            return override
    return await get_setting("approve_text") or DEFAULT_APPROVE_TEXT
```
`send_completion_and_bonus` needs a `participant_type` parameter (fetch via `get_user(telegram_id)`
inside `approve_user` before calling it, since `approve_user` — registration.py:1777 — is called
by chat id only, with no FSM `data` dict available at that point, exactly like the existing
`payment_enabled` check at line 1787 already does its own `get_setting` read there).

#### `_start_registration_flow` — persist track at flow start (D-02)

**Analog:** registration.py:974–1009, the existing `mark_reg_started` call at line 977 and the
`referrer_id`/`source_tag` persistence-into-FSM-state pattern at lines 989–996:
```python
async def _start_registration_flow(message, state, referrer_id=None, source_tag=None,
                                     participant_type: str | None = None):
    existing_data = await state.get_data()
    # Phase 5 (D-02): resolve the effective track BEFORE the mark_reg_started write — a fresh
    # deep-link arg wins; otherwise inherit whatever was already recorded in this FSM session
    # (mirrors saved_referrer_id / saved_source_tag one line below).
    saved_track = participant_type or existing_data.get("participant_type", "full")
    try:
        await mark_reg_started(message.from_user.id, message.from_user.username, saved_track)
    except Exception as e:
        logger.error(f"Failed to mark reg_started for {message.from_user.id}: {e}")
    ...
    await state.update_data(participant_type=saved_track)
```

#### `cmd_start` — enforce the master gate (D-11a) + recover track on a bare repeat `/start`

**Analog:** registration.py:1074–1146, specifically the pre-selection gate at
registration.py:1091–1116 (the exact fail-soft `try/except` + early-`return` shape to copy for
the "party closed" message) and the `user and status != 'rejected'` branch at line 1129.

```python
# Phase 5 (D-11a): master toggle. Placed alongside the existing pre-selection gate
# (registration.py:1091-1116) — same fail-soft posture, same early return before any
# registration-flow code runs.
if party_track and (await get_setting("party_enabled") or "off") != "on":
    closed_text = await get_setting("party_closed_text") or (
        "Регистрация на вечеринку сейчас закрыта."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Перейти к полной регистрации", callback_data="party_fallback_full")
    ]])
    await message.answer(closed_text, reply_markup=kb)
    return   # D-11a: NEVER silently reroute — user must tap the button to opt into `full`
```
**Recovering the track on a bare repeat `/start` (D-02, SC#2):** when `command.args` carries no
party token AND the user has no `users` row yet, check the `reg_started` row written by the
first `/start` (`get_reg_started_track`, added to db.py above) before defaulting to `full`:
```python
if not party_track and not user:
    party_track = await get_reg_started_track(user_id) or None
```

#### `finalize_registration` — persist track to `users` + route the sheet write (D-11, D-12)

**Analog:** registration.py:1812–1941, specifically the `add_user` call (line 1868), the
`clear_reg_started` call (line 1876), and the fire-and-forget sheet append
(registration.py:1897–1901).

```python
data.setdefault("participant_type", "full")
await add_user(data)     # unchanged call site — add_user's new column (db.py) picks it up
...
await clear_reg_started(message.from_user.id)   # unchanged — deletes the whole row, track included

# Phase 5 (D-11/D-12): route to the party tab INSTEAD of the main sheet — never both.
try:
    if _is_party_track(data.get("participant_type")):
        _party_row = await party_sheet_row(data)          # new helper, mirrors active_sheet_row (registration.py:892-897)
        asyncio.create_task(append_to_party_sheet(_party_row))   # new services.sheets function, see below
    else:
        _sheet_row = await active_sheet_row(data)
        asyncio.create_task(append_to_sheet(_sheet_row))
except Exception as e:
    logger.error(f"Failed to schedule sheet append for {message.from_user.id}: {e}")
```

**D-11 — party sheet column set**, mirror `SHEET_COLUMNS`/`active_sheet_headers`/`active_sheet_row`
(registration.py:787–897) but as a SEPARATE, deliberately-curated list (no ВУЗ/резюме/course
columns that are always empty for a party guest):
```python
PARTY_SHEET_COLUMNS = [
    ("ID Telegram", None, lambda d: d.get("telegram_id") or "-"),
    ("Username", None, lambda d: d.get("username") or "-"),
    ("Дата регистрации", None, lambda d: d.get("registration_date") or "-"),
    ("Статус", None, _status_label),
    ("ФИО", None, lambda d: d.get("full_name") or "-"),
    ("Трек", None, lambda d: {"party_overnight": "С ночёвкой", "party_noovernight": "Без ночёвки"}.get(d.get("participant_type"), "-")),
    ("Телефон", "reg_q_phone", lambda d: d.get("phone") or "-"),
    ("ВК", "reg_q_vk", lambda d: d.get("vk_username") or "-"),
    ("Аллергии", "reg_q_allergies", lambda d: d.get("allergies") or "-"),
    ("Питание", "reg_q_food", lambda d: d.get("food_pref") or "-"),
    ("Проживание", "reg_q_housing", lambda d: d.get("housing") or "-"),
    ("Общая кровать", "reg_q_bed_sharing", lambda d: d.get("bed_sharing") or "-"),
    ("Сосед по кровати", "reg_q_bed_partner", lambda d: d.get("bed_partner") or "-"),
]


async def party_sheet_headers() -> list[str]:
    """Mirror active_sheet_headers (registration.py:855-864) but gate against the __party
    override (tri-state resolution), not the plain global _is_step_enabled."""
    out = []
    for header, gate, _fn in PARTY_SHEET_COLUMNS:
        if gate is None or await _is_step_enabled_for_track(gate, "party_overnight"):
            out.append(header)
    return out


async def party_sheet_row(data: dict) -> list:
    """Mirror active_sheet_row (registration.py:892-897) — no frozen-schema snapshot needed
    (Claude's Discretion: party volume is low enough that live headers are fine; add a
    party_sheet_header_schema snapshot later only if header drift becomes a real problem)."""
    headers = await party_sheet_headers()
    values = {h: fn(data) for h, _g, fn in PARTY_SHEET_COLUMNS}
    return [values.get(h, "-") for h in headers]
```

---

### `handlers/payment.py` — mechanic 8: optional trailing field in a pipe-delimited list (D-16, D-17, D-18)

**Analog:** `_parse_options` (payment.py:98–115) currently parses `label|price` and MUST keep
accepting exactly that 2-field shape (D-16's explicit backward-compat requirement — existing
RusCo `payment_options` config has no third field and must keep working byte-identical).

Current:
```python
def _parse_options(raw: str) -> list[tuple[str, int]]:
    options: list[tuple[str, int]] = []
    for line in (raw or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            label, price_raw = line.split("|", 1)
            label = label.strip() or "Участие"
            try:
                price = int(price_raw.strip())
            except ValueError:
                price = 0
        else:
            label, price = line, 0
        options.append((label, price))
    return options
```

**D-16 — extend the return type to a 3-tuple, track field optional and comma-separated:**
```python
def _parse_options(raw: str) -> list[tuple[str, int, set[str] | None]]:
    """'label|price' (2-field, unchanged) or 'label|price|track1,track2' (Phase 5, D-16).
    Third field is OPTIONAL — a bare 2-field line still parses exactly as before and its
    track set is None, meaning "offered to ALL tracks" (D-16's compat guarantee)."""
    options: list[tuple[str, int, set[str] | None]] = []
    for line in (raw or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 2:
            label = parts[0].strip() or "Участие"
            try:
                price = int(parts[1].strip())
            except ValueError:
                price = 0
            tracks = None
            if len(parts) >= 3 and parts[2].strip():
                tracks = {t.strip() for t in parts[2].split(",") if t.strip()}
        else:
            label, price, tracks = line, 0, None
        options.append((label, price, tracks))
    return options
```
**Every caller that unpacks `(label, price)` must widen to `(label, price, tracks)`** — there are
exactly 4 unpack sites: `start_payment_step` (payment.py:153, 157, 167),
`process_payment_option` (payment.py:198), and the registration-side price-preview
(`_payment_price_block`, registration.py:419–434, `for label, price in options:`).

**D-17 — index contract preserved, only the rendered keyboard is filtered:**
```python
async def start_payment_step(bot: Bot, telegram_id: int, participant_type: str = "full"):
    options = _parse_options(await get_setting("payment_options") or "")
    # D-17: filter the KEYBOARD only. i is the index into the FULL unfiltered `options` list —
    # a filtered-then-reindexed list would shift indices and make an already-sent keyboard
    # (built before a later settings edit) select the wrong tariff on tap.
    visible = [(i, label, price) for i, (label, price, tracks) in enumerate(options)
               if tracks is None or participant_type in tracks]
    paid = [t for t in visible if t[2] > 0]
    if len(visible) > 1 and paid:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{label} — {price} ₽", callback_data=f"pay_option:{i}")]
            for i, label, price in visible
        ] + [[_PAY_LATER_BTN]])
        ...
```
`process_payment_option` (payment.py:185–207) does NOT need the track filter at all — it already
indexes into the full `options` list by the tapped `i` (payment.py:189–191,198), which is exactly
what D-17 requires; leave that function's indexing untouched, only widen its tuple unpack.

**D-18 — no tariff for this track ⇒ treat as free** (same shape as the existing free-path branch
in `_show_payment_details`, payment.py:244–248):
```python
if not visible:
    # D-18: no tariff matches this track — same outcome as payment_enabled=off (fail through
    # to completion, never strand an approved user on a dead end).
    from handlers.registration import send_completion_and_bonus
    await send_completion_and_bonus(bot, telegram_id)
    return
```

---

### `handlers/admin.py` — mechanic 7: tri-state toggle cycle + settings wiring

**Analog:** the existing `reg_q_toggle:` boolean handler (admin.py:1998–2018) is the CLOSEST
analog but is explicitly 2-state (`get_setting` is None-or-"on"-or-"off" collapsed to a boolean).
Phase 5 needs all THREE observable states surfaced, which is closer in spirit to
`_toggle_approval_setting`'s 2-way cycle (admin.py:549–558) — extended to 3.

```python
async def _toggle_party_question(callback: types.CallbackQuery, setting_key: str):
    """D-04: cycle inherit(absent) -> on -> off -> inherit. Unlike toggle_reg_question
    (admin.py:1998-2018) which collapses None into a resolved boolean, this handler must
    read the RAW tri-state value (None vs 'on' vs 'off') so it can show and advance through
    all three — collapsing None early (like _is_question_on, admin.py:1919-1921) would make
    "inherit" indistinguishable from "off" and break the cycle."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    party_key = f"{setting_key}__party"
    current = await get_setting(party_key)   # None | "on" | "off" — do NOT collapse
    if current is None:
        new_val, label = "on", "✅ Вкл"
    elif current == "on":
        new_val, label = "off", "❌ Выкл"
    else:
        await delete_setting(party_key)      # back to inherit — key ABSENCE is the inherit state
        new_val, label = None, "➕ Наследует"
    if new_val is not None:
        await set_setting(party_key, new_val)
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (party): {label}", show_alert=True)
    # re-render the party-track question list (mirror admin.py:2016-2018)
```
`delete_setting` already exists (db.py:197–200) and is exactly the primitive needed to represent
"back to inherit" as key-absence — no new DB primitive required.

**D-06 — track switcher on the SAME screen** (admin.py:1939–1977, `render_questions_text` /
`build_questions_keyboard` / `show_reg_questions`). Add a state param threaded through both
render functions (FSM-free — store the currently-viewed track in `callback.message` edit state
via a leading keyboard row, mirroring how `admin_event_preset` (admin.py:2051–2069) renders a
one-shot inline picker):
```python
def _track_switcher_row(active: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=("• " if active == "full" else "") + "Полный", callback_data="reg_q_track:full"),
        InlineKeyboardButton(text=("• " if active == "party" else "") + "Party", callback_data="reg_q_track:party"),
    ]
```
Both `render_questions_text(track="full")` and `build_questions_keyboard(track="full")`
(admin.py:1939, 1954) get a `track` parameter; when `track == "party"`, each status check calls
`_is_step_enabled_for_track`-style tri-state resolution (reading `f"{setting_key}__party"` raw,
not through the collapsed boolean helper) instead of `_is_question_on` (admin.py:1919–1921).

**D-19 — `participant_type` joins `_PICKER_FIELDS`** (admin.py:1641–1645). One-line change gets
the value picker for free via `get_distinct_filter_values` (db.py:815–835, already generic over
any whitelisted column) — but `participant_type` must ALSO be added to `_FILTER_COLUMNS`
(db.py:785–790) or `get_distinct_filter_values`/`_build_filter_clause` will silently drop it
(the exact "non-whitelisted field is dropped" comment at db.py:810):
```python
# database/db.py:785-790
_FILTER_COLUMNS = {
    "city", "university", "status", "source", "payment_status",
    "local_committee", "department", "aiesec_role", "education_status",
    "course", "study_field", "position", "attendance_format",
    "participant_type",   # Phase 5 (D-19, TRACK-06 SC#8)
}

# handlers/admin.py:1641-1645
_PICKER_FIELDS = {
    "city", "university", "source", "status", "payment_status",
    "local_committee", "department", "aiesec_role", "education_status",
    "course", "study_field", "position", "attendance_format",
    "participant_type",   # Phase 5 (D-19)
}
```
Also add a `_FILTER_FIELD_LABELS["participant_type"] = "Трек"` entry (admin.py:1629–1637) and one
button to `_filter_menu_kb` (admin.py:1697–1717) — copy the existing button-pair shape exactly.

**D-07 apply-preset handler** — clone `admin_event_preset`/`preset_apply`/`preset_confirm`
(admin.py:2051–2113) verbatim for the party preset, calling `_apply_party_preset()` (defined in
the registration.py section above) instead of `_apply_event_preset`. It does NOT need the
confirm-dialog "will disturb current settings" warning text, since `__party` keys never
overlap with what a live full-form admin is looking at (D-07's whole point).

**D-13/D-14 — new module settings**, add to `SETTINGS_FIELDS`/module-toggle rows exactly like
`payment_enabled`/`consent_enabled` (admin.py:475–501, `_toggle_module_setting` at 573–584):
```python
[InlineKeyboardButton(text=party_toggle_text, callback_data="toggle_party_enabled")],
[InlineKeyboardButton(text=party_appr_txt, callback_data="settings_toggle_party_approval")],
[InlineKeyboardButton(text=party_fork_toggle_text, callback_data="toggle_party_fork_question")],
```
`party_approval` is `manual`/`auto` (same 2-value cycle as `full_approval`/`short_approval` —
reuse `_toggle_approval_setting(callback, "party_approval", "manual", "Модерация вечеринки")`
verbatim, admin.py:549–558). `party_enabled` and `party_fork_question` are plain on/off — reuse
`_toggle_module_setting` verbatim (admin.py:573–584).

**D-14 — track line on the shared application card**, one conditional line added to
`_render_application_card` (admin.py:2246–2277), right after the name line:
```python
track = user.get("participant_type") or "full"
if track != "full":
    track_label = {"party_overnight": "🎉 Трек: вечеринка с ночёвкой",
                    "party_noovernight": "🎉 Трек: вечеринка без ночёвки"}.get(track, f"🎉 Трек: {track}")
    lines.append(track_label)
```
No change needed to `_show_current_card`/`get_pending_users`/`appr_approve`/`appr_reject_*`
(admin.py:2296–2439) — they already operate on `status='pending'` across the whole `users` table
regardless of `participant_type` (D-14's "shared queue" requirement is satisfied by NOT adding
any track filter here).

---

### `services/sheets.py` — mechanic 5: writing to a SECOND worksheet tab (D-11)

**Analog:** `_get_sheet`/`_append_to_sheet_sync`/`append_to_sheet`/`ensure_sheet_header`
(sheets.py:18–47, 55–58, 115–145) is the exact per-row incremental-append shape needed for the
party tab — but it is hardcoded to ONE cached `_sheet` global bound to `config.GOOGLE_SHEET_TAB`.
`sync_named_worksheet` (sheets.py:159–170, 204–213) targets an arbitrary named tab but does a
**full overwrite** (`ws.clear()` + rewrite everything) — wrong shape for party, which needs
incremental per-registration appends exactly like the main sheet, just to a different tab.

**Generalize the caching + append/header functions to take a tab name**, mirroring the
`_get_sheet` lazy-cache-with-lock pattern (sheets.py:18–47) with a SECOND cache keyed by tab name
(simplest correct option — a `dict[str, worksheet]` cache, same lock):
```python
_named_sheets: dict[str, object] = {}
_named_sheets_lock = threading.Lock()   # same primitive as _sheet_lock (sheets.py:22)


def _get_named_sheet(tab_name: str):
    """D-11: second cached worksheet handle, parallel to _get_sheet (sheets.py:25-47) but
    keyed by tab name instead of a single global. Auto-creates the tab (mirrors _get_sheet's
    WorksheetNotFound branch at sheets.py:43-46) so the party tab needs no manual setup."""
    if tab_name in _named_sheets:
        return _named_sheets[tab_name]
    with _named_sheets_lock:
        if tab_name in _named_sheets:
            return _named_sheets[tab_name]
        gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows=1000, cols=30)
        _named_sheets[tab_name] = ws
        return ws


def _reset_named_sheet_cache(tab_name: str):
    _named_sheets.pop(tab_name, None)


def _append_to_named_sheet_sync(tab_name: str, data: list):
    _get_named_sheet(tab_name).append_row(data)


async def append_to_named_sheet(tab_name: str, data: list):
    """D-11/D-12: fire-and-forget row append to a SECOND tab. Mirror append_to_sheet's
    retry/backoff loop verbatim (sheets.py:115-133) — same MAX_RETRIES/RETRY_DELAYS, same
    fail-soft posture, PII-safe logging (id only, not the row — WR-06 precedent sheets.py:125)."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        logger.warning("Google Sheet ID or Credentials not set. Skipping party sheet export.")
        return
    for attempt in range(MAX_RETRIES):
        try:
            await asyncio.to_thread(_append_to_named_sheet_sync, tab_name, data)
            logger.info(f"Appended party row for telegram_id={(data[0] if data else '?')!r} to tab {tab_name!r}")
            return
        except Exception as e:
            _reset_named_sheet_cache(tab_name)
            delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
            logger.warning(f"Party sheet append attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)
    logger.error(f"Failed to append party row after {MAX_RETRIES} attempts for telegram_id={(data[0] if data else '?')!r}")


def _ensure_named_header_sync(tab_name: str, headers: list[str]):
    """Mirror _ensure_header_sync (sheets.py:60-86) exactly, targeting the named tab."""
    sheet = _get_named_sheet(tab_name)
    if sheet.col_count < len(headers):
        sheet.add_cols(len(headers) - sheet.col_count)
    col1 = sheet.col_values(1)
    if not col1:
        sheet.append_row(headers)
        return
    first = (col1[0] or "").strip()
    if first.lstrip("-").isdigit():
        sheet.insert_row(headers, 1)
        return
    current = [h.strip() for h in sheet.row_values(1)]
    if current != headers:
        end = gspread.utils.rowcol_to_a1(1, len(headers))
        sheet.update(values=[headers], range_name=f"A1:{end}")


async def ensure_named_sheet_header(tab_name: str, headers: list[str]):
    """Fail-soft, mirror ensure_sheet_header (sheets.py:136-145)."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return
    try:
        await asyncio.to_thread(_ensure_named_header_sync, tab_name, headers)
    except Exception as e:
        _reset_named_sheet_cache(tab_name)
        logger.warning(f"ensure_named_sheet_header({tab_name!r}) failed (skipping): {e}")
```
Then in `handlers/registration.py`, `append_to_party_sheet(data)` (referenced in the
`finalize_registration` excerpt above) is a one-line wrapper:
```python
PARTY_SHEET_TAB_DEFAULT = "Party"


async def append_to_party_sheet(data: list):
    tab = await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT   # admin-configurable, Claude's Discretion
    from services.sheets import append_to_named_sheet
    await append_to_named_sheet(tab, data)
```
Header creation for the party tab is a startup concern exactly like the main sheet's
`ensure_sheet_header` call in `main.py` (main.py:80) — add a parallel `_spawn(ensure_named_sheet_header(tab, await party_sheet_headers()))`
call gated on `party_enabled == "on"` (skip entirely when the track is off, so a bot that never
turns party on never creates the tab).

Reference for "tab name resolved from `bot_settings` with a hardcoded default" — the exact same
idiom `services/allowlist.py` already uses for its own second tab (`DEFAULT_TAB = "Отобранные"`,
allowlist.py:14, `await get_setting("preselect_tab") or DEFAULT_TAB`, allowlist.py:54).

---

## Shared Patterns

### Additive migration (never break live rows)
**Source:** `database/db.py` `_ensure_column` (db.py:31–35) + every call site in `init_db()`.
**Apply to:** `users.participant_type`, `reg_started.participant_type`.
```python
await _ensure_column(db, "users", "participant_type", "TEXT DEFAULT 'full'")
```
`DEFAULT 'full'` is mandatory here (not `NULL`) — the ROADMAP success criterion #1 requires every
one of the ~590 live rows to read back as `full` with zero data loss, mirroring the Phase-1
`status TEXT DEFAULT 'approved'` precedent (db.py:76) exactly.

### Tri-state override-with-fallback (key absence = inherit)
**Source:** `get_setting`/`set_setting`/`delete_setting` (db.py:179–200) — the three primitives
already support "present with a value" / "present, different value" / "absent" with zero schema
change. `_is_step_enabled` (registration.py:335–339) is the boolean 2-state precedent this phase
widens to 3-state by checking a `__party`-suffixed key first.
**Apply to:** every `reg_q_<step>__party`, `reg_prompt_<step>__party`, `approve_text__party` key.
```python
override = await get_setting(f"{base_key}__party")
if override is not None:
    return override == "on"        # or return override directly for text keys
# else: fall through to the existing global resolution — inherit
```

### Fail-soft gate before the registration flow starts
**Source:** the pre-selection gate in `cmd_start` (registration.py:1089–1116) — try/except around
the whole gate, early `return` on rejection, never lets a gate bug crash `/start`.
**Apply to:** `party_enabled` master toggle (D-11a).

### Settings-driven module on/off (default OFF for a brand-new capability)
**Source:** `_toggle_module_setting` (admin.py:573–584), Phase 4 `payment_enabled`/
`consent_enabled` precedent, Phase 3 preselect-gate precedent (CONTEXT.md D-15 lineage).
**Apply to:** `party_enabled`, `party_fork_question` — both default `"off"`; live full-registration
flow is byte-identical until an admin deliberately flips them (matches D-11a/D-10's explicit
"nothing changes until tapped" requirement, echoing Phase 4 D-15).

### Bulk-preset write, isolated key namespace
**Source:** `REG_PRESETS` + `_apply_event_preset` (registration.py:281–302, admin.py:2040–2048).
**Apply to:** the party preset — writes ONLY `__party`-suffixed keys, deterministic (every
`REG_FLOW` step gets an explicit on/off, not just the ones being turned on).

### Positional index into an UNFILTERED list (never re-index after filtering)
**Source:** `pay_option:{i}` contract (payment.py:157, 189–198) — the existing single hazard
comment in this codebase about exactly this failure mode (`04-PATTERNS.md` "Known hazards").
**Apply to:** party-tariff-filtered payment keyboard (D-17) — filter the rendered buttons, never
the list you build `i` from.

### Second-tab Sheets write (separate header, name resolved from settings)
**Source:** `services/allowlist.py` (`DEFAULT_TAB`, `get_setting("preselect_tab")` — allowlist.py:14,54)
for the "second tab, admin-configurable name, hardcoded fallback" idiom; `_get_sheet`'s
lazy-cache-with-lock (sheets.py:18–47) for the caching shape; `append_to_sheet`'s retry/backoff
loop (sheets.py:115–133) for the fail-soft network wrapper.
**Apply to:** the party worksheet tab (D-11/D-12) — see the full `services/sheets.py` section
above for the concrete generalized functions.

### Fail-soft try/except for every user-facing send / DB write in the finalize path
**Source:** `finalize_registration` (registration.py:1849–1866 Nextcloud upload,
1874–1878 clear_reg_started, 1897–1901 sheet schedule, 1926–1930 admin notify loop) — five
separate try/except blocks in ONE function, each independently non-fatal.
**Apply to:** party-tab sheet scheduling, track-recovery `get_reg_started_track` lookup in
`cmd_start`, `party_sheet_row` computation — none of these may ever block registration.

---

## No Analog Found

| Concern | Role | Data Flow | Reason / Guidance |
|---------|------|-----------|--------------------|
| Incremental per-row append to a SECOND, admin-named tab | service | file-I/O | No prior code does this — `append_to_sheet` is single-tab-only (main), `sync_named_worksheet` is full-overwrite-only (Незавершённые export). See the `services/sheets.py` section above for the generalized functions to add; this is the one genuinely new piece of infrastructure in the phase, built by combining two existing idioms rather than inventing a third. |
| Tri-state (inherit/on/off) admin toggle UI | handler | request-response | Every existing admin toggle in this repo is 2-state (`reg_q_toggle:`, `_toggle_module_setting`, `_toggle_approval_setting` all cycle exactly 2 values). The 3-state cycle in the admin.py section above is new UI logic, though it reuses `get_setting`/`set_setting`/`delete_setting` unchanged. |

---

## Metadata

**Analog search scope:** `handlers/`, `database/`, `services/`
**Files scanned:** registration.py, admin.py, payment.py, db.py, sheets.py, allowlist.py, states.py, main.py
**Pattern extraction date:** 2026-07-20
**Note:** Every Phase 5 file is a modification of an existing module — no new Python file is
required. `handlers/states.py` was scanned and needs no changes (D-09: no new FSM step states).
