# Architecture Research

**Domain:** Aiogram 3 + SQLite Telegram event bot — brownfield extension
**Researched:** 2026-06-25
**Confidence:** HIGH (based on direct code inspection + established aiogram 3 patterns)

---

## System Overview

### Current State (baseline)

```
Telegram API
    |  (long polling)
    v
main.py
 ├── Dispatcher (MemoryStorage FSM)
 ├── Router order: admin → registration → user_actions
 └── init_db() on startup

handlers/
 ├── admin.py       — admin commands, inline settings panel, broadcast, reply-to-question
 ├── registration.py — /start, REG_FLOW engine, finalize_registration()
 └── user_actions.py — main menu buttons, ensure_registered() gating

keyboards/builders.py — all keyboards, get_main_menu_kb() reads bot_settings
database/db.py       — aiosqlite, open-per-call pattern, _ensure_column() migrations
services/sheets.py   — gspread with retry

SQLite data/forum.db
 ├── users            — one row per registered user
 └── bot_settings     — key-value runtime config (registration toggles, event info, etc.)
```

### Target State (after this milestone)

```
Telegram API
    |  (long polling)
    v
main.py
 ├── scheduler.start()    ← NEW: AsyncIOScheduler on same event loop
 ├── Dispatcher (MemoryStorage FSM)
 └── Router order:
      admin → coins → approvals → payments → registration → user_actions

handlers/
 ├── admin.py         — extended: event type/module toggles, approval reminder, scheduled broadcast UI
 ├── coins.py         ← NEW: /coins admin command, /рейтинг user command
 ├── approvals.py     ← NEW: tinder review for pending applications
 ├── payments.py      ← NEW: payment flow (user), receipt tinder (admin)
 ├── registration.py  — extended: submit_application(), new date/consent/file step types
 ├── user_actions.py  — extended: ensure_registered() checks status='approved'
 └── states.py        — extended: ApprovalReview, Payment, ScheduledBroadcast states

keyboards/builders.py  — extended: consent keyboard, leaderboard button, payment keyboards
database/db.py         — extended: new tables, new columns, new query functions
services/
 ├── sheets.py        — unchanged
 ├── coins.py         ← NEW: coins balance, transactions, leaderboard
 └── scheduler.py     ← NEW: APScheduler wrapper, job registration on startup

SQLite data/forum.db
 ├── users                — extended with status, resume_file_id, payment_* columns
 ├── bot_settings         — extended with module flags, event_type, approval modes
 ├── coins                ← NEW: transactions log
 ├── scheduled_broadcasts ← NEW: pending broadcast jobs
 ├── reg_in_progress      ← NEW: tracks incomplete registrations for reminder
 └── user_consents        ← NEW: per-user consent records
```

---

## Component Boundaries

### Existing files — what changes and what doesn't

| File | Changes | Stays Same |
|------|---------|------------|
| `main.py` | Add `scheduler.start()` before polling; add 3 new routers in correct order | Bot init, MemoryStorage, long polling |
| `handlers/admin.py` | Add event type/module toggle UI section; add scheduled broadcast UI; add "Заявки" entry point calling approvals router | All existing settings, stats, export, broadcast, reply-to-question |
| `handlers/registration.py` | Split `finalize_registration()` into `submit_application()` + `finalize_registration()`; add channel-check in `cmd_start`; add date/consent/file step handlers; add resume upload handler | All existing step handlers, `_advance()`, `_get_enabled_steps()`, REG_FLOW list |
| `handlers/user_actions.py` | `ensure_registered()` must also check `user['status'] == 'approved'` | All menu handlers |
| `handlers/states.py` | Add `ApprovalReview`, `Payment`, `ScheduledBroadcast`, `date`/`consent`/`resume` states to `Registration` | All existing state classes |
| `keyboards/builders.py` | Add consent keyboard, payment keyboard, leaderboard pagination | All existing keyboards |
| `database/db.py` | Add columns via `_ensure_column`, add 4 new tables in `init_db`, add new query functions | `get_setting`, `set_setting`, `add_user`, `get_user`, existing queries |
| `services/sheets.py` | Unchanged | Unchanged |

### New files — single responsibility

