# Phase 2: Approval Flow - Context

**Gathered:** 2026-06-26
**Updated:** 2026-06-26 (reconciled against master plan `PLAN_YOULEAD_TZ.md`; resolved APP-02↔D-15 notify conflict, rejected re-apply, mass-approve sends, resume-toggle scope)
**Status:** Ready for planning (discussion done; coding deferred to next session — token budget)

<domain>
## Phase Boundary

Phase 2 delivers manager-side moderation of registrations: (1) a settings-driven approval gate (`status='pending'` until a manager approves), (2) a paginated "tinder" application-review UI in the admin panel driven by the oldest `status='pending'` DB row, (3) atomic approve/reject with double-approval protection, (4) batch "Одобрить все N" with confirmation, (5) a periodic (anti-storm) pending-count reminder, and (6) a self-documenting settings command so a new admin can see every configurable toggle without reading git/README.

Builds directly on Phase 1: the `status` column (DEFAULT 'approved'), `resume_file_id`, and the confirm step before `finalize_registration` are already live. Existing ~590 users are `status='approved'` and MUST stay unaffected.

**Requirements:** APP-01..APP-08 (verify exact text against `.planning/REQUIREMENTS.md` at plan time).
</domain>

<decisions>
## Implementation Decisions

### Gating (APP-01, APP-07)
- **D-01:** Approval is **settings-driven via in-bot toggles** (no hardcoding). Two keys in `bot_settings`: `short_approval` and `full_approval`, each `auto` | `manual`.
- **D-02:** Defaults: `full_approval = manual`, `short_approval = auto`. (Full-form = delegates needing review; short-form auto-approved.)
- **D-03:** `finalize_registration` sets `status` based on form type + the matching setting: full form + `full_approval=manual` → `pending`; short form + `short_approval=manual` → `pending`; otherwise `approved`. Existing column DEFAULT 'approved' keeps the 590 live users intact.
- **D-04:** After registration, a `pending` user sees a "заявка отправлена / на рассмотрении" message and **no main menu**. An `approved` user gets the existing complete-text + main menu flow.
- **D-05:** `ensure_registered(message)` (user_actions.py:19) returns True **only** when the user exists AND `status='approved'`. `pending` → "заявка на рассмотрении" message + False; `rejected` → rejection text + False. This gates ALL menu actions (decided: manual approval is meaningless without gating; impact is limited because short=auto).
- **D-05a (NEW — rejected re-apply):** A `rejected` user who runs `/start` again **can re-register** → new submission resets `status` to `pending` (or `approved` if that form is auto), creating a fresh application the manager sees again. `/start` for rejected does NOT dead-end on the rejection text — it re-enters the registration flow. `ensure_registered` still returns False for `rejected` (menu stays gated until re-approval). Implementation: on re-submit, `submit_application`/`finalize_registration` overwrites `status` via the existing ON CONFLICT DO UPDATE path (Phase 1 D-17). Clear any prior `reject reason` on re-submit.

