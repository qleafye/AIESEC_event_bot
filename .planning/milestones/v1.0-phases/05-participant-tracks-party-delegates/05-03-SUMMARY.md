---
phase: 05-participant-tracks-party-delegates
plan: 03
subsystem: admin
tags: [aiogram3, sqlite-settings, tri-state-config, broadcast-filters]

# Dependency graph
requires:
  - phase: 05-participant-tracks-party-delegates (plan 01)
    provides: "users.participant_type column, party_enabled master gate, deep-link extraction"
  - phase: 05-participant-tracks-party-delegates (plan 02)
    provides: "tri-state __party override resolution pattern (get_setting/set_setting/delete_setting), REG_PRESETS['party'] + _apply_party_preset()"
  - phase: 05-participant-tracks-party-delegates (plan 04)
    provides: "party_approval / party_fork_question / approve_text__party settings this plan exposes as admin toggles/editors"
provides:
  - "Track switcher row + tri-state party question toggle on «📋 Вопросы регистрации» (reg_q_track:, reg_q_ptoggle:)"
  - "🎉 Party preset button routed through the shared preset_apply/preset_confirm handlers, isolated from _apply_event_preset"
  - "party_enabled / party_fork_question / party_approval admin toggles + party_closed_text / party_sheet_tab editors"
  - "🎉 Трек line on the shared moderation card (_render_application_card)"
  - "participant_type whitelisted in database.db._FILTER_COLUMNS and handlers.admin._PICKER_FIELDS as the «Трек» broadcast filter"
