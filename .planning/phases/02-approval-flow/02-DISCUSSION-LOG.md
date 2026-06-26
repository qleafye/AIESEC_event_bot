# Phase 2: Approval Flow - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-26
**Phase:** 02-approval-flow
**Session:** Update pass — reconciled committed context against master plan `PLAN_YOULEAD_TZ.md` (added that session). Original discussion (D-01..D-16) preserved in CONTEXT.md; this log covers the 4 tensions resolved this session.

---

## Submission notification (APP-02 vs D-15 anti-storm)

| Option | Description | Selected |
|--------|-------------|----------|
| Periodic only (keep D-15) | No instant ping; 30-min reminder satisfies APP-02 | |
| Instant ping + reminder | One ping per pending submission + periodic backstop | |
| Throttled instant | Rate-limited instant ping, batching new pendings | |
| **Admin-configurable** (Other) | Toggle via /admin: instant ping OR batched every N min | ✓ |

**User's choice:** "настраивается через /admin мгновенный пинг/батчи раз в N минут"
**Notes:** Resolved into setting `pending_notify_mode` ∈ {instant, batched}, default `batched`. Batched path = periodic reminder (D-13/14). Instant path = literal APP-02. → CONTEXT D-15 (revised).

---

## Rejected delegate path

| Option | Description | Selected |
|--------|-------------|----------|
| Terminal — show rejection text | Rejected is final; /start re-shows reject text | |
| Can re-apply | Rejected user can restart registration → new pending | ✓ |
| You decide at plan time | Default terminal unless re-apply trivial | |

**User's choice:** Can re-apply
**Notes:** /start re-enters registration; re-submit overwrites status via ON CONFLICT DO UPDATE (Phase 1 D-17); prior reject reason cleared. `ensure_registered` still gates menu while rejected. → CONTEXT D-05a (new).

---

## Mass approve ("Одобрить все N") at 590+ scale

| Option | Description | Selected |
|--------|-------------|----------|
| Sequential w/ 429 handling | Loop rows, send welcome each, catch TelegramRetryAfter | |
| DB-flip now, queue sends | Atomic flip-all immediately, drain sends in background | ✓ |
| You decide at plan time | Planner picks; 429-handling mandatory | |

**User's choice:** DB-flip now, queue sends
**Notes:** Single atomic UPDATE flips all pending (capture flipped ids via RETURNING/snapshot); welcome+menu sends drained in background with TelegramRetryAfter handling. → CONTEXT D-11 (revised).

---

## Resume on/off toggle (TЗ line 139) — Phase 2 or defer?

| Option | Description | Selected |
|--------|-------------|----------|
| Defer (out of scope) | Not an APP requirement | |
| Fold into Phase 2 settings | Add resume_enabled toggle | |
| **Already exists** (Other) | User: toggle already implemented; verify vs Phase 2 | ✓ |

**User's choice:** "вроде как такой тоггл уже есть и работает... вроде как мы перенесли реализацию в фазу 1"
**Notes:** Verified — Phase 1 D-08 ships `reg_q_resume` (toggleable REG_FLOW step, default off, full form, PDF/DOCX, mandatory when on). Phase 2 adds only the manager-side VIEW (D-09, answer_document). No new Phase 2 work. → CONTEXT D-09a (clarification).

---

## Claude's Discretion

- Exact RETURNING vs pre-SELECT snapshot for mass-approve id capture.
- Whether batched submission-notify reuses the existing periodic-reminder task or a separate flush loop.
- (Carried from original session) pagination/skip mechanism, card copy, settings-guide command name.

## Deferred Ideas

- Full per-form question-set split (`REG_DEFAULTS_SHORT`/`REG_DEFAULTS_FULL`, two admin question screens) from TЗ §"Раздельные настройки для форм" — broader than APP-07 (which is moderation toggle only). Belongs with Phase 4 modularity / short-form configurator parity (Phase 1 deferred item).
- APScheduler-based scheduling → Phase 3 (unchanged).
