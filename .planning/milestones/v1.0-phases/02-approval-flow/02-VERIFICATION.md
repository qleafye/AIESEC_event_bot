---
phase: 02-approval-flow
verified: 2026-07-24T00:00:00Z
status: passed
score: 5/5 must-haves verified
mode: mvp
overrides_applied: 0
re_verification:
  previous_status: none
  note: initial verification, no prior VERIFICATION.md, no SUMMARY.md (SUMMARY absence is not a failure — code is source of truth)
---

# Phase 2: Approval Flow — Verification Report

**Phase Goal:** Manager can fully moderate applications through a paginated tinder UI — submission to approval/rejection — without notification floods or double-approvals.

**Verified:** 2026-07-24
**Status:** PASSED (5/5 success criteria met)
**Mode:** mvp (user-story goal; verified via goal-backward against real code)

## Overall Verdict: PASS

All five ROADMAP success criteria are achieved in the actual codebase. The DB approval layer, admin tinder UI, atomic double-approval guard, bulk-approve confirmation, and anti-storm periodic reminder all exist, are substantive, are wired end-to-end, and compile (`python -m py_compile` OK across all six touched files).

---

## Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Pending user sees "заявка отправлена" w/ no menu; `ensure_registered()` gates until approved | ✓ PASS | registration.py:2333-2337, 2278-2280; user_actions.py:38-55 |
| 2 | Paginated single-card queue (DB-driven oldest-pending) with 4 buttons + resume re-send via `answer_document` | ✓ PASS | admin.py:2487-2512, 2471-2484; db.py:689-698; admin.py:2551-2553 |
| 3 | Concurrent double-approve → exactly one welcome (atomic UPDATE...WHERE status='pending' + rowcount guard) | ✓ PASS | db.py:666-675; admin.py:2573-2586 |
| 4 | "Одобрить все N" confirmation dialog; each user welcomed exactly once | ✓ PASS | admin.py:2633-2648, 2676-2692, 2660-2673; db.py:710-721 |
| 5 | Periodic reminder with pending count (not per-submission storm) | ✓ PASS | services/reminders.py:31-49; main.py:132; registration.py:2301-2305 |

**Score:** 5/5 truths verified

---

## Per-Criterion Detail

### Criterion 1 — Pending gating + "заявка отправлена", no menu — PASS
- `_decide_status()` (registration.py:64-78) maps form-type × per-form setting → `pending`/`approved`; party tracks resolve from `party_approval`, defaulting to `manual`/pending (T-05-04-02). Status persisted via `set_user_status` before menu decision (registration.py:2274-2280).
- Completion message sent to ALL submitters with `ReplyKeyboardRemove()` — no main menu (registration.py:2333-2337). `DEFAULT_REG_COMPLETE_TEXT` = "Поздравляем, твоя заявка принята!..." Main menu keyboard is only sent inside `approve_user()` (registration.py:2130), which is called only when `status == "approved"` (registration.py:2338-2339).
- `ensure_registered()` (user_actions.py:38-55) returns False for `pending` ("⏳ Твоя заявка на рассмотрении…") and `rejected`; True only for approved/legacy. Gate wired into 11 gated actions across handlers.

### Criterion 2 — Paginated single-card DB-driven queue + resume re-send — PASS
- Queue driver is DB, restart-safe: `get_pending_users()` (db.py:689-698) selects `WHERE status='pending' ORDER BY registration_date ASC, telegram_id ASC`. `_show_current_card()` (admin.py:2487-2512) renders exactly ONE card (`visible[0]`) with position/total header.
- Buttons present: Одобрить/Отклонить (admin.py:2474-2475), Пропустить (2481), Одобрить все (N) (2483), plus 📎 Резюме when a resume exists (2479-2480).
- The `appr_skipped` FSM set is a *session-only* supplementary filter (admin.py:2489, 2520, cleared each open), NOT the queue driver — on restart it clears and all pending re-appear. This satisfies "not FSM page offsets that reset on restart."
- Resume re-send: `appr_resume` handler re-sends the stored `resume_file_id` via `callback.message.answer_document(...)` (admin.py:2553), with text-resume fallback (2557-2561). Note: delivery is button-triggered (📎 Резюме) rather than auto-pushed on every card render — a deliberate UX choice that avoids sending files for cards the manager will skip. The required mechanism (`answer_document` re-send of the stored file) is present and wired. Functionally satisfies APP-04 / manager-side QW-03.

