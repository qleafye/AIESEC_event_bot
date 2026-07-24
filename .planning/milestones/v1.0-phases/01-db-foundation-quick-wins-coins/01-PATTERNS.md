# Phase 1: db-foundation-quick-wins-coins - Pattern Map

**Mapped:** 2026-06-25
**Files analyzed:** 7 (5 modified, 2 new)
**Analogs found:** 7 / 7 (all in-repo; brownfield, every new subsystem has a sibling)

> Codebase is aiogram 3 + aiosqlite + Google Sheets, raw-SQL (no ORM), single-file-per-concern.
> Every new feature in Phase 1 has a concrete same-repo analog. Prefer copying these exact
> patterns over RESEARCH/STACK abstractions. No new files strictly required except an optional
> `handlers/coins.py` (or fold coins handlers into existing routers).

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `database/db.py` (modify) | model / data-access | CRUD + append-only ledger | itself (`_ensure_column`, `add_user`, `get_user_by_username`) | exact (self) |
| `handlers/registration.py` (modify) — confirm step (QW-01) | handler (FSM) | request-response | `_ask_step` + `process_*` handlers (same file) | exact |
| `handlers/registration.py` (modify) — resume step (QW-03) | handler (FSM) | file-I/O (file_id) | `settings_receive_file_doc` (`admin.py:593`) + REG_FLOW toggle | exact |
| `handlers/registration.py` (modify) — `reg_started` write/delete (SCHED-02) | handler + data-access | event-driven (lifecycle hook) | `cmd_start` (343) / `finalize_registration` (650) | role-match |
| `handlers/registration.py` (modify) — subscription check (QW-02) | handler (guard, non-blocking) | request-response (Bot API) | `get_user_by_username` lookup + settings read; `cmd_start` hook | role-match (no getChatMember precedent) |
| `keyboards/builders.py` (modify) — `🪙 Мои монеты` button | config / keyboard builder | n/a | `MENU_BUTTONS` + `get_main_menu_kb` (8-25) | exact |
| `handlers/admin.py` (modify) — `/coins`, broadcast segments | handler (admin command + FSM) | CRUD + pub-sub broadcast | `cmd_find_user` (96), `cmd_create_link` (120), broadcast flow (657-871) | exact |
| `handlers/coins.py` OR fold into user_actions/admin (new, optional) | handler | CRUD read | `my_referrals` (`user_actions.py:186`) | role-match |

---

## Pattern Assignments

### `database/db.py` — schema migrations + ledger (model, CRUD + append-only)

**Analog:** itself (source of truth per CLAUDE.md).

**Additive column migration** — copy the existing `_ensure_column` calls inside `init_db()` (db.py:48-56). Add new columns the same way (note `status` MUST default to `'approved'` per D-18 so the ~590 live users keep access):
```python
await _ensure_column(db, "users", "status", "TEXT DEFAULT 'approved'")
await _ensure_column(db, "users", "resume_file_id", "TEXT")
```
The primitive already exists (db.py:10-18) and is idempotent via `PRAGMA table_info` — do not add a migration library.

**New tables** — copy the `CREATE TABLE IF NOT EXISTS` block style from `bot_settings` (db.py:58-63). Coins ledger is **append-only** (D-12 — never UPDATE a balance):
```python
await db.execute('''
    CREATE TABLE IF NOT EXISTS coins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        reason TEXT,
        changed_by INTEGER,
        timestamp TEXT NOT NULL
    )
''')
await db.execute('''
    CREATE TABLE IF NOT EXISTS reg_started (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        started_at TEXT NOT NULL
    )
''')
```
Add all `CREATE TABLE` / `_ensure_column` statements **before** the single `await db.commit()` at db.py:65 (one commit per `init_db`, matching current style).

**`add_user` — switch INSERT OR REPLACE → ON CONFLICT (D-17).** Current code (db.py:91-134) uses `INSERT OR REPLACE INTO users (...)` which is DELETE+INSERT and wipes `status`/`resume_file_id` on re-registration. Change the statement head only; keep the exact column list and the `data.get(...)` value tuple (104-133):
```python
INSERT INTO users (telegram_id, username, ...) VALUES (?, ?, ...)
ON CONFLICT(telegram_id) DO UPDATE SET
    username=excluded.username,
    full_name=excluded.full_name,
    ...
```
Do not touch the value tuple — `telegram_id` is already PRIMARY KEY (db.py:24), so it is a valid conflict target.

