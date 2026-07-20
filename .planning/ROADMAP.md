# Roadmap: AIESEC Event Bot — YL'26 Milestone

## Overview

Brownfield extension of a production aiogram 3 + SQLite bot. The milestone adds approval queue, coins economy, scheduled/filtered broadcasts, a payment module, and event-modularity toggles — all integrated additively into the existing handler/service structure. Build order is dependency-driven: schema migrations first (safe against 590 live users), coins before any state accumulates, approval flow as the critical-path core, then scheduler infrastructure, then the highest-complexity payment/consent modules last.

## Phases

- [ ] **Phase 1: DB Foundation + Quick Wins + Coins** - Safe schema migrations, coins ledger, registration confirmation, subscription check + reminder broadcast, incomplete-registration tracking
- [x] **Phase 2: Approval Flow** - Core milestone: moderated application queue, tinder UI, atomic guards, per-form moderation toggles
- [ ] **Phase 3: Scheduler + Communications + Verification** - Persistent APScheduler, filtered/scheduled broadcasts, dropout reminders, pre-selection gate
- [ ] **Phase 4: Universal Modules** - Payment flow, consent module, event type/module toggles for conference support
- [ ] **Phase 5: Participant Tracks (Party Delegates)** - Per-track registration: participant_type, per-track question sets, deep-link entry, per-track approval and pricing

## Phase Details

### Phase 1: DB Foundation + Quick Wins + Coins
**Goal:** Bot runs safely against ~590 live users with correct schema migrations, coins economy established correctly from the start, and visible UX quick wins
**Mode:** mvp
**Depends on:** Nothing (brownfield baseline; all migrations safe to deploy immediately due to DEFAULT 'approved')
**Requirements:** DB-01, DB-02, DB-03, QW-01, QW-02, QW-03, COIN-01, COIN-02, COIN-03, SCHED-02
**Success Criteria** (what must be TRUE):
  1. Running `init_db()` against the production `data/forum.db` leaves all existing users with `status='approved'` — no user loses access after migration
  2. Re-registration of an existing user (via admin test-reregister) does not reset `status`, `resume_file_id`, or any new columns — ON CONFLICT DO UPDATE verified
  3. `/coins @username +10` followed immediately by `/coins @username -3` produces a balance of 7 computed as `SUM(delta)` from the ledger; no read-modify-write race is possible
  4. `/рейтинг` returns the top-10 users by coin balance and shows the requesting user's rank; users see their own balance via the `🪙 Мои монеты` menu button
  5. Subscription is **checked** (not gated) against the channel in the existing `contact_tg` setting via `getChatMember`; the admin is shown the count of non-subscribers and can send them a reminder via a broadcast segment; the check fails open when the bot lacks admin rights in the channel (no user is blocked at any point)
  6. A user who starts `/start` but abandons before finishing is recorded in a persistent `reg_started` DB table (survives restart, independent of MemoryStorage) and deleted on completion; these incomplete registrations are selectable as a distinct broadcast audience segment (automated scheduled nudging remains SCHED-03 in Phase 3)
**Note:** QW-03 is split across phases — Phase 1 delivers the delegate-side upload/validation/storage of `resume_file_id`; the manager-side viewing half is delivered by APP-04 in Phase 2 (see below).
**Plans:** 4 plans (3 waves)
Plans:
- [ ] 01-01-PLAN.md — DB foundation: safe migrations (status/resume_file_id/subscribed), ON CONFLICT add_user, coins ledger + reg_started + subscription helpers (Wave 1)
- [ ] 01-02-PLAN.md — Registration UX: confirmation step, resume upload (PDF/DOCX), reg_started dropout hooks (Wave 2)
- [ ] 01-03-PLAN.md — Coins economy: /coins admin command, /рейтинг + aliases, 🪙 Мои монеты balance button (Wave 2)
- [ ] 01-04-PLAN.md — Subscription check (fail-open) + non-subscriber & incomplete-registration broadcast segments (Wave 3)
**UI hint:** yes

### Phase 2: Approval Flow
**Goal:** Manager can fully moderate applications through a paginated tinder UI — submission to approval/rejection — without notification floods or double-approvals
**Mode:** mvp
**Depends on:** Phase 1 (status column live in schema; confirmation step stable in registration.py before finalize_registration() is split)
**Requirements:** APP-01, APP-02, APP-03, APP-04, APP-05, APP-06, APP-07, APP-08
**Success Criteria** (what must be TRUE):
  1. User completing registration with `short_approval=manual` or `full_approval=manual` sees "заявка отправлена" with no main menu; `ensure_registered()` denies all gated actions until status is 'approved'
  2. Manager opens "Заявки" in the admin panel and sees one paginated application card at a time with Одобрить / Отклонить / Пропустить / Одобрить все N buttons; the queue is driven by the oldest `status=pending` DB row, not FSM page offsets that would reset on restart; when the applicant has a stored `resume_file_id` (from Phase 1 QW-03 upload) the card re-sends that file via `answer_document` so the manager can open the resume — this delivers the manager-side viewing half of QW-03 (APP-04)
  3. Two managers clicking "Одобрить" on the same application simultaneously results in exactly one approval message sent to the user — the atomic `UPDATE … WHERE status='pending'` + rowcount=0 guard prevents the duplicate
  4. "Одобрить все N" shows a confirmation dialog before executing; each approved user receives welcome content + main menu exactly once
  5. Managers receive a periodic reminder with the count of pending applications (not one push per submission — notification storm prevented)
