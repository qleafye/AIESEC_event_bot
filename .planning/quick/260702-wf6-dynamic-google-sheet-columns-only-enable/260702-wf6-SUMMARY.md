---
quick_id: 260702-wf6
slug: dynamic-google-sheet-columns-only-enable
date: 2026-07-02
status: complete
---

# Quick 260702-wf6 — Summary

Google Sheet now projects to only the enabled-question columns; duplicate expectation/comment
labels disambiguated.

## What changed

### `handlers/registration.py`
- Replaced the static 44-entry `SHEET_HEADERS` + positional `_build_sheet_row` with a single
  `SHEET_COLUMNS` spec: each column is `(header, gate_setting_or_None, value_fn)`. `gate=None`
  → system column (always); `gate=reg_q_*` → present only when that question is enabled.
- `SHEET_HEADERS` (full list) and `_build_sheet_row` (full row) kept, derived from the spec —
  used by existing tests / as reference.
- Added `active_sheet_headers()`, `active_sheet_row(data)`, `_sheet_value_map(data)` — the live
  dynamic projection. Header + row share the spec so they always align.
- Renamed admin labels: `Ожидания (общие)`, `Ожидания: организация`, `Ожидания: контент`,
  `Доп. комментарии` (was «Ожидания», «Ожид. от орг», «Ожид. от контента», «Комментарии»).

### Live paths wired to the projection
- `handlers/registration.py` finalize → `append_to_sheet(await active_sheet_row(data))`.
- `main.py` startup → `ensure_sheet_header(await active_sheet_headers())`.
- `handlers/admin.py` sync → compute `active_sheet_headers()` once, align each backfill row to it.

## Result
Sheet width follows the preset: **forum → 16 cols, conference → 20, all-on → 44** (was always 44).

## Safety / assumptions
Header + row derive from one spec → no width drift within a run. Physical header is created once;
event type is set before delegates register (user-confirmed workflow), so mid-event toggle
changes don't misalign existing rows. Renames are admin-facing only (delegate summary unaffected).

## Verification
- `python -m pytest tests/ -q -p no:asyncio` → **109 passed** (2 new: dynamic header shrink,
  full width when all on).
- Manual projection check: 44 → 16 (forum) / 20 (conf).

## Follow-ups
- Backlog: consider a Telegram Web App admin UI for settings (nicer than nested inline
  keyboards for 40 toggles + presets) — deferred, needs hosted HTTPS front + settings API.
