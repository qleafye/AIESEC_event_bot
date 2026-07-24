---
phase: 05-participant-tracks-party-delegates
audit: independent-fresh
reviewed: 2026-07-24T00:00:00Z
depth: deep
reviewer: gsd-code-reviewer (adversarial audit)
files_reviewed: 6
files_reviewed_list:
  - handlers/registration.py
  - handlers/payment.py
  - handlers/admin.py
  - database/db.py
  - services/sheets.py
  - main.py
findings:
  critical: 0
  high: 1
  medium: 2
  low: 5
  total: 8
status: issues_found
prior_review: 05-REVIEW.md — CR-01, WR-01, WR-03 confirmed RESOLVED; WR-02, WR-04, WR-05, IN-01 confirmed still OPEN (deferred to backlog)
---

# Phase 5: Independent Audit Review — Participant Tracks (Party Delegates)

## Overall Verdict

**SHIP WITH FIXES.** The Phase 5 party-track implementation is fundamentally sound. The
full-track regression surface is genuinely clean: `add_user`'s column/placeholder/value
counts are exactly 59/59/59 (verified programmatically), the additive migration
(`participant_type TEXT DEFAULT 'full'`) back-fills every one of the ~590 live rows with
`'full'` and cannot NULL an old row, deep-link parsing is a closed 2-entry exact-match map
with no collision against the numeric-referrer or `src_` extractors, sheet routing in
`finalize_registration` is provably exclusive (`if/else` on `_is_party_track`), per-track
approval (`_decide_status` party-first branch) and per-track pricing (`_visible_options`
index-preservation) are correct, and every new admin callback re-checks `ADMIN_IDS`.

The three fixed prior-review findings (CR-01, WR-01, WR-03) are genuinely resolved in code.
**However, the CR-01 fix introduced a new silent data-loss regression of the same class it
set out to fix** (HIGH-01 below): a repeat `/start` on the fork screen clobbers the referral/
source attribution the fix was supposed to preserve. That is the one finding that should be
fixed before this ships to an event that turns the fork question on.

143/143 Phase 5 tests pass; all six source files compile clean.

## Prior Review (05-REVIEW.md) Status

| Prior finding | Claimed resolution | Audit verdict |
|---|---|---|
| CR-01 (fork drops referrer_id/source_tag) | Fixed (commit 3326fcf) | **RESOLVED** — fork branch now persists `referrer_id`/`source`/`_source_from_tag` to FSM before returning (registration.py:1433-1435). But see **HIGH-01**: the fix is not idempotent under a repeat `/start`. |
| WR-01 (approve_text__party not applied on payment paths) | Fixed (commit f1b9802) | **RESOLVED** — `_show_payment_details` now takes `participant_type` and both call sites thread it; `rcpt_confirm` resolves the track via `get_user` (admin.py:2789-2795); `start_payment_step` passes it. |
| WR-03 (Party preset forces housing/bed off) | Fixed (commit 136ea0b) | **RESOLVED** — `_PARTY_PRESET_OVERNIGHT_EXEMPT` skips housing/bed_sharing/bed_partner in `_apply_party_preset` (registration.py:336, 355-356). |
| WR-02 (no admin UI for `approve_text__party` / `reg_prompt_*__party`) | Deferred to backlog | **STILL OPEN** — see LOW-01. |
| WR-04 (party sheet header never resynced on `__party` toggle) | Deferred to backlog | **STILL OPEN** — see MEDIUM-01. |
| WR-05 (payment_options help omits track field) | Deferred to backlog | **STILL OPEN** — see LOW-02. |
| IN-01 (raw track codes in broadcast picker) | Deferred to backlog | **STILL OPEN** — see LOW-03 (info-level). |
| IN-02 (`mark_reg_started` COALESCE branch unreachable) | Documentation-only | Still accurate; no action. |

## High

### HIGH-01: Repeat `/start` on the fork screen silently clobbers referral/source attribution (regression of CR-01's fix)

