# Phase 3: Scheduler + Communications + Verification - Pattern Map

**Mapped:** 2026-06-27
**Files analyzed:** 9 (4 new, 5 modified)
**Analogs found:** 9 / 9 (all in-repo)

This is a brownfield aiogram 3 + aiosqlite bot. Every Phase 3 file has an in-repo
analog — no greenfield patterns required except APScheduler wiring (which adapts the
existing `pending_reminder_loop` startup-task shape). All excerpts below are copied
verbatim from current source with file:line anchors so the planner can reference them
directly in PLAN action steps.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `services/scheduler.py` (NEW) | service | event-driven / scheduled | `services/reminders.py` (`pending_reminder_loop`) | role-match (startup task → APScheduler) |
| `services/sheets.py` (EXTEND) | utility | file-I/O (gspread) | `services/sheets.py` (`_get_sheet`, `get_existing_sheet_ids`) | exact |
| `services/allowlist.py` (NEW, or fold into sheets.py) | service | transform / cache | `services/reminders.py` pure helpers + `sheets.py` `to_thread` | role-match |
| `database/db.py` (EXTEND) | model / migration | CRUD | `database/db.py` (`_ensure_column`, `reg_started` helpers, `get_setting`) | exact |
| `handlers/admin.py` (EXTEND) | controller | request-response / batch send | `show_admin_broadcast`, `_start_segment_broadcast`, `_welcome_flipped`, `process_broadcast` | exact |
| `handlers/registration.py` (EXTEND) | controller | request-response | `cmd_start` rejected-status guard (`registration.py:553`) | exact |
| `handlers/states.py` (EXTEND) | model (FSM) | — | `Broadcast` StatesGroup (`states.py:45`) | exact |
| `main.py` (EXTEND) | config / bootstrap | — | `pending_reminder_loop` wiring (`main.py:46`) | role-match |
| `tests/test_*_phase3.py` (NEW) | test | — | `tests/test_reminders_phase2.py` (pure-sync helpers) | exact |

---

## Pattern Assignments

### `services/scheduler.py` (NEW — service, event-driven) — SCHED-01 / SCHED-03

**Analog:** `services/reminders.py` (startup background task; the contrast/shape model).
APScheduler replaces the hand-rolled `while True` loop, but the **module-level structure,
fail-soft per-iteration logging, and settings-driven thresholds** copy directly.

**Module skeleton + logger pattern** (`services/reminders.py:6-14`):
```python
import asyncio
import logging
from config import config
from database.db import get_pending_count, get_setting

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1800  # seconds (30 min)
```

**Settings-driven threshold helpers — copy this pure-helper shape** (`services/reminders.py:17-28`):
```python
def _reminder_enabled(raw: str | None) -> bool:
    """on/None -> True, off -> False, unknown -> True (default on)."""
    return raw != "off"

def _reminder_interval(raw: str | None) -> int:
    """Positive int seconds; None/empty/invalid/<=0 -> DEFAULT_INTERVAL."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return value if value > 0 else DEFAULT_INTERVAL
```
> Phase 3 mirrors this for `nudge_after_minutes` / `nudge_scan_minutes` / `allowlist_refresh_minutes`.
> These pure parsers are the unit-test surface (RESEARCH Wave 0 `test_scheduler_helpers_phase3.py`).

**Per-iteration fail-soft + per-send fail-soft** (`services/reminders.py:34-49`) — apply
the same try/except discipline inside `nudge_incomplete_registrations()` and
`send_scheduled_broadcast()`:
```python
    while True:
        interval = DEFAULT_INTERVAL
        try:
            interval = _reminder_interval(await get_setting("pending_reminder_interval"))
            if _reminder_enabled(await get_setting("pending_reminder_enabled")):
                count = await get_pending_count()
                if count > 0:
                    text = f"📋 Заявок в ожидании: {count}. Открой /admin → Заявки."
                    for admin_id in config.ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, text)
                        except Exception as e:
                            logger.error(f"Pending reminder: failed to notify admin {admin_id}: {e}")
        except Exception as e:
            logger.error(f"Pending reminder loop iteration failed: {e}")
        await asyncio.sleep(interval)
```