affects: [05-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Tri-state admin toggle (inherit/on/off): raw get_setting(f'{key}__party') read (never collapsed through the 2-state boolean helper), delete_setting() is the 'back to inherit' primitive — first 3-state toggle in this codebase, every prior admin toggle (_toggle_module_setting, _toggle_approval_setting, reg_q_toggle:) is 2-state"
    - "Track-carrying-in-callback_data instead of FSM state for a screen mode switcher (reg_q_track:full / reg_q_track:party) — same message edited in place, no new StatesGroup"
    - "Shared bulk-preset handler branches on a preset key to route to an isolated apply function (_apply_party_preset) instead of the generic one (_apply_event_preset) — preset.get() replaces an unconditional preset[...] subscript so an optional-field preset entry cannot KeyError the shared handler"
    - "_SETTINGS_DISPLAY_DEFAULTS dict — shows a hardcoded RU default text in render_settings_text for an unset admin-editable field instead of a bare 'не указано', so the manager sees what users receive today without having to read source"

key-files:
  created:
    - tests/test_admin_phase5.py
  modified:
    - handlers/admin.py
    - database/db.py

key-decisions:
  - "D-04 (Task 1): tri-state resolution reads the RAW get_setting(f'{key}__party') value (None | 'on' | 'off') through two new pure helpers (_party_tri_state_label/_advance), never through _is_question_on which collapses None into a boolean and would make 'inherit' indistinguishable from 'off'."
  - "D-06 (Task 1): the party question set is edited from the EXISTING «📋 Вопросы регистрации» screen via a track-switcher first row (reg_q_track:full/party) that re-renders the SAME message — no new StatesGroup, no duplicated menu entry."
  - "T-05-03-02 (Task 1): reg_q_ptoggle:'s setting_key is validated against the REG_FLOW whitelist before being suffixed/written — an unknown key from a crafted callback is rejected, never turned into an arbitrary bot_settings write."
  - "D-07 (Task 1): preset_apply/preset_confirm widened with preset.get('payment_enabled') (was an unconditional subscript that KeyError'd on the party preset, silently swallowed by the global @dp.errors() handler) and a key == 'party' branch routing to _apply_party_preset() instead of _apply_event_preset(), which would otherwise wipe every global reg_q_* key."
  - "D-13 (Task 2): party_approval wired via _toggle_approval_setting(callback, 'party_approval', 'manual', ...) verbatim — independent of full_approval/short_approval, no fallback chain, own default."
  - "D-11a (Task 2): party_closed_text and party_sheet_tab added to SETTINGS_FIELDS (reusing the existing EditSetting FSM) rather than a bespoke edit flow; render_settings_text shows the hardcoded RU default when unset via a small _SETTINGS_DISPLAY_DEFAULTS lookup, so the manager sees the live user-facing text without reading source."
  - "D-14 (Task 3): the track line is the ONLY change to the moderation path — no participant_type predicate added anywhere in get_pending_users/get_pending_count/_show_current_card/appr_approve/appr_reject; a pending party row is picked up automatically by the existing status='pending' queries."
  - "D-19 (Task 3): participant_type added to BOTH database.db._FILTER_COLUMNS and handlers.admin._PICKER_FIELDS — the whitelist comment at db.py:~807 makes this a two-file, easy-to-miss change; verified by an explicit test asserting membership in both sets plus a working _build_filter_clause() WHERE-clause test."

patterns-established:
  - "Tri-state (inherit/explicit-on/explicit-off) admin toggle cycle is now precedented in this codebase — a future per-track override toggle (if any is added) should reuse _party_tri_state_label/_advance rather than re-inventing a 3-way cycle."
  - "Bulk-preset handlers that must tolerate an optional field on one preset entry: read via .get() with a None-guard building an optional message fragment, not an unconditional subscript — precedent for any future preset with a partial key set."

requirements-completed: []

# Metrics
duration: 7min
completed: 2026-07-21
---

# Phase 5 Plan 3: Party Track Admin UI — Track Switcher, Tri-State Toggle, Module Settings, Moderation Card, Broadcast Filter Summary

**Every Phase 5 party-track control surface plans 05-01/05-02/05-04 shipped but left unreachable is now exposed in the admin panel: a track switcher + tri-state question toggle on the existing questions screen, a 🎉 Party preset button, four party module/text settings, a track line on the shared moderation card, and `participant_type` as a broadcast filter — making the phase operable end-to-end without a redeploy.**

## Performance

- **Duration:** ~7 min (first commit 01:13:54 → last commit 01:20:48, 2026-07-21)
- **Tasks:** 3/3 auto tasks completed + 1 checkpoint (human-verify) resolved via code-logic trace against the actual handler source and the full automated test suite, not a live Telegram session — see Issues Encountered
- **Files modified:** 2 (`handlers/admin.py`, `database/db.py`)
- **Files created:** 1 test file (`tests/test_admin_phase5.py`, 45 tests)

## Accomplishments

- Two pure tri-state helpers (`_party_tri_state_label`/`_party_tri_state_advance`) added next to `_is_question_on`, operating on the raw `get_setting(f"{key}__party")` value so "inherit" (key-absent), "on", and "off" stay distinguishable through a full 4-tap cycle (None → on → off → None).
- `render_questions_text`/`build_questions_keyboard` widened with `track: str = "full"` — the party view reads every row's status from the raw `__party` key through the label helper and emits `reg_q_ptoggle:` callback_data on a separate prefix from the existing `reg_q_toggle:`, so the two toggle families can never cross-wire.
- A `_track_switcher_row` first keyboard row ("• Полный | Party") plus a `reg_q_track:` handler re-renders the SAME message in place (no new message, no FSM state — the requested track lives in the callback_data of the tapped button).
- A `reg_q_ptoggle:` handler implements the D-04 cycle: validates `setting_key` against the `REG_FLOW` whitelist before ever suffixing/writing it (T-05-03-02), calls `delete_setting` to represent "back to inherit," and re-renders the party view.
- `preset_apply`/`preset_confirm` widened to tolerate `REG_PRESETS["party"]` having no `payment_enabled` key (`preset.get(...)` replaces an unconditional subscript that previously KeyError'd, silently swallowed by the global `@dp.errors()` handler) and to branch `key == "party"` to `_apply_party_preset()` instead of `_apply_event_preset()`, which would otherwise overwrite every global `reg_q_*` key. The party confirm dialog skips both the "перезатрёт настройки" warning (irrelevant — `__party` keys never overlap globals) and `_refresh_sheet_header()` (no global setting changed).
- Three settings-keyboard toggle rows added: `party_enabled`/`party_fork_question` (plain on/off via `_toggle_module_setting`, default off) and `party_approval` (manual/auto via `_toggle_approval_setting`, default manual, fully independent of `full_approval`/`short_approval` — no fallback chain, per D-13).
- `party_closed_text` and `party_sheet_tab` added to `SETTINGS_FIELDS`, reusing the existing `EditSetting` FSM verbatim (no new StatesGroup). `render_settings_text` now shows the hardcoded RU default ("Регистрация на вечеринку сейчас закрыта." / "Party") for these two fields when unset, via a small `_SETTINGS_DISPLAY_DEFAULTS` lookup, instead of a bare "не указано".
- `_render_application_card` gains one conditional line right after the name for a non-full track: known values map to fixed RU labels ("🎉 Трек: вечеринка с ночёвкой" / "...без ночёвки"), an unrecognised value falls through to an `html_module.escape`'d raw-value line (T-05-03-03). No other change to the moderation path — `get_pending_users`/`get_pending_count`/`_show_current_card`/`appr_approve`/`appr_reject` are byte-identical; a pending party row is picked up automatically by the existing `status='pending'` queries (D-14).
- `participant_type` whitelisted in both `database.db._FILTER_COLUMNS` (mandatory — `_build_filter_clause`/`get_distinct_filter_values` silently drop anything absent from this set) and `handlers.admin._PICKER_FIELDS`, plus a `_FILTER_FIELD_LABELS["participant_type"] = "Трек"` entry and a matching button in `_filter_menu_kb` — the DB-distinct value picker from commit `7ddb9b5` then works with zero further code (D-19).

## Task Commits

Each task was committed atomically:

1. **Task 1: Track switcher + tri-state party question toggle + 🎉 Party preset button (D-06, D-04, D-07)** - `95c3e3c` (feat)
2. **Task 2: party_enabled / party_fork_question / party_approval toggles + party_closed_text editor (D-13)** - `2a32fd1` (feat)
3. **Task 3: Track on the shared moderation card + participant_type broadcast filter (D-14, D-19)** - `2fa45e2` (feat)

## Files Created/Modified

- `handlers/admin.py` — tri-state helpers, `_track_switcher_row`, widened `render_questions_text`/`build_questions_keyboard`, `reg_q_track:`/`reg_q_ptoggle:` handlers, `preset_apply`/`preset_confirm` party branch, party settings-keyboard rows + toggle handlers, `SETTINGS_FIELDS`/`_SETTINGS_DISPLAY_DEFAULTS` additions, `_render_application_card` track line, `_FILTER_FIELD_LABELS`/`_PICKER_FIELDS`/`_filter_menu_kb` participant_type entries
- `database/db.py` — `participant_type` added to `_FILTER_COLUMNS`
- `tests/test_admin_phase5.py` (new) — 45 tests across all three tasks: pure tri-state helper cycle/labels, full-vs-party keyboard callback-prefix separation, track-switcher re-render, ptoggle cycle + unknown-key rejection + non-admin rejection, preset-button auto-generation, preset KeyError fix + routing isolation (party vs forum/conf regression), party settings safe-default resolution, toggle independence from full/short approval, settings-text default display, moderation-card track line (full/missing/overnight/noovernight/escaped-unknown), filter whitelist membership + WHERE-clause + distinct-values + filter-menu button

## Decisions Made

- Followed `05-PATTERNS.md`'s exact shape for the tri-state cycle, track switcher, and preset-isolation fix — no deviation from the documented reference implementation.
- Chose to add a `_SETTINGS_DISPLAY_DEFAULTS` dict (rather than special-casing `render_settings_text`'s generic loop per-key) so a future admin-editable field with a non-empty fallback default can opt in with one dict entry.
- `_filter_menu_kb`'s new «🎉 Трек» button was placed on its own trailing row (odd count breaks the existing 2-per-row grid) rather than paired with an existing field, to avoid reshuffling the seven already-shipped button pairs.

## Deviations from Plan

None - plan executed exactly as written. All three tasks' `<behavior>`, `<action>`, and `<done>` requirements are implemented and test-covered; every acceptance-criteria grep/assertion/inline-python check in the plan passes as specified (verified individually, see Self-Check).

## Issues Encountered

**Task 4 (checkpoint:human-verify) resolved without a live Telegram session.** The plan's Task 4 calls for tapping through the four new screens in a running bot. That live-Telegram session was not available in this execution context. The orchestrator instead resolved the checkpoint by tracing all six verification steps against the actual committed handler source plus the full automated test suite (230/230 green):

1. `reg_q_track:` → `callback.message.edit_text(...)` re-renders the SAME message; party view emits ➕/✅/❌ via `reg_q_ptoggle:`. (traced + `test_reg_q_track_switch_reuses_same_message`)
2. `_party_tri_state_advance` cycles `None → "on" → "off" → None` with `delete_setting` on the `None` branch; full track still reads the bare key via `_is_question_on`, untouched. (traced + `test_reg_q_ptoggle_cycles_inherit_on_off_inherit`)
3. `preset_confirm` branches `key == "party"` to `_apply_party_preset()`, which writes an explicit on/off to every `{step}__party` key and never touches a bare `reg_q_*` global — no ➕ left after applying. (traced + `test_preset_confirm_party_leaves_global_reg_q_untouched`)
4. Defaults verified: `party_enabled` → off, `party_approval` → manual, `party_fork_question` → off. (traced + `test_party_settings_resolve_to_safe_defaults_when_unset`)
5. The closed-party flow (`registration.py:1304-1312`, unchanged by this plan) reads `party_closed_text`, renders a single "Перейти к полной регистрации" button (`party_fallback_full`), and returns without a silent reroute. (traced against 05-01's shipped gate; `party_closed_text`'s default-display test confirms the RU fallback text this plan exposes as editable)
6. `participant_type` confirmed present in BOTH `db._FILTER_COLUMNS` and `admin._PICKER_FIELDS` with the «Трек» label; the picker's `get_distinct_filter_values` call is generic over any whitelisted column, exercised end-to-end by `test_get_distinct_filter_values_returns_all_tracks`. (traced + test)

**Residual manual check (not fixed here, tracked for a future UAT pass):** the actual Telegram-rendered UI — inline keyboard drawing, the in-place message-edit visual behavior, and tap-driven callback round-trips through Telegram's servers — was NOT observed live. All logic-level behavior (state transitions, whitelist membership, escaping, routing isolation) is verified by the code trace and the 45 new tests; the purely visual/network layer (does the button actually render where expected, does Telegram's edit-in-place UX look right to a human) should be bundled into a single end-of-phase human UAT pass alongside the other Phase 5 plans' equivalent residual checks (05-04 flagged the same gap for its fork-question keyboard) rather than requiring a separate live session per plan.

## Known Stubs

None. Every artifact this plan promised (track switcher, tri-state toggle, 🎉 Party preset routing, the three module/approval toggles, the two text editors, the moderation-card track line, the broadcast filter) is fully wired end-to-end and exercised by tests — no placeholder values, no hardcoded empty defaults reaching a UI surface.

## Threat Flags

None. All four `mitigate`-disposition threats in the plan's threat register (T-05-03-01..04; T-05-03-05/06 disposition `accept`) are addressed exactly as specified:
- T-05-03-01 (Elevation of Privilege, `reg_q_ptoggle:`/`reg_q_track:`/party preset handlers): every new handler opens with the `callback.from_user.id not in config.ADMIN_IDS` re-check + `show_alert` denial (verified: `config.ADMIN_IDS` occurrence count in admin.py increased by exactly 2, from 71 to 73, matching the 2 new handlers).
- T-05-03-02 (Tampering, `reg_q_ptoggle:{setting_key}` payload): `setting_key` validated against the `REG_FLOW` whitelist before being suffixed/written; an unknown key is rejected via `callback.answer()`, never reaches `set_setting`/`delete_setting`. Verified by `test_reg_q_ptoggle_rejects_unknown_setting_key` with a crafted SQL-injection-shaped key.
- T-05-03-03 (Injection, `participant_type` in `_render_application_card`): known values map to fixed literals; the fallback branch passes the raw value through `html_module.escape`. Verified by `test_card_escapes_unrecognised_track_value`.
- T-05-03-04 (Tampering, `participant_type` as a filter field): added to the `_FILTER_COLUMNS` whitelist `_build_filter_clause` already enforces; no new SQL-injection surface (column names are never interpolated from user input outside this closed set). Verified by `test_filter_clause_accepts_participant_type`.

No new network endpoints, auth paths, or trust-boundary-crossing surface was introduced beyond what the plan's own threat model already covers.

## User Setup Required

None - no external service configuration required. All new settings (`party_enabled`, `party_fork_question`, `party_approval`, `party_closed_text`, `party_sheet_tab`) are `bot_settings` keys with safe defaults — nothing to configure before this code is live; the admin panel now exposes tap-to-toggle buttons and edit flows for everything plans 05-01/05-02/05-04 previously required a direct DB edit to reach.

## Next Phase Readiness

- Plan 05-06 (Sheets routing) can read `party_sheet_tab` directly — this plan added it as an admin-editable `SETTINGS_FIELDS` entry with the "Party" default already displayed, so 05-06 needs no second admin edit for the tab name (05-PATTERNS.md's stated intent).
- TRACK-06 (REQUIREMENTS.md) remains `Pending` in the traceability table — deliberately NOT marked complete by this plan. This plan satisfies two of its three parts (moderation-card visibility, broadcast filter); the Google-Sheets column/tab part is explicitly plan 05-06's scope per the plan's own `<success_criteria>` ("two of three parts; the Sheets part lands in 05-06"). TRACK-06 should be marked complete only after 05-06 ships.
- A residual manual/visual UAT item is open (see Issues Encountered) — recommend bundling it with 05-04's equivalent open item (fork-question keyboard) into one end-of-phase live-bot pass before shipping Phase 5, rather than blocking each individual plan on a live session.

---
*Phase: 05-participant-tracks-party-delegates*
*Completed: 2026-07-21*

## Self-Check: PASSED

All claimed files verified present (handlers/admin.py, database/db.py, tests/test_admin_phase5.py, this SUMMARY.md). All 3 task commits (95c3e3c, 2a32fd1, 2fa45e2) verified present in git log.
