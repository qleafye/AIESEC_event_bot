# Phase 4: Universal Modules - Pattern Map

**Mapped:** 2026-06-29
**Files analyzed:** 6 new/modified files
**Analogs found:** 6 / 6

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `database/db.py` | model/migration | CRUD | `database/db.py` itself (Phase 1–3 migration blocks) | exact |
| `handlers/registration.py` | handler | request-response | `handlers/registration.py` REG_FLOW + `_advance` + `finalize_registration` | exact |
| `handlers/states.py` | config | — | `handlers/states.py` existing StatesGroup classes | exact |
| `handlers/admin.py` | handler | request-response | `handlers/admin.py` tinder queue + EditSetting pattern | exact |
| `services/scheduler.py` | service | event-driven | `services/scheduler.py` `schedule_broadcast_job` + `init_scheduler` | exact |
| `handlers/payment.py` *(new, user-facing payment FSM)* | handler | request-response | `handlers/registration.py` resume upload handler (lines 626–638) + `handlers/admin.py` appr tinder shape | role-match |

---

## Pattern Assignments

### `database/db.py` — Phase 4 additive migrations

**Analog:** `database/db.py` Phase 1–3 migration blocks

**Imports / setup pattern** (lines 1–8):
```python
import logging
import os
from datetime import datetime

import aiosqlite
from config import config

logger = logging.getLogger(__name__)
```

**`_ensure_column` helper — copy verbatim** (lines 11–19):
```python
async def _column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return any(row[1] == column_name for row in rows)


async def _ensure_column(db: aiosqlite.Connection, table_name: str, column_name: str, definition: str):
    if not await _column_exists(db, table_name, column_name):
        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
```

**Phase 4 payment columns — copy the call style** (follow lines 59–77 for `_ensure_column` calls):
```python
# Phase 4 migrations (additive, idempotent — safe against ~590 live users)
await _ensure_column(db, "users", "payment_status", "TEXT DEFAULT 'not_paid'")
await _ensure_column(db, "users", "payment_option", "TEXT")
await _ensure_column(db, "users", "receipt_file_id", "TEXT")
await _ensure_column(db, "users", "payment_due", "TEXT")
await _ensure_column(db, "users", "paid_at", "TEXT")
```

**`user_consents` new table — copy the `CREATE TABLE IF NOT EXISTS` idiom** (follow lines 78–83 / 86–95 for shape):
```python
await db.execute('''
    CREATE TABLE IF NOT EXISTS user_consents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        consent_key TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        UNIQUE(user_id, consent_key)
    )
''')
await db.execute('CREATE INDEX IF NOT EXISTS idx_consents_user ON user_consents(user_id)')
```

**`get_setting` / `set_setting` pattern** (lines 126–141) — reuse unchanged for all Phase 4 settings keys (`event_type`, `payment_enabled`, `consent_enabled`, `payment_options`, `consent_list`, etc.):
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

**`add_user` `ON CONFLICT DO UPDATE` — extend, not replace** (lines 150–251): Add new payment columns to the `INSERT` column list and to the `DO UPDATE SET` block using `COALESCE(excluded.col, users.col)` for file_id fields (line 197 is the precedent: `resume_file_id=COALESCE(excluded.resume_file_id, users.resume_file_id)`).

---

### `handlers/registration.py` — REG_FLOW typed steps, consent, date validation

**Analog:** `handlers/registration.py` itself — `REG_FLOW`, `_get_enabled_steps`, `_advance`, `finalize_registration`, `approve_user`, resume handler

**Imports pattern** (lines 1–38):
```python
from config import config
from database.db import add_user, get_user, get_setting, mark_reg_started, clear_reg_started, set_user_subscribed, set_user_status
from handlers.states import Registration
```

**D-07: REG_FLOW — extend tuple to 3-tuple with `type` field** (current definition lines 59–91):

Current shape:
```python
REG_FLOW = [
    ("age", "reg_q_age"),
    ("email", "reg_q_email"),
    ...
    ("resume", "reg_q_resume"),
]
```

