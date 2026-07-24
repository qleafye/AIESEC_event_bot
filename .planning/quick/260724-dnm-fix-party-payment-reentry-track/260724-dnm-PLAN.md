---
phase: quick-260724-dnm
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - handlers/user_actions.py
  - tests/test_payment_phase5.py
autonomous: true
requirements: [BLOCKER-party-payment-reentry]

must_haves:
  truths:
    - "A party delegate re-entering payment via the «💳 Оплата» button is filtered against their OWN track's tariffs, not full-track tariffs."
    - "upload_receipt_entry resolves participant_type from the DB (get_user) and threads it into start_payment_step."
    - "A party delegate whose only tariff is track-restricted is never silently routed to completion (D-18) because their track was mistaken for 'full'."
  artifacts:
    - path: "handlers/user_actions.py"
      provides: "upload_receipt_entry passing participant_type to start_payment_step"
      contains: "start_payment_step(bot, message.from_user.id, participant_type"
    - path: "tests/test_payment_phase5.py"
      provides: "Regression test for the re-entry track threading"
      contains: "upload_receipt_entry"
  key_links:
    - from: "handlers/user_actions.py:upload_receipt_entry"
      to: "handlers/payment.py:start_payment_step"
      via: "participant_type resolved via get_user()"
      pattern: "start_payment_step\\(bot, message\\.from_user\\.id, participant_type"
---

<objective>
Fix the party-track payment re-entry blocker. `upload_receipt_entry` (the «💳 Оплата»
button re-entry handler, `handlers/user_actions.py:106`) calls
`start_payment_step(bot, message.from_user.id)` WITHOUT `participant_type`, so it defaults
to `"full"`. A party delegate re-entering payment (deferred earlier via «Оплачу позже», or
FSM lost on a bot restart) is then filtered against full-track tariffs: they see the wrong
(full) price when a track=None fallback tariff exists, or — when none exists —
`_visible_options` returns empty, D-18 fires, and they are silently routed to completion
WITHOUT paying.

The approval path (`handlers/registration.py:2231/2245`) already resolves `participant_type`
via `get_user()` and threads it correctly. This plan makes the re-entry path do the same.

Purpose: A party delegate re-entering payment must be charged their own track's tariff.
Output: One-line-scope threading fix in `upload_receipt_entry` + a regression test.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<interfaces>
<!-- Executor uses these directly — no codebase exploration needed. -->

From handlers/payment.py:
```python
async def start_payment_step(bot: Bot, telegram_id: int, participant_type: str = "full")
async def should_offer_receipt_upload(telegram_id: int) -> bool
```

From database/db.py:
```python
async def get_user(telegram_id: int)  # returns dict | None; dict has "participant_type"
```

Correct threading pattern already in handlers/registration.py:2230-2245 (approve path):
```python
try:
    user_row = await get_user(telegram_id)
    participant_type = (user_row or {}).get("participant_type") or "full"
except Exception as e:
    logger.error(f"Failed to resolve participant_type for {telegram_id}, defaulting to 'full': {e}")
    participant_type = "full"
...
await start_payment_step(bot, telegram_id, participant_type)
```

Current site — handlers/user_actions.py:95-106 (`get_user` is ALREADY imported at module top,
line 8; `start_payment_step` is lazy-imported inside the function):
```python
@router.message(F.text == "💳 Оплата")
async def upload_receipt_entry(message: types.Message, bot: Bot):
    if not await ensure_registered(message):
        return
    from handlers.payment import should_offer_receipt_upload, start_payment_step
    if not await should_offer_receipt_upload(message.from_user.id):
        await message.answer("Оплатили или оплата не требуется.")
        return
    await start_payment_step(bot, message.from_user.id)
```

Test conventions (tests/test_payment_phase5.py): plain `def test_*` using `asyncio.run(...)`,
`tmp_path` + `_use_tmp_db(tmp_path)`, `monkeypatch.setattr`. NO pytest-asyncio in this env.
`db.add_user({...})` seeds a users row; `db.set_setting(key, val)` seeds bot_settings.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Thread participant_type through upload_receipt_entry</name>
  <files>handlers/user_actions.py</files>
  <action>
