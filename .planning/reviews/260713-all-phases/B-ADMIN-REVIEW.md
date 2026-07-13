---
phase: 260713-all-phases-zone-B-admin
reviewed: 2026-07-13T00:00:00Z
depth: deep
files_reviewed: 1
files_reviewed_list:
  - handlers/admin.py
findings:
  critical: 4
  warning: 5
  info: 3
  total: 12
status: issues_found
---

# Zone B (Admin Panel) Code Review Report

**Reviewed:** 2026-07-13
**Depth:** deep
**Files Reviewed:** 1 (`handlers/admin.py`, 2631 lines)
**Status:** issues_found

## Summary

`handlers/admin.py` is generally well-structured: nearly every callback handler carries an
explicit `config.ADMIN_IDS` gate, DB access is fully delegated to parameterized functions in
`database/db.py` (no f-string SQL in this file), and the developers have clearly been through
a hardening pass before (comments like `# D-06 / T-04-03` and `# CR-01` throughout show prior
security fixes for callback-level auth and HTML escaping in the payment flow).

That said, deep tracing found four real correctness/security bugs that should block ship:

1. The "Заявки"/"Чеки" tinder-style review queues **silently break above ~50 pending items**
   once a manager skips through the fetched batch — the exact scale (1000+ applications) this
   UI was built to handle.
2. `/find`, `/stats`, and the "📈 Источники" panel render **user-submitted free text
   (`full_name`, `username`, `email`, `university`, `source`) unescaped with
   `parse_mode="HTML"`** — any registrant can break these admin views or inject formatting/
   links into the admin's own client. Note the bot's `Bot(default=DefaultBotProperties(parse_mode=ParseMode.HTML))`
   in `main.py` means **every** `.answer()`/`.edit_text()` call in this file is HTML-parsed by
   default, even where `parse_mode="HTML"` is not explicitly repeated — this raises the stakes
   for any unescaped interpolation.

Five further warnings (inconsistent admin-check coverage on 4 callbacks, an unguarded `int()`
parse, a formatting-loss bug in scheduled photo broadcasts, a stuck-FSM edge case, and an
undocumented/unescaped `reject_text` setting) and three minor info items round out the report.

## Critical Issues

### CR-01: Application/receipt review queues report "no items" once skips exceed the 50-item fetch window

**File:** `handlers/admin.py:2204-2223` (`_show_current_card`) and `handlers/admin.py:2438-2452` (`_show_current_receipt_card`)

**Issue:** Both queues fetch a fixed, non-paginated batch:

```python
pending = await get_pending_users(limit=50)
skipped = set((await state.get_data()).get("appr_skipped", []))
visible = [u for u in pending if u["telegram_id"] not in skipped]
total = await get_pending_count()
if not visible:
    await target.answer("✅ Заявок нет.", reply_markup=build_admin_keyboard())
    return
```

`get_pending_users` supports an `offset` parameter (`database/db.py:634`) but it is never
passed — every call re-fetches the **same oldest 50** pending rows. "⏭ Пропустить" only adds
the `telegram_id` to a session-local skip list; it never changes DB status, so skipped rows
keep reappearing in that same 50-row fetch. Once a manager has skipped (not approved/rejected)
all 50 fetched rows, `visible` becomes empty and the handler shows **"✅ Заявок нет."** —
a checkmark implying the queue is genuinely empty — even though `get_pending_count()` may
report hundreds more pending rows that were never fetched (because they sit past the LIMIT 50
cutoff). The stated design goal is "must scale to 1000+ applications"; this bug makes the
queue unusable/misleading past the first 50 items reviewed-by-skip, and the only recovery is
reopening "📋 Заявки" from the admin menu (which silently resets the skip list) — nothing in
the UI hints that this is necessary. Identical bug for receipts via `rcpt_skipped` /
`get_receipt_pending_users(limit=50)`.

**Fix:** Either page forward with `offset` once the current 50-row window is exhausted by
skips, or size the fetch to the true remaining count:

