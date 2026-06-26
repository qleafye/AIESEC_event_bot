---
phase: 3
slug: scheduler-communications-verification
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-27
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (sync helper tests only — pytest-asyncio is version-broken in this env per RESEARCH) |
| **Config file** | none — existing tests are pure synchronous helper tests |
| **Quick run command** | `python -m pytest -q` |
| **Full suite command** | `python -m pytest` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest -q`
- **After every plan wave:** Run `python -m pytest`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 3-XX-XX | XX | X | REQ-XX | T-3-XX / — | {filled by planner} | unit | `python -m pytest -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*Note: this phase is async/DB/Telegram-heavy. Per RESEARCH Validation Architecture, extract pure helpers (filter-SQL builder, username normalization, reg_started cutoff comparison, retry_after math) into synchronously testable functions; scheduler/gspread/send-loop integration is Manual-Only.*

---

## Wave 0 Requirements

- [ ] `tests/test_filters.py` — pure filter-spec → SQL/predicate builder (COMM-02)
- [ ] `tests/test_verify.py` — username normalization (strip @, lowercase, trim) (VERIF-01)
- [ ] `tests/test_nudge.py` — reg_started cutoff comparison + one-shot dedup logic (SCHED-03)
- [ ] `tests/test_floodsafe.py` — retry_after+1 math / blocked-vs-retried classification (COMM-04)

*Planner refines exact file/helper names.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Scheduled broadcast survives restart | SCHED-01 | Requires live scheduler + restart cycle | Schedule a broadcast +2min, restart bot, confirm it fires |
| Filtered broadcast count preview + delivery | COMM-01/02/03 | Requires live Telegram + DB | Build filter, confirm count matches, only matched users receive |
| 429 handling | COMM-04 | Requires real rate-limit trigger | Observe TelegramRetryAfter caught, retried, not counted blocked |
| Dropout nudge one-shot | SCHED-03 | Requires interval job + 2h aging | Start+abandon reg, confirm exactly one nudge, registered users none |
| Pre-selection gate at /start | VERIF-01/02 | Requires live gspread allowlist | Username in/out of sheet, usernameless user prompt |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
