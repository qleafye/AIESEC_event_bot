---
phase: 01-db-foundation-quick-wins-coins
verified: 2026-07-24T00:00:00Z
status: passed
score: 6/6 must-haves verified
overrides_applied: 0
note: >
  Phase built via ad-hoc quick tasks, not GSD execute-phase. No SUMMARY.md and
  ROADMAP box unchecked by design — verified goal-backward against live code + tests.
  Code is the source of truth. All 34 Phase 1 tests pass.
---

# Phase 1: DB Foundation + Quick Wins + Coins — Verification Report

**Phase Goal:** Bot runs safely against ~590 live users with correct schema migrations, coins economy established correctly, and visible UX quick wins.
**Verified:** 2026-07-24
**Status:** PASS (6/6 success criteria)
**Re-verification:** No — initial verification (ad-hoc quick-task build, no prior VERIFICATION.md)

## Overall Verdict: ✅ PASS

All six ROADMAP success criteria are achieved in the actual codebase. Each has both
implementation evidence and a passing integration/unit test. `python -m pytest`
across the four Phase 1 suites: **34 passed**.

## Success Criteria

| # | Criterion | Status | Key Evidence |
|---|-----------|--------|--------------|
| 1 | init_db backfills existing users → `status='approved'` | ✅ PASS | `database/db.py:76` |
| 2 | Re-registration preserves status/resume/new cols (ON CONFLICT DO UPDATE) | ✅ PASS | `database/db.py:208-291` |
| 3 | `/coins +10` then `-3` → balance 7 as SUM(delta), no RMW race | ✅ PASS | `database/db.py:498-515` |
| 4 | `/рейтинг` top-10 + requester rank; `🪙 Мои монеты` balance | ✅ PASS | `handlers/user_actions.py:74-92` |
| 5 | Subscription CHECKED (not gated) via getChatMember, fails open | ✅ PASS | `handlers/registration.py:1301-1326` |
| 6 | Abandoned /start → persistent `reg_started`, cleared on finish, broadcast segment | ✅ PASS | `database/db.py:117-123`, `registration.py:1205/2262` |

---

### Criterion 1 — Migration leaves existing users approved ✅ PASS

`database/db.py:76`:
```python
await _ensure_column(db, "users", "status", "TEXT DEFAULT 'approved'")
```
`_ensure_column` (db.py:31-35) runs `ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'approved'`
only when the column is absent. SQLite backfills every existing row with the DEFAULT,
so all ~590 live users land `status='approved'` — none lose access. The migration is
additive and idempotent (guarded by `_column_exists`).

**Test:** `tests/test_db_phase1.py:21` `test_migration_preserves_existing_users_with_approved_status`
seeds an OLD-schema table (no `status` column) + a row, runs `init_db()`, asserts
`SELECT status ... == 'approved'` (line 44). Passes.

### Criterion 2 — Re-registration preserves state ✅ PASS

`add_user` (db.py:208-291) uses `INSERT ... ON CONFLICT(telegram_id) DO UPDATE SET ...`
(db.py:232) — deliberately NOT `INSERT OR REPLACE` (which is DELETE+INSERT and would wipe
omitted columns). `status` is intentionally absent from the SET list (owned by the approval
flow), so re-registration never resets it. `resume_file_id/resume_text/resume_url` use
`COALESCE(excluded.x, users.x)` (db.py:259-261) so a re-reg without a new resume keeps the
stored one. Payment columns are deliberately omitted (WR-06 comment, db.py:275-279).

**Test:** `tests/test_db_phase1.py:47` `test_add_user_on_conflict_preserves_status_and_resume`
flips status→'pending' + resume→'abc123', re-registers with a changed name, asserts
`status=='pending'` and `resume_file_id=='abc123'` (lines 73-74). Passes.

### Criterion 3 — Coins balance = SUM(delta), race-free ✅ PASS

`add_coins` (db.py:498-506) is append-only: `INSERT INTO coins (...) VALUES (...)` — never
UPDATE. `get_balance` (db.py:509-515) derives `SELECT COALESCE(SUM(delta),0) FROM coins WHERE
user_id=?`. Because writes are pure inserts and reads are aggregates, there is no
read-modify-write window — concurrent `+10` and `-3` both append and the sum is 7 regardless
of ordering. Admin command `/coins` at `handlers/admin.py:149-177` parses signed amounts
(`_parse_coins_amount`, admin.py:84-93) and calls `add_coins` then reports `get_balance`.

