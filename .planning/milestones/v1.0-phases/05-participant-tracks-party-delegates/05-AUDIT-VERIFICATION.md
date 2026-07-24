---
phase: 05-participant-tracks-party-delegates
audit: fresh-independent-pass
verified: 2026-07-24T00:00:00Z
status: passed
score: 8/8 success criteria verified
verifier: Claude (independent audit — separate from 05-VERIFICATION.md)
method: goal-backward against actual code; code is source of truth
tests: 143 passed (test_db/registration/admin/payment/sheets _phase5.py)
---

# Phase 5: Participant Tracks (Party Delegates) — Independent Audit

**Overall verdict: PASS (8/8 success criteria verified against real code).**

This is a fresh, independent audit pass distinct from the prior `05-VERIFICATION.md`.
Every criterion was traced to concrete code (file:line) and cross-checked with the
phase-5 test suites (143 tests, all passing in 63s). No BLOCKERs, no WARNINGs.

Phase goal: Один бот обслуживает делегатов разных треков — полное участие и «только
вечеринка» (с ночёвкой/без) — с отдельными наборами вопросов, модерацией и тарифами,
настраиваемыми из админки без деплоя.

## Per-Criterion Results

| # | Criterion | Status | Key Evidence |
|---|-----------|--------|--------------|
| 1 | `participant_type` migrated with DEFAULT 'full', no data loss | ✅ PASS | db.py:179 |
| 2 | Track fixed in DB at reg start; bare repeat /start stays party | ✅ PASS | registration.py:1404-1411, db.py:566-594 |
| 3 | Per-track question toggle `reg_q_<step>__party`, absent→inherit | ✅ PASS | registration.py:411-423 |
| 4 | `?start=party_over/_noover` sets track; numeric→referrer, src_→source | ✅ PASS | registration.py:846-852, 795-807, 826-832 |
| 5 | Fork question only when `party_fork_question=on` (default off) | ✅ PASS | registration.py:868-886, 1421-1438 |
| 6 | `party_approval` independent of full/short approval | ✅ PASS | registration.py:64-78, 2269-2277 |
| 7 | Approved party delegate sees only party tariffs | ✅ PASS | payment.py:99-150, 185-277 |
| 8 | Track in mod card + Sheet column + broadcast filter | ✅ PASS | admin.py:2434-2443, sheets.py:1070-1072, db.py:808-814 |

---

## Detailed Findings

### Criterion 1 — Migration DEFAULT 'full', non-destructive — PASS

- `database/db.py:179` — `_ensure_column(db, "users", "participant_type", "TEXT DEFAULT 'full'")`.
- `_ensure_column` (db.py:31-35) issues `ALTER TABLE ... ADD COLUMN` only when the column is
  absent — purely additive, existing ~590 rows retain all data and receive `'full'` by the
  column DEFAULT. Idempotent (guarded by `_column_exists`).
- `add_user` (db.py:208-353) uses `ON CONFLICT(telegram_id) DO UPDATE SET` — NOT
  `INSERT OR REPLACE` — with an explicit comment (lines 210-212) that REPLACE would wipe
  unlisted columns. `participant_type` binds `data.get('participant_type', 'full')` (line 350).
- No destructive DDL (DROP/RENAME/recreate) touches `users`. Verified.

### Criterion 2 — Track fixed in DB at start, survives bare repeat /start — PASS

- Two-point persistence, not FSM-only:
  - `_start_registration_flow` (registration.py:1196-1233) resolves `saved_track`
    (deep-link arg wins, else inherit) and calls `mark_reg_started(..., saved_track)`
    (line 1205) which writes `participant_type` into the `reg_started` DB row
    (db.py:566-577, `INSERT ... ON CONFLICT ... participant_type=COALESCE(...)`).
  - Bare repeat `/start` with no arg: cmd_start lines 1404-1411 —
    `if not party_track and not user: recovered_track = await get_reg_started_track(user_id)`
    then `party_track = recovered_track`. `get_reg_started_track` (db.py:588-594) reads the
    persisted track. So the user stays in the party track and does NOT fall into the full form.
- Guarded fail-soft (try/except at 1408-1413). Verified by `mark_reg_started`/`get_reg_started_track`
  tests in test_db_phase5 + test_registration_phase5.

### Criterion 3 — Per-track question tri-state override — PASS

- `_is_step_enabled_for_track` (registration.py:411-423): for a party track reads
  `get_setting(f"{setting_key}__party")`; `if override is not None: return override == "on"`.
  Key ABSENCE (None) falls through to `_is_step_enabled(setting_key)` — inherits the global
  `reg_q_<step>`. The `is not None` check (not truthiness) is load-bearing so "off" ≠ "inherit".
- Full track (or None) never reads the `__party` key — byte-identical to legacy behavior.
- Admin surface tri-state cycle inherit→on→off→inherit at admin.py:2146-2173
  (`toggle_party_question`, deletes key to return to inherit). Prompt wording override
  `reg_prompt_<step>__party` at registration.py:397-401. Verified by test_admin_phase5.

