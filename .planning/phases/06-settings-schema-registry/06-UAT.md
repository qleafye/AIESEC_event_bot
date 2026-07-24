---
status: partial
phase: 06-settings-schema-registry
source: [06-01-SUMMARY.md, 06-02-SUMMARY.md, 06-03-SUMMARY.md, 06-04-SUMMARY.md, 06-05-SUMMARY.md, 06-06-SUMMARY.md, 06-07-SUMMARY.md]
started: 2026-07-24T00:00:00Z
updated: 2026-07-24T00:00:00Z
---

## Current Test

[testing paused — live smoke deferred to post-SumMeet by user decision]

## Tests

<!--
Phase 6 is a pure internal refactor: SETTINGS_SCHEMA registry as the single
metadata/default source, every consumer + admin-UI read routed through
get_setting_typed. North star = BYTE-IDENTICAL behavior — the user should
observe zero change. There are no new user-facing features to walk.

Automated proof (independently re-run in VERIFICATION.md): 397/397 passing,
4/4 ROADMAP success criteria verified (4-layer net: parse-equivalence +
render-snapshot + coverage + consumer-wiring).

The only human-observable checks are the live-bot smoke walk (06-SMOKE-CHECKLIST.md
§3). Per user decision (this session), that walk is CONFIRMED DEFERRED to
post-SumMeet (forum 31 Jul–2 Aug 2026) — same deferred-UAT precedent as Phase 5.
Each smoke section below is recorded blocked, not failed.
-->

### 1. Landing screen renders identically (§3.1)
expected: /admin → ⚙️ Настройки форума landing text (Форма регистрации / Модерация / Трек вечеринки / Автонапоминания lines) reads exactly as pre-migration.
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet per user/orchestrator decision (accepted at phase close). Needs live bot instance with real bot_settings data."

### 2. 14-button toggle before/after parity (§3.2, MANDATORY)
expected: Every one of the 14 toggle buttons shows identical text pre/post-migration; tapping flips the underlying setting, updates the button text, and logs no exception.
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet. Mandatory per-button comparison table requires live admin traffic; 12 buttons fan through the 3 helpers closed in 06-07."

### 3. Migrated group sub-screens (§3.3)
expected: Событие/Медиа, Регистрация, Оплата, Party, Согласия sub-screens — field labels, order, задано/не задано/по умолчанию flags, ── не настроено ── collapse all match prior behavior.
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet. Needs live bot."

### 4. Edit round-trip per group (§3.4)
expected: Editing Дата (event), payment_deadline (pay), party_closed_text (party) saves and flips the flag to задано.
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet. Needs live bot."

### 5. Default-fallback display (§3.5)
expected: party_closed_text and party_sheet_tab still show по умолчанию when unset.
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet. Needs live bot."

### 6. reg_q_* question toggle (§3.6)
expected: Flipping a reg_q_* question toggles correctly; default-off/on question set unchanged from pre-migration.
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet. Needs live bot."

### 7. Scheduler timing spot-check (§3.7)
expected: Pending-application reminder still fires on interval; payment-deadline/payment-reminders timing unchanged (or no error in logs).
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet. Needs live running bot over time."

### 8. Restart-persistence (§3.8)
expected: After bot restart, NO setting resets to blank/default across the 5 migrated groups + 14 toggle buttons.
result: blocked
blocked_by: prior-phase
reason: "Live smoke deferred to post-SumMeet. Needs live bot restart with real data."

## Summary

total: 8
passed: 0
issues: 0
pending: 0
skipped: 0
blocked: 8

## Gaps

[none — all outstanding items are blocked on the deferred live-smoke gate, not code defects; automated net is 397/397 green, phase verified 4/4]