Phase 4 shape — add `type` as third element; default `"text"` keeps all existing steps working:
```python
REG_FLOW = [
    ("age",   "reg_q_age",   "text"),
    ("email", "reg_q_email", "text"),
    ...
    ("resume", "reg_q_resume", "text"),
    # inserted dynamically from consent_list setting when consent_enabled == "on":
    # ("consent:data_processing", "consent_enabled", "consent"),
]
```

Caller in `_get_enabled_steps` currently does `for step_key, setting_key in REG_FLOW` (line 171). Change to `for step_key, setting_key, *rest in REG_FLOW` (star-unpack keeps old 2-tuple rows working during migration).

**`_get_enabled_steps` — copy and extend** (lines 169–185):
```python
async def _get_enabled_steps(data: dict) -> list[str]:
    enabled = []
    for step_key, setting_key in REG_FLOW:  # change to *rest unpacking for typed steps
        if not await _is_step_enabled(setting_key):
            continue
        # existing skip conditions ...
        enabled.append(step_key)
    # Phase 4: append consent steps at the end when consent_enabled == "on"
    # (injected as synthetic "consent:<key>" entries, checked against consent_enabled flag)
    return enabled
```

**`_advance` — step progression, copy verbatim** (lines 292–310):
```python
async def _advance(after_step: str, message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    enabled = await _get_enabled_steps(data)
    try:
        idx = enabled.index(after_step)
        next_idx = idx + 1
    except ValueError:
        next_idx = 0
    if next_idx < len(enabled):
        step = data.get("_reg_step", 0) + 1
        total = data.get("_reg_total", len(enabled))
        await state.update_data(_reg_step=step)
        await _ask_step(enabled[next_idx], message, state, step, total)
    else:
        await message.answer(_build_summary(data), reply_markup=get_confirm_kb(), parse_mode="HTML")
        await state.set_state(Registration.confirm)
```

**D-07: `date` type step dispatcher — add branch to `_ask_step`** (follow the `elif step_key == "..."` chain in lines 188–290):
```python
elif step_key == "arrival_date":  # example date-type step
    await message.answer(f"{p} Дата приезда (ДД.ММ.ГГГГ):", reply_markup=get_cancel_kb())
    await state.set_state(Registration.arrival_date)
```

Handler validates format before calling `_advance` (follow `process_age` validation at lines 687–697):
```python
@router.message(Registration.arrival_date)
async def process_arrival_date(message: types.Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    try:
        datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        await message.answer("Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз.")
        return
    await state.update_data(arrival_date=raw)
    await _advance("arrival_date", message, state, bot)
```

**D-07/D-03: `consent` type step — ask step branch + handler:**
```python
# In _ask_step — add branch for "consent:<key>" synthetic step_key
elif step_key.startswith("consent:"):
    consent_key = step_key.split(":", 1)[1]
    pdf_file_id = await get_setting(f"consent_pdf_{consent_key}")
    label = <derive label from consent_list setting>
    caption = f"Согласие: {label}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принимаю", callback_data=f"consent_accept:{consent_key}")
    ]])
    if pdf_file_id:
        await message.answer_document(pdf_file_id, caption=caption, reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)
    await state.set_state(Registration.consent_pending)
    await state.update_data(_consent_key=consent_key)
```

**`resume_file_id` upload handler — copy shape for `receipt_file_id`** (lines 626–638):
```python
# ORIGINAL (resume — D-10 precedent):
@router.message(Registration.resume, F.document)
async def process_resume(message: types.Message, state: FSMContext, bot: Bot):
    if not _is_allowed_resume(message.document.file_name):
        await message.answer("Принимаются только PDF или DOCX. Прикрепи файл ещё раз.")
        return
    await state.update_data(resume_file_id=message.document.file_id)  # file_id only, no download (D-10)
    await _advance("resume", message, state, bot)

# MIRROR for receipt (D-11 — PDF document OR photo):
@router.message(Registration.receipt_upload, F.document)
async def process_receipt_document(message: types.Message, state: FSMContext, bot: Bot):
    if message.document.mime_type != "application/pdf":
        await message.answer("Принимается только PDF. Для скриншота используй функцию отправки фото.")
        return
    await state.update_data(receipt_file_id=message.document.file_id)
    await _finalize_receipt_upload(message, state, bot)

@router.message(Registration.receipt_upload, F.photo)
async def process_receipt_photo(message: types.Message, state: FSMContext, bot: Bot):
    await state.update_data(receipt_file_id=message.photo[-1].file_id)
    await _finalize_receipt_upload(message, state, bot)

@router.message(Registration.receipt_upload)
async def process_receipt_invalid(message: types.Message, state: FSMContext):
    await message.answer("Отправь PDF-документ или скриншот оплаты (фото).")
```

