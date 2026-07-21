---
phase: 05-participant-tracks-party-delegates
verified: 2026-07-21T12:00:00Z
status: human_needed
score: 14/14 must-haves verified (code-level)
overrides_applied: 0
human_verification:
  - test: "Consolidated live-bot + live-Google-Sheet end-to-end UAT (10-step checklist from 05-06-SUMMARY.md, covering 05-03's track switcher/preset rendering and 05-04's fork-question keyboard too)"
    expected: "Party deep link asks only party questions, survives a bare repeat /start, lands in the shared moderation queue with the track line, approves with the party text, shows only party tariffs, and appends to a separate 'Party' Sheet tab with no ВУЗ/Курс/Резюме columns and no duplicate row on the main tab; ordinary full-delegate flow and legacy referrer_id/src_ deep links are byte-identical to pre-Phase-5 behavior"
    why_human: "Requires a running Telegram session and the real Google Sheet — inline-keyboard rendering, in-place message edits, and tap-driven callback round-trips cannot be exercised by static code/tests. No plan in this phase ran this live; all three human-verify checkpoints (05-03, 05-04, 05-06) were resolved by code trace + automated tests only, per an explicit coordinator decision to defer to one consolidated pass."
---

# Phase 5: Participant Tracks (Party Delegates) Verification Report

**Phase Goal:** Один бот обслуживает делегатов разных треков — полное участие и «только вечеринка» (с ночёвкой / без) — с отдельными наборами вопросов, модерацией и тарифами, настраиваемыми из админки без деплоя
**Verified:** 2026-07-21
**Status:** human_needed
**Re-verification:** No — initial verification (follows an in-session code review + 3 fixes: CR-01, WR-01, WR-03)

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria, cross-checked against live code)

| # | Truth (ROADMAP SC) | Status | Evidence |
|---|------|--------|----------|
| 1 | `users.participant_type` migrated `DEFAULT 'full'`, additive, no data loss | ✓ VERIFIED | `database/db.py:178-179` — `_ensure_column(db, "users", "participant_type", "TEXT DEFAULT 'full'")` + `_ensure_column(db, "reg_started", "participant_type", "TEXT")`, both idempotent additive migrations inside the existing `init_db()` block |
| 2 | Track persisted to DB at flow start, survives a bare repeat `/start` mid-flow | ✓ VERIFIED | `handlers/registration.py:1391-1398` reads `get_reg_started_track()` when no deep-link/user row exists; `_start_registration_flow` (:1183-1220) writes `mark_reg_started(..., saved_track)` at flow start and `users.participant_type` at finalize (`finalize_registration`:2235-2236) — the two-write-point design from D-02 |
| 3 | Admin can toggle a question per-track (`reg_q_<step>__party`) without affecting the global/full setting; unset → inherits global | ✓ VERIFIED | `_is_step_enabled_for_track` (`registration.py:407-419`) — `is not None` tri-state check, isolated `__party` suffix; admin UI in `admin.py:1992-2005` (`_party_tri_state_label/_advance`) and `:2145-2173` (`toggle_party_question`) cycles inherit→on→off via `delete_setting`/`set_setting` on the suffixed key only |
| 4 | `?start=party_over` / `?start=party_noover` set the track; existing referrer_id / `src_*` deep links unbroken | ✓ VERIFIED | `_extract_party_track` (`registration.py:836-842`) matches only the closed `_PARTY_TAG_MAP` dict; `cmd_start` (:1346-1348) calls all three extractors independently — `_extract_referrer_id`, `_extract_source_tag`, `_extract_party_track` — mutually exclusive by construction, confirmed by `tests/test_registration_phase5.py` deep-link tests |
| 5 | Fork question only shown when `party_fork_question=on` (default off); no extra screen otherwise | ✓ VERIFIED | `_should_show_fork` (`registration.py:858`) gates on the setting plus `party_enabled`, deep-link authority, and registration state; called at `cmd_start:1409`; test coverage in `tests/test_registration_phase5.py:605-680` (`test_should_show_fork_false_when_fork_question_unset_for_every_combo` etc.) |
| 6 | `party_approval` independent of `full_approval`/`short_approval` | ✓ VERIFIED | `_decide_status` (`registration.py:64-78`) — party branch is first, returns before ever reading `full_setting`/`short_setting`; wired from `finalize_registration:2255-2259` with its own `get_setting("party_approval")` read |
| 7 | Approved party delegate sees only party tariffs; full delegate sees only full tariffs | ✓ VERIFIED | `_parse_options` 3-tuple with optional `track` field (`payment.py:98-133`), `_visible_options` index-preserving filter (:136-151), `start_payment_step` renders only `visible` (:193-226); `process_payment_option` server-side re-checks eligibility (:254-271) |
| 8 | Track visible on moderation card, on a separate Sheet tab, and as a broadcast filter | ✓ VERIFIED | Card line `admin.py:2425-2442` (`_render_application_card`); `participant_type` in `_FILTER_COLUMNS` (`db.py:808`) and `_PICKER_FIELDS` (`admin.py:1707`); separate "Party" tab via `services/sheets.py` `append_to_named_sheet`/`ensure_named_sheet_header` (:393-464) and `handlers/registration.py` `PARTY_SHEET_COLUMNS`/`party_sheet_headers`/`party_sheet_row`/`append_to_party_sheet` (:1053-1106), routed exclusively in `finalize_registration:2271-2281` (D-11/D-12, superseding the "отдельной колонкой" wording per 05-CONTEXT D-11 correction) |