**File:** `handlers/registration.py:1433-1435` (fork branch), reached from `cmd_start` (1314-1442)

**Issue:** The CR-01 fix persists deep-link attribution before showing the fork keyboard via
an **unconditional** overwrite:

```python
if show_fork:
    await state.update_data(
        referrer_id=referrer_id, source=source_tag, _source_from_tag=bool(source_tag)
    )
```

`cmd_start` never calls `state.clear()` anywhere before this branch (verified: the only
`state.clear()` calls are inside `_start_registration_flow` at 1222 and in unrelated
handlers). So the FSM state survives across `/start` invocations. Trace:

1. User opens `t.me/<bot>?start=123456` (a referral) with `party_fork_question=on`,
   `party_enabled=on`. `cmd_start` extracts `referrer_id=123456`, `_should_show_fork`
   returns True, line 1433 writes `referrer_id=123456` to FSM. Fork keyboard shown. Correct.
2. Before tapping, the user re-sends `/start` (bare — Telegram menu button, re-opening the
   chat, impatience). `cmd_start` runs again: `args=None`, so `referrer_id=None`,
   `source_tag=None`. There is still no `reg_started` row (the fork path never calls
   `mark_reg_started`), so `recovered_track=None` and `show_fork` is True again.
   Line 1433 now writes **`referrer_id=None`, `source=None`, `_source_from_tag=False`** —
   overwriting the value saved in step 1.
3. User taps a fork button. `party_pick` → `_start_registration_flow(participant_type=...)`
   reads `existing_data.get("referrer_id")` → `None`. The referral is permanently lost;
   `add_user` records `referrer_id=NULL` / `source='Самостоятельно'` for a user who was in
   fact referred.

This is the exact silent-attribution-loss failure mode CR-01 documented, reintroduced on a
narrower but realistic path. The asymmetry is the root cause: `_start_registration_flow`
correctly preserves with `saved_referrer_id = referrer_id or existing_data.get("referrer_id")`
(1209-1210), but the fork branch uses a raw assignment that a fresh `None` clobbers.
Precondition: `party_fork_question=on` (non-default, event-specific); within such an event a
referred user pressing /start twice is common. Not covered by any test.

**Fix:** Make the fork-branch persistence preserve, never clobber — mirror
`_start_registration_flow`'s `or existing` idiom:
```python
if show_fork:
    existing = await state.get_data()
    eff_ref = referrer_id or existing.get("referrer_id")
    eff_src = source_tag or existing.get("source")
    await state.update_data(
        referrer_id=eff_ref, source=eff_src,
        _source_from_tag=bool(eff_src) or existing.get("_source_from_tag", False),
    )
    ...
```
(Only write keys whose new value is truthy, or read-modify-write as above.)

## Medium

### MEDIUM-01: Party sheet header is never resynced on a `__party` question toggle → column misalignment (prior WR-04, still open)

**File:** `handlers/admin.py:2145-2173` (`toggle_party_question`), `handlers/admin.py:2275-2287`
(`preset_confirm` party branch); interacts with `handlers/registration.py:1084-1108`
(`party_sheet_headers`/`party_sheet_row`) and `services/sheets.py:419-420` (positional
`append_row`)

**Issue:** The main sheet resyncs its physical header on every question toggle
(`toggle_reg_question` → `_refresh_sheet_header()`) and freezes a schema snapshot
(`set_sheet_schema`). The party tab does neither: its header is written **once** at startup
by `_maybe_ensure_party_sheet_header` (main.py:64-77), and `toggle_party_question` has **no**
`ensure_named_sheet_header` call, while `preset_confirm`'s party branch explicitly skips the
resync (admin.py:2285-2286). `party_sheet_row` recomputes `party_sheet_headers()` live per
append, and `append_to_party_sheet` does a raw positional `append_row`. So if an admin flips
any `reg_q_*__party` override mid-event, every subsequent party row is projected onto a
different-length header than the physical row 1 (and than earlier rows) — a silent column
shift, a genuine data-integrity defect, not merely cosmetic. Acknowledged as an accepted
low-volume risk in the SUMMARY, but it remains present in code.

