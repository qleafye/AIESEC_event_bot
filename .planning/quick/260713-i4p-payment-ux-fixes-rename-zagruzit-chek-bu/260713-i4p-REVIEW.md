---
phase: 260713-i4p-payment-ux-fixes-rename-zagruzit-chek-bu
reviewed: 2026-07-13T00:00:00Z
depth: standard
files_reviewed: 4
files_reviewed_list:
  - handlers/payment.py
  - handlers/user_actions.py
  - keyboards/builders.py
  - services/scheduler.py
findings:
  critical: 0
  warning: 2
  info: 1
  total: 3
status: issues_found
---

# Quick Task 260713-i4p: Code Review Report

**Reviewed:** 2026-07-13
**Depth:** standard
**Files Reviewed:** 4
**Status:** issues_found

## Summary

Reviewed commits `700c43c` (button rename) and `4cfdffc` (requisites surfacing in
`process_pay_later` and the multi-option branch of `start_payment_step`) against diff base
`03254e6a`.

**Rename correctness confirmed byte-for-byte.** Compared the two literal strings
programmatically (codepoint-by-codepoint, not just visually): `keyboards/builders.py:31`
and `handlers/user_actions.py:95` both hold the identical 8-codepoint sequence
`U+1F4B3 U+0020 U+041E U+043F U+043B U+0430 U+0442 U+0430` (`💳 Оплата`). The reply-keyboard
button will fire correctly — no silent-dead-button regression. A repo-wide grep confirms no
remaining `.py` source references to the old label `Загрузить чек` outside `.planning/`
historical records. `services/scheduler.py`'s overdue-reminder default text was updated in
lockstep.

**HTML-escaping of the two new requisites surfaces is correct.** Both new call sites
(`start_payment_step`'s multi-option branch, `process_pay_later`) route the admin-entered
`payment_requisites`/`payment_requisites_by_lc` value through `html.escape()` before
interpolating into a `parse_mode="HTML"` message, matching the pre-existing pattern in
`_show_payment_details`. No injection vector found. Ran the full test suite
(`tests/test_payment_lc_requisites.py` plus the whole `tests/` directory, 121 tests) —
all pass, confirming `_resolve_requisites`'s signature and behavior are untouched.

Two robustness/maintainability issues found, detailed below — neither is an injection or
crash-the-bot risk, but one breaks this file's own documented fail-soft convention.

## Warnings

### WR-01: New `_resolve_requisites()` call in `process_pay_later` is unguarded, breaking the file's fail-soft convention

**File:** `handlers/payment.py:189-209`
**Issue:** Every other DB-dependent call in `handlers/payment.py` is deliberately fail-soft —
`start_payment_step` wraps its whole body (including its own new `_resolve_requisites` call
at line 143) in a `try/except` that falls back to `send_completion_and_bonus` on any failure
(see the `CR-01` comment at line 158: "never strand an approved user"), and
`_schedule_deadline_reminders` / `get_main_menu_kb` each swallow their own exceptions
internally. The new `requisites = await _resolve_requisites(callback.from_user.id)` call
added to `process_pay_later` (line 197) has no such guard. `_resolve_requisites` itself calls
`get_setting` and `get_user` with no internal try/except, so any transient DB error (locked
file, I/O error) propagates out of the handler.

Concretely: by the time this call executes, `state.clear()` has already run and
`_schedule_deadline_reminders` has already scheduled T-3/T-1 reminders (line 193-196). If
`_resolve_requisites` then raises, the handler exits without calling `callback.message.answer`
or `callback.answer()` — the user never sees the "Ок! Оплатишь позже." confirmation or the
requisites, the inline button's loading spinner is left to expire on its own, yet the FSM
state was already cleared and reminders were already scheduled. This is exactly the kind of
half-completed, silently-stranding state the rest of the file's `CR-01` comments are written
to prevent.

**Fix:** Wrap the new lookup (and the message it feeds) the same way `start_payment_step`
does, or at minimum guard just the lookup:
```python
try:
    requisites = await _resolve_requisites(callback.from_user.id)
except Exception as e:
    logger.error(f"Failed to resolve requisites for {callback.from_user.id}: {e}")
    requisites = None
```

### WR-02: Requisites-formatting logic duplicated across three call sites, risking future drift

**File:** `handlers/payment.py:143-149, 197-203, 230-233`
**Issue:** The pattern `requisites = await _resolve_requisites(...)` → `if requisites and
requisites.strip():` → `html.escape(requisites)` → format as "📋 Реквизиты:\n..." is now
copy-pasted three times (once pre-existing in `_show_payment_details`, twice newly added in
this diff), each with its own slightly different surrounding formatting/comment block
(including three separately duplicated copies of the identical `CR-01` explanatory comment).
Any future change to the escaping rule, the label text, or the fallback behavior has to be
applied in three places by hand; a partial edit (e.g. fixing escaping in one site but missing
another) would silently reintroduce an injection or formatting bug in the missed call site.
**Fix:** Extract a small helper, e.g.:
```python
def _requisites_block(requisites: str | None) -> str:
    """HTML-safe '📋 Реквизиты:\\n...' block, or '' if nothing to show."""
    if not requisites or not requisites.strip():
        return ""
    return f"📋 Реквизиты:\n{html.escape(requisites)}"
```
and call it from all three sites instead of re-deriving the block inline.

## Info

### IN-01: No regression test covers the two new requisites-rendering call sites

**File:** `handlers/payment.py` (`start_payment_step` multi-option branch, `process_pay_later`)
**Issue:** `tests/test_payment_lc_requisites.py` exercises `_parse_lc_requisites` and
`_resolve_requisites` directly, which this diff correctly leaves untouched (all 4 tests still
pass). But nothing in `tests/` invokes `process_payment_option`/`process_pay_later`/
`start_payment_step` to assert that the rendered message actually contains the escaped
requisites text with `parse_mode="HTML"` — the exact surface this task's own review brief
flags as an HTML-injection risk. Correct today by inspection, but a future edit to either call
site (or to `_requisites_block` if WR-02's suggestion is adopted) has no automated safety net.
**Fix:** Add a lightweight aiogram handler test (mocking `bot.send_message` /
`callback.message.answer`) asserting the sent text contains `html.escape()`'d requisites and
`parse_mode="HTML"` for both `process_pay_later` and the multi-option branch of
`start_payment_step`, using a requisites string containing `&`/`<` to prove the escape path is
actually exercised.

---

_Reviewed: 2026-07-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