| File | Owns | Calls |
|------|------|-------|
| `services/coins.py` | Coins balance, add/deduct transactions, leaderboard queries | `database/db.py` directly |
| `services/scheduler.py` | APScheduler instance, `schedule_broadcast()`, `schedule_reminder()`, `cancel_job()`, startup job restore | `database/db.py` for scheduled_broadcasts table; `bot` passed at init |
| `handlers/coins.py` | `/coins @username ±N` admin command; `/рейтинг` user command | `services/coins.py`, `database/db.py` |
| `handlers/approvals.py` | Paginated tinder for pending applications; approve/reject/skip callbacks; notify user on approval | `database/db.py`, `keyboards/builders.py`, `bot.send_message()` for user notification |
| `handlers/payments.py` | Payment instructions display; receipt upload FSM; receipt tinder for admin; payment status notifications | `database/db.py`, `services/scheduler.py` (cancel reminder on payment), `keyboards/builders.py` |

### What does NOT get a new file

- **Event type / module toggles**: pure `bot_settings` keys; UI lives in `admin.py` settings section, gating logic lives inline in `registration.py` and `payments.py`
- **Channel subscription check**: one `bot.get_chat_member()` call at top of `cmd_start` in `registration.py`
- **REG_FLOW new question types**: new branches in `_ask_step()` and new handler functions, all in `registration.py`
- **Consents in REG_FLOW**: each consent is a step in REG_FLOW with type `consent`; stored in `user_consents` table; UI gated by `module_consents=on`
- **Registration confirmation**: pre-finalize step using existing `get_confirm_kb()`, added as the last step in `_advance()` before calling `finalize_registration()`

---

## Database Schema Extensions

### `users` table — new columns via `_ensure_column()`

```python
# In init_db(), after existing _ensure_column calls:
await _ensure_column(db, "users", "status", "TEXT DEFAULT 'approved'")
   # 'approved' default preserves all existing rows without change
await _ensure_column(db, "users", "resume_file_id", "TEXT")
await _ensure_column(db, "users", "payment_status", "TEXT DEFAULT 'not_paid'")
await _ensure_column(db, "users", "payment_receipt_file_id", "TEXT")
await _ensure_column(db, "users", "payment_planned_date", "TEXT")
await _ensure_column(db, "users", "payment_confirmed_at", "TEXT")
```

### New tables — added to `init_db()`

```sql
-- Coins transaction log
CREATE TABLE IF NOT EXISTS coins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    amount        INTEGER NOT NULL,         -- positive = credit, negative = debit
    reason        TEXT,
    changed_by    INTEGER,                  -- admin telegram_id, NULL if system
    timestamp     TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(telegram_id)
);

-- Scheduled broadcast jobs (APScheduler uses MemoryJobStore; this is the persistent record)
CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT UNIQUE NOT NULL,       -- APScheduler job ID for cancellation
    scheduled_at TEXT NOT NULL,             -- ISO datetime
    target      TEXT DEFAULT 'all',         -- 'all' | JSON filter spec
    message_data TEXT NOT NULL,             -- JSON: {type, content, caption, file_id}
    status      TEXT DEFAULT 'pending',     -- pending | sent | cancelled | failed
    created_by  INTEGER,
    created_at  TEXT NOT NULL
);

-- Tracks users mid-registration (for incomplete-registration reminder)
CREATE TABLE IF NOT EXISTS reg_in_progress (
    telegram_id  INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    reminder_sent BOOLEAN DEFAULT 0
);

-- Per-user consent records (consent names are configured per event)
CREATE TABLE IF NOT EXISTS user_consents (
    user_id      INTEGER NOT NULL,
    consent_key  TEXT NOT NULL,
    accepted     BOOLEAN DEFAULT 0,
    accepted_at  TEXT,
    PRIMARY KEY (user_id, consent_key)
);
```

### New `bot_settings` keys