**D-09: `approve_user` — hook payment step after approval** (lines 974–991):
```python
async def approve_user(bot: Bot, telegram_id: int):
    """Phase 4: check payment_enabled flag; if ON, dispatch payment step instead of
    sending complete_text directly. Original complete_text path runs only when payment
    is OFF or already paid."""
    try:
        if await get_setting("payment_enabled") == "on":
            await _start_payment_step(bot, telegram_id)
            return
        # existing path:
        complete_text = await get_setting("reg_complete_text") or "Регистрация завершена! ..."
        await bot.send_message(telegram_id, complete_text, reply_markup=await get_main_menu_kb(), parse_mode="HTML")
        # bonus block unchanged ...
    except Exception as e:
        logger.error(f"Failed to send approval welcome to {telegram_id}: {e}")
```

**D-03/D-02: consent acceptance — store to `user_consents` table** (follow `_store_choice` shape at lines 894–900):
The callback handler for `consent_accept:<key>` must:
1. Write a row to `user_consents(user_id, consent_key, accepted_at)`.
2. Call `_advance(f"consent:{consent_key}", message, state, bot)` to move to next step.

---

### `handlers/states.py` — new FSM states for Phase 4

**Analog:** `handlers/states.py` existing StatesGroup classes (lines 1–59)

**Pattern — copy the StatesGroup idiom:**
```python
from aiogram.fsm.state import StatesGroup, State

# Add to Registration StatesGroup:
class Registration(StatesGroup):
    # ... existing states unchanged ...
    consent_pending = State()   # waiting for "Принимаю" callback on a consent card
    receipt_upload  = State()   # waiting for PDF or photo receipt
    payment_option  = State()   # waiting for user to pick a payment option
```

The `Approval` StatesGroup stays unchanged. Add a new group for receipt review in admin:
```python
class ReceiptReview(StatesGroup):
    # No sub-states needed — tinder queue is callback-only (mirrors Approval which has
    # only `reason` sub-state for the reject reason text)
    reject_reason = State()
```

---

### `handlers/admin.py` — event type/module settings + consent/payment options editing + receipt tinder queue

**Analog:** `handlers/admin.py` existing patterns

**Admin guard pattern — copy on every callback** (precedent at lines 281–283, 457–459, 561–563, 1621–1625, etc.):
```python
if callback.from_user.id not in config.ADMIN_IDS:
    await callback.answer("Недостаточно прав", show_alert=True)
    return
```

**D-06/D-05: event type + module toggles — add to `SETTINGS_FIELDS` list** (lines 330–342):
```python
SETTINGS_FIELDS = [
    # ... existing fields unchanged ...
    ("event_type",       "🎭 Тип события",   "Тип: Forum / Conference / Custom (влияет на пресеты модулей)"),
    ("consent_list",     "📋 Список согласий","Согласия — каждое с новой строки: label|pdf_key"),
    ("payment_options",  "💳 Варианты оплаты","Варианты оплаты — каждый с новой строки: Название|Цена"),
    ("payment_requisites","💰 Реквизиты",     "Текст с реквизитами оплаты (HTML)"),
    ("payment_deadline", "📅 Дедлайн оплаты","Дата дедлайна ДД.ММ.ГГГГ ЧЧ:ММ"),
]
```