**Ledger/balance read functions** — model on the existing read helpers (`get_referrals` db.py:159, `get_stats` db.py:179). Balance = `SELECT COALESCE(SUM(delta),0) FROM coins WHERE user_id=?`. Leaderboard top-10 = `GROUP BY user_id ORDER BY SUM(delta) DESC LIMIT 10` joined to `users` for names. Each function opens its own `async with aiosqlite.connect(config.DB_PATH) as db:` — match this connection-per-call convention (every function in db.py does this; there is no shared connection).

**Username lookup for `/coins @username`** — reuse `get_user_by_username` (db.py:147) verbatim; it already normalizes leading `@` and uses `COLLATE NOCASE`.

---

### `handlers/registration.py` — confirm step (handler/FSM, request-response) [QW-01]

**Analog:** the `_ask_step` engine (registration.py:137-204) + `_advance` (206-222) + any `process_*` handler.

**Keyboard already exists** — `get_confirm_kb()` (builders.py:136) returns `Всё верно ✓` / `Изменить`. Import it alongside the other keyboard imports (registration.py:16-30).

**Where confirm plugs in:** `_advance` (registration.py:206-222) calls `finalize_registration` when `next_idx >= len(enabled)`. Insert the confirm step **between** the last enabled step and `finalize_registration` (CONTEXT D-01, code_context line 93). Pattern: add a new FSM state (see states.py block below), render a summary, show `get_confirm_kb()`, set the confirm state instead of finalizing:
```python
# at the end of _advance, instead of finalize_registration(...):
data = await state.get_data()
summary = _build_summary(data)   # new helper, mirror _build_sheet_row (registration.py:252)
await message.answer(summary, reply_markup=get_confirm_kb(), parse_mode="HTML")
await state.set_state(Registration.confirm)
```

**Summary builder** — model on `_build_sheet_row` (registration.py:252-282): same `data.get(...) or "-"` null-coalescing, same field set. Build an HTML string (use `html.escape`, already imported registration.py:2; see `finalize_registration` admin_text 685-699 for the escape pattern).

**Confirm handler (`Всё верно ✓` / `Изменить`)** — mirror a two-branch text handler like `process_work_status` (registration.py:565-575):
```python
@router.message(Registration.confirm, F.text == "Всё верно ✓")
async def confirm_yes(message, state, bot):
    await finalize_registration(message, state, bot)

@router.message(Registration.confirm, F.text == "Изменить")
async def confirm_edit(message, state):
    await _start_registration_flow(message, state)   # full restart, D-02
```
`_start_registration_flow` (registration.py:287) already preserves `referrer_id`/`source` across `state.clear()` — reuse it for the "Изменить" restart, no per-field editing (D-02).

**Short form skip (D-03):** `process_full_name` (registration.py:413-415) finalizes immediately when `registration_mode != "full"`. Keep that path confirm-free.

---

### `handlers/registration.py` — resume step (handler/FSM, file-I/O) [QW-03]

**Analog:** `settings_receive_file_doc` (admin.py:593-608) for `F.document` + `file_id` capture; the REG_FLOW toggle machinery (registration.py:44-134) for enable/disable.

**Toggle registration (D-08):** add to `REG_FLOW` (registration.py:44-64) and `REG_DEFAULTS` (66-86). Per CONTEXT line 94, default **off**:
```python
REG_FLOW = [ ..., ("resume", "reg_q_resume") ]   # place as last step before confirm
REG_DEFAULTS = { ..., "reg_q_resume": "off" }
REG_LABELS  = { ..., "reg_q_resume": "📄 Резюме" }   # label dict at registration.py:88
```
Adding it to these three dicts auto-wires the admin toggle UI (`render_questions_text`/`build_questions_keyboard`, admin.py:876-900) and `_get_enabled_steps` (registration.py:118) — no admin.py change needed for the toggle itself.

**Ask step** — add a branch to `_ask_step` (registration.py:137-204) following the existing `elif step_key == ...:` shape:
```python
elif step_key == "resume":
    await message.answer(f"{p} Прикрепи резюме (PDF или DOCX):", reply_markup=get_skip_kb())
    await state.set_state(Registration.resume)
```