| Key | Values | Default | Purpose |
|-----|--------|---------|---------|
| `event_type` | `forum` / `conference` / `custom` | `forum` | Display labeling only |
| `module_payment` | `on` / `off` | `off` | Gates entire payment flow |
| `module_consents` | `on` / `off` | `off` | Gates consent steps in REG_FLOW |
| `module_reminders` | `on` / `off` | `off` | Gates scheduler auto-reminders |
| `short_approval` | `auto` / `manual` | `auto` | Approval mode for short-form registrations |
| `full_approval` | `auto` / `manual` | `auto` | Approval mode for full-form registrations |
| `channel_id` | Telegram channel ID | `None` | For subscription check |
| `payment_amount` | Numeric string | `None` | Payment amount in rubles |
| `payment_requisites` | Text | `None` | Bank details |
| `payment_deadline` | `DD.MM.YYYY` | `None` | Payment deadline |
| `payment_penalties` | JSON string | `None` | `[{"date": "DD.MM.YYYY", "amount": N}]` |
| `consent_list` | JSON string | `None` | `[{"key": "...", "text": "..."}]` |

---

## Data Flows

### Flow 1: Registration with approval mode

```
User sends /start
    |
registration.py: cmd_start()
    ├── [if channel_id set] bot.get_chat_member() → not subscribed → show subscribe prompt, return
    ├── [if user exists AND status='approved'] → show welcome + menu
    ├── [if user exists AND status='pending'] → show "заявка на рассмотрении" message, return
    └── [new user] → insert into reg_in_progress, → _start_registration_flow()

REG_FLOW multi-step FSM
    └── _advance() loops through enabled steps
            └── [last step] → [if module_consents=on] → consent steps → _pre_finalize()
                            └── [else] → _pre_finalize()

_pre_finalize() (new: confirmation step using existing get_confirm_kb())
    ├── [user confirms] → _route_to_finalize()
    └── [user edits] → return to last step

_route_to_finalize()
    ├── determine form_type = 'short' | 'full'
    ├── read approval_mode = get_setting(f'{form_type}_approval')
    ├── [auto] → finalize_registration() (existing path, status='approved')
    └── [manual] → submit_application() (new path, status='pending')

finalize_registration() (auto path)
    ├── add_user(status='approved')
    ├── delete from reg_in_progress
    ├── append_to_sheet()
    └── _send_approved_content() → complete_text + menu + bonus

submit_application() (manual path)
    ├── add_user(status='pending')
    ├── delete from reg_in_progress
    ├── append_to_sheet()
    ├── notify_admins_new_application() → "Новая заявка! Откройте раздел Заявки"
    └── send "Заявка отправлена! Ожидайте решения." (no menu yet)

Admin opens "Заявки" in admin panel
    └── approvals.py: show_applications_page(offset=0)
            └── get_pending_applications(offset=0, limit=1)
                    └── render tinder card + [Одобрить][Отклонить][Пропустить][Одобрить все N]

Admin clicks "Одобрить"
    └── approvals.py: approve_callback(user_id)
            ├── db.update_user_status(user_id, 'approved')
            ├── _send_approved_content(bot, user_id) → complete_text + menu + bonus → user
            ├── [if module_payment=on] → send payment instructions → user
            └── show next pending application
```

### Flow 2: Payment module (module_payment=on)

```
_send_approved_content() (called after approval)
    └── [if module_payment=on]
            └── payments.py: send_payment_instructions(user_id)
                    → "Сумма: X₽, реквизиты: ..., дедлайн: DD.MM.YYYY"
                    → "После оплаты отправьте чек (PDF или фото)"
                    → set state Payment.waiting_for_receipt

User sends receipt (PDF or photo)
    └── payments.py: process_receipt()
            ├── db.set_payment_status(user_id, 'receipt_sent', receipt_file_id=...)
            ├── notify_admins_receipt() → "Новый чек! Откройте раздел Чеки"
            └── send "Чек получен! Ожидайте подтверждения."

Admin opens "Чеки" (same tinder pattern as approvals)
    └── payments.py: show_receipts_page()
            └── tinder card: name, amount, deadline, receipt link
                    + [Подтвердить][Отклонить][Следующий]

Admin clicks "Подтвердить"
    └── payments.py: confirm_receipt_callback(user_id)
            ├── db.set_payment_status(user_id, 'paid', confirmed_at=now)
            ├── scheduler.cancel_payment_reminders(user_id)
            ├── send "Оплата подтверждена! Ждём вас на мероприятии" → user
            └── show next pending receipt

Scheduler auto-reminders (when module_reminders=on):
    On approval + module_payment=on:
        scheduler.schedule_payment_reminder(user_id, deadline - 3days)
        scheduler.schedule_payment_reminder(user_id, deadline - 1day)
    Job fires → check current payment_status → if still 'not_paid', send reminder
```

