# Phase 4: Universal Modules - Context

**Gathered:** 2026-06-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 4 makes the bot **conference-ready** by adding three settings-driven modules on top of the existing registration/approval/scheduler code — all toggleable in the admin panel with **no code deployment between events** (ROADMAP SC#1):

1. **Event modularity (MOD-01/02/03)** — admin picks an event type (Форум / Конференция / Кастом) which **presets** module toggles (payment / consent / reminders), each still manually overridable; REG_FLOW gains typed steps `date` (ДД.ММ.ГГГГ validation) and `consent` (auto-«Принимаю»).
2. **Consent module (CONS-01/02)** — a configurable list of consents, each shown as its own step with «Принимаю» before the form finalizes; acceptances are stored per-user.
3. **Payment module (PAY-01..06)** — options/tariffs (`{name, price}`), payment step **after approval**, requisites message, PDF-or-photo receipt upload, tinder receipt verification, payment statuses, and deadline reminders on the Phase-3 scheduler.

**Requirements (11):** MOD-01, MOD-02, MOD-03, CONS-01, CONS-02, PAY-01, PAY-02, PAY-03, PAY-04, PAY-05, PAY-06.

**MVP slicing:** Slice 1 = modularity + consent. Payment is the LAST slice. All modules default **OFF** so the ~590 live users + the in-flight RusCo free-flow registration are untouched.

**Out of scope:** resume disk-storage / File Browser module (design seed R-01..R-04 — deferred); gamification (v2); roles (v2).

</domain>

<decisions>
## Implementation Decisions

### Consent module (CONS-01/02)
- **D-01:** Consent list is **fully configurable with a per-item toggle** (CONS-01), reusing the existing configurable-list pattern (`source_options` → newline text in `bot_settings`, edited via `EditSetting.waiting_for_value`). Seed defaults: обработка данных / политика / фото-видео. Admin can add custom items and disable any.
- **D-02:** Acceptances stored in a **new normalized table `user_consents(user_id, consent_key, accepted_at)`** — gives a per-item timestamped audit trail (personal-data compliance) and adapts to an arbitrary consent list. Created via `CREATE TABLE IF NOT EXISTS`.
- **D-03:** Each consent is its **own REG_FLOW step with a «Принимаю» button** (NOT one bulk "accept all"). The `consent` step type blocks progression until tapped — skipping any consent is impossible (ROADMAP SC#2). Mirrors the RusCo'26 UX («✅ Согласие X принято» per item). Shown at the end, before finalize (CONS-02).
- **D-04:** Full consent/policy text is delivered as an **attached PDF** (a `file_id` stored in a `bot_settings` key per consent), with a short inline caption + the «Принимаю» button — not long inline text walls. (User: «только пдф».)

### Event modularity (MOD-01/02/03)
- **D-05:** Event type **presets the module toggles, then each toggle is manually overridable.** Конференция → payment+consent default ON; Форум → OFF; Кастом → all manual. Type is a convenience preset, not a hard binding.
- **D-06:** Event type + all module flags live in **`bot_settings`** (`event_type`, `payment_enabled`, `consent_enabled`, …) and are **read on the fly** so changes take effect for new registrations immediately, no redeploy (SC#1). Reuses the existing settings store.
- **D-07:** REG_FLOW engine is **extended with a `type` field per step** (`text` = current default / `date` / `consent`). A dispatcher branches on type: `date` validates `ДД.ММ.ГГГГ` (MOD-02), `consent` renders the PDF + «Принимаю» (MOD-03). Existing untyped steps keep working (backward compatible) — current `REG_FLOW` is a list of `(step_key, setting_key)` tuples (registration.py:59).

### Payment module (PAY-01..06)
- **D-08:** Pricing model is **options/tickets, NOT days** (seed N-02, confirmed). An event = a list of `{name, price}` options; `price=0` = free. Stored as newline text in `bot_settings` (reuse `source_options` pattern). Most events = a single option. A "paid extra day" = a second option, no calendar entity.
- **D-09:** Payment step triggers **after approval** (ROADMAP SC#3; ties to the Phase-2 approval flow). With moderation off (auto-approve) it fires immediately, so this works for both moderated and open events.
- **D-10:** Payment fields are **additive columns on `users`** via `_ensure_column`: `payment_status` (not_paid / receipt_sent / paid / overdue — PAY-05), `payment_option`, `receipt_file_id`, `payment_due`, `paid_at`. One payment per user (no separate payments table).
- **D-11:** Receipt accepts a **PDF document OR a photo/screenshot** (user clarification — relaxes ROADMAP SC#3's literal "PDF-only"). Accept Telegram `document` with PDF mime + `photo`; reject all other document types with a clear user-facing error.
- **D-12:** Receipt verification reuses the **tinder queue pattern from APP-04** (Подтвердить / Отклонить / Следующий); confirm → status `paid` + cancel that user's payment reminders; reject → user can re-upload (ROADMAP SC#4).
- **D-13:** Deadline reminders (PAY-06, deadline−3d and deadline−1d) reuse the **Phase-3 `AsyncIOScheduler`** (`services/scheduler.py`) — no new scheduling mechanism. Reminders must NOT fire if `payment_status` is already `paid` (SC#5).

### Build order / rollout
- **D-14:** **Slice 1 = modularity + consent; payment = last slice.** RusCo is launching now on the free/open flow and the payment content (requisites, penalty schedule) is not ready.
- **D-15:** Build with **placeholder defaults; all modules default OFF.** Code never waits on organizer content — admin fills texts/PDFs/requisites/penalties later via settings. Turning a module ON is a deliberate admin act; the live flow stays byte-identical when OFF (same posture as Phase-3 preselect gate).

### Claude's Discretion
- Exact `bot_settings` key names and the per-consent PDF key naming scheme.
- Whether the consent-list serialization is newline `label|pdf_key` pairs vs separate keyed settings (planner's call).
- Penalty-schedule (damage fee + cancellation dates/amounts, PAY-01) serialization format in `bot_settings`.
- Exact admin-UI placement of the event-type/module-toggle panel and the receipt tinder queue.
- `overdue` transition mechanism (scheduler sweep vs lazy-on-read).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` § "Phase 4: Universal Modules" — goal, 5 success criteria, requirement list, depends-on.
- `.planning/REQUIREMENTS.md` — MOD-01/02/03, CONS-01/02, PAY-01..06 text.

### Design seeds (Phase-4 specific, already captured)
- `.planning/phases/04-universal-modules/04-PAYMENT-NOTES.md` — N-01..N-04: payment-as-toggle, options-not-days model, reuse configurable-list, flow sketch.
- `.planning/phases/04-universal-modules/04-RESUME-STORAGE-NOTES.md` — R-01..R-04 resume disk-storage module (DEFERRED out of this phase — see Deferred Ideas).

### Product spec / reference UX
- `PLAN_YOULEAD_TZ.md` § "Модуль оплаты" / "Модуль согласий" / "Новые типы вопросов" — the YouLead/RusCo TZ: payment flow, receipt tinder card, statuses, consent list, `date`/`consent` question types. (Untracked working file at repo root; informal spec.)
- Reference implementation: **RusCo'26 bot** (registration + payment + reminders + receipts) — behavioral reference only, not a code dependency.

### Tech-stack & compatibility locks
- `CLAUDE.md` § "Fixed Core Stack" / constraints — additive DB migrations must not break ~590 live users; reuse Phase-3 APScheduler; file_id storage pattern; no core-stack rewrite.

### Upstream phase context (depended-on)
- `.planning/phases/02-approval-flow/02-CONTEXT.md` — approval flow / tinder card pattern (APP-04) reused by payment receipt verification.
- `.planning/phases/03-scheduler-communications-verification/03-CONTEXT.md` — `AsyncIOScheduler` service (reused for PAY-06) + the settings-toggle/fail-safe-OFF posture.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `handlers/registration.py:59` `REG_FLOW` (list of `(step_key, setting_key)` tuples) + the step dispatcher at `:171` — extend with a per-step `type` for `date`/`consent` (D-07).
- `handlers/registration.py:974` `approve_user()` / `:994` `finalize_registration()` — payment step hooks in after approval (D-09); consent steps run before finalize (D-03).
- `handlers/admin.py:340` configurable-list setting (`source_options`, "📢 Источники") + `EditSetting` states (`:50`, `:748`) — the edit-a-list pattern reused for consent list + payment options (D-01, D-08).
- `handlers/admin.py` APP-04 tinder application queue (Одобрить/Отклонить/Пропустить, paginated by oldest pending DB row) — the shape to clone for the receipt verification queue (D-12).
- `services/scheduler.py` (Phase 3) `AsyncIOScheduler` + `schedule_broadcast_job`/date-job pattern — reused for PAY-06 deadline reminders (D-13).
- `database/db.py:17` `_ensure_column` + `CREATE TABLE IF NOT EXISTS` idiom — payment columns on `users` (D-10) + `user_consents` table (D-02).
- `database/db.py:109` `get_setting`/`set_setting` `bot_settings` k/v — all event-type/module flags + texts (D-06).
- Phase-1 `resume_file_id` upload handler (registration.py:631) — the file_id-capture shape to mirror for `receipt_file_id` (PDF + photo, D-11).

### Established Patterns
- Additive, idempotent migrations (no data loss for ~590 live users) — mandatory for every new column/table.
- Settings-driven toggles default OFF to protect the live flow (Phase-3 preselect precedent) — D-15.
- Per-callback `config.ADMIN_IDS` re-check on every admin callback (admin.py:872 precedent) — applies to receipt-queue + settings callbacks.

### Integration Points
- `cmd_start` / REG_FLOW — consent steps inserted before finalize; conditional on `consent_enabled`.
- `approve_user()` — payment step dispatched here when `payment_enabled` (D-09).
- `services/scheduler.py` `init_scheduler` — register payment-reminder jobs (D-13).
- Admin panel keyboard — new "тип события / модули" settings entry + receipt verification queue entry.

</code_context>

<specifics>
## Specific Ideas

- **RusCo'26 bot is the UX reference** for both payment (receipt tinder card, «Оплата подтверждена!») and consent (per-item «✅ Согласие X принято»).
- Receipt upload must accept **screenshots, not just PDF** (D-11) — real users send photos of the bank app.
- Consent full text as an **attached PDF document**, not an inline text wall (D-04).
- Penalty schedule (PAY-01) = configurable dates + amounts; content comes from the organizer later — build the mechanism with empty defaults.

</specifics>

<deferred>
## Deferred Ideas

- **Resume disk-storage / File Browser module** (`04-RESUME-STORAGE-NOTES.md`, R-01..R-04) — download resumes from Telegram into a File-Browser-served dir behind a `resume_disk_save` toggle, with PII auth concerns. A separate optional module; not required for conference-readiness. Note for a future phase / backlog.
- **Deep cancellation/refund workflow** beyond displaying the penalty schedule — STATE.md pre-Phase-4 open question ("does the bot handle user-initiated cancellation or only display the penalty schedule?"). Default to **display-only** unless the planner is told otherwise; full self-service cancellation is deferred.
- Gamification (GAME-01..04) and Roles (ROLE-01..02) — already v2 backlog.

</deferred>

---

*Phase: 4-universal-modules*
*Context gathered: 2026-06-29*