**Score:** 8/8 ROADMAP success criteria verified at the code level.

### PLAN-Frontmatter Must-Haves (additional/finer-grained, all traced above or below)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 9 | D-11a: party-closed message + explicit opt-in button; never silent reroute | ✓ VERIFIED | `registration.py:1378-1386` — closed_text + `party_fallback_full` button, gated only for users with no existing non-rejected row (comment :1372-1376 confirms placement after the already-registered branch) |
| 10 | D-08: housing/bed_sharing/bed_partner reused, gated to `party_overnight` only, no new step keys | ✓ VERIFIED | `registration.py:451-457` skip rule inside `_get_enabled_steps`; `REG_FLOW` (`:87-133`) has no new step keys |
| 11 | D-07/D-06: 🎉 Party preset + track switcher on the existing questions screen, isolated from full-track state | ✓ VERIFIED | `REG_PRESETS["party"]` (`registration.py:312-323`), `_apply_party_preset` writes only `__party` keys (:336-354); `_track_switcher_row`/`reg_q_track_switch` (`admin.py:2008-2014`, `:2129-2142`); `preset_confirm` routes `key == "party"` to `_apply_party_preset()` not `_apply_event_preset()` (`admin.py:2275-2287`) |
| 12 | D-14: manual → shared queue via existing `status='pending'` query (no new query/screen); auto → never enqueued | ✓ VERIFIED | `finalize_registration` writes `status` once via `_decide_status` before any queue logic; no `participant_type` predicate exists in `get_pending_users`/`get_pending_count` (grep confirms) — an auto-approved row is written `status='approved'` and is structurally invisible to the pending query |
| 13 | D-15: `approve_text__party` applied on ALL completion paths (not just the direct non-payment path) | ✓ VERIFIED (post-fix, WR-01) | `_approve_text_for` (`registration.py:2085-2095`); threaded through `approve_user` (:2127-2159), `start_payment_step` (`payment.py:216,226`), `_show_payment_details` (:312-326), `process_payment_option` (:263-278), and `admin.py rcpt_confirm` (:2786-2793) — commit `f1b9802` closed the previously-open gap on the two highest-traffic party paths (free/single tariff, paid receipt-confirm) |
| 14 | CR-01: fork question must not drop `referrer_id`/`source_tag` attribution | ✓ VERIFIED (post-fix) | `registration.py:1413-1425` persists `referrer_id`/`source`/`_source_from_tag` into FSM state immediately before the fork keyboard is sent; `_start_registration_flow` (:1196-1200) recovers them via `existing_data.get(...)` from `party_pick`/`party_fallback_full` — commit `136ea0b`, with dedicated regression tests (`tests/test_registration_phase5.py:691+`, real `MemoryStorage`/`FSMContext` handoff) |
| 15 | WR-03: 🎉 Party preset must not force off overnight housing/bed questions in configs where they're globally enabled | ✓ VERIFIED (post-fix) | `_PARTY_PRESET_OVERNIGHT_EXEMPT` set (`registration.py:333`) excluded from `_apply_party_preset`'s blanket on/off pass (:350-354) — commit `3326fcf`, regression test at `tests/test_registration_phase5.py:370+` and `tests/test_admin_phase5.py:212+` |