**Plans:** 4 plans (3 waves)
Plans:
- [x] 02-01-PLAN.md — DB approval layer: atomic approve/reject guards, oldest-pending queue + count, bulk approve_all_pending (Wave 1)
- [x] 02-02-PLAN.md — finalize split (submit/approve_user), status decision + per-form toggles, ensure_registered gating, rejected re-apply (Wave 2)
- [x] 02-03-PLAN.md — admin "Заявки" tinder UI: cards + resume re-send, atomic approve, reject-reason FSM, Одобрить все N + 429-safe sends, /settings_guide (Wave 3)
- [x] 02-04-PLAN.md — periodic pending-count reminder asyncio task at startup (Wave 2)
**UI hint:** yes

### Phase 3: Scheduler + Communications + Verification
**Goal:** Admins can schedule filtered broadcasts that survive bot restarts; incomplete registrations are auto-reminded; pre-selected users are gated at /start
**Mode:** mvp
**Depends on:** Phase 2 (status field queryable for broadcast filters; finalize_registration() stable before reg_in_progress writes are added to it)
**Requirements:** COMM-01, COMM-02, COMM-03, COMM-04, SCHED-01, SCHED-03, VERIF-01, VERIF-02
**Note:** SCHED-02 (`reg_started` dropout tracking) moved to Phase 1 per discussion; SCHED-03 here reuses that table for the automated scheduled nudge once APScheduler exists.
**Success Criteria** (what must be TRUE):
  1. Admin schedules a broadcast for a future time, bot restarts, and the broadcast fires at the correct time — scheduler jobs survive restart via `scheduled_broadcasts` DB table and startup job restore
  2. Admin builds a filtered broadcast (e.g., город=Москва AND статус=approved AND registered after 01.06.2026), sees the matching user count before sending, and only those users receive the message
  3. User who starts registration but abandons mid-flow receives exactly one automatic reminder nudge (after ~2h of inactivity via `reg_in_progress` DB table); already-registered users are never nudged
  4. Broadcast loop handles Telegram 429 rate limits by catching `TelegramRetryAfter` and waiting `retry_after + 1` seconds before retrying — rate-limited users are not counted as blocked
  5. When pre-selection verification is on, a Telegram username not in the Google Sheet sees "отбор не пройден" + external registration link; a user without a username sees a prompt to set one
**Plans:** 5 plans (4 waves)
Plans:
- [ ] 03-01-PLAN.md — SCHED-01: AsyncIOScheduler + SQLAlchemyJobStore foundation, scheduled_broadcasts payload table, schedule-broadcast UI, main.py wiring (Wave 1)
- [ ] 03-02-PLAN.md — SCHED-03: reg_started.nudged_at migration + one-shot dropout nudge interval job on the shared scheduler (Wave 2)
- [ ] 03-03-PLAN.md — COMM-04: flood-safe 429 hardening of process_broadcast + album send loops (Wave 2)
- [ ] 03-04-PLAN.md — COMM-01/02/03: AND filter builder over city/uni/status/source/reg-date with count preview, send-now or schedule (Wave 3)
- [ ] 03-05-PLAN.md — VERIF-01/02: pre-selection gate at /start via cached Google-Sheet allowlist (separate tab) + usernameless prompt + manual-id fallback (Wave 4)
**UI hint:** yes

### Phase 4: Universal Modules
**Goal:** Bot is conference-ready — consent collection, payment flow, and event type/module toggles all work end-to-end without any code deployment between events
**Mode:** mvp
**Depends on:** Phase 3 (APScheduler service stable for PAY-06 deadline reminders; approval flow complete since payment is triggered on approval; all modules must exist before the modularity toggle UI is fully useful)
**Requirements:** MOD-01, MOD-02, MOD-03, CONS-01, CONS-02, PAY-01, PAY-02, PAY-03, PAY-04, PAY-05, PAY-06
**Success Criteria** (what must be TRUE):
  1. Admin can switch event type (Форум / Конференция / Кастом) and toggle payment/consent modules in the admin panel without touching code; changes take effect immediately for new registrations
  2. User completing registration with consent module enabled must tap "Принимаю" for each configured consent item before the form finalizes; skipping any consent is not possible
  3. Approved user with payment module enabled receives payment instructions (amount, bank details, deadline, penalty schedule) and can upload a receipt; non-PDF uploads are rejected by MIME type with a clear user-facing error
  4. Manager reviews the receipt queue in tinder format (Подтвердить / Отклонить / Следующий); confirmed users receive payment confirmation and their payment reminders are cancelled; rejected users can re-upload
  5. Users with `payment_status=not_paid` receive auto-reminders at deadline−3 days and deadline−1 day; reminders do not fire if payment is already confirmed