**Toggle module flags — copy `_toggle_approval_setting` shape** (lines 483–493):
```python
async def _toggle_approval_setting(callback: types.CallbackQuery, key: str, default: str, title: str):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    current = await get_setting(key) or default
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{title}: {label}")
    # re-render settings panel
```

Use the same pattern for `payment_enabled`, `consent_enabled` toggles.

**`EditSetting.waiting_for_value` — consent list + payment options are plain newline text** (lines 559–581, 748–765):
```python
# Settings edit start — copy verbatim for consent / payment option keys:
@router.callback_query(F.data.startswith("settings_edit:"))
async def settings_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    ...
    await state.set_state(EditSetting.waiting_for_value)
    await state.update_data(setting_key=key)
    await callback.answer()

# Value receive handler (line 748):
@router.message(EditSetting.waiting_for_value, is_admin)
async def settings_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]
    value = (message.text or "").strip()
    if value == "-":
        await delete_setting(key)
    else:
        await set_setting(key, value)
    await state.clear()
    text = await render_settings_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
```

`consent_list` and `payment_options` are NOT in `HTML_SETTINGS` (line 746), so plain `.text` is used — same as `source_options`.

**D-12: Receipt verification tinder queue — clone the APP-04 tinder shape exactly** (lines 1551–1724):

Key pieces to clone:

`_parse_appr` helper shape (lines 1551–1559):
```python
def _parse_rcpt(data: str) -> tuple[str, int | None]:
    """'rcpt_confirm:123' -> ('rcpt_confirm', 123)."""
    if ":" in data:
        prefix, uid = data.split(":", 1)
        try:
            return prefix, int(uid)
        except ValueError:
            return prefix, None
    return data, None
```

Card render (mirror `_render_application_card` at lines 1562–1584):
```python
def _render_receipt_card(user: dict, position: int, total: int) -> str:
    lines = [f"🧾 <b>Чек {position}/{total}</b>", ""]
    lines.append(f"👤 {html_module.escape(str(user.get('full_name', '—')))}")
    lines.append(f"💳 Вариант: {html_module.escape(str(user.get('payment_option') or '—'))}")
    lines.append(f"📎 Чек: {'загружен' if user.get('receipt_file_id') else 'нет'}")
    return "\n".join(lines)
```

Keyboard (mirror `_appr_card_kb` at lines 1587–1600):
```python
def _rcpt_card_kb(uid: int, has_receipt: bool, total: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rcpt_confirm:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить",   callback_data=f"rcpt_reject:{uid}"),
    ]]
    third = []
    if has_receipt:
        third.append(InlineKeyboardButton(text="🧾 Чек", callback_data=f"rcpt_view:{uid}"))
    third.append(InlineKeyboardButton(text="⏭ Следующий", callback_data=f"rcpt_skip:{uid}"))
    rows.append(third)
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

`_show_current_receipt_card` (mirror `_show_current_card` at lines 1603–1618):
```python
async def _show_current_receipt_card(target: types.Message, state: FSMContext):
    # query users WHERE payment_status = 'receipt_sent' ORDER BY ... LIMIT 50
    pending = await get_receipt_pending_users(limit=50)
    skipped = set((await state.get_data()).get("rcpt_skipped", []))
    visible = [u for u in pending if u["telegram_id"] not in skipped]
    total = await get_receipt_pending_count()
    if not visible:
        await target.answer("✅ Чеков нет.", reply_markup=build_admin_keyboard())
        return
    current = visible[0]
    position = total - len(visible) + 1
    await target.answer(
        _render_receipt_card(current, position, total),
        parse_mode="HTML",
        reply_markup=_rcpt_card_kb(current["telegram_id"], bool(current.get("receipt_file_id")), total),
    )
