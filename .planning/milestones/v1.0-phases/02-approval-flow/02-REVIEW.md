---
phase: 02-approval-flow
reviewed: 2026-07-24T03:33:48Z
depth: deep
files_reviewed: 5
files_reviewed_list:
  - database/db.py
  - handlers/admin.py
  - handlers/registration.py
  - handlers/user_actions.py
  - services/reminders.py
  - main.py
findings:
  critical: 0
  warning: 4
  info: 4
  total: 8
status: issues_found
---

# Phase 2: Code Review Report — Approval Flow

**Reviewed:** 2026-07-24T03:33:48Z
**Depth:** deep (cross-file: registration → admin → db → reminders → main)
**Files Reviewed:** 6
**Status:** issues_found

## Overall Verdict

**SHIP WITH FIXES.** The core approval mechanics are sound: the atomic
`UPDATE ... WHERE status='pending'` + `rowcount==1` guard correctly prevents
double-approval and double-welcome across concurrent managers (D-10); the mass
approve does DB-flip-first via `RETURNING` then drains sends with a real
`TelegramRetryAfter` back-off (D-11); the re-apply path is correct (`add_user`
ON CONFLICT excludes `status`, and `finalize_registration` force-sets it via
`set_user_status`); user-facing Telegram sends (welcome, rejection, reminder,
resume re-send) are uniformly wrapped fail-soft against blocked/deleted chats;
and all free-text (reject reason, `reject_text` prefix, card fields) is
`html.escape`d — no injection surface. Security is clean.

The defects are correctness/robustness gaps around the mass-approve UI path and
background-task lifecycle, plus a rejection-FSM state leak. **No CRITICAL / no
BLOCKER**, but two WARNINGs (HL-01, HL-02) can silently drop the welcome+menu
for users who are already flipped to `approved` in the DB — a visible "approved
but locked out" state for the delegate — and should be fixed before this ships
to the 590+ surge.

Severity counts: **CRITICAL 0 · HIGH 2 · MEDIUM 2 · LOW/INFO 4**
(mapped to frontmatter as warning=4 [HIGH+MEDIUM], info=4).

## Warnings

### WR-01 (HIGH): Mass-approve welcome drain is skipped if the UI `edit_text` throws — users flipped to `approved` in DB never get welcome/menu

**File:** `handlers/admin.py:2681-2691` (`appr_all_yes`)
**Issue:** The order is: (1) `approve_all_pending()` commits every pending row to
`approved`, then (2) an **unguarded** `await callback.message.edit_text(...)`,
then (3) `asyncio.create_task(_welcome_flipped(...))` schedules the welcome
drain. If step 2 raises — which it will for a real, reachable case: the manager
opens the "Одобрить все N?" confirm dialog and clicks **Да** more than 48h later
(inline buttons never expire, but Telegram rejects editing a message older than
48h with `TelegramBadRequest`), or the confirm message was deleted — the
exception propagates to the global `@dp.errors()` handler and **steps 3–4 never
run**. Result: N users are `approved` in the DB but receive no welcome, no main
menu, and (with `payment_enabled`) no payment requisites. They are silently
locked in an "approved-but-nothing-happened" state. This directly violates the
D-11 guarantee "each flipped user receives welcome exactly once."
**Fix:** Schedule the drain independent of the UI edit, and make the edit
fail-soft:
```python
ids = await approve_all_pending()  # atomic flip first (D-11)
# Schedule the sends BEFORE any fragile UI call so a display failure
# can never strand approved-in-DB users without their welcome.
if ids:
    _spawn(_welcome_flipped(callback.bot, ids))
    _spawn(bulk_update_status_in_sheet({str(t): STATUS_LABELS["approved"] for t in ids}))
try:
    await callback.message.edit_text(
        f"✅ Одобрено: {len(ids)}. Рассылаю приветствия…",
        reply_markup=build_admin_keyboard(),
    )
except Exception:
    logger.warning("appr_all_yes: confirm edit failed (drain already scheduled)", exc_info=True)
await callback.answer()
```
(See WR-02 for the `_spawn` strong-reference requirement.)

### WR-02 (HIGH): Background tasks are `create_task`'d without a strong reference — welcome drain / sheet-sync can be garbage-collected mid-run

