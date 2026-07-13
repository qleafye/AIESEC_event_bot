---
phase: all-phases-260713-zone-A-registration
reviewed: 2026-07-13T12:00:00Z
depth: deep
files_reviewed: 1
files_reviewed_list:
  - handlers/registration.py
findings:
  critical: 3
  warning: 5
  info: 2
  total: 10
status: issues_found
---

# Zone A: Registration Flow — Code Review Report

**Reviewed:** 2026-07-13T12:00:00Z
**Depth:** deep
**Files Reviewed:** 1 (`handlers/registration.py`, 1876 lines)
**Status:** issues_found

## Summary

`handlers/registration.py` is the largest and most central handler in the bot — it owns
the entire FSM question engine (REG_FLOW), consent collection, resume upload hand-off to
Nextcloud, and finalize→Sheets/DB/admin-notify orchestration. Overall the file is
disciplined about fail-soft error handling around external I/O (Sheets append, Nextcloud
upload, admin notify all wrapped in try/except with logging), and free-text is
consistently HTML-escaped before being sent back to Telegram with `parse_mode="HTML"` in
`_build_summary`.

Three real defects were found that go beyond style: a consent-step that can be silently
bypassed by tapping a stale inline button (a compliance-relevant bug given consent is
explicitly gated on `consent_enabled` for personal-data processing), an unhandled crash
class from Unicode digit characters that pass `str.isdigit()` but fail `int()` — reachable
both from the bot's `/start` entry point (referrer-id parsing) and from the age step — and
a Google Sheet column-alignment bug where the row written for each registration is
projected against the *live* `reg_q_*` toggle state rather than a fixed snapshot, so
toggling a question mid-event silently shifts data into the wrong columns for later
registrants without any error surfaced to the admin.

Cross-checked against `services/nextcloud.py` (fully fail-soft, sanitizes filenames
correctly — no path traversal) and `services/sheets.py` (all write paths use gspread's
`RAW` value-input-option by default in the installed `gspread==6.2.1`, so a formula-like
answer such as `=1+1` is stored as literal text, not evaluated — verified directly against
the installed library rather than assumed).

## Critical Issues

### CR-01: Consent step can be silently skipped via a stale inline button

**File:** `handlers/registration.py:633-651` (question rendered), `handlers/registration.py:1283-1296` (accept handler)

**Issue:** Each consent card is sent as its own message with its own inline
"Принимаю"/`consent_accept:<key>` button (line 643-649). When the user accepts a consent,
`process_consent_accept` never disables/removes the button on that message
(`callback.message.edit_reply_markup` is never called here, unlike the equivalent
multi-select "Готово" handler at lines 1256-1259 which *does* clear its markup). The
handler also does not verify that the `consent_key` extracted from `callback.data`
matches the consent the FSM is actually currently waiting on (stored at line 650 as
`_consent_key`, but never read back):

```python
async def process_consent_accept(callback, state, bot):
    consent_key = callback.data.split(":", 1)[1]      # trusted blindly
    await record_user_consent(callback.from_user.id, consent_key)
    ...
    i = data.get("_consent_i", 0) + 1                  # advances regardless of which key fired
    if i < len(queue):
        await state.update_data(_consent_i=i)
        await _ask_step(queue[i], callback.message, state, i + 1, len(queue))
    else:
        await _ask_full_name(callback.message, state)
```

Because old consent messages stay live and clickable, and `_consent_i` is a blind
counter, a user with 2+ consents queued can: tap consent #1 → advance to consent #2 →
scroll up and re-tap the still-active consent #1 button → the handler increments
`_consent_i` again and jumps straight to consent #3 (or to ФИО if #2 was last),
**never having accepted consent #2 at all**. Since `consent_enabled` exists specifically
to gate personal-data-processing consent, this is a real compliance-relevant bypass, not
just a UX glitch.

**Fix:** Validate the callback against the step actually being waited on, and disable the
button once used:
```python
async def process_consent_accept(callback, state, bot):
    consent_key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if consent_key != data.get("_consent_key"):
        await callback.answer()  # stale/foreign tap — ignore silently
        return
    await record_user_consent(callback.from_user.id, consent_key)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("✅ Принято")
    ...
```

---

### CR-02: `str.isdigit()` accepts Unicode digits that crash `int()` — breaks `/start` and the age step

**File:** `handlers/registration.py:680-689` (`_extract_referrer_id`, called unguarded from `cmd_start` at line 1065), `handlers/registration.py:1353-1364` (`process_age`)

**Issue:** Both call sites validate numeric input with `str.isdigit()` and then convert
with `int()`. These are *not* equivalent: `str.isdigit()` returns `True` for Unicode
"digit" characters (superscripts, circled digits, etc.) that `int()` cannot parse.
Verified directly against the installed interpreter:

```python
'²'.isdigit() -> True   int('²') -> ValueError: invalid literal for int() with base 10: '²'
'①'.isdigit() -> True   int('①') -> ValueError: invalid literal for int() with base 10: '①'
```