**Receive + validate (D-09/D-10 — mandatory, PDF/DOCX only, must NOT crash on wrong type):** copy the `F.document` capture from admin.py:593-598 (`message.document.file_id`). Validate by extension/mime and re-prompt instead of crashing — model the graceful re-prompt on the invalid branch `settings_receive_file_invalid` (admin.py:613-615) and the validation-then-return pattern in `process_age` (registration.py:433-441):
```python
@router.message(Registration.resume, F.document)
async def process_resume(message, state, bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".pdf") or name.endswith(".docx")):
        await message.answer("Принимаются только PDF или DOCX. Прикрепи файл ещё раз.")
        return
    await state.update_data(resume_file_id=doc.file_id)   # store file_id only, no download (D-10)
    await _advance("resume", message, state, bot)

@router.message(Registration.resume)   # any non-document → re-prompt, do not crash
async def process_resume_invalid(message, state):
    await message.answer("Пожалуйста, прикрепи документ (PDF или DOCX).")
```
Persist `resume_file_id` through `finalize_registration` → `add_user` (the new column added in db.py). `finalize_registration` already passes the whole `data` dict to `add_user` (registration.py:677), so just `data.setdefault("resume_file_id", None)` near the other setdefaults (656-675).

---

### `handlers/registration.py` — `reg_started` dropout tracking (lifecycle) [SCHED-02]

**Analog:** `cmd_start` (registration.py:343-375) write point; `finalize_registration` (650-709) delete point. DB write modeled on `set_setting`/`delete_setting` (db.py:76-88).

**Why a DB row, not FSM (PITFALLS + code_context line 92):** FSM is `MemoryStorage` (main.py:37) — volatile, wiped on restart. The incomplete-registration broadcast segment must survive restarts, so write a `reg_started` row when the flow begins and delete it on finalize.

**Write** — at the start of the actual registration flow. Best hook is `_start_registration_flow` (registration.py:287-306, called for new users at cmd_start:375) so admins doing re-registration tests are not counted twice; or `cmd_start` itself per CONTEXT line 97. Use a new `db.py` helper shaped like `set_setting` (db.py:76-82):
```python
async def mark_reg_started(telegram_id, username):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO reg_started (telegram_id, username, started_at) VALUES (?, ?, ?)",
            (telegram_id, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        await db.commit()
```

**Delete** — inside `finalize_registration` after `add_user` (registration.py:677), modeled on `delete_setting` (db.py:85-88): `await clear_reg_started(message.from_user.id)`. Segment query for the broadcast = telegram_ids in `reg_started` (those who started but never finished). `datetime` is already imported (registration.py:3).

---

### `handlers/registration.py` — subscription check, NON-blocking (guard) [QW-02]

**Analog:** no exact getChatMember precedent in repo (see "No Analog Found"). Closest patterns: settings read via `get_setting("contact_tg")` (user_actions.py:153), the `cmd_start` hook point (registration.py:343), and the fail-soft `try/except` wrapping of every Bot API call in this codebase (e.g. `finalize_registration` admin-notify loop, registration.py:701-705).

**Do NOT gate (D-04..07).** Check membership against the channel already in `contact_tg` (`bot_settings`) — reuse that setting, no new channel config (D-04). Wrap `bot.get_chat_member` in try/except and **fail-open** (D-07) — exactly how this codebase treats every fallible Bot API call:
```python
async def is_subscribed(bot, channel, user_id) -> bool | None:
    try:
        m = await bot.get_chat_member(channel, user_id)
        return m.status in {"creator", "administrator", "member"}
    except Exception:
        return None   # bot not admin / unknown → do not penalize (fail-open)
```
Persist non-subscriber status for the admin broadcast segment (a `subscribed` flag column on `users` via `_ensure_column`, or a derived check at broadcast time). The admin prompt phrasing is specified in CONTEXT line 106. The reminder itself is a **broadcast segment** — see admin.py section below.

---

### `keyboards/builders.py` — `🪙 Мои монеты` menu button (config)

**Analog:** `MENU_BUTTONS` list + `get_main_menu_kb` (builders.py:8-25) — exact.