**Fix:** Call `sheets_service.ensure_named_sheet_header(tab, await party_sheet_headers())`
from `toggle_party_question` and the `preset_confirm` party branch, mirroring
`_refresh_sheet_header()` for the main tab. (Note: `_ensure_named_header_sync` only overwrites
`A1:<len>` — if the new header is shorter it leaves stale trailing header cells; acceptable,
but worth knowing.)

### MEDIUM-02: New fire-and-forget `create_task` for the party sheet append is not held by a strong reference (GC hazard)

**File:** `handlers/registration.py:2294` (`asyncio.create_task(append_to_party_sheet(...))`);
sibling full-track path at 2297

**Issue:** The codebase explicitly recognizes this hazard — `main.py:51-61` documents that
the event loop keeps only weak refs to tasks and provides `_spawn()` (a strong-ref set +
done-callback) precisely to stop fire-and-forget tasks being GC'd mid-run. Phase 5 added a
new bare `asyncio.create_task(...)` at line 2294 for the party-tab append that does **not**
use `_spawn` (and the full-track append at 2297 shares the same unguarded pattern). If the
task is collected before `asyncio.to_thread` completes, the sheet write is silently dropped.
`_spawn` lives in `main.py` and cannot be imported here without a circular dependency, so the
pre-existing pattern was copied — but the new occurrence is a fresh instance of a bug the
project has fixed elsewhere.

**Fix:** Introduce a module-level strong-ref set in `handlers/registration.py` (same 4-line
pattern as `main._spawn`) and route both branch appends through it, or extract a shared
`utils` spawner both `main.py` and the handlers import.

## Low

### LOW-01: No admin UI to configure `approve_text__party`, `reg_prompt_<step>__party`, or `party_fork_text` (prior WR-02, still open)

**File:** `handlers/admin.py:340-383` (`SETTINGS_FIELDS`), `handlers/admin.py:2032-2074`
(questions view has a track switcher but the prompt-wording editor does not)

