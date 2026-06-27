---
phase: 3
slug: scheduler-communications-verification
status: approved
nyquist_compliant: true
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
| 3-01-01 | 03-01 | 1 | SCHED-01 | T-3-01 | No APScheduler 4.0 API; jobstore on separate jobs.sqlite | unit | `python -m pytest tests/test_scheduler_helpers.py -q` | ❌ W0 | ⬜ pending |
| 3-01-02 | 03-01 | 1 | SCHED-01 | T-3-01 | Job args = broadcast_id only (serializable) | manual | restart-fire end-to-end | ❌ | ⬜ pending |
| 3-01-03 | 03-01 | 1 | SCHED-01 | T-3-02 | Schedule UI admin-id re-check | manual | admin schedule flow | ❌ | ⬜ pending |
| 3-02-01 | 03-02 | 2 | SCHED-03 | T-3-03 | One-shot dedup via nudged_at; no duplicate table | unit | `python -m pytest tests/test_nudge.py -q` | ❌ W0 | ⬜ pending |
| 3-02-02 | 03-02 | 2 | SCHED-03 | T-3-03 | Registered users never nudged | manual | 2h aging + interval scan | ❌ | ⬜ pending |
| 3-03-01 | 03-03 | 2 | COMM-04 | T-3-04 | retry_after+1 wait; retried ≠ blocked | unit | `python -m pytest tests/test_broadcast_429.py -q` | ❌ W0 | ⬜ pending |
| 3-03-02 | 03-03 | 2 | COMM-04 | T-3-04 | TelegramRetryAfter caught in both loops | manual | live 429 observation | ❌ | ⬜ pending |
| 3-04-01 | 03-04 | 3 | COMM-01/02/03 | T-3-05 | Whitelisted columns + `?` binds; injection rejected | unit | `python -m pytest tests/test_filters.py -q` | ❌ W0 | ⬜ pending |
| 3-04-02 | 03-04 | 3 | COMM-01/02/03 | T-3-05 | Count preview == recipient set | manual | count-vs-recipients | ❌ | ⬜ pending |
| 3-05-01 | 03-05 | 4 | VERIF-01/02 | T-3-10 | Normalization prevents allowlist bypass (case/@/ws) | unit | `python -m pytest tests/test_allowlist.py -q` | ❌ W0 | ⬜ pending |
| 3-05-02 | 03-05 | 4 | VERIF-01/02 | T-3-11 | Fail-open + admin alert on empty allowlist (accepted) | manual | live gating in/out + usernameless | ❌ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*W0 = Wave-0 pure-helper test stub, written first in its slice before integration.*

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
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-06-27