`_extract_referrer_id` is called directly (no try/except) from `cmd_start` — the bot's
`/start` handler:
```python
arg = command_args.strip()
if not arg.isdigit():
    return None
referrer_id = int(arg)          # line 686 — unguarded, raises ValueError
```
A user can trigger this simply by typing `/start ²` (bypassing Telegram's stricter
deep-link URL charset, since this is a literal command they type, not a t.me URL) —
this raises an uncaught `ValueError` inside `cmd_start`, and there is no global error
handler registered anywhere in the codebase (`main.py`/dispatcher), so the update is
dropped with no response to the user. This is the bot's primary entry point.

The same pattern in `process_age` (line 1356 `raw_age.isdigit()` → line 1359
`int(raw_age)`) crashes the age step identically if the user pastes/types such a
character, leaving them stuck mid-registration with no error message.

**Fix:** Use `try/except ValueError` around the conversion instead of trusting
`isdigit()`, in both places:
```python
def _extract_referrer_id(command_args, current_user_id):
    if not command_args:
        return None
    arg = command_args.strip()
    try:
        referrer_id = int(arg)
    except ValueError:
        return None
    if referrer_id == current_user_id:
        return None
    return referrer_id
```
```python
raw_age = (message.text or "").strip()
try:
    age = int(raw_age)
except ValueError:
    await message.answer("Возраст должен быть числом. Попробуй еще раз.")
    return
```

---

### CR-03: Google Sheet row projection is recomputed live per-registration — mid-event toggle changes silently misalign columns

**File:** `handlers/registration.py:826-843` (`active_sheet_headers`, `active_sheet_row`), used at `handlers/registration.py:1832` (`finalize_registration`)

**Issue:** `active_sheet_row(data)` filters `SHEET_COLUMNS` down to only the columns whose
gating `reg_q_*` setting is enabled **at the moment that specific registration
finalizes** (line 833: `if gate is None or await _is_step_enabled(gate)`). The physical
header row, however, is written once by `ensure_sheet_header` at bot startup (or by an
explicit admin rebuild) and is not kept in sync automatically — the code comment at
lines 828-830 acknowledges the header can go stale, but the more serious consequence is
undocumented: because `active_sheet_row` is recomputed **per call**, if an admin flips
any `reg_q_*` toggle mid-event (a normal admin action — see `REG_CATEGORIES`/per-question
toggles in `/admin`), every row appended *after* the toggle change has a different
column count/order than rows appended *before* it, and both sets can silently mismatch
the header. `sheet.append_row()` writes positionally with no name-based alignment, so
delegate data (phone numbers, cities, expectations, …) ends up under the wrong header for
an unknown number of subsequent rows, with no error surfaced anywhere. This directly
undermines the project's core value ("менеджер... без ручного учёта в таблицах и без
[ошибок]") — the exact failure mode the sheet export exists to prevent.

