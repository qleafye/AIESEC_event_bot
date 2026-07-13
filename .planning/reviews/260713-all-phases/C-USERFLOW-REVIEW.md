---
phase: C-USERFLOW
reviewed: 2026-07-13T00:00:00Z
depth: deep
files_reviewed: 4
files_reviewed_list:
  - handlers/payment.py
  - handlers/user_actions.py
  - keyboards/builders.py
  - handlers/states.py
findings:
  critical: 1
  warning: 5
  info: 4
  total: 10
status: issues_found
---

# Zone C (user-facing flow): Code Review Report

**Reviewed:** 2026-07-13
**Depth:** deep
**Files Reviewed:** 4
**Status:** issues_found

## Summary

Reviewed the payment flow (`handlers/payment.py`), the persistent-menu handlers
(`handlers/user_actions.py`), keyboard builders (`keyboards/builders.py`), and FSM state
declarations (`handlers/states.py`), plus cross-file call chains into
`handlers/registration.py`, `services/scheduler.py`, and `database/db.py`.

Every reply-keyboard button text was checked byte-for-byte against its
`F.text ==`/`F.text.in_()` handler filter (including the newly-renamed «💳 Оплата» button) —
all pairs match; no silently-dead buttons found. The FSM catch-all for `Registration.receipt_upload`
correctly excludes slash-commands (verified against aiogram's `magic_filter` semantics for
`None`-valued `message.text`, which resolves to `False`/`True` rather than raising). Router
registration order (`admin.router` → `payment.router` → `registration.router` → `user_actions.router`
in `main.py`) was traced and does not create cross-router interception bugs.

The prior targeted review's two flags (WR-01: unguarded `_resolve_requisites` in
`process_pay_later`; WR-02: requisites resolve/escape/format duplicated 3×) both still hold and
are re-surfaced below (widened for WR-01, since the whole handler — not just the requisites
call — lacks fail-soft handling).

New findings from the full-zone pass: a genuine HTML-injection bug in `my_referrals` where
user-controlled `full_name` is interpolated into a `parse_mode="HTML"` message without
`html.escape` (unlike the sibling `render_leaderboard`, which does escape) — this can break
message delivery entirely for the referrer. Several admin-controlled settings strings
(`event_date`, `place_name`, `place_address`, captions) are interpolated into HTML messages
the same unescaped way, echoing the exact class of bug `payment.py` already patched (CR-01)
elsewhere in the same milestone. A cross-file gap was found between the reminder-scheduling
path and the overdue-sweep gating in `services/scheduler.py`: a user who taps "Оплачу позже"
directly from the multi-option picker (without ever picking an option) gets deadline reminders
scheduled but can never be flipped to `overdue` by the sweep, since `payment_option`/`payment_due`
are never populated for that path.

## Critical Issues

### CR-01: Unescaped user-controlled `full_name` breaks HTML-mode referral list

**File:** `handlers/user_actions.py:281-285`
**Issue:** `my_referrals` builds the referred-users list from `get_referrals()`, which returns
raw `full_name` values collected during registration (attacker/user-controlled free text), and
interpolates them directly into a `parse_mode="HTML"` message with no escaping:

```python
names = "\n".join(f"• {name}" for name in referrals)
await message.answer(
    f"👥 <b>Твои приглашённые ({len(referrals)}):</b>\n\n{names}",
    parse_mode="HTML",
)
```

Any registrant whose `full_name` contains `<`, `&`, or an unbalanced tag (e.g. `"<b>Vasya"`,
`"Tom & Jerry"`) will cause Telegram's `sendMessage` to reject the whole message with a 400
"can't parse entities" error. Since this call is not wrapped in `try/except`, the exception
propagates unhandled and the referrer gets **no response at all** — a single malicious/careless
registration silently breaks this feature for every user who referred them, indefinitely (this
is stored/reflected, not one-off). This is the exact class of bug `payment.py` explicitly
guards against elsewhere in this milestone (see the repeated `CR-01` comments there for
requisites/options/penalties) — it was simply missed here. Compare to `render_leaderboard`
(`user_actions.py:67`) in the very same file, which correctly does
`html.escape(str(name))`.

**Fix:**
```python
names = "\n".join(f"• {html.escape(str(name))}" for name in referrals)
```

## Warnings

### WR-01: `process_pay_later` / `process_payment_option` are fully unguarded, breaking the file's fail-soft convention

**File:** `handlers/payment.py:170-209`
**Issue:** Every other entry point in this file (`start_payment_step`, and implicitly
`_show_payment_details` when called from the wrapped `start_payment_step` path) is wrapped in
`try/except` with a documented fallback so an approved user is never stranded. `process_payment_option`
(170-186) and `process_pay_later` (189-209) have **no** exception handling at all:

```python
@router.callback_query(F.data.startswith("pay_option:"))
async def process_payment_option(callback: types.CallbackQuery, state: FSMContext):
    ...
    await update_payment_status(callback.from_user.id, "not_paid", payment_option=label)
    await _show_payment_details(callback.bot, callback.from_user.id, state, label, price)
    await callback.answer()
```

```python
@router.callback_query(F.data == "pay_later")
async def process_pay_later(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _schedule_deadline_reminders(callback.from_user.id)
    requisites = await _resolve_requisites(callback.from_user.id)
    ...
    await callback.message.answer(..., reply_markup=await get_main_menu_kb(callback.from_user.id))
    await callback.answer()
```

If any awaited call raises (DB hiccup, `callback.message` being an `InaccessibleMessage`/`None`
for a deleted-message callback, a transient Telegram API error from `get_main_menu_kb`'s photo/
setting lookups, etc.), `callback.answer()` is never reached — the tapped button spins forever
client-side and the user gets no confirmation text. In `process_payment_option` this also means
`_show_payment_details`'s own `state.set_state(...)` may or may not have run depending on where
the exception occurred, with nothing downstream to recover the user into a known state.

**Fix:** Wrap both handlers with the same fail-soft pattern used in `start_payment_step`, and
guarantee `callback.answer()` via `try/finally`:
```python
@router.callback_query(F.data == "pay_later")
async def process_pay_later(callback: types.CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await _schedule_deadline_reminders(callback.from_user.id)
        requisites = await _resolve_requisites(callback.from_user.id)
        ...
        await callback.message.answer(..., reply_markup=await get_main_menu_kb(callback.from_user.id))
    except Exception as e:
        logger.error(f"process_pay_later failed for {callback.from_user.id}: {e}")
    finally:
        await callback.answer()
```

### WR-02: Admin-controlled settings interpolated unescaped into `parse_mode="HTML"` messages

**File:** `handlers/user_actions.py:121-126, 139-141, 152-154, 191, 198, 215`
**Issue:** `payment.py` explicitly escapes every admin-entered free-text field it renders in HTML
mode (requisites, deadline, penalties — see the repeated `CR-01` comments). The same discipline
is missing in `user_actions.py` for `event_date`, `event_time`, `event_place_name`,
`event_place_address`, `program_caption`, `speakers_caption` — all admin-settable via
`/admin` free-text entry, all interpolated unescaped into `parse_mode="HTML"` text/captions:

```python
text += f"📍 <b>Место:</b> {place_name}"                       # show_info_menu, line 126
text = f"🗓 Форум пройдет <b>{event_date}</b>!"                  # info_date, line 139
text = f"<b>Наша площадка — {place_name}!</b> 🚀"                # info_place, line 152
await message.answer_photo(program_file_id, caption=program_caption, parse_mode="HTML")   # line 191/198
await message.answer_photo(speakers_file_id, caption=speakers_caption, parse_mode="HTML")  # line 215
```

An address or caption containing a stray `&` or `<` (very plausible free text — e.g.
`"ТЦ Плаза & Ко"`) will make Telegram reject the whole message. `show_info_menu`/`info_date`/
`info_place` have no `try/except` at all around the `message.answer(...)` call, so the user gets
no response whatsoever. `show_program`/`show_speakers` do catch the exception, but the fallback
branch reuses the *same* unescaped caption (so it fails identically) or falls through to a
misleading "не загружена/формируется" message even though content does exist — just malformed.

**Fix:** Escape all admin-free-text fields before interpolating into HTML-mode messages, same as
`payment.py`:
```python
text += f"📍 <b>Место:</b> {html.escape(place_name)}"
```

### WR-03: Overdue-sweep eligibility gap for users who defer straight from the option picker

**File:** `handlers/payment.py:137-150, 189-209` (cross-referenced against `services/scheduler.py:242-269`)
**Issue:** When `payment_options` has multiple paid options, `start_payment_step` shows a picker
with inline `pay_option:i` buttons plus the `_PAY_LATER_BTN` — before any option is chosen,
`payment_option`/`payment_due` are still `NULL` on the user's row (only
`process_payment_option` writes `payment_option`; nothing in this flow ever writes
`payment_due`). If the user taps "⏭ Оплачу позже" directly from that picker (never picking an
option), `process_pay_later` still calls `_schedule_deadline_reminders`, which schedules T-3/T-1
reminder jobs unconditionally. But `services/scheduler.py`'s `sweep_payment_overdue` only
flips a user to `overdue` when:
```python
select_where = (
    "payment_status='not_paid' "
    "AND (payment_option IS NOT NULL OR payment_due IS NOT NULL)"
)
```
Since this user's `payment_option` and `payment_due` are both `NULL`, they are permanently
excluded from the overdue sweep — they will keep getting reminder pings on schedule, but their
`payment_status` never transitions to `overdue`, so they never appear in `overdue`-based admin
segments/broadcasts even after the deadline passes. This is a real (if narrow) inconsistency
between "who gets reminded" and "who counts as overdue."