**Test:** `tests/test_db_phase1.py:93` `test_balance_is_sum_of_deltas`: `add_coins(333,10)`,
`add_coins(333,-3)`, asserts `get_balance(333)==7` (line 98). Passes.

### Criterion 4 — /рейтинг top-10 + rank; Мои монеты ✅ PASS

`show_leaderboard` (`handlers/user_actions.py:82-92`) handles `Command("рейтинг","rating",
"leaderboard")`, pulls `get_leaderboard(10)` (db.py:518-533, ordered by `SUM(delta) DESC LIMIT
10`), `get_user_rank` (db.py:536-561, 1-based rank via `COUNT(*) ... HAVING bal > my_balance`),
and requester `get_balance`. `render_leaderboard` (user_actions.py:60-71) renders ranked names
+ "Твоё место / баланс". `🪙 Мои монеты` button handler `show_my_coins`
(user_actions.py:74-79) shows the caller's own `get_balance`.

**Test:** `tests/test_db_phase1.py:114` leaderboard ordering + rank; `test_coins_phase1.py:44-65`
render (ranked, requester line, HTML-escape, empty). Passes.

### Criterion 5 — Subscription checked (not gated), fails open ✅ PASS

`is_subscribed` (`handlers/registration.py:1301-1308`) calls `bot.get_chat_member(channel,
user_id)` against the `contact_tg` setting and maps status via `_membership_status_to_bool`
(registration.py:1296-1298). On ANY exception (bot not admin / unknown channel) it returns
`None` — fail-open (D-07). In `cmd_start` (registration.py:1318-1326) the check is wrapped in
try/except and only records the flag via `set_user_subscribed` when the result is not None; it
never blocks or gates the user. Admin segment `process_broadcast_unsubscribed`
(`handlers/admin.py:1398-1408`) pulls `get_non_subscriber_ids` and shows the count in the
prompt before sending a reminder broadcast; segment button wired at admin.py:1257/1287.

**Test:** `tests/test_subscription_phase1.py:29` fails open on raising bot (returns None, no
raise); `:41` true for member; `:47-54` segment handlers exist. Passes.

Minor note (non-blocking): the non-subscriber count is surfaced at the moment the admin selects
the broadcast segment (`{N} пользователей не подписаны`, admin.py:1406) rather than as a
standalone dashboard stat. This satisfies "admin is shown the count ... and can send a
reminder via a broadcast segment."

### Criterion 6 — Persistent reg_started dropout tracking + segment ✅ PASS

`reg_started` table created in `init_db` (db.py:117-123) — a real DB table, independent of
MemoryStorage FSM, survives restart. `mark_reg_started` is called at flow start in
`_start_registration_flow` (registration.py:1205, fail-soft try/except). `clear_reg_started` is
called on completion after `add_user` (registration.py:2262). Broadcast segment
`process_broadcast_incomplete` (admin.py:1411-1421) uses `get_incomplete_user_ids`
(db.py:597-600) as a distinct audience; button wired at admin.py:1258/1288. Automated nudging
is correctly deferred to SCHED-03/Phase 3 (`nudged_at` column already present, db.py:125).

**Test:** `tests/test_db_phase1.py:133` mark/query/clear round-trip; `:142` double-mark upserts;
`test_subscription_phase1.py:54` segment handler exists. Passes.

## Behavioral Spot-Check

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Phase 1 test suites | `pytest test_coins/db/subscription/registration_phase1` | 34 passed in ~11s | ✅ PASS |

## Anti-Patterns Found

None blocking. The append-only ledger, ON CONFLICT DO UPDATE, and fail-open subscription
patterns are deliberate and well-commented (WR-06, D-07, IN-* references). No unreferenced
TBD/FIXME/XXX markers in the Phase 1 code paths.

## Gaps Summary

No gaps. All six success criteria are observably true in the codebase with corroborating
passing tests. The missing SUMMARY.md and unchecked ROADMAP box are process artifacts of the
ad-hoc quick-task build path, not goal failures — the goal (safe migration against ~590 live
users, correct coins economy, visible UX quick wins) is achieved.

---

_Verified: 2026-07-24_
_Verifier: Claude (gsd-verifier)_