```

Confirm/reject handlers (mirror `appr_approve`/`appr_reject_start` at lines 1668–1724):
- Confirm → set `payment_status = 'paid'`, set `paid_at`, cancel payment reminders, send "Оплата подтверждена!" to user.
- Reject → set `payment_status = 'not_paid'`, allow re-upload, optionally message user reason.

Both require `if callback.from_user.id not in config.ADMIN_IDS` guard.

---

### `services/scheduler.py` — PAY-06 payment deadline reminders

**Analog:** `services/scheduler.py` `schedule_broadcast_job` + `init_scheduler` (lines 76–107, 170–175)

**Imports pattern** (lines 1–25) — copy unchanged; add no new imports:
```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from aiogram.exceptions import TelegramRetryAfter
from config import config
from database.db import get_setting
```

**`schedule_broadcast_job` — copy the `"date"` trigger shape for payment reminders** (lines 170–175):
```python
def schedule_broadcast_job(broadcast_id: int, run_at: datetime):
    get_scheduler().add_job(
        send_scheduled_broadcast, "date",
        run_date=run_at, args=[broadcast_id],
        id=f"bcast_{broadcast_id}", replace_existing=True,
    )
```

Phase 4 payment reminder jobs — same `"date"` trigger, same `replace_existing=True`:
```python
def schedule_payment_reminder(user_id: int, run_at: datetime, label: str):
    """Register a one-shot payment-deadline reminder for one user.
    label: 'minus3d' | 'minus1d' — disambiguates the job id."""
    get_scheduler().add_job(
        send_payment_reminder, "date",
        run_date=run_at, args=[user_id],
        id=f"pay_reminder_{user_id}_{label}", replace_existing=True,
    )

def cancel_payment_reminders(user_id: int):
    """Cancel all outstanding payment reminder jobs for a user (called on receipt confirm)."""
    for label in ("minus3d", "minus1d"):
        try:
            get_scheduler().remove_job(f"pay_reminder_{user_id}_{label}")
        except Exception:
            pass
```

**Job target function — copy `send_scheduled_broadcast` guard pattern** (lines 131–167):
```python
async def send_payment_reminder(user_id: int):
    """Date-job target: send deadline reminder if user not yet paid (D-13 SC#5)."""
    try:
        from database.db import get_user
        user = await get_user(user_id)
        if not user or user.get("payment_status") in ("paid", None):
            return  # SC#5: never fire if already paid
        text = await get_setting("payment_reminder_text") or (
            "⏰ Напоминание: срок оплаты участия истекает скоро. "
            "Пожалуйста, загрузи чек оплаты через бота."
        )
        await _safe_send(lambda cid: _bot.send_message(cid, text), user_id)
    except Exception as e:
        logger.error(f"send_payment_reminder({user_id}) failed: {e}")
```

**`init_scheduler` — register payment sweep if needed; date jobs restore automatically** (lines 76–107):

Date reminder jobs are re-registered per-user at receipt-upload time via `schedule_payment_reminder()`. They persist in `data/jobs.sqlite` and auto-restore on boot without changes to `init_scheduler`. No new interval job is required unless an `overdue` sweep is added (Claude's Discretion item in CONTEXT.md).

---

### `handlers/payment.py` (new — user-facing payment FSM)

**Analog:** `handlers/registration.py` resume upload handler (lines 626–638) + `_store_choice` (lines 894–900)

This file contains the user-facing payment step: choose payment option → display requisites → upload receipt. It is invoked from `approve_user()` in registration.py when `payment_enabled == "on"`.

**Imports pattern — mirror registration.py style** (lines 1–38):
```python
from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from config import config
from database.db import get_setting, get_user
from handlers.states import Registration
from services.scheduler import schedule_payment_reminder, cancel_payment_reminders
```

**Payment option selection — mirror `_store_choice` shape** (lines 894–900):
```python
@router.callback_query(F.data.startswith("pay_option:"))
# IMPORTANT: No Registration.payment_option state filter — start_payment_step is called
# from approve_user() without FSMContext, so that state is never set. Adding the filter
# silently blocks all pay_option callbacks (user taps button, nothing fires, no error).
# See 04-04-PLAN.md Task 1 action for the complete rationale.
async def process_payment_option(callback: types.CallbackQuery, state: FSMContext):
    option_key = callback.data.split(":", 1)[1]
    await state.update_data(payment_option=option_key)
    # display requisites from get_setting("payment_requisites")
    # transition to Registration.receipt_upload
    await state.set_state(Registration.receipt_upload)
    await callback.answer()