**NEW (no in-repo analog — from RESEARCH Pattern 1):** module-level `_scheduler` +
`_bot` reference, `init_scheduler(bot)`, importable job targets
`send_scheduled_broadcast(broadcast_id)` / `nudge_incomplete_registrations()`,
`SQLAlchemyJobStore(url="sqlite:///data/jobs.sqlite")` (SEPARATE file from `forum.db` —
Pitfall 2). Job args = `broadcast_id: int` only (picklable). See RESEARCH.md:183-245.

---

### `services/sheets.py` (EXTEND — utility, file-I/O) — VERIF-01

**Analog:** `services/sheets.py` itself — the allowlist reader is a near-clone of
`_get_sheet` + `_get_existing_ids_sync` + the `to_thread` async wrapper, just pointed at a
**different worksheet/tab** (`ws = sh.worksheet(tab_name)` instead of `sh.sheet1`).

**Sync gspread accessor + creds guard** (`services/sheets.py:13-16`, `41-44`):
```python
def _get_sheet():
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    return sh.sheet1
```
```python
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        logger.warning("Google Sheet ID or Credentials not set. Skipping sheet export.")
        return
```
> NEW `_get_allowlist_rows_sync(tab_name)` swaps `sh.sheet1` → `sh.worksheet(tab_name)`
> and returns `ws.col_values(1)`. Wrap in the same creds guard; catch `WorksheetNotFound`
> fail-soft (Pitfall 6, D-13 default-off).

**Column-read + per-value parse loop** (`services/sheets.py:24-33`) — copy the
`col_values(1)[1:]` skip-header + per-row guard shape (normalize instead of `int()`):
```python
def _get_existing_ids_sync() -> set[int]:
    sheet = _get_sheet()
    col_values = sheet.col_values(1)
    ids = set()
    for v in col_values[1:]:
        try:
            ids.add(int(v))
        except (ValueError, TypeError):
            continue
    return ids
```

**`to_thread` async wrapper — copy exactly** (`services/sheets.py:59-60`):
```python
async def get_existing_sheet_ids() -> set[int]:
    return await asyncio.to_thread(_get_existing_ids_sync)
```

---

### `services/allowlist.py` (NEW — service, transform/cache) — VERIF-01 / VERIF-02

**Analog:** combines `services/reminders.py` pure helpers (testable) + `sheets.py`
`to_thread`. RAM `set` cache, normalize, `is_allowed`. See RESEARCH Pattern 4
(RESEARCH.md:281-302). These are the pure-test surface (`test_allowlist_phase3.py`):
`_normalize` (strip/`@`/lower, D-10) and `_parse_manual_ids` (CSV → `set[int]`, D-12).
No in-repo cache precedent — the module-global `set` + refresh function is new but small.

---

### `database/db.py` (EXTEND — model/migration, CRUD) — D-02 / D-15 / COMM-01..03 / SCHED-03

**Analog:** `database/db.py` itself. Four established patterns to copy:

**1. Additive migration** (`database/db.py:11-19`) — for `reg_started.nudged_at TEXT` (D-15):
```python
async def _column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return any(row[1] == column_name for row in rows)

async def _ensure_column(db: aiosqlite.Connection, table_name: str, column_name: str, definition: str):
    if not await _column_exists(db, table_name, column_name):
        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
```
> Add `await _ensure_column(db, "reg_started", "nudged_at", "TEXT")` inside `init_db()`
> alongside the existing `_ensure_column` calls (`db.py:49-76`).

**2. `CREATE TABLE IF NOT EXISTS` inside `init_db`** (`database/db.py:78-105`) — the exact
shape for the new `scheduled_broadcasts` table (D-02). Note the AUTOINCREMENT PK + indexed
table idiom already in use:
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
        await db.execute('CREATE INDEX IF NOT EXISTS idx_coins_user ON coins(user_id)')