**Score:** 15/15 truths (8 ROADMAP SC + 7 finer-grained plan must-haves) verified at the code level. One item (below) remains human-only.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `database/db.py` | Phase 5 migrations, `mark_reg_started`/`get_reg_started_track`, `add_user` wiring, `_FILTER_COLUMNS` entry | ✓ VERIFIED | All present and wired; `add_user`'s INSERT/ON CONFLICT column list includes `participant_type` end-to-end |
| `handlers/registration.py` | `_is_party_track`, `_extract_party_track`, party gate, tri-state resolver, `_prompt` widening, `_decide_status` party branch, `_approve_text_for`, fork question, `PARTY_SHEET_COLUMNS`/party sheet functions | ✓ VERIFIED | All present, all wired into the live call paths (`cmd_start`, `_start_registration_flow`, `finalize_registration`, `approve_user`) |
| `handlers/admin.py` | Track switcher, tri-state toggle, party preset button, `party_enabled`/`party_fork_question`/`party_approval` toggles, `party_closed_text` editor, card track line, broadcast filter | ✓ VERIFIED | All present and reachable from the existing «📋 Вопросы регистрации» / «⚙️ Настройки» screens |
| `handlers/payment.py` | 3-tuple `_parse_options`, `_visible_options`, track-filtered `start_payment_step`, server-side re-check in `process_payment_option` | ✓ VERIFIED | Index-preservation contract intact — `pay_option:{i}` always indexes the unfiltered list |
| `services/sheets.py` | `_get_named_sheet`, `_append_to_named_sheet_sync`, `append_to_named_sheet`, `ensure_named_sheet_header` | ✓ VERIFIED | Generalized named-tab cache/append/header functions present, parallel to the main-tab globals |
| `main.py` | Startup party-tab header creation gated on `party_enabled` | ✓ VERIFIED | `_maybe_ensure_party_sheet_header` (`main.py:64-77`), called at startup (:100), no-ops when `party_enabled != "on"` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `registration.py cmd_start` | `database.db mark_reg_started`/`get_reg_started_track` | deep-link + repeat-/start track persistence | ✓ WIRED | Confirmed by code read + `tests/test_db_phase5.py` |
| `registration.py _get_enabled_steps` | `registration.py _is_step_enabled_for_track` | per-step gate resolution | ✓ WIRED | Every `REG_FLOW` iteration in `_get_enabled_steps` (:436-437) calls the tri-state resolver, not the old 2-state `_is_step_enabled` directly |
| `admin.py` party preset button | `registration.py _apply_party_preset` | `preset_confirm` branch | ✓ WIRED | `admin.py:2275-2287` |
| `admin.py` broadcast filter | `database.db get_distinct_filter_values` | `participant_type` in both `_PICKER_FIELDS` and `_FILTER_COLUMNS` | ✓ WIRED | Both present; cross-checked (WR note in review: dropping either breaks the picker — both confirmed present) |
| `registration.py finalize_registration` | `registration.py _decide_status` | `participant_type` + `party_approval` threading | ✓ WIRED | `registration.py:2254-2259` |
| `registration.py approve_user` | `registration.py _approve_text_for` | per-track approve text, resolved once and threaded through payment paths | ✓ WIRED (post-fix) | Confirmed threaded through `start_payment_step`, `_show_payment_details`, `process_payment_option`, and `admin.py rcpt_confirm` |
| `payment.py process_payment_option` | `payment.py _parse_options` | positional index into the unfiltered list | ✓ WIRED | `idx` bounds-checked against `len(options)` (the full list), not `len(visible)` |
| `registration.py finalize_registration` | `services.sheets append_to_named_sheet` | `append_to_party_sheet(...)` in the party branch only | ✓ WIRED | Exclusive `if/else` at `registration.py:2274-2279` — no code path writes both tabs |
| `main.py` startup | `services.sheets ensure_named_sheet_header` | party tab header creation gated on `party_enabled == 'on'` | ✓ WIRED | `main.py:64-77`, `:100` |