In `upload_receipt_entry` (handlers/user_actions.py:95-106), before the final
`start_payment_step(...)` call, resolve `participant_type` from the DB exactly as the
approval path does (registration.py:2230-2235): call the already-imported `get_user(message.from_user.id)`,
take `(user_row or {}).get("participant_type") or "full"`, wrapped in try/except that logs
and degrades to `"full"` on lookup failure (an approved user must never be blocked from paying).
Then pass it positionally as the third argument:
`await start_payment_step(bot, message.from_user.id, participant_type)`.
Do NOT add a second get_user call beyond this one, do NOT modify `should_offer_receipt_upload`
or `start_payment_step`, and keep the existing lazy import of `start_payment_step`. Keep the
change minimal and format-preserving. This fixes the BLOCKER where a party delegate re-entering
payment was filtered against full-track tariffs (wrong price, or silent D-18 skip-to-completion).
  </action>
  <verify>
    <automated>python -c "import inspect, handlers.user_actions as u; s=inspect.getsource(u.upload_receipt_entry); assert 'get_user(message.from_user.id)' in s and 'participant_type' in s and 'start_payment_step(bot, message.from_user.id, participant_type' in s, s"</automated>
  </verify>
  <done>upload_receipt_entry resolves participant_type via get_user (try/except → "full" fallback) and passes it positionally into start_payment_step; no other functions touched.</done>
</task>

<task type="auto">
  <name>Task 2: Regression test — re-entry filters against the delegate's own track</name>
  <files>tests/test_payment_phase5.py</files>
  <action>
Append a regression test to tests/test_payment_phase5.py mirroring the existing structure
(plain `def test_*`, `asyncio.run(...)`, `tmp_path`, `_use_tmp_db`, `monkeypatch`). The test:
(1) `_use_tmp_db(tmp_path)`, `asyncio.run(db.init_db())`.
(2) Seed a party_overnight user via `db.add_user({...})` with `participant_type="party_overnight"`,
`status="approved"`, `payment_status="not_paid"`, plus the required base fields
(`telegram_id`, `full_name`, `registration_date`).
(3) Seed settings so `should_offer_receipt_upload` returns True: `payment_enabled="on"`, and
whatever requisites setting `_resolve_requisites` reads so it returns a non-empty string
(inspect `handlers.payment._resolve_requisites` to find the exact setting key — set it to a
non-empty value). Also set `payment_options` with a party-only tariff at a NON-zero index and a
different full/no-overnight tariff at index 0, e.g.
`"Без ночёвки|1000|party_noovernight\nС ночёвкой|1500|party_overnight"`.
(4) Monkeypatch `handlers.payment.start_payment_step` with an async capture stub that records the
`participant_type` argument it receives (accept `(bot, telegram_id, participant_type="full")`).
(5) Build a minimal FakeMessage (`.from_user.id`, async `.answer(...)`) and FakeBot (`id=1`), then
`asyncio.run(user_actions.upload_receipt_entry(FakeMessage(), FakeBot()))`.
(6) Assert the captured `participant_type == "party_overnight"` — proving the re-entry path
threads the delegate's OWN track, NOT the "full" default. `import handlers.user_actions as user_actions`
at the top of the test (module-level import inside the test function is fine to keep the append localized).
Note: if `ensure_registered` reads settings/status that would reject the seeded user, ensure the
seeded status makes `_gate_decision` return allowed (status="approved").
  </action>
  <verify>
    <automated>python -m pytest tests/test_payment_phase5.py -k "reentry or re_entry or upload_receipt" -x -q</automated>
  </verify>
  <done>New test seeds a party_overnight delegate, invokes upload_receipt_entry, and asserts start_payment_step receives participant_type="party_overnight"; test passes. Existing tests in the file remain green.</done>
</task>

</tasks>

<verification>
- `python -m pytest tests/test_payment_phase5.py -q` — full file green (no regressions).
- upload_receipt_entry source contains the get_user resolution + 3-arg start_payment_step call.
</verification>

<success_criteria>
- A party delegate re-entering via «💳 Оплата» is filtered against their own track's tariffs.
- start_payment_step receives the DB-resolved participant_type from the re-entry path.
- Lookup failure degrades to "full" (never blocks an approved user from paying).
- No changes to start_payment_step or should_offer_receipt_upload beyond the call site.
</success_criteria>

<output>
After completion, create `.planning/quick/260724-dnm-fix-party-payment-reentry-track/260724-dnm-SUMMARY.md`
</output>