Add one tuple to `MENU_BUTTONS` (builders.py:8-16). The `(setting_key, label)` shape auto-wires the admin menu-toggle UI (`render_menu_text`/`build_menu_keyboard`, admin.py:949-967) and the per-button on/off check in `get_main_menu_kb` (builders.py:21-23):
```python
MENU_BUTTONS = [
    ...
    ("menu_coins", "🪙 Мои монеты"),
]
```
Default-on requires no entry (the builder treats `None` as on, builders.py:22). The button **handler** goes in a message handler keyed on `F.text == "🪙 Мои монеты"` — model on `my_referrals` (user_actions.py:186-206): `ensure_registered` guard first, then read balance, then `message.answer(..., parse_mode="HTML")`.

---

### `handlers/admin.py` — `/coins` command + broadcast segments

**`/coins @username +N [reason]` (admin only, D-13/D-14)** — analog: `cmd_find_user` (admin.py:96-117) for arg parsing + `get_user_by_username` lookup, and `cmd_create_link` (120-135) for `split(maxsplit=...)` free-text tail. Pattern:
```python
@router.message(Command("coins"), is_admin)
async def cmd_coins(message, bot):
    args = message.text.split(maxsplit=3)   # /coins, @user, +N, reason(optional)
    if len(args) < 3:
        await message.answer("⚠️ Формат: /coins @username +N [причина]")
        return
    user = await get_user_by_username(args[1])   # reuse db.py:147
    if not user:
        await message.answer(f"❌ Пользователь {args[1]} не найден.")
        return
    # parse signed int args[2]; reason = args[3] if present (optional, D-13)
    # INSERT into coins ledger (negative allowed, D-14); never UPDATE a balance
```
Use the `is_admin` filter (admin.py:50-51) on the handler exactly like every other admin command. Register the command in the `/admin` help text (admin.py:85-93) for discoverability.

**Leaderboard `/рейтинг` + aliases `/rating` `/leaderboard` (D-15/D-16)** — aiogram supports multiple command names in one `Command(...)` filter:
```python
@router.message(Command("рейтинг", "rating", "leaderboard"))
async def cmd_leaderboard(message):
    # top-10 from ledger + requester's own rank; render like render_monthly_stats (admin.py:66-81)
```
Render text with the line-accumulator style of `render_monthly_stats` (admin.py:66-81) / `my_referrals` (user_actions.py:202-205): build `lines = [...]`, append `f"• ..."`, `"\n".join(...)`, send `parse_mode="HTML"`.

**Broadcast audience segments (non-subscribers, incomplete registrations) [D-06, SCHED-02]** — analog: the entire broadcast flow (admin.py:657-871). Add two inline buttons to the target-selection keyboards at **admin.py:663-667 AND admin.py:689-693** (both `show_admin_broadcast` and `cmd_broadcast` build the same keyboard — keep them in sync). Mirror the existing `broadcast_local` segment handler (admin.py:711-749) which sets `target_type` + `target_users` then advances to `Broadcast.message`:
```python
[InlineKeyboardButton(text="🚫 Не подписаны на канал", callback_data="broadcast_unsubscribed")],
[InlineKeyboardButton(text="📝 Не завершили регистрацию", callback_data="broadcast_incomplete")],
```
Each new callback handler (copy shape of `process_broadcast_local_file`, admin.py:711-749) resolves its segment to a `user_ids` list, then `state.update_data(target_type="list", target_users=user_ids)` and `state.set_state(Broadcast.message)`. The send loop `process_broadcast` (admin.py:829-871) consumes `target_users` unchanged — no change to the sender. Segment sources: incomplete = telegram_ids in `reg_started`; non-subscribers = users where the subscription check returned False.

---

### `handlers/coins.py` (optional new file) OR fold into existing routers

If a dedicated file is preferred, model its skeleton on any handler module header (user_actions.py:1-17): `router = Router()`, `logger = logging.getLogger(__name__)`, import `get_user`/balance helpers + `ensure_registered`. **Register it in main.py** `dp.include_router(...)` (main.py:40-42) — note admin router is included FIRST to intercept commands (comment at main.py:40); place a coins router after admin if it owns `/coins` style commands, or just put admin-only `/coins` in admin.py and the user-facing balance button in user_actions.py to avoid a new file entirely (lower risk, recommended).