### Behavioral Spot-Checks

Skipped — this is a Telegram long-polling bot with a live DB/Sheets dependency; no runnable HTTP/CLI entry point exists to spot-check without starting the bot. Covered instead by the 283/283 automated pytest suite (re-run below) and the human UAT item.

### Probe Execution

No `scripts/*/tests/probe-*.sh` convention exists in this project and none is referenced by any Phase 5 PLAN/SUMMARY. N/A.

### Automated Test Suite

Re-ran `.venv/Scripts/python.exe -m pytest tests/ -q` directly (not trusting SUMMARY claims): **283 passed** in 64.2s, 0 failures, 0 errors — includes `tests/test_db_phase5.py`, `tests/test_registration_phase5.py`, `tests/test_admin_phase5.py`, `tests/test_payment_phase5.py`, `tests/test_sheets_phase5.py`, and every pre-Phase-5 file (no regressions).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|--------------|--------|----------|
| TRACK-01 | 05-01 | `participant_type` migration + DB-level persistence surviving repeat `/start` | ✓ SATISFIED | SC#1, SC#2 above |
| TRACK-02 | 05-02, 05-03 | Per-track question set/wording with tri-state override + admin track switcher | ✓ SATISFIED | SC#3, must-have #11 |
| TRACK-03 | 05-01, 05-04 | Deep-link entry `party_over`/`party_noover` + optional fork question, referrer/src unbroken | ✓ SATISFIED | SC#4, SC#5 |
| TRACK-04 | 05-03, 05-04 | Independent `party_approval` moderation toggle | ✓ SATISFIED | SC#6, must-have #12 |
| TRACK-05 | 05-05 | Tariffs split per track, only relevant tariffs shown | ✓ SATISFIED | SC#7 |
| TRACK-06 | 05-03, 05-06 | Track visible on card, own Sheet tab, broadcast filter | ✓ SATISFIED | SC#8 (wording correction per D-11 applied and reflected in REQUIREMENTS.md) |

No orphaned requirements found — REQUIREMENTS.md's Phase 5 traceability table (all six TRACK-01..06 rows marked "Complete") matches exactly the six IDs declared across the six plans' frontmatter; no additional Phase-5-tagged ID exists in REQUIREMENTS.md that isn't claimed by a plan.

### Anti-Patterns Found

None. Scanned `database/db.py`, `handlers/registration.py`, `handlers/admin.py`, `handlers/payment.py`, `services/sheets.py`, `main.py` for `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER`/"not yet implemented"/"coming soon" markers — zero matches. No debt-marker blocker.

### Known-Open Items (deferred to backlog per 05-REVIEW.md, NOT phase gaps)