**Issue:** `SETTINGS_FIELDS` exposes `approve_text` (line 352) but not `approve_text__party`;
there is no per-track prompt-wording editor and no `party_fork_text` editor. These three
`bot_settings` keys are read by the running bot (`_approve_text_for`, `_prompt`,
`cmd_start`'s fork) but can only be populated by a direct DB write, contradicting the project's
core value ("менеджер DXP … полностью … через бота"). Functionally safe (all fall back to
sensible defaults), so Low, but the feature is effectively unreachable through the UI.

**Fix:** Add an `approve_text__party` `SETTINGS_FIELDS` entry and a `party_fork_text` entry;
extend the reg-prompt editor with the same `track` switcher used for `admin_reg_questions`.

### LOW-02: `payment_options` admin help text still omits the track-filter syntax (prior WR-05, still open)

**File:** `handlers/admin.py:359`

**Issue:** `_parse_options` accepts an optional third `label|price|track1,track2` field
(payment.py:98-133), but the in-bot help still documents only `Название | Цена`. An admin
configuring party pricing through the bot cannot discover the syntax.

**Fix:** Append a line documenting the optional third field, its accepted values
(`full`, `party_overnight`, `party_noovernight`), and that omitting it makes a tariff visible
to all tracks.

### LOW-03: Broadcast «Трек» filter value picker shows raw DB codes, not translated labels (prior IN-01, still open)

**File:** `handlers/admin.py:1698, 1707, 1776` and the generic value picker

**Issue:** The «🎉 Трек» filter surfaces raw `participant_type` values (`full`,
`party_overnight`, `party_noovernight`) with no label translation, unlike the moderation card
(admin.py:2439-2442) and the party sheet «Трек» column, which translate to readable Russian.
Matches the pre-existing raw-value picker pattern (e.g. `status`), so not a regression — an
inherited readability gap on a new surface.

**Fix:** Optional; add a label map like the moderation card's if addressed.

### LOW-04: `_payment_price_block` shows every tariff (including full-only) to a party delegate during registration

**File:** `handlers/registration.py:511-529`

**Issue:** The pre-approval price preview prepended to the payment-date question renders all
parsed options with no track filter (the third tuple element is deliberately ignored, per the
in-code comment — filtering is post-approval only). A party delegate who reaches
`reg_q_payment_date` would see the full 3-day forum price, contradicting the "party guests
never see the forum price" goal. Blast radius is small: the party preset sets
`reg_q_payment_date__party=off`, so party delegates normally never reach this step; it only
surfaces if an admin manually enables the payment-date question for the party track.

**Fix:** If ever exposed to party tracks, thread `participant_type` into `_payment_price_block`
and filter via `_visible_options`.

### LOW-05: Fork-screen abandoners are never recorded in `reg_started` (analytics gap)

**File:** `handlers/registration.py:1426-1438`

**Issue:** `mark_reg_started` runs only inside `_start_registration_flow`; the fork branch
returns before that. A user shown the fork keyboard who leaves without tapping is never
counted as "started registration," so dropout analytics and the dropout-nudge job miss every
fork abandoner. Behavioral gap, not a correctness bug.

**Fix:** Optionally call `mark_reg_started(user_id, username)` (track left NULL) before
rendering the fork, so the abandon is tracked; the subsequent tap's write updates the track
via the existing COALESCE guard.

---

## Cross-Cutting Confirmations (audited, no defect found)

- **Migration on old rows:** `_ensure_column(db, "users", "participant_type", "TEXT DEFAULT 'full'")`
  (db.py:179) — SQLite back-fills existing rows with `'full'` on `ADD COLUMN`; no NULL
  `participant_type` reaches `_is_party_track` for a pre-migration user. `reg_started.participant_type`
  is nullable by design (recovered via COALESCE).
- **Deep-link ambiguity:** `_PARTY_TAG_MAP` is exact-match (`.get(args.strip())`), tokens are
  non-numeric and non-`src_`-prefixed → mutually exclusive with `_extract_referrer_id`
  (ASCII-digit-only) and `_extract_source_tag` (`src_` prefix). Empty/unknown params fall
  through to normal full registration.
- **`add_user` shape:** 59 columns / 59 placeholders / 59 values (verified programmatically) —
  `participant_type` round-trips at INSERT, ON CONFLICT SET, and VALUES; defaults to `'full'`.
- **Two-point persistence:** `reg_started` (flow start, COALESCE-guarded) and `users` (finalize)
  both carry the track; FSM `participant_type` is the single source read at finalize and by
  the question engine. No DB-vs-FSM desync path found on the normal or bare-repeat-`/start`
  flows.
- **Approval independence:** `_decide_status` party-first branch (db-independent of
  full/short), `party_approval or "manual"` fails closed; pending queue picks party rows up via
  the unchanged `status='pending'` query with no track predicate — no wrong-queue routing.
- **Pricing leak:** `_visible_options` filters party-only tariffs out for full callers and
  full-only tariffs out for party callers; `process_payment_option` re-validates server-side
  against a freshly-resolved `get_user` track. No post-approval cross-track price exposure.
- **Sheet exclusivity:** `finalize_registration` if/else on `_is_party_track` — party rows go
  to the party tab only, full rows to the main tab only (behaviorally test-covered).
- **CSV/formula injection:** `party_sheet_row` neutralizes every cell via `_csv_safe`;
  moderation card escapes the raw track value via `html.escape`.

---

_Reviewed: 2026-07-24_
_Reviewer: Claude (gsd-code-reviewer) — independent adversarial audit_
_Depth: deep_