### Flow 3: Scheduler lifecycle

```
main.py async startup:
    await scheduler.init(bot)   ← pass bot instance
        ├── scheduler = AsyncIOScheduler(jobstores={'default': MemoryJobStore()})
        ├── Load pending rows from scheduled_broadcasts WHERE status='pending'
        │       └── for each: add_job(run_date=..., func=_execute_broadcast, args=[row_id])
        └── scheduler.start()  ← runs on same asyncio event loop as aiogram

Admin schedules a broadcast:
    admin.py → scheduler.schedule_broadcast(run_at, target, message_data)
        ├── INSERT INTO scheduled_broadcasts (...) → get row_id
        ├── job = scheduler.add_job(func=_execute_broadcast, trigger='date',
        │       run_date=run_at, args=[row_id], id=f'bcast_{row_id}')
        └── UPDATE scheduled_broadcasts SET job_id=job.id

APScheduler fires _execute_broadcast(row_id):
    ├── SELECT * FROM scheduled_broadcasts WHERE id=row_id AND status='pending'
    ├── Deserialize message_data JSON
    ├── Get target user_ids (all or filtered)
    ├── Execute broadcast loop (same logic as existing broadcast handler)
    ├── UPDATE scheduled_broadcasts SET status='sent', sent_at=now
    └── [on error] UPDATE status='failed', log error

Bot restart with pending jobs:
    scheduler.init() reloads all status='pending' rows
    APScheduler fires missed jobs if within misfire_grace_time (set to 3600s)
```

### Flow 4: REG_FLOW extension (new step types)

The existing engine: `REG_FLOW` list → `_get_enabled_steps()` → `_ask_step()` → `_advance()`. Adding a new type means:

```python
# 1. In REG_FLOW list (registration.py) — new entries:
("birth_date",   "reg_q_birth_date"),     # date type
("resume",       "reg_q_resume"),          # file type
("consent_data", "reg_q_consent_data"),    # consent type (one per configured consent)

# 2. In states.py — new states:
class Registration(StatesGroup):
    # ... existing states ...
    birth_date = State()
    resume = State()
    consent_data = State()
    consent_photo = State()
    # (one generic state per consent "slot"; actual consent keys stored in FSM data)

# 3. In _ask_step() — new branches (registration.py):
elif step_key == "birth_date":
    await message.answer(f"{p} Введите дату рождения (ДД.ММ.ГГГГ):", reply_markup=get_cancel_kb())
    await state.set_state(Registration.birth_date)
elif step_key == "resume":
    await message.answer(f"{p} Отправьте резюме (PDF или DOCX):", reply_markup=get_skip_kb())
    await state.set_state(Registration.resume)
elif step_key.startswith("consent_"):
    consent_text = await _get_consent_text(step_key)  # reads from bot_settings
    await message.answer(f"{p} {consent_text}", reply_markup=get_consent_kb())
    await state.set_state(Registration.consent_slot)

# 4. Handler for date validation:
@router.message(Registration.birth_date)
async def process_birth_date(message, state, bot):
    try:
        datetime.strptime(message.text.strip(), "%d.%m.%Y")
    except ValueError:
        await message.answer("Неверный формат. Используйте ДД.ММ.ГГГГ")
        return
    await state.update_data(birth_date=message.text.strip())
    await _advance("birth_date", message, state, bot)

# 5. Handler for consent:
@router.message(Registration.consent_slot, F.text == "Принимаю")
async def process_consent(message, state, bot):
    data = await state.get_data()
    current_step = data.get("_current_consent_key")
    await state.update_data(**{current_step: True})
    await _insert_user_consent(message.from_user.id, current_step)
    await _advance(current_step, message, state, bot)
```

Key constraint: consent steps are dynamically generated from `consent_list` bot_setting. `_get_enabled_steps()` must expand the consent list into individual steps when `module_consents=on`.

---

## Router Order in main.py