---

## Shared Patterns

### Connection-per-call (all DB access)
**Source:** every function in `database/db.py` (e.g. get_setting db.py:67-73).
**Apply to:** all new ledger / reg_started / subscription read+write functions.
```python
async def fn(...):
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("...", (...)) as cursor:
            ...
        await db.commit()   # only for writes
```
No shared/global connection exists — do not introduce one. Use `db.row_factory = aiosqlite.Row` + `dict(row)` for dict results (db.py:138-143).

### Settings-driven toggles (on/off via bot_settings)
**Source:** `_is_step_enabled` (registration.py:111-115), `get_main_menu_kb` (builders.py:21-23), `REG_DEFAULTS` (registration.py:66).
**Apply to:** resume step toggle, coins menu button, subscription-check enable/disable.
Convention: `val = await get_setting(key); is_on = (val == "on") if val is not None else DEFAULT`. New toggles get a `REG_DEFAULTS`-style default and are surfaced automatically by the existing admin toggle UIs if added to `REG_FLOW`/`MENU_BUTTONS`.

### Admin authorization
**Source:** `is_admin` filter (admin.py:50-51) for message handlers; inline `if callback.from_user.id not in config.ADMIN_IDS` (admin.py:659-661) for callbacks.
**Apply to:** `/coins`, new broadcast-segment callbacks.
```python
@router.message(Command("coins"), is_admin)            # message handler
...
if callback.from_user.id not in config.ADMIN_IDS:      # callback handler
    await callback.answer("Недостаточно прав", show_alert=True); return
```

### Fail-soft Bot API calls
**Source:** admin-notify loop (registration.py:701-705), photo sends (user_actions.py:79-84), broadcast send loop (admin.py:858-864).
**Apply to:** getChatMember subscription check (fail-open per D-07), any coins/segment send.
Every fallible `await bot.*` / `message.answer_*` is wrapped in `try/except Exception` and logged or counted, never allowed to crash the handler.

### file_id storage (no disk I/O)
**Source:** `settings_receive_file_doc` (admin.py:593-608), bonus doc handling (registration.py:715-718).
**Apply to:** resume upload (QW-03).
Store `message.document.file_id` (TEXT column) — never download. Re-send later via `message.answer_document(file_id)`.

### FSM cancel handler
**Source:** `cancel_registration` (registration.py:378-384), `cancel_broadcast` (admin.py:762-766).
**Apply to:** any new FSM state (resume, confirm) — ensure `Отмена`/`/cancel` clears state. Existing `cancel_registration` already filters `StateFilter(Registration)`, so new `Registration.*` states (confirm, resume) are covered automatically.

### New FSM states
**Source:** `Registration` StatesGroup (states.py:3-24).
**Apply to:** QW-01 confirm, QW-03 resume — add `confirm = State()` and `resume = State()` to the `Registration` group (states.py). `Broadcast` group (states.py:29-31) already covers the broadcast segments (reuses `target_selection`/`message`).

---

## No Analog Found

| Concern | Role | Data Flow | Reason / Guidance |
|---------|------|-----------|-------------------|
| `getChatMember` subscription check | guard | Bot API request-response | No membership-check code exists in repo. Use aiogram 3 `bot.get_chat_member(chat, user_id)` (native, no new dep per STACK.md). Wrap fail-open (D-07). Reuse `contact_tg` setting as the channel id. |
| Append-only ledger semantics | model | append-only | No prior ledger table; all existing tables are mutable. Enforce "INSERT only, balance = SUM(delta)" by convention (D-12) — there is no analog that does running-sum balance. Add an index on `coins(user_id)` (Claude's discretion, D-51). |

---

## Metadata

**Analog search scope:** `database/`, `handlers/`, `keyboards/`, `main.py`
**Files scanned:** db.py, registration.py, builders.py, user_actions.py, states.py, admin.py, main.py (full repo handler surface)
**Pattern extraction date:** 2026-06-25
**Note:** All new code stays within the existing 3-router architecture (admin / registration / user_actions, main.py:40-42). No new infrastructure, no new dependencies for Phase 1 (APScheduler/SQLAlchemy are Phase 3 per CLAUDE.md — not used here).