### Criterion 3 — Atomic single-winner approval — PASS
- `approve_user_atomic()` (db.py:666-675): `UPDATE users SET status='approved' WHERE telegram_id=? AND status='pending'`, returns `cursor.rowcount == 1`. SQLite per-statement atomicity + the `status='pending'` predicate guarantees exactly one caller flips the row.
- Handler `appr_approve` (admin.py:2567-2586): welcome (`approve_user`) + sheet sync fire ONLY when `won` is True (2574-2578); the loser gets "Уже обработано" (2581) and no welcome. No TOCTOU: the guard is the UPDATE itself, not a prior SELECT.

### Criterion 4 — "Одобрить все N" confirmation + once-each welcome — PASS
- `appr_all_confirm` (admin.py:2633-2648) shows a Да/Отмена inline dialog before any mutation; `appr_all_no` (2651-2657) aborts cleanly.
- `appr_all_yes` (2676-2692) calls `approve_all_pending()` (db.py:710-721), a single atomic `UPDATE ... WHERE status='pending' RETURNING telegram_id` that returns each flipped id exactly once. `_welcome_flipped()` (2660-2673) iterates those ids once each, calling `approve_user` per id with TelegramRetryAfter (429) handling — welcome delivered exactly once per approved user.

### Criterion 5 — Periodic reminder, no notification storm — PASS
- `pending_reminder_loop()` (reminders.py:31-49): forever loop, once per configurable interval (default 1800s), pings each admin the live `get_pending_count()` only when count > 0; fail-soft per-iteration and per-admin.
- Started at startup via `_spawn(pending_reminder_loop(bot))` (main.py:132) with a strong task ref held in `_background_tasks` (main.py:54-61) to prevent GC — a documented WR-02 fix.
- Storm prevention confirmed on the write side: per-submission admin notify fires only for `approved`, or `pending` when `pending_notify_mode == 'instant'`; the default `batched` mode sends NO per-submission admin push (registration.py:2301-2305).

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| database/db.py (654-721) | approval DB layer | ✓ VERIFIED | atomic approve/reject, oldest-pending queue, count, bulk RETURNING |
| handlers/admin.py (2412-2692) | tinder UI + handlers | ✓ VERIFIED | card render, 4 buttons, resume re-send, atomic approve, reject FSM, bulk confirm |
| handlers/registration.py (2145-2339) | submit/approve split + status decision | ✓ VERIFIED | `_decide_status`, `approve_user`, no-menu completion, gated notify |
| handlers/user_actions.py (38-55) | `ensure_registered` gate | ✓ VERIFIED | pending/rejected denied, 11 call sites |
| services/reminders.py | periodic reminder task | ✓ VERIFIED | interval loop, count ping, fail-soft |
| main.py (132) | startup wiring | ✓ VERIFIED | `_spawn(pending_reminder_loop)` with strong ref |

## Key Link Verification

| From | To | Via | Status |
|------|----|----|--------|
| admin.py appr_approve | db.approve_user_atomic | rowcount guard | ✓ WIRED (admin.py:2573) |
| admin.py appr_approve/all | registration.approve_user | import + call | ✓ WIRED (admin.py:58 import; 2575, 2664) |
| admin.py appr_all_yes | db.approve_all_pending | atomic RETURNING | ✓ WIRED (admin.py:2681) |
| main.py | reminders.pending_reminder_loop | `_spawn` at startup | ✓ WIRED (main.py:11, 132) |
| registration.finalize | db.set_user_status | status persist | ✓ WIRED (registration.py:2280) |
| appr_reject FSM | states.Approval.reason | set_state | ✓ WIRED (states.py:54; admin.py:2597) |

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Touched files parse | `python -m py_compile` (6 files) | COMPILE OK | ✓ PASS |
| approve_user import resolves in admin | grep import chain | present (admin.py:58) | ✓ PASS |

Runtime concurrency/timing behaviors (two managers racing; 30-min reminder cadence; live 429 backoff) are provably correct from the code by construction (SQLite per-statement atomicity + WHERE predicate; single interval loop) and were not exercised against a live bot. Optional non-blocking smoke test recommended before production: fire two rapid `appr_approve` callbacks for one tid and confirm one welcome.

## Anti-Patterns Found

None blocking. No `TBD`/`FIXME`/`XXX` debt markers in the Phase-2 approval code paths. Empty-return patterns in the reviewed ranges are legitimate fail-soft/early-return guards, not stubs. Resume delivery is button-gated (see Criterion 2 note) — an intentional design decision, not a stub.

## Gaps Summary

No gaps. All five success criteria are achieved with concrete, wired, compiling code evidence. The single nuance worth recording (not a gap): resume re-send is delivered via a 📎 Резюме button rather than auto-pushed on every card render — the `answer_document` re-send mechanism required by the criterion is present and functional.

---

_Verified: 2026-07-24_
_Verifier: Claude (gsd-verifier), goal-backward against codebase_