### Application review UI (APP-02, APP-03, APP-04)
- **D-06:** Admin panel gets a "📋 Заявки" entry. Shows **one paginated application card at a time**.
- **D-07:** Queue is **DB-driven**: always the oldest `status='pending'` row (ORDER BY registration_date / id). NOT FSM page offsets (which reset on restart). "Пропустить" advances within the current admin session without changing status (track skipped ids in FSM state so they aren't re-shown until refresh).
- **D-08:** Card buttons: **Одобрить / Отклонить / Пропустить / Одобрить все N** (N = current pending count).
- **D-09:** When the applicant has a stored `resume_file_id` (Phase 1 QW-03), the card re-sends that file via `answer_document` so the manager can open it. This delivers the **manager-side viewing half of QW-03** (deferred from Phase 1).

### Atomic approval (APP-05)
- **D-10:** Approve = **atomic** `UPDATE users SET status='approved' WHERE telegram_id=? AND status='pending'`; check `rowcount`. If `rowcount=0` the row was already handled by another manager → **send nothing** (prevents duplicate approval message). Only on `rowcount=1` send the welcome content + main menu to the user (exactly once).
- **D-11:** "Одобрить все N" shows a **confirmation dialog** before executing. **DB-flip-first, queue the sends:** atomically flip all pending → approved in one `UPDATE ... WHERE status='pending'` (capture the flipped telegram_ids via RETURNING or a pre-SELECT snapshot), so the queue clears immediately and the manager UI returns fast. THEN drain the welcome+menu sends in a background pass that handles Telegram rate limits — catch `TelegramRetryAfter` and `sleep(retry_after + 1)` before retrying (reuse the existing broadcast loop's 429 pattern in admin.py). At 590+ scale a mass approve can be hundreds of sends; sequential-with-sleep is acceptable as long as the DB flip is not blocked on send completion. Each flipped user receives welcome + main menu exactly once (the atomic flip guarantees no double-send even if two managers race).

### Rejection (APP-03)
- **D-12:** "Отклонить" prompts the manager for a **free-text reason** (FSM step). Sets `status='rejected'`. The user receives a configurable rejection text (`reject_text` setting) **+ the manager's reason**. html.escape the reason before sending.

### Submission notification + periodic reminder (APP-02, APP-08) — admin-configurable
- **D-13:** A single **asyncio background task** (started in main.py startup; NO APScheduler this phase) sends the **standing pending count** to all `config.ADMIN_IDS`. One message per interval, NOT one push per submission. Fires only when pending count > 0.
- **D-14:** Configurable via settings: `pending_reminder_enabled` (on|off, default on) and `pending_reminder_interval` (seconds). **Default interval = 1800s (30 min)** — storm-safe; admin can lower via the settings command.
- **D-15 (REVISED — resolves APP-02↔anti-storm conflict):** The per-submission manager notification is **admin-configurable via /admin**, not hardcoded-suppressed. New setting `pending_notify_mode` ∈ {`instant`, `batched`}, **default `batched`**:
  - `instant` — each `pending` submission fires an immediate admin ping (literal APP-02 "менеджер получает уведомление"). Admin opt-in for low-volume events.
  - `batched` (default) — NO per-submission ping; the manager learns of new pendings through the periodic reminder (D-13/D-14) every `pending_reminder_interval`. This is the storm-safe path for the 590+ surge. Satisfies APP-02 as "notified on a batched cadence."
  - The existing instant admin ping for **auto-approved** registrations (registration.py ~684-705) is unaffected — only the `pending`-submission ping is gated by `pending_notify_mode`.
  - Expose `pending_notify_mode` in the D-16 settings-guide command.

### Self-documenting settings (cross-cutting, user-requested)
- **D-16:** Add an admin command (e.g. `/settings_guide` or extend the `/admin` panel) that **lists every configurable `bot_settings` key with a human description and current value** — so a new admin understands what to configure and where, without reading git/README. Cover at minimum: `short_approval`, `full_approval`, `reject_text`, `pending_notify_mode`, `pending_reminder_enabled`, `pending_reminder_interval`, plus the existing REG_FLOW toggles and texts (incl. Phase 1 `reg_q_resume`).

### Resume toggle — scope clarification (NOT a Phase 2 decision)
- **D-09a (carry-over, no new work):** The "Резюме: Вкл/Выкл" per-form toggle in `PLAN_YOULEAD_TZ.md` (line 139) is **already delivered by Phase 1** as `reg_q_resume` (Phase 1 D-08: toggleable REG_FLOW step, default off, full form, PDF/DOCX, mandatory when on). Phase 2 adds ONLY the **manager-side view** (D-09: re-send the stored `resume_file_id` via `answer_document` on the application card). No new resume setting or upload logic in Phase 2.

### Claude's Discretion (resolve at plan/code time)
- Exact pagination/skip-tracking mechanism; "Одобрить все N" implementation (loop vs bulk UPDATE + RETURNING); whether auto-approved registrations keep the instant admin ping or also defer to reminder; exact card layout/copy; settings-guide command name and formatting; where the reminder task is registered in main.py.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents/coder MUST read before implementing.**

### Plan & requirements
- `.planning/REQUIREMENTS.md` §Approval — APP-01..APP-08 (verified: each maps to a decision above; APP-07 = moderation toggle only, NOT the REG_DEFAULTS_SHORT/FULL question-set split)
- `.planning/ROADMAP.md` §Phase 2 — goal + 5 success criteria (criterion 2 includes the QW-03 resume-view via answer_document)
- `PLAN_YOULEAD_TZ.md` — master plan / source TЗ digest. §"регистрация → заявки" (approval architecture, tinder UI mock lines 103-121), §"Раздельные настройки для форм" (per-form settings — note: full question-set split is broader than Phase 2; APP-07 is only the moderation toggle), §"Открытые вопросы" (баллы слетали → transactions log already addressed by Phase 1 coins ledger).

### Existing code (source of truth)
- `database/db.py` — `add_user` (ON CONFLICT, status excluded — Phase 1), `get_setting/set_setting`, `get_user`, `get_user_by_username`. NEW needed: `get_pending_users()` (oldest first), `approve_user_atomic(telegram_id) -> bool` (rowcount guard), `reject_user(telegram_id)`, `get_pending_count()`.
- `handlers/registration.py` — `finalize_registration` (~650; sets status here, suppress pending ping), confirm step already before it; `_build_summary` (reuse for the application card).
- `handlers/user_actions.py` — `ensure_registered` (19; add status check).
- `handlers/admin.py` — `is_admin`, `build_admin_keyboard` (add "Заявки"), broadcast/callback patterns, `process_broadcast_local_file` (FSM reason-input analog), `Broadcast` FSM style for a new `Approval` FSM (reason input).
- `handlers/states.py` — add an `Approval` StatesGroup (e.g. `reason = State()`).
- `keyboards/builders.py` — `get_main_menu_kb` (shown only to approved), confirm/inline kb patterns.
- `main.py` — startup (register the asyncio reminder task near dp.start_polling).

### Phase 1 carry-over
- `.planning/phases/01-db-foundation-quick-wins-coins/01-CONTEXT.md` — D-17/D-18 (status semantics), QW-03 resume storage (resume_file_id) now consumed by D-09 here.
</canonical_refs>

<specifics>
## Specific Ideas
- Pending submitted message: e.g. "✅ Заявка отправлена! Менеджер рассмотрит её в ближайшее время."
- Pending gated message (ensure_registered): "⏳ Твоя заявка на рассмотрении. Доступ откроется после одобрения."
- Reminder message: "📋 Заявок в ожидании: N. Открой /admin → Заявки."
- Settings keys defaults: `full_approval=manual`, `short_approval=auto`, `pending_notify_mode=batched`, `pending_reminder_enabled=on`, `pending_reminder_interval=1800`, `reject_text` (configurable prefix).
- Rejected re-apply: on `/start` a rejected user re-enters registration; new submission overwrites status (pending/approved per form). Prior reject reason cleared.
- Mass approve: DB flip-all-pending first (fast UI return), then background welcome-send drain with `TelegramRetryAfter` handling.
</specifics>

<deferred>
## Deferred Ideas
- APScheduler-based scheduling (SCHED-01/03 automated nudges) → Phase 3. Phase 2 reminder is a plain asyncio loop only.
- Pre-Phase-3 blocker still open (from STATE.md): confirm APScheduler persistence approach before Phase 3.
</deferred>

---

*Phase: 02-approval-flow*
*Context gathered: 2026-06-26; updated 2026-06-26 (reconciled vs PLAN_YOULEAD_TZ.md). Implementation pending — Phase 1 not yet executed.*
</context>