### Criterion 4 — Deep-link routing, non-breaking — PASS

- `_extract_party_track` (registration.py:846-852) uses a fixed 2-entry exact-match map
  `_PARTY_TAG_MAP = {"party_over": "party_overnight", "party_noover": "party_noovernight"}`
  (line 843) — no prefix/regex, so no crafted payload escapes the closed vocabulary.
- `_extract_referrer_id` (795-807): ASCII-digit-only → numeric arg still parses as referrer_id
  (rejects "party_over" since not digits). `_extract_source_tag` (826-832): `src_` prefix →
  source. The three extractors are mutually exclusive by construction (a party token is neither
  digit nor `src_`-prefixed). cmd_start invokes all three independently (lines 1359-1361).

### Criterion 5 — Fork question only when enabled (default off), zero extra screens — PASS

- `_should_show_fork` (registration.py:868-886) returns False unless
  `get_setting("party_fork_question") == "on"` (default coerced to "off" at line 881) AND
  `party_enabled == "on"` (line 884) AND no deep-link/recovered track AND not already registered.
- cmd_start only renders the fork keyboard when `show_fork` is True (lines 1421-1438); otherwise
  it proceeds directly to `_start_registration_flow` (line 1440). With the toggle off, an
  ordinary delegate takes the direct path — no fork screen. Admin default is also "off"
  (admin.py:434, 525). Verified by test_registration_phase5.

### Criterion 6 — Independent party_approval — PASS

- `_decide_status` (registration.py:64-78): `if _is_party_track(participant_type):
  setting = party_setting or "manual"; return "pending" if setting == "manual" else "approved"`.
  This branch NEVER reads full_setting/short_setting — fully independent. Party defaults to
  "manual" (moderated) when unconfigured (T-05-04-02).
- Wired at finalize (registration.py:2269-2277): reads `party_approval` via `get_setting`
  and passes it plus the resolved `participant_type` into `_decide_status`. So
  `party_approval=auto` + `full_approval=manual` → party auto-approved, full → pending queue.
  Verified by test_registration_phase5.

### Criterion 7 — Per-track tariffs — PASS

- `_parse_options` (payment.py:99-132): optional 3rd `label|price|track1,track2` field →
  `tracks` set; blank/absent → None ("offered to all", backward-compatible with Phase 4 configs).
- `_visible_options` (payment.py:137-150): keeps only entries where `tracks is None or
  participant_type in tracks`, preserving the ORIGINAL index (`enumerate(options)`), so
  `pay_option:{i}` stays stable.
- `start_payment_step` (185-226) renders the filtered keyboard; if none match → free-fallback
  (line 212-216). `pay_option` handler (245-277) RE-validates the track server-side via
  fresh `get_user` (`if tracks is not None and current_track not in tracks: reject`), defeating
  a stale/cross-track keyboard. Verified by test_payment_phase5.

### Criterion 8 — Track visible: mod card + Sheet column + broadcast filter — PASS

- **Moderation card** (admin.py:2434-2443): appends a «🎉 Трек: вечеринка с/без ночёвки» line
  to the pending application card for any non-full track.
- **Google Sheet column**: `PARTY_SHEET_COLUMNS` (sheets.py:1064-1081) includes a dedicated
  «Трек» column (1070-1072). Exclusive routing at finalize (registration.py:2289-2297):
  party registrations go to `append_to_party_sheet` (party tab) ONLY, full to the main tab —
  never both. Party tab auto-creates (`_get_named_sheet`, sheets.py:397-412).
- **Broadcast filter**: `participant_type` whitelisted in `db._FILTER_COLUMNS` (db.py:808-814),
  and surfaced in admin filter UI — `_FILTER_FIELD_LABELS["participant_type"]="Трек"`
  (admin.py:1698), in `_PICKER_FIELDS` (1707), and as a filter button (admin.py:1776).
  Verified by test_admin_phase5 + test_sheets_phase5.

---

## Behavioral Spot-Check

- `pytest tests/test_{db,registration,admin,payment,sheets}_phase5.py` → **143 passed** (63.5s).
  Covers migrations, two-point track persistence, tri-state overrides, deep-link extractors,
  fork gating, independent approval decision, tariff filtering/index-preservation, party sheet
  row/header, and broadcast filter columns.

## Anti-Pattern Scan

- No stubs, no `return null/[]`-style empty implementations in the phase-5 code paths.
- No unreferenced TODO/FIXME/XXX debt markers introduced in the audited functions.
- Fail-soft try/except guards on every new /start branch (gate, recovery, fork) so a Phase-5
  defect can never crash `/start` — deliberate, not a code smell.

## Human Verification (optional, not blocking)

Automated code + test evidence is conclusive for all 8 criteria. The only items that would
benefit from live confirmation are inherently UI/manual and already covered by 05-HUMAN-UAT.md:
end-to-end party deep-link tap in a real Telegram client, and visual confirmation of the
party tab appearing in the live Google Sheet.

---

_Independent audit — does not replace 05-VERIFICATION.md._
