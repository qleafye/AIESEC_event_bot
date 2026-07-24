---
quick_id: 260724-dnm
status: complete
date: 2026-07-24
commits:
  - 663d697  # fix: thread participant_type through payment re-entry
  - f561da0  # test: regression test for payment re-entry track threading
tests: "tests/test_payment_phase5.py — 27/27 pass (26 pre-existing + 1 new)"
---

# Quick Task 260724-dnm — Fix party-track payment re-entry blocker

## Problem (from v1.0 milestone audit — cross-phase integration blocker, HIGH)

`handlers/user_actions.py:upload_receipt_entry` (the «💳 Оплата» re-entry handler) called
`start_payment_step(bot, message.from_user.id)` without `participant_type`, defaulting to
`"full"` (signature `handlers/payment.py:188`). A party delegate re-entering payment
(deferred via «Оплачу позже», or FSM lost on bot restart) was filtered against full-track
tariffs — wrong (full) price if a `track=None` fallback existed, or silently routed to
completion without paying (D-18) if none existed. Uncovered by any test.

## Fix

Resolve `participant_type` from the DB via `get_user()` with a fail-soft `"full"` fallback
(mirroring the already-correct approval path `registration.py:2230-2245`), and thread it as
the third positional arg to `start_payment_step`. Lazy import preserved;
`should_offer_receipt_upload` and `start_payment_step` untouched.

```python
try:
    user_row = await get_user(message.from_user.id)
    participant_type = (user_row or {}).get("participant_type") or "full"
except Exception as e:
    logger.error(f"Failed to resolve participant_type for {message.from_user.id}, defaulting to 'full': {e}")
    participant_type = "full"
await start_payment_step(bot, message.from_user.id, participant_type)
```

## Regression test

Appended to `tests/test_payment_phase5.py` (env conventions: plain `def test_*`,
`asyncio.run()`, `tmp_path`, `monkeypatch` — no pytest-asyncio). Seeds a `party_overnight`
delegate with a party-only tariff, invokes `upload_receipt_entry`, asserts
`start_payment_step` receives `participant_type="party_overnight"` (not `"full"`).

## Result

- `handlers/user_actions.py` — participant_type threading (fail-soft)
- `tests/test_payment_phase5.py` — +1 regression test
- 27/27 pass, 0 fail. Closes TRACK-05/PAY-02/PAY-05 re-entry gap from `v1.0-MILESTONE-AUDIT.md`.

**Note:** SUMMARY.md reconstructed by orchestrator after worktree cleanup (original untracked copy lost on `git worktree remove --force`; content faithful to executor report).
