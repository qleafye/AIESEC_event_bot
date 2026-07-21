# Backlog

Ideas captured outside the active milestone. Promote via `/gsd-review-backlog`.

---

## Phase 5 code-review deferrals (Fable 5 review, 2026-07-21)

Deferred findings from `.planning/phases/05-participant-tracks-party-delegates/05-REVIEW.md`.
CR-01 + WR-01 + WR-03 were fixed in-session; the below were consciously deferred.

- **WR-02** — `approve_text__party` and `reg_prompt_<step>__party` have no admin UI
  (`admin.py:2305-2343`, `SETTINGS_FIELDS`). Per-track wording (D-05) and per-track
  approval message (D-15) can currently only be set by writing `bot_settings` directly.
  Contradicts the "всё через бота" core value. Fix: add `approve_text__party` editor +
  a `track` switcher on the prompt-text screen, mirroring `reg_q_track_switch`.
- **WR-04** — party sheet header has no resync hook on `__party` toggle
  (`registration.py:1061-1094`, `admin.py:2145-2173`). Column-misalignment risk if an
  admin flips a `reg_q_*__party` override mid-event. Fix: call
  `ensure_named_sheet_header(tab, await party_sheet_headers())` from `toggle_party_question`
  and `preset_confirm`'s party branch.
- **WR-05** — `payment_options` admin help text (`admin.py:359`) not updated for the new
  `label|price|track1,track2` syntax. Admin has no in-bot way to discover track filtering.
- **IN-01** — broadcast filter «Трек» picker shows raw codes (`party_overnight`) instead
  of RU labels (`admin.py:1808-1827`). Matches pre-existing raw-picker pattern; low priority.
- **IN-02** — `mark_reg_started` COALESCE-preserve branch (`db.py:558-565`) unreachable in
  production (only live caller always passes a concrete track). Documentation-only note.
- **Pre-existing (out of Phase-5 scope)** — main sheet `active_sheet_row` does not apply
  `_csv_safe`, unlike the party sheet. CSV-injection parity gap worth a quick-task.

---

## Attendance check-in + post-event feedback survey

**Captured:** 2026-07-20 (during Phase 5 execution)

**Idea:** At the end of a forum the admin presses a button and a feedback survey
fires to attendees. Survey should reach only people who actually showed up — so
attendance has to be recorded first.

**Proposed mechanism:** QR code carrying a dedicated deep-link start payload
(e.g. `t.me/<bot>?start=checkin_<event>`). Guest scans on arrival, bot stamps
attendance for that user. Reuses the deep-link → payload → DB-stamp pattern
built in Phase 5 for `participant_type` (`?start=party_over`), so the plumbing
already exists.

**Rough shape:**
- `attended` / `checked_in_at` column on `users` (additive migration, existing
  `_ensure_column` pattern)
- Deep-link handler branch for the check-in payload; idempotent (re-scan is a
  no-op, not a duplicate)
- Admin screen: generate/show the check-in QR, see live check-in count
- Survey builder: admin-defined questions (likely reuses the existing
  configurable-question machinery rather than a new engine)
- Admin "send survey now" button → broadcast filtered to `attended = 1`
- Responses stored + synced to Sheets, same append pattern as registrations

**Open questions:**
- One QR for the whole event, or per-day / per-session QRs?
- Should the survey be reusable across events (template) or per-event config?
- Does a party-track guest get a different survey than a delegate?
- Anonymous responses, or tied to the user row?

**Depends on:** Phase 5 deep-link + `participant_type` foundation (in progress).
Survey filtering would want the broadcast-filter work from 05-03.