```python
# Order matters — more specific filters first
dp.include_router(admin.router)      # commands with is_admin filter, intercepts before others
dp.include_router(coins.router)      # /coins (admin+), /рейтинг (user-facing)
dp.include_router(approvals.router)  # approval tinder callbacks (admin-only)
dp.include_router(payments.router)   # payment FSM + receipt tinder callbacks
dp.include_router(registration.router)  # /start + REG_FLOW FSM states
dp.include_router(user_actions.router)  # text-match menu buttons (least specific)
```

Rationale:
- `admin` stays first — it uses `is_admin` filter or `is_question_reply` predicate, must not be intercepted by FSM filters
- `coins` before `registration` — `/рейтинг` is a text command that must not fall through to menu-button matching
- `approvals` and `payments` are callback-only (no text handlers), order relative to each other doesn't matter
- `registration` before `user_actions` — FSM state handlers are more specific and must win over free-text matching
- `user_actions` last — text-match on specific button labels, catch-all path

---

## Recommended Build Order

Each step in the list is unblocked only when its dependencies are done.

### Step 1 — DB schema (no dependencies, do first)

**Files:** `database/db.py`
- Add `_ensure_column` calls for `status`, `resume_file_id`, `payment_*` on `users`
- Add `coins`, `scheduled_broadcasts`, `reg_in_progress`, `user_consents` tables to `init_db()`
- Add new query functions: `get_pending_applications()`, `update_user_status()`, `get_coin_balance()`, `add_coin_transaction()`, `get_leaderboard()`, `set_payment_status()`, `get_users_by_filter()`

**Why first:** Everything else reads/writes the DB. Safe to deploy — `DEFAULT 'approved'` on `status` means existing users keep working.

---

### Step 2 — Coins service + handler (depends on Step 1 only)

**Files:** `services/coins.py` (new), `handlers/coins.py` (new), `handlers/states.py` (no new states needed)

- `services/coins.py`: `add_transaction()`, `get_balance()`, `get_leaderboard()`, `get_rank()`
- `handlers/coins.py`: `/coins @username ±N reason` (admin), `/рейтинг` (all users)
- Wire into `main.py` router list

**Why second:** Purely additive, no existing code touched, delivers immediate value.

---

### Step 3 — Channel subscription check (depends on Step 1 only, parallel with Step 2)

**Files:** `handlers/registration.py` (top of `cmd_start`), `database/db.py` (new `channel_id` setting key)

```python
channel_id = await get_setting("channel_id")
if channel_id:
    try:
        member = await bot.get_chat_member(int(channel_id), user_id)
        if member.status in ('left', 'kicked', 'banned'):
            await message.answer("Для участия подпишись на канал: ...")
            return
    except Exception:
        pass  # if check fails, let user through (graceful degradation)
```

**Why parallel with Step 2:** No shared state changes.

---

### Step 4 — Registration confirmation step (depends on Step 1)

**Files:** `handlers/registration.py` — add `_pre_finalize()` between `_advance()` reaching end and `finalize_registration()` call

The existing `get_confirm_kb()` in `keyboards/builders.py` is already implemented. Wire it:
- Last step in `_advance()` calls `_pre_finalize()` not `finalize_registration()` directly
- `_pre_finalize()` shows summary + confirm/edit buttons
- Add `Registration.confirm` state

---

### Step 5 — Approval flow (depends on Steps 1, 4)

**Files:** `handlers/registration.py` (split finalize), `handlers/approvals.py` (new), `handlers/states.py` (new states), `handlers/user_actions.py` (update `ensure_registered()`), `handlers/admin.py` (add "Заявки" button), `keyboards/builders.py` (approval tinder keyboard)

Execution order within Step 5:
1. Add `submit_application()` to `registration.py`, extract `_send_approved_content()` from `finalize_registration()`
2. Update `_route_to_finalize()` to branch on `short_approval`/`full_approval` setting
3. Build `approvals.py` router with tinder callbacks
4. Update `admin.py` admin keyboard to add "Заявки" button
5. Update `ensure_registered()` to check `status='approved'`

**Why after Step 4:** Confirmation step must be finalized before splitting finalize — doing it simultaneously causes merge conflicts in registration.py.

---

### Step 6 — Scheduler infrastructure (depends on Step 1)

**Files:** `services/scheduler.py` (new), `main.py` (add `await scheduler.init(bot)`)