**File:** `handlers/admin.py:2577, 2615, 2686, 2689-2691` (`appr_approve`,
`appr_reject_reason`, `appr_all_yes`)
**Issue:** These fire-and-forget tasks are created with a bare
`asyncio.create_task(...)` and the returned Task is never stored. `main.py:51-61`
already documents this exact hazard (WR-02 there) — "the event loop keeps only
weak references to tasks — a fire-and-forget create_task() can be
garbage-collected mid-run" — and provides `_spawn()` to hold a strong ref, but
the Phase-2 admin handlers do not use it. The highest-impact instance is
`_welcome_flipped` (line 2686): it is the *sole* delivery path for the
"welcome exactly once" guarantee on a mass approve of potentially hundreds of
users, and it suspends on `asyncio.sleep(0.05)` between every send, giving the
GC repeated opportunities. If collected mid-drain, the remaining approved users
silently never receive their welcome/menu. A related durability gap: because the
drain is in-process only, a redeploy/SIGTERM during the drain (common at 590+
scale) also strands the not-yet-welcomed remainder with no recovery path.
**Fix:** Route every one of these through the existing `_spawn` helper (export it
from `main.py` or replicate the `set + add_done_callback(discard)` pattern in
`admin.py`) so the task is strongly referenced for its whole lifetime:
```python
_spawn(update_status_in_sheet(tid, STATUS_LABELS["approved"]))
...
_spawn(_welcome_flipped(callback.bot, ids))
```

### WR-03 (MEDIUM): Rejection FSM (`Approval.reason`) leaks state and captures unintended input

**File:** `handlers/admin.py:2608-2630` (`appr_reject_reason`)
**Issue:** The reason handler `@router.message(Approval.reason, is_admin)` matches
any message, but stateless command handlers registered earlier in the same
router (e.g. `cmd_admin_help` at `admin.py:131`, `cmd_settings_guide` at 2921)
have no `StateFilter` and therefore still fire while the admin is in
`Approval.reason`. If the manager, mid-rejection, types `/admin` (or any command),
the command runs and the FSM is **left stuck** in `Approval.reason` — the next
plain-text message the manager sends is then silently consumed as a rejection
reason for the previously-selected applicant. Additionally, a non-text reply
(sticker/photo) sets `reason = message.text or "-"`, rejecting the user with a
literal "-" reason. There is no timeout/escape other than the exact strings
"Отмена"/"/cancel".
**Fix:** (a) Add an explicit command guard/cancel inside the state, e.g. a
`@router.message(Approval.reason, is_admin, F.text.startswith("/"))` handler that
clears the state and re-shows the card; and/or (b) require a real text reason —
if `message.text` is None, re-prompt instead of rejecting with "-".

### WR-04 (MEDIUM): `appr_all_confirm` / `appr_all_no` leave a stale confirm dialog with live buttons

**File:** `handlers/admin.py:2647` (`appr_all_confirm`), `2651-2657`
(`appr_all_no`)
**Issue:** `appr_all_confirm` edits the card into "Одобрить все N заявок?" with
Да/Отмена buttons but this `edit_text` is unguarded (a 48h-old card raises and is
only swallowed by the global error handler — no confirm shown, no feedback). On
`appr_all_no`, the code sends a *new* card via `_show_current_card` but never
removes or disables the stale "Одобрить все N?" dialog, so its Да button remains
clickable and can later fire `appr_all_yes` against a now-different pending set.
**Fix:** Wrap the confirm `edit_text` in try/except (fall back to `answer`), and
in `appr_all_no` edit the dialog message to a terminal state (or strip its
`reply_markup`) before rendering the next card.

## Info

### IN-01 (LOW): Card position indicator is miscomputed when applications are skipped or span batches

**File:** `handlers/admin.py:2502-2503` (`_show_current_card`)
**Issue:** `position = total - len(visible) + 1` uses `len(visible)` from only the
*last* fetched batch and ignores skipped rows in earlier batches, so the
"Заявка X/total" header can show a wrong X once the manager has skipped some
applications or the queue exceeds one 50-row batch. Display-only; no functional
impact.
**Fix:** Track the real offset of the chosen row (e.g. `position = offset -
len(batch) + batch.index(current) + 1`, adjusted for filtered skips) or drop the
X/total precision and show just the total.