**Fix:** Freeze the enabled-column set once (e.g. cache it at the point
`ensure_sheet_header` runs, or store the active header list itself in `bot_settings` and
reuse it for every `active_sheet_row` call until the next explicit "♻️ Пересобрать
таблицу"), rather than recomputing gate state independently for every registration. At
minimum, log a loud warning (not just a docstring note) whenever `active_sheet_row`'s
column set differs from the last-known header, so drift is visible instead of silent.

## Warnings

### WR-01: `_reg_total` goes stale when conditional steps are added later in the flow

**File:** `handlers/registration.py:668-670` (`_advance`), `handlers/registration.py:1339-1348` (`process_full_name`)

**Issue:** `_reg_total` is computed once in `process_full_name` from `_get_enabled_steps(data)`
*before* the user has answered `education_status` — at that point `studying` is `False`
by default, so the `edu_conditional` gate (line 369-376) excludes `university`/`course`/
`specialty`/`study_field` from the count. `_advance` recomputes `enabled` fresh on every
call (correctly, so the flow itself doesn't break), but reuses the stale
`total = data.get("_reg_total", len(enabled))` (line 669) instead of the freshly computed
`len(enabled)`. Once the user answers "Да, учусь", the *actual* enabled-step count grows,
but the displayed `(step/total)` progress indicator (only shown when `reg_show_progress`
is turned on) keeps using the original, too-small total, so progress numbers become
inconsistent (e.g. "12/9").

**Fix:** In `_advance`, recompute total fresh each time instead of trusting the cached
value: `total = len(enabled)` and re-persist it, e.g. `await state.update_data(_reg_step=step, _reg_total=len(enabled))`.

### WR-02: Reserved control words collide with admin-configurable option text

**File:** `handlers/registration.py:1100` (global cancel), `handlers/registration.py:710-720` (`_reply_kb` "Другое"/"Пропустить" sentinels), and every "Другое"/"Пропустить" text-equality check (e.g. `handlers/registration.py:1206-1208`, `1421-1424`, `1435-1437`, `1448-1450`, `1461-1463`, `1486-1488`, `1593-1597`, `_store_text` at `1602-1608`)

**Issue:** `"Отмена"`/`"/cancel"` is matched globally across the *entire* `Registration`
state group (`StateFilter(Registration)`, line 1100), and `"Другое"` / `"Пропустить"` are
treated as reserved control tokens inside almost every free-text/choice step. None of
these are validated against the admin-editable option lists (`city_options`,
`university_options`, `source_options`, `study_field_options`, `goal_options`,
`formats_options`, local-committee/position lists, etc. — all raw newline text via
`get_setting`/`_get_options`). If an organizer ever adds a real option whose label is
exactly `"Отмена"`, `"Другое"`, or `"Пропустить"` (plausible — e.g. a city literally named
similarly, or reusing "Другое" as a category on a select step), that option becomes
unreachable: selecting it either triggers the cancel-confirmation dialog or the "type your
own" free-text prompt instead of being recorded as the chosen answer.

**Fix:** Either reserve these tokens explicitly in the admin option-editing UI (reject/warn
on save if an option list contains one of them), or use an unambiguous sentinel that can't
collide with real answers (e.g. a fixed callback-data-driven "Другое" button rather than
text-equality matching, for the select/reply-keyboard steps that currently compare raw
text).

### WR-03: Consent card sent with `parse_mode="HTML"` and no exception handling around a user/admin-controlled string

**File:** `handlers/registration.py:641-649`

**Issue:** `caption` embeds `await _prompt(f'consent_{consent_key}', html.escape(label))` —
the *default* is properly escaped, but if an admin sets a per-consent override via
`reg_prompt_consent_<key>` (the `_prompt` admin-override mechanism used throughout the
file), that raw override text is **not** escaped and is sent with `parse_mode="HTML"`
(lines 647/649) with no surrounding `try/except`, unlike essentially every other
user-facing send in this file (`_send_welcome`, the summary message at line 1871-1873,
etc., all guard against `parse_mode="HTML"` failures). An admin typo like an unescaped
`<` in a custom consent prompt will raise `TelegramBadRequest` on every single user who
reaches that consent step, blocking registration entirely for the whole event until the
admin notices and fixes the setting.

**Fix:** Escape the resolved prompt (or wrap the send in try/except with a plain-text
fallback, matching the pattern already used elsewhere in this file):
```python
caption = await _prompt(f'consent_{consent_key}', html.escape(label))
try:
    ... send with parse_mode="HTML" ...
except Exception:
    ... resend with parse_mode=None ...
```

### WR-04: `_ask_step` is a single ~215-line if/elif dispatcher (high cyclomatic complexity)

**File:** `handlers/registration.py:436-651`

**Issue:** `_ask_step` handles every one of ~45 step keys via sequential `if/elif
step_key == "..."` branches, mixing prompt text, keyboard construction, and state
transitions inline. This is the largest single function in the largest handler file in
the codebase, and any change to one step's behavior requires scanning the whole function
to confirm no other branch shares logic. The `date`/`select`/`multi`/`consent` branches
already show the pattern this could generalize to (a small per-type dispatch table).

**Fix:** No functional change required, but consider extracting a `step_key ->
async callable` registry (already partially done via `REG_STEP_TYPES` for the
generic types) to keep future additions bounded and reviewable.

### WR-05: `reg_cancel_yes`/`reg_cancel_no` callbacks are not state-scoped

**File:** `handlers/registration.py:1111`, `handlers/registration.py:1125`

**Issue:** Unlike every other registration callback handler in this file, these two are
registered with only `F.data == "reg_cancel_yes"` / `"reg_cancel_no"` — no `StateFilter`.
A stale "Точно отменить регистрацию?" message (e.g. left over from an earlier aborted
attempt, or from the admin's re-registration test flow) still has live buttons; tapping
"Да, отменить" later, from an unrelated context, calls `state.clear()` unconditionally —
silently wiping whatever unrelated FSM state the user (including an admin mid-broadcast
or mid-settings-edit) happens to be in at that moment, with no confirmation that anything
was actually cleared.

**Fix:** Scope both handlers with `StateFilter(Registration)` (or check `await
state.get_state()` before clearing) so a stale button can only affect an active
registration.

## Info

### IN-01: `expectations_ar` is a vestigial field, always `"-"`, never populated by any step

**File:** `handlers/registration.py:1770`, `handlers/registration.py:777`

**Issue:** `finalize_registration` sets `data.setdefault("expectations_ar", "-")`
unconditionally, and no `REG_FLOW`/`_ask_step` branch ever asks a question that populates
`expectations_ar`. It nonetheless has a dedicated, always-visible ("Ожидания (AR)")
Sheets column gated on `reg_q_expectations` (line 777). This looks like leftover
scaffolding from a removed feature (an AR/Arabic or "ambassador request" expectations
variant) and adds a permanently-empty column to every export.

**Fix:** Either wire up the intended second question, or remove the field/column if it's
confirmed dead.

### IN-02: Redundant exception tuple

**File:** `handlers/registration.py:1799`

**Issue:** `except (asyncio.TimeoutError, Exception) as e:` — `asyncio.TimeoutError` is an
alias for the built-in `TimeoutError`, itself a subclass of `OSError`/`Exception`, so it's
already covered by `Exception`. Harmless but misleading (reads as if `TimeoutError` needed
special-casing here).

**Fix:** `except Exception as e:` is equivalent and clearer.

---

_Reviewed: 2026-07-13T12:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