**Fix:** Either (a) have `process_pay_later` also persist a `payment_option` fallback (e.g. the
first/default option's label) when none was picked, or (b) relax `sweep_payment_overdue`'s
`WHERE` clause to also catch `not_paid` rows with a scheduled reminder / no payment_option at
all but a set `payment_deadline`, matching the actual reminder-eligibility rule.

### WR-04: Unvalidated admin URLs passed to `InlineKeyboardButton(url=...)` with no error handling

**File:** `keyboards/builders.py:176-182`, `handlers/user_actions.py:223-250`
**Issue:** `get_socials_kb(tg_url, vk_url)` passes `contact_tg`/`contact_vk` — free-text admin
settings — straight into `InlineKeyboardButton(text=..., url=tg_url)` with no scheme/format
validation:
```python
def get_socials_kb(tg_url: str, vk_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if tg_url:
        builder.button(text="Группа в Telegram", url=tg_url)
    ...
```
If an admin enters e.g. `"@aiesec_channel"` or `"vk.com/aiesec"` instead of a full
`https://…` URL, Telegram's `BUTTON_URL_INVALID` error will raise when `show_contacts` sends the
message — and `show_contacts` (`user_actions.py:223-250`) has no `try/except` around
`message.answer(text, reply_markup=get_socials_kb(...))`, so the whole "📞 Контакты" response
fails silently for the user.

**Fix:** Validate/normalize the URL (e.g. require `http(s)://` prefix, or auto-prepend it) when
the setting is saved in the admin UI, and/or wrap the send in `try/except` with a text-only
fallback (mirroring the pattern already used for `venue_photo`/`program_photo`).

### WR-05: Per-LC requisites resolve/escape/format logic duplicated across 3 call sites (re-surfaced)

**File:** `handlers/payment.py:143-149, 197-203, 213-233`
**Issue:** `start_payment_step`, `process_pay_later`, and `_show_payment_details` each
independently call `_resolve_requisites`, check `requisites and requisites.strip()`, and format
the same `"📋 Реквизиты:\n" + html.escape(requisites)` block (each carrying an identical `CR-01`
comment). This is functionally correct today but is a maintenance hazard — a future edit to the
formatting/escaping rule (e.g. adding a `payment_requisites_note`) is likely to be applied to
only 1-2 of the 3 sites, silently reintroducing the exact CR-01-class bug this pattern was meant
to prevent.

**Fix:** Extract a single helper, e.g.:
```python
def _format_requisites_block(requisites: str | None) -> str:
    if not requisites or not requisites.strip():
        return ""
    return f"📋 Реквизиты:\n{html.escape(requisites)}"
```
and use it at all three call sites.

## Info

### IN-01: `Registration.payment_option` is a dead FSM state with no explanatory note in `states.py`

**File:** `handlers/states.py:46`
**Issue:** `payment_option = State()` is declared but, per `handlers/payment.py:172-173`'s own
comment, deliberately never set anywhere (`process_payment_option` intentionally omits it as a
state filter). This is correct by design, but `states.py` itself gives no hint — a future
reader of `states.py` in isolation (e.g. while wiring a new handler) has no way to know this
state is intentionally orphaned rather than a leftover/bug.

**Fix:** Add a one-line comment next to the declaration, e.g.
`payment_option = State()  # intentionally never set — see handlers/payment.py:172`.

### IN-02: Convoluted conditional expression in `get_main_menu_kb`

**File:** `keyboards/builders.py:23`
**Issue:**
```python
if (val == "on") if val is not None else True:
```
Functionally correct (button shown when setting is unset, or explicitly `"on"`) but the nested
ternary is harder to read than necessary.

**Fix:**
```python
if val is None or val == "on":
    kb.button(text=text)
```

### IN-03: Unused `bot: Bot` parameter in receipt handlers

**File:** `handlers/payment.py:259-266, 269-271`
**Issue:** `process_receipt_document(message, state, bot)` and
`process_receipt_photo(message, state, bot)` both declare a `bot: Bot` parameter that is never
referenced in either function body.

**Fix:** Drop the unused parameter from both signatures (aiogram will simply not inject it).

### IN-04: Referral list renders literal `"• None"` for rows with a NULL `full_name`

**File:** `database/db.py:352-357`, `handlers/user_actions.py:281`
**Issue:** `get_referrals` returns raw `full_name` values via `SELECT full_name FROM users WHERE
referrer_id = ?` with no `COALESCE`/filtering; if a referred user's `full_name` is `NULL`
(e.g. a row inserted through a path that skips this field), `my_referrals` will render a
`"• None"` line in the list — cosmetic but confusing to the end user.

**Fix:** `COALESCE(full_name, 'Без имени')` in the query, or filter/replace `None` in the
Python-side join.

---

_Reviewed: 2026-07-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
