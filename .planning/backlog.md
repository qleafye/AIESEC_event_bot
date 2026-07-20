# Backlog

Ideas captured outside the active milestone. Promote via `/gsd-review-backlog`.

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