- `AsyncIOScheduler` with `MemoryJobStore` (APScheduler 3.x)
- Restore pending `scheduled_broadcasts` rows on startup
- Expose: `schedule_broadcast()`, `schedule_reminder()`, `cancel_job()`

**Dependency note:** APScheduler must be added to `requirements.txt`. No SQLAlchemy needed — use custom DB table pattern described above.

```
requirements.txt additions:
apscheduler>=3.10,<4.0
```

**Why after Step 1:** needs the `scheduled_broadcasts` table; before Step 7 and 8.

---

### Step 7 — Scheduled broadcasts + re-registration reminder (depends on Steps 5, 6)

**Files:** `handlers/admin.py` (new "Запланировать рассылку" flow), `handlers/states.py` (new `ScheduledBroadcast` states), `handlers/registration.py` (insert/delete `reg_in_progress` on start/finish)

- Extend existing broadcast flow: after composing message, offer "Отправить сейчас" vs "Запланировать"
- Scheduled path: collect date/time via FSM, call `scheduler.schedule_broadcast()`
- Re-registration reminder: periodic job (every 2h) scans `reg_in_progress WHERE started_at < now - 2h AND reminder_sent=0`, sends nudge, marks `reminder_sent=1`
- Approval reminder: periodic job (every 4h) counts `users WHERE status='pending'`, sends "N заявок ждут обработки" to all admins

**Why after Step 5:** approval reminder queries the users table with status field; re-registration reminder deletes from `reg_in_progress` in the same `finalize_registration()` that Step 5 refactors.

---

### Step 8 — REG_FLOW: date + file types (depends on Step 1, independent of Steps 5-7)

**Files:** `handlers/registration.py`, `handlers/states.py`, `keyboards/builders.py`

- Add `birth_date` step type with `datetime.strptime` validation
- Add `resume` step type — receives `message.document`, stores `file_id`
- New `reg_q_birth_date`, `reg_q_resume` setting keys for admin toggle
- `_get_enabled_steps()` skips `resume` if `reg_q_resume` is off

**Why can be done early:** These are new step types; they don't conflict with approval flow. Can be done in parallel with Steps 5-7 if separate developer.

---

### Step 9 — Event modularity UI + consent module (depends on Steps 5, 8)

**Files:** `handlers/admin.py` (new "Настройки мероприятия" section), `handlers/registration.py` (`_get_enabled_steps()` expands consent list), `keyboards/builders.py` (consent keyboard), `database/db.py` (already has `user_consents` from Step 1)

- Admin UI to set `event_type`, toggle `module_payment`/`module_consents`/`module_reminders`
- `_get_enabled_steps()` reads `module_consents` setting; if on, reads `consent_list` JSON from bot_settings and appends consent steps dynamically
- Each consent step stores result in `user_consents` table

**Why after Step 5:** the confirmation + finalization flow must be stable before adding consent steps that precede finalization.

---

### Step 10 — Payment module (depends on Steps 5, 6, 9)

**Files:** `handlers/payments.py` (new router), `handlers/states.py` (new Payment states), `handlers/admin.py` (add "Чеки" button), `keyboards/builders.py` (payment keyboards)

- User flow: payment instructions on approval (when `module_payment=on`), receipt upload FSM
- Admin flow: receipt tinder (same paginated card pattern as approvals.py)
- Scheduler: reminder jobs at deadline-3d and deadline-1d

**Why last:** Depends on the approval flow (knows when user is approved), scheduler (for reminders), and event modularity (gated by `module_payment` toggle).

---

## Architectural Patterns

### Pattern 1: bot_settings key-value for all runtime flags

**What:** All toggles and runtime configuration live as string rows in `bot_settings`. Gate logic reads via `get_setting(key)`.

**When to use:** Any piece of configuration that an admin must change at runtime without deployment.

**Do this for:** `module_payment`, `module_consents`, `short_approval`, `full_approval`, `channel_id`, `payment_amount`, `consent_list`

**Do NOT do this for:** structural configuration that requires a code change anyway (e.g., adding a new table). That goes in `init_db()`.

### Pattern 2: _ensure_column for safe migrations

**What:** `_ensure_column(db, table, col, definition)` adds a column only if it doesn't exist. Used in `init_db()`.