**Plans:** 5 plans (4 waves)
Plans:
- [ ] 04-01-PLAN.md — DB schema foundation: user_consents table, payment columns on users, DB helper functions, FSM states (Wave 1)
- [ ] 04-02-PLAN.md — Admin event type + module toggles panel: SETTINGS_FIELDS additions, payment_enabled/consent_enabled toggles, event_type preset handler (Wave 1)
- [ ] 04-03-PLAN.md — REG_FLOW typed steps + consent module: 3-tuple migration, date validator, consent step injection and accept handlers (Wave 2)
- [ ] 04-04-PLAN.md — Payment FSM: option selection, requisites display, receipt upload, approve_user hook, router wiring (Wave 3)
- [ ] 04-05-PLAN.md — Receipt tinder queue + PAY-06 payment reminders: admin confirm/reject/skip, atomic guard, scheduler deadline jobs, overdue sweep (Wave 4)
**UI hint:** yes

### Phase 5: Participant Tracks (Party Delegates)
**Goal:** Один бот обслуживает делегатов разных треков — полное участие и «только вечеринка» (с ночёвкой / без) — с отдельными наборами вопросов, модерацией и тарифами, настраиваемыми из админки без деплоя
**Mode:** mvp
**Depends on:** Phase 4 (payment_options и module toggles должны существовать до того, как тарифы разделяются по трекам; REG_FLOW 3-tuple миграция — база для per-track оверрайдов)
**Requirements:** TRACK-01, TRACK-02, TRACK-03, TRACK-04, TRACK-05, TRACK-06
**Success Criteria** (what must be TRUE):
  1. `users.participant_type` мигрирована с `DEFAULT 'full'` — все ~590 существующих делегатов после миграции остаются треком `full`, ни одна запись не теряет данные
  2. Трек фиксируется в БД в момент старта регистрации (не только в FSM): пользователь переходит по party-ссылке, отправляет `/start` повторно уже без параметра — и остаётся в party-треке, а не проваливается в полную анкету
  3. Админ включает/выключает конкретный вопрос отдельно для трека party (`reg_q_<step>__party`), не задев тот же вопрос для полных делегатов; при отсутствии оверрайда шаг наследует глобальную настройку `reg_q_<step>`
  4. Переход по `t.me/<bot>?start=party_over` и `?start=party_noover` заводит соответствующий трек и не ломает существующие deep-link'и: числовой аргумент по-прежнему разбирается как referrer_id, `src_*` — как источник
  5. Вопрос-развилка «формат участия» показывается только при `party_fork_question=on` (default `off`) — при выключенном тумблере обычный делегат не видит ни одного лишнего экрана
  6. `party_approval` работает независимо от `full_approval`/`short_approval`: при `party_approval=auto` и `full_approval=manual` party-заявка одобряется автоматически, а полная уходит в очередь модерации
  7. Одобренный party-делегат видит только party-тарифы из `payment_options`; полный делегат — только полные
  8. Трек виден манагеру в карточке заявки, попадает отдельной колонкой в Google Sheet и доступен как фильтр рассылки
**Plans:** 6 plans (5 waves)
Plans:
**Wave 1**
- [x] 05-01-PLAN.md — Track foundation: participant_type migrations, two-point persistence, party deep-link parsing, party_enabled master gate + closed-message fallback (Wave 1)

**Wave 2** *(blocked on Wave 1 completion)*
- [x] 05-02-PLAN.md — Per-track question engine: tri-state __party overrides, per-track wording, overnight-only skip rule, 🎉 Party seed preset (Wave 2)

**Wave 3** *(blocked on Wave 2 completion)*
- [x] 05-03-PLAN.md — Admin surface: track switcher + tri-state toggle, party preset button, party settings, track on the moderation card, participant_type broadcast filter (Wave 3)
- [x] 05-04-PLAN.md — Approval routing: independent party_approval, per-track approve text, optional in-flow fork question (Wave 3)

**Wave 4** *(blocked on Wave 3 completion)*
- [x] 05-05-PLAN.md — Per-track tariffs: optional third track field in payment_options, index-preserving keyboard filter, free fallback (Wave 4)

**Wave 5** *(blocked on Wave 4 completion)*
- [ ] 05-06-PLAN.md — Party Google Sheets tab: named-worksheet append infrastructure, party column set, exclusive routing, gated startup header (Wave 5)
**UI hint:** yes (Telegram reply/inline keyboards — не веб-фронтенд)

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. DB Foundation + Quick Wins + Coins | 0/4 | Planned | - |
| 2. Approval Flow | 4/4 | Implemented | 2026-06-26 |
| 3. Scheduler + Communications + Verification | 0/5 | Planned | - |
| 4. Universal Modules | 0/5 | Planned | - |
| 5. Participant Tracks (Party Delegates) | 0/6 | Planned | - |