### IN-02 (LOW): `get_pending_count()` / `get_pending_users()` read on separate connections — transient "Заявок нет" under concurrent registration

**File:** `handlers/admin.py:2490-2499` (`_show_current_card`)
**Issue:** `total = get_pending_count()` and the `get_pending_users` batch loop
run as independent queries. If `total` is read as 0 a moment before a new pending
row lands, the `while ... offset < total` loop body never executes and the
manager sees "✅ Заявок нет." despite a pending application existing. Self-heals
on the next open. Acceptable given the DB-driven design, but worth a note.
**Fix:** Drop the `offset < total` guard and instead loop until a batch returns
fewer than `limit` rows, so a freshly-arrived pending row is not missed.

### IN-03 (LOW): Settings-guide renders a blank value for a setting explicitly set to empty string

**File:** `handlers/admin.py:2914-2915` (`_render_settings_guide`)
**Issue:** `shown = val if val is not None else f"{default} (по умолчанию)"`
shows an empty string (not the default hint) when a manager has intentionally
saved an empty value for a setting, making `/settings_guide` display a blank
"Текущее:" line that reads like a bug.
**Fix:** Treat empty string like `None`: `shown = val if val else f"{default} (по
умолчанию)"` (accepting that a deliberately-blank text setting then shows the
default label).

### IN-04 (INFO): Reminder loop reads/sleeps the interval even when disabled; magic `0.05` send-gap

**File:** `services/reminders.py:38-49`, `handlers/admin.py:2673`
**Issue:** (a) `pending_reminder_loop` reads `pending_reminder_interval` and
sleeps it every cycle even when `pending_reminder_enabled == "off"`, so a tiny
interval left over from testing spins the loop doing near-nothing work; harmless
but slightly wasteful. (b) `_welcome_flipped` uses a bare `0.05` inter-send
sleep — a magic number that should be a named constant documenting the ~20 msg/s
pacing intent alongside the 429 handler.
**Fix:** (a) When disabled, sleep a fixed floor (e.g. `DEFAULT_INTERVAL`) rather
than the configured value; (b) hoist `0.05` to a module constant, e.g.
`MASS_APPROVE_SEND_GAP = 0.05`.

## Verified Correct (no action)

- **`approve_user_atomic` / `reject_user`** (`db.py:666-686`): correct
  `UPDATE ... WHERE status='pending'` + `rowcount==1` double-approval guard;
  concurrent second approve returns False and sends nothing (D-10).
- **`approve_all_pending`** (`db.py:710-721`): single atomic `UPDATE ... RETURNING
  telegram_id`; a double-tap of "Да" returns `[]` on the second call — no
  double-welcome.
- **Re-apply path**: `add_user` ON CONFLICT DO UPDATE (`db.py:232-`) omits
  `status`; `finalize_registration` (`registration.py:2274-2282`) force-sets it
  via `set_user_status`; `cmd_start` (`registration.py:1370`) lets only `rejected`
  users fall through to re-register — approved users early-return and are never
  reset.
- **`_decide_status`** (`registration.py:64-78`): pure, correct per-form
  manual→pending / auto→approved mapping; party track defaults to pending.
- **`ensure_registered` / `_gate_decision`** (`user_actions.py:27-55`): legacy /
  missing / unknown status → allowed (protects the ~590 `approved` users);
  pending/rejected gated with the correct copy.
- **Fail-soft Telegram sends**: `approve_user` (`registration.py:2145-2177`),
  rejection notify (`admin.py:2617-2625`), reminder per-admin
  (`reminders.py:41-45`), resume re-send (`admin.py:2552-2556`), and
  `_welcome_flipped` 429 back-off (`admin.py:2665-2670`) all wrapped — a
  blocked/deleted chat never raises.
- **Reminder task lifecycle**: started via `main.py` `_spawn` (strong ref);
  `while True` body try/except logs and continues; `CancelledError`
  (BaseException) is not swallowed, so shutdown cancellation is clean.
- **Injection**: all SQL parameterized; all free-text `html.escape`d before
  HTML-mode sends. No SQL/HTML injection surface in the reviewed code.

---

_Reviewed: 2026-07-24T03:33:48Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