```python
async def _show_current_card(target, state):
    skipped = set((await state.get_data()).get("appr_skipped", []))
    total = await get_pending_count()
    offset = 0
    visible = []
    while not visible and offset < total:
        batch = await get_pending_users(limit=50, offset=offset)
        if not batch:
            break
        visible = [u for u in batch if u["telegram_id"] not in skipped]
        offset += len(batch)
    if not visible:
        await target.answer("✅ Заявок нет.", reply_markup=build_admin_keyboard())
        return
    ...
```

(Same change for `_show_current_receipt_card` / `get_receipt_pending_users`.)

---

### CR-02: `/find` renders unescaped user-submitted data with `parse_mode="HTML"`

**File:** `handlers/admin.py:188-197`

**Issue:**

```python
text = (
    f"👤 <b>Пользователь найден:</b>\n"
    f"ID: <code>{user['telegram_id']}</code>\n"
    f"Имя: {user['full_name']}\n"
    f"Username: {user['username']}\n"
    f"Email: {user['email']}\n"
    f"Регистрация: {user['registration_date']}"
)
await message.answer(text, parse_mode="HTML")
```

`full_name`, `username`, and `email` are stored verbatim from registration free-text input
(`handlers/registration.py:1325` — `full_name = (message.text or "").strip()`, no
sanitization). Any registrant can set e.g. `full_name` to `<a href="http://evil">click</a>`
or an unbalanced tag. With `parse_mode="HTML"`, the former renders as a live clickable link
inside the admin's own client when they look the user up; the latter makes Telegram reject
the whole message ("can't parse entities"), breaking `/find` for that user entirely. Note the
rest of the file is careful about this — `_render_application_card`, `_render_receipt_card`,
and the pending-application admin notification in `registration.py:1842-1856` all
`html.escape()` the same fields — this handler was simply missed.

**Fix:**

```python
text = (
    f"👤 <b>Пользователь найден:</b>\n"
    f"ID: <code>{user['telegram_id']}</code>\n"
    f"Имя: {html_module.escape(str(user['full_name']))}\n"
    f"Username: {html_module.escape(str(user['username']))}\n"
    f"Email: {html_module.escape(str(user['email']))}\n"
    f"Регистрация: {html_module.escape(str(user['registration_date']))}"
)
```

---

### CR-03: `/stats` and "📊 Статистика" render unescaped university names with `parse_mode="HTML"`

**File:** `handlers/admin.py:267-280` (`cmd_stats`), `handlers/admin.py:288-305` (`show_admin_stats`)

**Issue:**

```python
for i, (uni, count) in enumerate(top_unis, 1):
    text += f"{i}. {uni} — {count}\n"
...
await message.answer(text, parse_mode="HTML")   # / same in show_admin_stats via edit_text
```

`uni` comes straight from `get_stats()` (`database/db.py:372-387`), which groups on the raw
`university` column. `university` is a free-text registration field by default
(`reg_university_mode` defaults to `"text"`, see `handlers/admin.py:481`/`registration.py:85`)
— any registrant controls this string. Because the bot's default `parse_mode` is HTML
(`main.py:61`, `DefaultBotProperties(parse_mode=ParseMode.HTML)`), this is parsed as HTML
even where `parse_mode` isn't repeated, and both call sites do repeat it explicitly. A
malicious/careless university name breaks `/stats` (and its callback twin) for every admin
until the offending row is fixed in the DB directly — a persistent, self-inflicted DoS of a
core admin command triggerable by any applicant.

**Fix:**

```python
for i, (uni, count) in enumerate(top_unis, 1):
    text += f"{i}. {html_module.escape(str(uni))} — {count}\n"
```

---

### CR-04: "📈 Источники" renders unescaped user-submitted `source` with `parse_mode="HTML"`

**File:** `handlers/admin.py:318-334` (`show_admin_source_stats`)

**Issue:** Same pattern as CR-03:

```python
for source, count in rows:
    lines.append(f"• {source} — {count}")
text = "\n".join(lines)
...
await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_admin_keyboard())
```

`source` is `REG_FLOW`'s `("source", "reg_q_source", "text")` — a free-text field filled in
directly by the registrant (`registration.py:90`). Same break/inject risk as CR-03.

**Fix:**

```python
for source, count in rows:
    lines.append(f"• {html_module.escape(str(source))} — {count}")
```

## Warnings

### WR-01: Four admin callback handlers are missing the `config.ADMIN_IDS` gate present on every sibling handler