**When to use:** Every new column on an existing table. Never use raw `ALTER TABLE` unconditionally.

**Contract:** New columns that could break existing row reads/writes must have a safe `DEFAULT`. The `status DEFAULT 'approved'` pattern lets all existing users pass `ensure_registered()` without a data migration.

### Pattern 3: Service layer for domain logic, db.py only for queries

**What:** `database/db.py` owns only SQL (SELECT, INSERT, UPDATE, DELETE). Business logic (leaderboard ranking formula, broadcast fan-out loop, job restoration logic) lives in `services/`.

**Boundary rule:** `db.py` functions take primitives, return dicts/lists/primitives. They do not import from `handlers/` or `services/`. `services/` may import from `db.py`. `handlers/` may import from both.

**Example violation to avoid:** Do not put "send reminder if payment_status='not_paid'" logic inside a `db.py` function. That goes in `services/scheduler.py` job callback.

### Pattern 4: Tinder pagination via inline callback_data, not FSM pages

**What:** The approval tinder and receipt tinder are stateless from the FSM perspective. Each card is rendered by fetching `get_pending_X(offset=N)`. The admin's current offset is stored in FSM data when they enter the "reviewing" state.

**Callback data shape:**
- `appr_ok:{user_id}` → approve user, fetch next pending
- `appr_no:{user_id}` → reject user, fetch next pending
- `appr_skip:{user_id}` → skip (advance offset in FSM data), fetch next
- `appr_all` → mass-approve all pending

**Why not encode offset in callback_data:** Telegram's 64-byte callback_data limit. Storing offset in FSM state is cleaner and doesn't require URL-encoding.

### Pattern 5: Scheduler as a wrapper, not a job store

**What:** `services/scheduler.py` wraps APScheduler. The `scheduled_broadcasts` DB table is the persistent record; APScheduler's MemoryJobStore is the runtime executor.

**Startup contract:**
1. `scheduler.init(bot)` is called before `dp.start_polling(bot)` in `main.py`
2. `init()` loads all `status='pending'` rows from `scheduled_broadcasts`, schedules them with APScheduler
3. `misfire_grace_time=3600` — if bot was down for <1h when a job was due, it fires on restart

**Failure mode:** If the bot is down for >1h when a scheduled broadcast was due, `coalesce=True` fires it once on restart. Operator-visible in the `scheduled_broadcasts` table.

---

## Anti-Patterns

### Anti-Pattern 1: Adding new FSM states to Registration for non-registration flows

**What people do:** Put payment state (`Payment.waiting_for_receipt`) inside the `Registration` StatesGroup to avoid creating a new class.

**Why wrong:** `cancel_registration` handler catches `StateFilter(Registration)` and will clear the payment FSM state on `/cancel`, losing the receipt. All registration-phase checks will incorrectly match payment state.

**Do this instead:** Each subsystem gets its own `StatesGroup` in `states.py`: `ApprovalReview`, `Payment`, `ScheduledBroadcast`.

### Anti-Pattern 2: Calling `add_user()` with the new `status` column from `admin.py`

**What people do:** Write a separate admin function that calls `add_user()` to force-approve a user from the admin panel.

**Why wrong:** `add_user()` uses `INSERT OR REPLACE`, which overwrites ALL user fields with whatever is in the dict. If `admin.py` only passes `{telegram_id, status: 'approved'}`, all other fields get reset to None/defaults.

**Do this instead:** Add a dedicated `update_user_status(telegram_id, status)` function to `db.py` that runs `UPDATE users SET status=? WHERE telegram_id=?`. Only update the columns you intend to change.

### Anti-Pattern 3: One connection for the entire bot session

**What people do:** Open a single `aiosqlite.connect()` connection in `main.py` and pass it to all handlers.

**Why wrong:** aiosqlite connections are not thread-safe for concurrent coroutines. The existing pattern (open-per-call) is correct for aiogram's concurrent handler execution.

