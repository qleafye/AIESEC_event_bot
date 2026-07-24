# Milestones

## v1.0 YouLead'26 MVP (Shipped: 2026-07-24)

**Phases completed:** 5 phases, 24 plans, 17 tasks

**Key accomplishments:**

- Party deep links (`?start=party_over` / `?start=party_noover`) now set `participant_type` on `users` via a two-point persistence chain (reg_started at flow start, users at finalize), gated by a fail-soft `party_enabled` master toggle that never silently reroutes a visitor into the pricier full track.
- Registration engine is now track-aware end to end: every question gate and every question prompt resolves through a single `__party` override namespace (tri-state inherit/on/off) before falling back to the global setting, overnight-only questions are skipped for `party_noovernight`, and a one-tap 🎉 Party preset seeds six questions without ever touching a full delegate's live settings.
- Every Phase 5 party-track control surface plans 05-01/05-02/05-04 shipped but left unreachable is now exposed in the admin panel: a track switcher + tri-state question toggle on the existing questions screen, a 🎉 Party preset button, four party module/text settings, a track line on the shared moderation card, and `participant_type` as a broadcast filter — making the phase operable end-to-end without a redeploy.
- Party applications now resolve their approval status and their welcome message from `party_approval`/`approve_text__party` alone (never inheriting full/short moderation or the forum welcome text), and an admin-gated pre-flow keyboard lets a delegate self-select a party track when no deep link set it — adding zero new REG_FLOW steps and zero new FSM states.
- `payment_options` now accepts an optional `|track` suffix so party guests see only their tariff (never the three-day forum price), while `pay_option:{i}` keeps indexing the full unfiltered list — a keyboard rendered before a settings edit can never resolve to a shifted tariff, and a stale/foreign-track tap is now rejected server-side.
- A second, admin-named Google Sheets tab (default "Party") now receives incremental per-registration appends via a new `_named_sheets` cache + `append_to_named_sheet`/`ensure_named_sheet_header` pair in `services/sheets.py`, with `finalize_registration` routing party registrations there exclusively (never to the main tab) using a deliberately curated, formula-injection-neutralized column set that omits university/course/resume entirely.

**Completion basis:** All 5 phases verified goal-backward against live code (VERIFICATION.md passed for each; built via ~24 quick-tasks, so no per-plan SUMMARY.md for phases 1-4 — code is source of truth). Milestone audit `v1.0-MILESTONE-AUDIT.md`: 42/43 requirements satisfied; the one cross-phase blocker (TRACK-05/PAY party-payment re-entry) was fixed post-audit (quick 260724-dnm). P0 hardening pack applied (quick 260724-dw1). Full test suite 359/359 pass.

**Known deferred items at close (1):** Phase 5 consolidated live-bot + live-Google-Sheet 10-step party-track UAT — never run (needs a running Telegram session + real Sheet); accepted as residual by user decision, not a code gap (party track is code-verified 15/15). Nextcloud TLS verification default left OFF (operator to set `NEXTCLOUD_VERIFY_TLS=true` in live `.env` once behind a trusted CA).

---