**File:** `handlers/admin.py:1210` (`process_broadcast_all`), `:1224` (`process_broadcast_local_file`), `:756` (`cancel_edit_setting_callback` / `settings_cancel`), `:1330` (`cancel_broadcast_callback` / `broadcast_cancel`)

**Issue:** Every other callback handler in this file explicitly re-checks
`callback.from_user.id not in config.ADMIN_IDS` — the file even has a comment documenting
*why* this is required per-callback (`handlers/admin.py:1269`: "Callbacks are not covered by
the message-level `is_admin` filter — re-check here (D-06 / T-04-03)"). These four callbacks
break that pattern:

```python
@router.callback_query(F.data == "broadcast_all", Broadcast.target_selection)
async def process_broadcast_all(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()   # no ADMIN_IDS check
    ...
```

Under the bot's current setup (default aiogram `FSMContext` key = `(bot_id, chat_id,
user_id)`, `MemoryStorage`, no custom key builder) this is not remotely exploitable today —
only the admin who was already validated at `Command("broadcast")`/`admin_broadcast` can ever
be sitting in `Broadcast.target_selection`/`EditSetting.*` state, and `broadcast_cancel` only
ever appears on a message the admin themselves can see. But it is a real inconsistency
against the codebase's own stated invariant, and it becomes a live privilege-escalation gap
the moment the storage backend or key builder changes (e.g. a future move to
`RedisStorage`/multi-instance deployment with a coarser key), or if this bot is ever run in
a shared/group admin chat with a different key policy.

**Fix:** Add the same guard used everywhere else:

```python
@router.callback_query(F.data == "broadcast_all", Broadcast.target_selection)
async def process_broadcast_all(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    ...
```

---

### WR-02: `sched_cancel` parses callback_data with a bare `int()` — no malformed-input guard

**File:** `handlers/admin.py:1559-1571`

**Issue:**

```python
@router.callback_query(F.data.startswith("sched_cancel_"))
async def sched_cancel(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    bid = int(callback.data.rsplit("_", 1)[1])
    ...
```

Every other callback-data parser in this file (`_parse_appr`, `_parse_rcpt`) wraps `int()` in
`try/except ValueError`. This one does not — a callback_data value like `"sched_cancel_"`
(empty suffix) raises an unhandled `ValueError`, which aiogram logs and swallows, but the tap
silently fails (no `callback.answer()`, so the Telegram client shows an indefinite loading
spinner on the button) rather than degrading gracefully.

**Fix:**

```python
try:
    bid = int(callback.data.rsplit("_", 1)[1])
except ValueError:
    await callback.answer("Некорректные данные.", show_alert=True)
    return
```

---

### WR-03: Scheduled photo broadcasts drop caption formatting (`message.caption` instead of `message.html_text`)

**File:** `handlers/admin.py:1511-1526` (`broadcast_schedule_message`)

**Issue:**

```python
photo = message.photo[-1].file_id if message.photo else None
if message.text:
    text = message.html_text
elif message.caption:
    text = message.caption          # <-- plain text, entities dropped
else:
    text = None
```

`aiogram.types.Message.html_text` (`_unparse_entities`) already falls back to
`self.caption`/`self.caption_entities` when `self.text` is empty — it is the correct
formatting-preserving accessor for *both* text and caption messages. Using raw
`message.caption` here silently strips any bold/italic/link entities the admin applied to a
photo's caption before scheduling it, which is inconsistent with the immediate-send path
(`process_broadcast` uses `message.send_copy`, which preserves entities) and with the
text-only branch two lines above.

**Fix:**

```python
if message.text or message.caption:
    text = message.html_text
else:
    text = None
```

---

### WR-04: `_wait_and_send_album` can leave the admin's FSM state stuck without notice

**File:** `handlers/admin.py:1344-1414`

**Issue:**

```python
if not media:
    return
```

This early return fires when none of the collected album messages matched
photo/video/document/audio (e.g. a media group containing only unsupported content). It skips
both the admin notification and `await state.clear()` at the bottom of the function, leaving
the admin's FSM parked in `Broadcast.message` indefinitely with no feedback that the broadcast
was dropped.

**Fix:** Notify and clear state on the early-exit path too:

```python
if not media:
    try:
        await bot.send_message(admin_id, "⚠️ Рассылка отменена: неподдерживаемый тип вложения.")
    except Exception:
        pass
    await state.clear()
    return
```

---

### WR-05: `reject_text` setting is documented and consumed but has no edit UI, and is unescaped when sent

**File:** `handlers/admin.py:2580-2602` (`APPROVAL_SETTINGS_DOC`) and `handlers/admin.py:2319-2339` (`appr_reject_reason`)

**Issue:** `reject_text` is listed in `APPROVAL_SETTINGS_DOC` (surfaced via `/settings_guide`)
and read in `appr_reject_reason`:

```python
prefix = await get_setting("reject_text") or "К сожалению, твоя заявка отклонена."
await message.bot.send_message(
    tid, f"{prefix}\n\n{html_module.escape(reason)}", parse_mode="HTML"
)
```

but it is **not** present in `SETTINGS_FIELDS` and has no `settings_edit:reject_text` button
anywhere in `build_settings_keyboard()` — an admin who reads `/settings_guide` and wants to
change it has no way to do so through the bot; only direct DB access works. Separately,
`prefix` (once it *is* set, e.g. via a DB script) is interpolated **unescaped** while `reason`
right next to it is correctly `html_module.escape()`d — an inconsistency that will silently
break `/rcpt_reject`-style rejection messages the day this setting becomes editable and an
admin enters `&`/`<` in it.

**Fix:** Add `reject_text` to `SETTINGS_FIELDS` (with a prompt) so it is actually editable, and
escape it symmetrically with `reason`:

```python
await message.bot.send_message(
    tid, f"{html_module.escape(prefix)}\n\n{html_module.escape(reason)}", parse_mode="HTML"
)
```

## Info

### IN-01: Dead variable `status_msg` in `process_broadcast`

**File:** `handlers/admin.py:1443`

**Issue:** `status_msg = await message.answer(f"Начинаю рассылку на {len(users_ids)} пользователей...")` is assigned but never read again — looks like a leftover from a removed "live progress" feature (editing `status_msg` as the loop progresses). As written it's a wasted message and a misleading variable name.

**Fix:** Either drop the assignment (`await message.answer(...)`) or actually use it to show live progress (e.g. `await status_msg.edit_text(...)` every N sends).

### IN-02: `/scheduled` double-escapes already-HTML broadcast text, showing literal tags

**File:** `handlers/admin.py:1542-1556`

**Issue:** `preview = (row.get("text") or "(фото)")[:60]` — `row["text"]` was captured via
`message.html_text` at schedule time (`broadcast_schedule_message`), i.e. it already contains
literal HTML markup like `<b>...</b>` for any formatting the admin applied. `cmd_scheduled`
then does `html_module.escape(preview)`, which is the right defensive move for arbitrary text
but here converts the admin's own intended markup into visible `&lt;b&gt;` noise in the
`/scheduled` list instead of rendering it.

**Fix:** If a rendered preview is desired, strip entities back to plain text for the preview
(e.g. re-derive from stored plain text/caption at schedule time, or store both a raw and a
plain preview column) rather than escaping the HTML-laden string.

### IN-03: `_parse_coins_amount` can `IndexError` on a stripped-to-empty token

**File:** `handlers/admin.py:83-92`

**Issue:**

```python
def _parse_coins_amount(token: str) -> int | None:
    if not token:
        return None
    token = token.strip()
    body = token[1:] if token[0] in "+-" else token
```

The emptiness check happens *before* `.strip()`. A token that is entirely whitespace (e.g.
`" "`, a non-breaking space, which `str.isspace()` treats as whitespace but which
survives `str.split()` differently than ASCII space in some edge cases) would pass the initial
`if not token` check, then become `""` after `.strip()`, and `token[0]` raises `IndexError`.
Not reachable today because the only caller (`cmd_coins`) feeds it tokens produced by
`message.text.split(maxsplit=3)`, which never yields whitespace-only entries — but the
function is not safe to reuse elsewhere without this caller-side guarantee in mind.

**Fix:**

```python
token = token.strip()
if not token:
    return None
body = token[1:] if token[0] in "+-" else token
```

---

_Reviewed: 2026-07-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