```

**Receipt upload handlers — copy verbatim from the shape described above** (mirror lines 626–638):
- Document (PDF only) → store `receipt_file_id`, update `payment_status = 'receipt_sent'`, schedule reminders.
- Photo → store `receipt_file_id` (highest-res: `message.photo[-1].file_id`), same outcome.
- Non-matching input → re-prompt (mirror line 636–638).

**Error / fail-soft pattern — copy try/except from `approve_user`** (lines 978–991):
```python
try:
    ...
except Exception as e:
    logger.error(f"Failed to start payment step for {telegram_id}: {e}")
```

---

## Shared Patterns

### Admin guard on every callback
**Source:** `handlers/admin.py` — appears at lines 281, 457, 484, 507, 520, 537, 561, 770, 787, 929, 1623, 1633, 1651, 1669, 1688, 1728, 1745, 1771
**Apply to:** All callback query handlers in admin.py and payment.py that touch admin actions (receipt confirm/reject/view, module toggles, settings edits)
```python
if callback.from_user.id not in config.ADMIN_IDS:
    await callback.answer("Недостаточно прав", show_alert=True)
    return
```

### Settings read-on-fly pattern (default OFF for new modules)
**Source:** `handlers/registration.py` `_is_step_enabled` (lines 162–166) + Phase-3 preselect precedent
**Apply to:** `consent_enabled`, `payment_enabled`, `event_type` checks in registration.py and approve_user()
```python
async def _is_module_enabled(key: str) -> bool:
    val = await get_setting(key)
    return val == "on"  # None / absent / any other value → OFF (D-15 fail-safe)
```

### Additive migration (never break live rows)
**Source:** `database/db.py` `_ensure_column` (lines 11–19) + comment at lines 59 and 64
**Apply to:** All new `_ensure_column` calls in Phase 4 migration block
- Use `DEFAULT 'not_paid'` on `payment_status` so existing rows get a defined initial value.
- Use `DEFAULT NULL` (no DEFAULT clause) on optional text fields like `receipt_file_id`, `payment_option`, etc.
- Never use `ALTER TABLE ... DROP COLUMN` or `INSERT OR REPLACE` on `users`.

### File_id storage (never download)
**Source:** `handlers/registration.py` line 631 comment `# file_id only, no download (D-10)`
**Apply to:** `receipt_file_id` capture in payment.py, consent PDF `file_id` in bot_settings
```python
await state.update_data(resume_file_id=message.document.file_id)  # file_id only, no download
```

### Fail-soft try/except for all user-facing sends
**Source:** `handlers/registration.py` `approve_user` (lines 978–991), `finalize_registration` (lines 1025–1072)
**Apply to:** `approve_user` payment hook, consent acceptance notifications, payment reminder sends
```python
try:
    await bot.send_message(telegram_id, text, ...)
except Exception as e:
    logger.error(f"Failed to send ... to {telegram_id}: {e}")
```

### Scheduler date job — picklable int arg only
**Source:** `services/scheduler.py` Pitfall 3 comment (lines 6–10), `send_scheduled_broadcast` signature (line 131)
**Apply to:** `send_payment_reminder` — take only `user_id: int`, read bot from `_bot` module global
```python
async def send_payment_reminder(user_id: int):  # int arg only — picklable
    # read _bot from module global, never accept Bot as arg
```

---

## No Analog Found

All files in Phase 4 have direct analogs in the existing codebase. The receipt tinder queue (`handlers/admin.py` addition) and the payment FSM (`handlers/payment.py`) are both clones of existing patterns.

---

## Metadata

**Analog search scope:** `handlers/`, `database/`, `services/`
**Files scanned:** 7 source files (registration.py, admin.py, states.py, db.py, scheduler.py, reminders.py, allowlist.py)
**Pattern extraction date:** 2026-06-29