These were identified by the Fable-5 code review and explicitly deferred by the reviewer/coordinator rather than fixed in this phase. Confirmed still open in the live code (not silently fixed, not silently forgotten — genuinely deferred):

- **WR-02** — `approve_text__party` and `reg_prompt_<step>__party` have no admin-facing editor UI. Confirmed: `SETTINGS_FIELDS` (`admin.py`) has no `approve_text__party` entry; `admin_reg_prompts`/`reg_prompt_edit` have no track-switcher analogous to the questions screen. These two settings can currently only be set by writing directly to `bot_settings`, not through the bot UI. Note: `05-03-SUMMARY.md` overclaimed this was delivered — it was not; this verification confirms the review's finding, not the summary's claim.
- **WR-04** — no resync hook (`toggle_party_question` → `ensure_named_sheet_header`) when a `__party` question toggle changes the party tab's live column set mid-event. Confirmed absent in `admin.py:2145-2173`.
- **WR-05** — `payment_options` admin help text (`admin.py:359` area) does not document the new optional third `track` field syntax.
- **IN-01** — broadcast filter value picker for `participant_type` shows raw DB codes (`full`/`party_overnight`/`party_noovernight`), not translated Russian labels.
- **IN-02** — `mark_reg_started`'s `COALESCE`-preserve-on-NULL branch is unreachable in production (defensive code only, no live caller passes `participant_type=None`).

These are backlog items, not blockers to this phase's goal — none of them prevent the core observable truths above from holding.

### Human Verification Required

### 1. Consolidated live-bot + live-Google-Sheet end-to-end UAT

**Test:** Run the 10-step checklist recorded in `05-06-SUMMARY.md` (turn on `party_enabled`, apply the 🎉 Party preset, register via `?start=party_over`, verify only party+overnight questions appear, send a bare repeat `/start` mid-flow and confirm the track is retained, finish registration and see manual-moderation confirmation, approve from the shared «Заявки» queue and see the track line + party approve text + party-only tariffs, open the real Google Sheet and confirm a "Party" tab exists with the correct header/row and NO duplicate on the main tab, repeat with `?start=party_noover` and confirm housing/bed questions are skipped, register once through the ordinary flow and confirm it is byte-identical to pre-Phase-5 behavior on the main tab, and confirm legacy `?start=12345` / `?start=src_vk` deep links still work).
**Expected:** All ten steps behave as specified; no visual/rendering surprise in Telegram's actual inline-keyboard drawing or in-place message edits; the real Sheets API round-trip succeeds and lands the row in the correct tab.
**Why human:** Requires a running Telegram session against a live bot instance and the real Google Sheet — inline keyboard rendering, message-edit-in-place visual behavior, and tap-driven callback round-trips through Telegram's servers cannot be exercised by static code reading or unit tests. This was explicitly and consistently deferred across all three of this phase's `human-verify` checkpoints (05-03, 05-04, 05-06) to one consolidated pass by an explicit coordinator decision recorded in each plan's SUMMARY — it was never run, in this session or any prior one.

### Gaps Summary

No code-level gaps found. All 8 ROADMAP success criteria and all 7 additional plan-level must-haves (including the three review-flagged issues — CR-01, WR-01, WR-03 — fixed in commits `136ea0b`, `f1b9802`, `3326fcf`) are verified present, substantive, and wired in the live codebase, not merely claimed in SUMMARY.md. The 283/283 automated test suite was independently re-run and passes with no regressions. Five secondary review findings (WR-02, WR-04, WR-05, IN-01, IN-02) remain intentionally open as documented backlog items — none of them block the phase goal. The single remaining item is the consolidated live-bot/live-Sheet UAT pass, which by design (an explicit, repeatedly-documented coordinator decision across three plans) was never executed with a running Telegram session — this is routed to human verification, not treated as a gap, per the phase's own stated residual.

---

_Verified: 2026-07-21_
_Verifier: Claude (gsd-verifier)_