```
> RESEARCH.md:392-406 has the proposed `scheduled_broadcasts` DDL (status pending/sent/cancelled,
> `filter_spec` JSON, `scheduled_at` as `"%Y-%m-%d %H:%M:%S"`). `init_db` ends with
> `await db.commit()` (`db.py:107`) — new DDL goes before it.

**3. `reg_started` helpers — the exact shape for nudge scan + mark** (`database/db.py:403-425`):
```python
async def mark_reg_started(telegram_id: int, username: str | None):
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute('''
            INSERT INTO reg_started (telegram_id, username, started_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                started_at=excluded.started_at
        ''', (telegram_id, username, started_at))
        await db.commit()

async def clear_reg_started(telegram_id: int):
    ...

async def get_incomplete_user_ids() -> list[int]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT telegram_id FROM reg_started") as cursor:
            return [row[0] for row in await cursor.fetchall()]
```
> NEW `get_nudge_candidates(older_than_minutes)` + `mark_nudged(telegram_id)` follow this
> idiom exactly (RESEARCH.md:409-426). `started_at`/`nudged_at` stored as
> `datetime.now().strftime("%Y-%m-%d %H:%M:%S")` → lexicographic `<` cutoff is valid (db.py:404).

**4. Parameterized list-returning query** (`database/db.py:439-444`) — the model for the
COMM-01..03 dynamic filter query (build whitelisted `WHERE` + `?` params, return id list):
```python
async def get_non_subscriber_ids() -> list[int]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id FROM users WHERE subscribed = 0"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]
```
> Extend to `count_and_list_filtered(filters)` with a column whitelist
> (`{"city","university","status","source"}`) + `registration_date` after/before
> (RESEARCH Pattern 3, RESEARCH.md:259-279). `registration_date` is `"%Y-%m-%d %H:%M:%S"`
> (verified `registration.py:969`) so string compare `>= '2026-06-01'` is valid (COMM-02).
> Count preview (D-06) = `len(...)` of the returned list. **Never f-string the value** (Pitfall 5).

**5. `get_setting` / `set_setting` k/v store — all new toggles/thresholds** (`database/db.py:109-124`):
```python
async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
```
> Already imported in `admin.py` (`db.py` import block, admin.py:13-34) and used everywhere.
> Use for: `preselect_enabled`, `preselect_tab`, `preselect_fail_text`, `preselect_link`,
> `preselect_manual_ids`, `nudge_enabled`, `nudge_after_minutes`, `nudge_scan_minutes`,
> `nudge_text`, `allowlist_refresh_minutes` (RESEARCH.md:355-356).

---

### `handlers/admin.py` (EXTEND — controller) — COMM-01..04 / SCHED-01

**Analog:** `handlers/admin.py` itself. Four insertion points, four in-repo patterns.

**1. Add "🎯 По фильтру" menu entry** (`handlers/admin.py:768-783` AND `:800-810`) —
**both** `show_admin_broadcast` (callback) and `cmd_broadcast` (command) render the same
keyboard; add the new button row to **both** to keep them in sync:
```python
@router.callback_query(F.data == "admin_broadcast")
async def show_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Все пользователи", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="📄 По файлу в проекте", callback_data="broadcast_local")],
        [InlineKeyboardButton(text="🚫 Не подписаны на канал", callback_data="broadcast_unsubscribed")],
        [InlineKeyboardButton(text="📝 Не завершили регистрацию", callback_data="broadcast_incomplete")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await callback.message.edit_text("Выберите целевую аудиторию рассылки:", reply_markup=kb)
    await state.set_state(Broadcast.target_selection)
    await callback.answer()
```

**2. Filtered-segment → send handoff — copy `_start_segment_broadcast`** (`handlers/admin.py:870-914`).
This is the canonical "build user_ids → hand to Broadcast.message FSM" helper AND the
per-callback admin re-check precedent (V4 access control). The filter builder ends by
calling exactly this with the materialized id list:
```python
async def _start_segment_broadcast(callback, state, user_ids: list, prompt: str):
    # Callbacks are not covered by the message-level is_admin filter — re-check here (D-06 / T-04-03).
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_ids = list(set(user_ids))
    if not user_ids:
        await callback.message.edit_text("В этом сегменте сейчас нет пользователей.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(target_type="list", target_users=user_ids)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await state.set_state(Broadcast.message)
```
> Each new callback handler in the filter builder MUST repeat the `config.ADMIN_IDS`
> re-check (precedent at admin.py:872, 893, 906, 1067, 1078 — every callback in the file does).

**3. HARDEN `process_broadcast` 429 loop (COMM-04, D-07/D-08)** — current loop has NO 429
handling (`handlers/admin.py:1015-1033`):
```python
    count = 0
    blocked = 0
    status_msg = await message.answer(f"Начинаю рассылку на {len(users_ids)} пользователей...")
    for chat_id in users_ids:
        try:
            await message.send_copy(chat_id)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            blocked += 1
```
Replace the loop body with the **already-written** 429-safe pattern from `_welcome_flipped`
(`handlers/admin.py:1380-1393`) — `TelegramRetryAfter` is already imported (admin.py:35):
```python
async def _welcome_flipped(bot, ids: list):
    """Drain welcome sends for a mass approval, handling Telegram 429 (D-11)."""
    for tid in ids:
        try:
            await approve_user(bot, tid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await approve_user(bot, tid)
            except Exception as e2:
                logger.error(f"Mass-approve welcome retry failed for {tid}: {e2}")
        except Exception as e:
            logger.error(f"Mass-approve welcome failed for {tid}: {e}")
        await asyncio.sleep(0.05)
```
> Mapping to D-08: a `TelegramRetryAfter` that succeeds on retry must NOT increment `blocked`;
> only the genuine-failure `except Exception` branches do. RESEARCH.md:304-322 shows the
> exact `process_broadcast`-shaped adaptation. Extract the per-user classify decision into a
> pure helper for `test_broadcast_429_phase3.py` (RESEARCH Wave 0).
> NOTE: the album path `_wait_and_send_album` (admin.py:971-979) has the same bare-except loop
> — harden it too for consistency (it sends via `bot.send_media_group`).

**4. Mass-send dispatched via `asyncio.create_task` (background drain)** (`handlers/admin.py:1396-1407`)
— the model for "schedule now / fire scheduled broadcast" not blocking the handler:
```python
@router.callback_query(F.data == "appr_all_yes")
async def appr_all_yes(callback, state):
    ...
    ids = await approve_all_pending()  # atomic flip first (D-11)
    await callback.message.edit_text(f"✅ Одобрено: {len(ids)}. Рассылаю приветствия…", ...)
    asyncio.create_task(_welcome_flipped(callback.bot, ids))  # drain sends in background
    await callback.answer()
```

**5. Self-documenting settings registry — extend `APPROVAL_SETTINGS_DOC`** (`handlers/admin.py:1410-1437`).
Add all new Phase-3 `bot_settings` keys (preselect_*, nudge_*, allowlist_refresh_minutes)
to this list so `/settings_guide` documents them (D-16):
```python
APPROVAL_SETTINGS_DOC = [
    ("registration_mode", "Режим формы регистрации (full/short)", "short"),
    ...
    ("pending_reminder_interval", "Интервал напоминалки, сек", "1800"),
    ("reg_q_resume", "Запрос резюме в полной форме (on/off)", "off"),
]
```

---

### `handlers/registration.py` (EXTEND — controller) — VERIF-01 / VERIF-02

**Analog:** `cmd_start` itself — the existing guard ordering (`handlers/registration.py:527-570`).
The VERIF gate inserts AFTER the subscription check (ends ~line 540) and BEFORE the
`get_user` / registered-vs-rejected branch at line 553. Conditional on `preselect_enabled`
(D-13, default off → live flow untouched).

**Existing guard ordering to slot into** (`handlers/registration.py:527-553`):
```python
@router.message(Command("start"), StateFilter("*"))
async def cmd_start(message, state, bot, command=None):
    user_id = message.from_user.id
    logger.info(f"User {user_id} requested /start")

    # QW-02: observe-only subscription check — never blocks the user (D-04), never crashes /start (D-07).
    try:
        channel = await get_setting("contact_tg")
        if channel:
            result = await is_subscribed(bot, channel, user_id)
            if result is not None:
                await set_user_subscribed(user_id, result)
    except Exception as e:
        logger.warning(f"Subscription check skipped for {user_id}: {e}")

    # <<< NEW VERIF GATE INSERTS HERE (conditional on preselect_enabled) >>>

    user = await get_user(user_id)
    ...
    if user and (user.get("status") or "approved") != "rejected":
```
> RESEARCH.md:429-445 has the gate body: read `preselect_enabled`; if `on`, branch on
> `message.from_user.username is None` (prompt + manual-id check, VERIF-02) vs
> `is_allowed(uname)` set membership; `return` early with fail-text + link on miss.
> Follows the same fail-soft `try/except` + setting-driven discipline as the subscription
> check directly above it. `get_setting` is already imported in this module.

---

### `handlers/states.py` (EXTEND — FSM model) — COMM-01..03

**Analog:** the existing `Broadcast` group (`handlers/states.py:45-47`):
```python
class Broadcast(StatesGroup):
    target_selection = State()
    message = State()
```
> Add filter-builder states here (e.g. `filter_build = State()`, `filter_value = State()`)
> or a sibling `FilterBroadcast(StatesGroup)`. The builder terminates by setting
> `Broadcast.message` (reusing the existing send path), so adding states to `Broadcast`
> is the lowest-friction option. `Broadcast` is already imported in admin.py (admin.py:37).

---

### `main.py` (EXTEND — bootstrap) — SCHED-01 / SCHED-03 / allowlist startup

**Analog:** the existing startup-task wiring (`main.py:44-48`):
```python
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(pending_reminder_loop(bot))
    logger.info("Pending-application reminder task started")
    await dp.start_polling(bot)
```
> Per RESEARCH Pattern 2 (RESEARCH.md:247-257): add `init_scheduler(bot)` here (starts
> `AsyncIOScheduler`, jobstore auto-restores `date` jobs) BEFORE `start_polling`, alongside
> the existing `create_task(pending_reminder_loop(bot))` line. Also kick an initial
> `refresh_allowlist()` (create_task) at startup. `init_db()` already runs at main.py:28 —
> the new `scheduled_broadcasts` table + `nudged_at` column are created there.
> NOTE: `main.py:53-54` already sets `WindowsSelectorEventLoopPolicy` — APScheduler's
> AsyncIOScheduler attaches to this same loop; no change needed.

---

### `tests/test_*_phase3.py` (NEW — test) — all requirements

**Analog:** `tests/test_reminders_phase2.py` — pure-synchronous helper tests, NO async/DB/
Telegram (pytest-asyncio is version-broken, RESEARCH.md:465-470). Mirror Phase-2's
convention: extract pure side-effect-free helpers and unit-test those. Target files per
RESEARCH Wave 0 (RESEARCH.md:489-494):
`test_filters_phase3.py` (WHERE builder), `test_broadcast_429_phase3.py` (retry/block
classifier), `test_allowlist_phase3.py` (`_normalize` + `_parse_manual_ids`),
`test_nudge_phase3.py` (cutoff helper), `test_scheduler_helpers_phase3.py` (threshold parsers).
Run: `python -m pytest tests/test_<module>_phase3.py -x`.

---

## Shared Patterns

### Per-callback admin authorization (V4 access control)
**Source:** `handlers/admin.py:872-874` (and repeated at :893, :906, :1067, :1078, :1373, :1398)
**Apply to:** EVERY new callback handler in the filter builder + schedule UI.
```python
if callback.from_user.id not in config.ADMIN_IDS:
    await callback.answer("Недостаточно прав", show_alert=True)
    return
```
> The message-level `is_admin` filter does NOT cover callback queries. Every callback in
> admin.py re-checks; new ones must too.

### Fail-soft external I/O (gspread / Telegram send / settings)
**Source:** `services/reminders.py:43-48`, `services/sheets.py:42-44`, `registration.py:539-540`
**Apply to:** allowlist refresh (Pitfall 6 `WorksheetNotFound`), every per-user send loop,
the VERIF gate. Wrap in `try/except Exception`, `logger.error/warning`, never crash the
caller. Default-off settings (`preselect_enabled`) protect the live flow.

### Settings-driven thresholds via `bot_settings`
**Source:** `database/db.py:109-124` (`get_setting`/`set_setting`) + `reminders.py:22-28` (parse helpers)
**Apply to:** all Phase-3 toggles/intervals. Pattern: `get_setting(key)` returns `str | None`;
parse with a pure `_int_or_default` / `_enabled` helper; expose default in `_render_settings_guide`.

### ISO datetime string format (lexicographic-safe)
**Source:** `database/db.py:404`, `handlers/registration.py:969`
**Apply to:** `scheduled_broadcasts.scheduled_at`, `reg_started.nudged_at`, nudge cutoff,
`registration_date` filter (COMM-02). Always `datetime.now().strftime("%Y-%m-%d %H:%M:%S")`
so string `<`/`>=` comparisons are valid.

### Parameterized SQL only (V5 input validation)
**Source:** `database/db.py:439-444`, `:462-466` (all queries use `?` binds)
**Apply to:** the dynamic filter query (COMM-03). Whitelist column names, bind values with
`?` — never f-string a filter value (Pitfall 5).

---

## No Analog Found

Genuinely new mechanisms with no in-repo precedent (planner uses RESEARCH.md patterns):

| File / Concern | Role | Data Flow | Reason | RESEARCH ref |
|----------------|------|-----------|--------|--------------|
| `services/scheduler.py` APScheduler wiring | service | event-driven | First scheduler in the repo; `AsyncIOScheduler` + `SQLAlchemyJobStore` + module-level bot injection has no precedent (the `reminders.py` startup-task is only a structural analog) | RESEARCH.md:183-257 (Pattern 1 & 2) |
| In-RAM allowlist `set` cache | service | cache | No existing in-process cache pattern; module-global `set` + refresh is new | RESEARCH.md:281-302 (Pattern 4) |
| `scheduled_broadcasts` payload table semantics | model | CRUD | DDL shape copies `coins`/`reg_started`, but the "APScheduler stores id, table stores payload" split (D-02) is a new design | RESEARCH.md:392-406 |

> For all three, the DDL/structure idioms (CREATE TABLE, to_thread, logger/fail-soft) still
> copy from in-repo analogs above; only the orchestration is new.

---

## Metadata

**Analog search scope:** `services/`, `database/`, `handlers/`, `main.py`, `config.py`, `tests/`
**Files scanned (read this session):** `services/reminders.py`, `services/sheets.py`,
`handlers/states.py`, `main.py`, `config.py`, `handlers/admin.py` (targeted: 1-45, 760-919,
960-1089, 1360-1437), `database/db.py` (targeted: 1-135, 395-515), `handlers/registration.py`
(targeted: 520-630, 960-974)
**Key insight:** Every Phase-3 file maps to an exact or role-match in-repo analog. The only
non-precedented code is APScheduler orchestration + the RAM cache — both covered by RESEARCH
Patterns 1, 2, 4. No abstract guidance needed; planner can cite file:line directly.
**Pattern extraction date:** 2026-06-27