**Do this instead:** Keep the open-per-call pattern. If performance becomes an issue (it won't at 1500 users), switch to a `aiosqlite` connection pool or an async ORM.

### Anti-Pattern 4: Blocking the event loop in scheduler jobs

**What people do:** Write a scheduler job that calls `asyncio.run(some_coroutine())` inside an APScheduler job function.

**Why wrong:** APScheduler `AsyncIOScheduler` jobs run as coroutines on the existing event loop. Calling `asyncio.run()` inside a running loop raises `RuntimeError`.

**Do this instead:** Mark scheduler job functions as `async def` and use `await bot.send_message(...)` directly. APScheduler's `AsyncIOScheduler` will execute them as coroutines.

### Anti-Pattern 5: Reading `_ensure_column` result from `_get_enabled_steps()`

**What people do:** Dynamic step checking reads ALL `bot_settings` keys on every step to determine what's enabled — N DB queries per REG_FLOW step traversal.

**Why wrong:** `_get_enabled_steps()` already does N settings reads. Adding consent steps that each need a separate settings read multiplies this. At 19+ questions + N consents = 25+ DB round-trips per step navigation.

**Do this instead:** Load all relevant settings once at the start of `_get_enabled_steps()` using a batch query:
```python
async def _get_all_settings(keys: list[str]) -> dict[str, str]:
    # Single SQL: SELECT key, value FROM bot_settings WHERE key IN (...)
```
Or cache in FSM data: store the full `enabled_steps` list at the start of the registration flow (after `full_name` is collected) rather than recomputing on every `_advance()` call.

---

## Integration Points

### External Services

| Service | Integration | Notes |
|---------|-------------|-------|
| Telegram Bot API | aiogram 3 long polling | `bot.get_chat_member()` for subscription check; `bot.send_message()` called from scheduler jobs |
| Google Sheets | `services/sheets.py` (gspread + retry) | Only called in `finalize_registration()` and `submit_application()` — no change to interface |
| APScheduler | `services/scheduler.py` wrapper | `AsyncIOScheduler`, same event loop as aiogram; MemoryJobStore + DB-based persistence |

### Internal Boundaries

| Boundary | Communication | Rule |
|----------|---------------|------|
| `handlers/*` ↔ `database/db.py` | Direct function calls | handlers may import db functions; db must not import handlers |
| `handlers/*` ↔ `services/*` | Direct function calls | handlers may import services; services may import db; services must not import handlers |
| `handlers/approvals.py` ↔ `handlers/registration.py` | `_send_approved_content()` function call | Extract to shared function in registration.py; approvals imports it |
| `handlers/payments.py` ↔ `services/scheduler.py` | `scheduler.cancel_payment_reminders(user_id)` | On payment confirmed, cancel scheduled reminder jobs |
| `services/scheduler.py` ↔ `main.py` | `scheduler.init(bot)` at startup | `bot` instance passed at init and stored as module-level variable in scheduler.py |
| `admin.py` ↔ `registration.py` | REG_FLOW, REG_DEFAULTS, REG_LABELS imported | This import already exists; extend by also exposing `REG_DEFAULTS_SHORT`/`REG_DEFAULTS_FULL` if needed |

---

## Scaling Considerations

| Scale | Notes |
|-------|-------|
| Current (500-1500 users/season) | All proposed patterns work. Open-per-call SQLite is fine. APScheduler MemoryJobStore + DB restore handles restart. |
| 5000+ users | `_get_enabled_steps()` batch-settings-read becomes important. Consider caching enabled steps list in FSM data at flow start rather than recomputing per step. |
| 10k+ users | SQLite write contention on concurrent registrations. Switch to PostgreSQL + asyncpg (requires rewriting db.py but services/handlers interfaces stay the same). APScheduler SQLAlchemyJobStore with Postgres. |

At the current and projected scale (1000-1500/season, 500 peak concurrent), the brownfield architecture is sound. No premature scaling changes needed.

---

## Sources

- Direct code inspection: `handlers/registration.py`, `database/db.py`, `handlers/admin.py`, `handlers/states.py`, `keyboards/builders.py`, `main.py`
- Project specifications: `PLAN_YOULEAD_TZ.md`, `.planning/PROJECT.md`, `README.md`
- aiogram 3 router/FSM model: established patterns from code (MemoryStorage, StateFilter, StatesGroup)
- APScheduler 3.x AsyncIOScheduler: aiogram community standard for scheduled tasks in async bots

---
*Architecture research for: AIESEC Event Bot — brownfield extension (approval flow, coins, scheduler, payments, event modularity)*
*Researched: 2026-06-25*
