# Feature Research

**Domain:** Telegram event-registration bot — AIESEC forums and conferences (subsequent milestone)
**Researched:** 2026-06-25
**Confidence:** HIGH (all features derived from PROJECT.md, PLAN_YOULEAD_TZ.md, direct requirements; no speculative research needed)

> This document covers only the **new** features for this milestone. Existing capabilities
> (dynamic registration, Google Sheets sync, referral links, broadcasts, admin panel, source
> tags, user search, registration bonus, Q&A replies) are already validated and excluded.

---

## Feature Landscape

### Table Stakes (Users Expect These)

Features the AIESEC manager and delegates expect from this feature set. Missing any of these
makes the delivered feature feel incomplete or broken.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Approval flow: pending/approved/rejected statuses | Any moderated registration needs explicit states; manager cannot track status without a field | MEDIUM | DB migration adds `status` to `users`; `ensure_registered()` must check `status=approved` |
| Approval tinder UI with pagination | 1000+ applications make per-message inline buttons untenable; paginated card view is the only ergonomic option at this scale | MEDIUM | Counter "3/47", approve/reject/skip/approve-all buttons on one message; edit-in-place, no new messages per action |
| User notified on approve / reject | Without a notification, approved users don't know they can proceed; rejected users don't know to stop waiting | LOW | Simple `bot.send_message` in `approve_user()` and reject handler |
| Separate moderation toggle per form variant | Short form (quick RSVP) and full form (delegate application) have different moderation needs | LOW | `short_approval` / `full_approval` settings keys; each defaults to `auto` |
| Channel subscription gate | Organizers always want subscribers before access; standard Telegram bot UX pattern | LOW | `getChatMember` call; prompt with channel link + "I subscribed" recheck button |
| Coins transactions log (INSERT-only audit trail) | Previous bot lost points — this is a hard requirement driven by a real production failure | LOW | Table: `coins(id, user_id, delta, reason, changed_by, timestamp)`. Never UPDATE balances; compute by SUM. |
| User can see own coin balance | Without self-visibility, coin system has no motivational effect on delegates | LOW | Button in main menu or `/balance` command |
| Leaderboard top-10 + own position | Standard for any points system; missing it removes competitive motivation entirely | LOW | SQL: `ROW_NUMBER() OVER (ORDER BY SUM(delta) DESC)`; show top-10 + requesting user's rank |
| Admin can give/take coins with reason | Core admin tool; without it the coin system is unmanageable | LOW | `/coins @username ±N reason` command |
| Resume file_id stored in DB | Manager needs to open resume during approval; storing file_id is the Telegram-native approach | LOW | One new column `resume_file_id TEXT` in `users`; toggle in full-form settings |
| Verification against pre-selected list | YL'26 scenario: only selected candidates should register; gate must be first touchpoint | LOW | Read one column from Google Sheet on `/start`; case-insensitive username match |
| Verification toggle (enable/disable) | Not all events use pre-selection; the gate must be bypassed for open events | LOW | `verification_enabled` setting key |
| Filtered broadcast with recipient preview count | Sending to wrong audience is a critical UX failure; manager needs to see "you're about to message 47 people" before confirming | MEDIUM | Multi-step inline FSM; AND-combined filters; `SELECT COUNT(*)` preview before send |
| Scheduled broadcast persisted in DB | APScheduler in-memory jobs are lost on restart; this is explicit in project constraints | MEDIUM | `scheduled_broadcasts` table; APScheduler loads pending jobs on startup |
| Re-registration reminder via persistent DB tracking | FSM MemoryStorage resets on restart — dropout tracking MUST be in DB, not FSM | LOW-MEDIUM | `registration_started_at` column in `users`; scheduled job finds non-completers |
| Re-registration reminder sends once | Sending multiple reminders to a non-completer is spammy and will cause blocks | LOW | `reminder_sent` flag or check `registration_started_at` + `status` against sent log |
| Payment module toggle (off for forums) | Forums don't collect fees; the module must be invisible unless enabled | MEDIUM | `payment_module_enabled` setting; all payment handlers behind this toggle |
| Payment statuses: not_paid / receipt_sent / paid / overdue | Standard for any manual payment confirmation flow | MEDIUM | `payment_status` column in `users` (or separate `payments` table for audit) |
| Receipt upload (PDF + image) stored as file_id | Manual receipt review requires manager to open the file; Telegram file_id is sufficient | LOW | Accept document + photo types; store `receipt_file_id` |
| Payment receipt review tinder (same UX as application tinder) | Manager needs same paginated review pattern for receipts as for applications | MEDIUM | Reuse tinder UI pattern; separate queue for `status=receipt_sent` |
| Auto-reminders T-3 and T-1 before payment deadline | Standard for any payment-deadline flow; missing = higher no-payment rate | MEDIUM | Two scheduled jobs per user (or one job that checks both thresholds) via APScheduler |
| Consent module: configurable list shown before finalization | Legal and policy requirement for any data-collecting registration form | LOW-MEDIUM | `consents` settings (key-value list); inject consent steps into REG_FLOW before finalize |
| Each consent requires explicit acceptance | A consent shown but not confirmed has no legal value | LOW | Inline "Accept" button per consent step; cannot proceed without clicking |

### Differentiators (Competitive Advantage)

Features beyond baseline that give this bot its edge over generic registration tools
(Google Forms, Typeform, manual spreadsheets).

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Bulk "Approve all" in tinder queue | Manager can process 1000 applications in one tap after quick scan; no other tool gives this for Telegram-native flow | LOW | Button visible in tinder UI; confirmation dialog before executing; mass `approve_user()` loop |
| Manager periodic reminder about pending queue | Prevents applications from sitting unreviewed; proactive rather than reactive | LOW | APScheduler job every N hours; configurable via settings; "47 applications waiting" message |
| Coins as foundation for gamification | Unlike a static leaderboard, the transaction-log architecture allows adding task rewards, referral bonuses, and event triggers without schema changes | LOW | Just the table design; gamification mechanics are future scope |
| Admin full coin history per user | Allows debugging "I lost my points" complaints with a concrete audit log | LOW | `/coins_history @username` — query all rows for user_id |
| Rejection message with optional reason | Rejected delegate gets context; reduces support burden on manager | LOW | Optional text field in reject action in tinder UI |
| Filter broadcast by approval status + payment status | Organizers can target exactly "approved, not paid, deadline in 3 days" with one filtered send | LOW | Additional filter options once those DB fields exist |
| Cancellation penalty scale visible to delegate | Proactively showing the fee schedule reduces "I didn't know" disputes at cancellation | MEDIUM | Formatted table from bot_settings; shown at payment step |
| Re-receipt upload after manager rejection | User can correct a rejected receipt without restarting; reduces back-and-forth | LOW | After rejection, re-open receipt upload step for that user |
| Consent version note in DB | Timestamp of acceptance lets organizers prove consent was given at a specific point | LOW | `consents_accepted_at` timestamp column alongside `consents_accepted` flag |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| One Telegram message per pending application in admin chat | "Just notify me for each new application" feels natural | At 1000+ applications, floods admin chat making it unusable; inline buttons on old messages go stale when bot restarts | Paginated tinder section in admin panel + periodic count reminder |
| Real-time payment gateway (YooKassa, Stripe) | "Automate the money handling" | Requires legal entity registration, acquiring accounts, refund flows, compliance; the org's payment process is already defined via bank transfers | Receipt upload + manual manager confirmation — matches the org's actual process |
| Automatic cancellation at payment deadline | "Auto-enforce the deadline" | Boot a delegate who simply forgot their phone; no recourse; manager may want flexibility | Auto-mark as `overdue`; send reminder; require manager to explicitly cancel — preserves human judgment |
| Google Sheets as admin interface (two-way sync) | "Edit registrations from Sheets" | Explicit known failure — two-way sync crashed the previous bot; concurrent writes cause race conditions | All admin actions in bot; Sheets is write-only append log |
| OCR / resume parsing | "Auto-extract skills from PDF" | Adds major complexity (external OCR API, parsing, error handling) for minimal gain; manager already opens the file | Store file_id; manager opens file directly — this is already the documented decision |
| Balance field on users table | "Simpler to just track current balance" | This is exactly what caused the production points-loss bug — overwriting loses history | INSERT-only `coins` table; compute balance as SUM; never lossy |
| Optional consents (user can skip some) | "Not everyone needs to consent to photos" | Creates legal ambiguity; if a consent is optional it should simply not be shown; shown consents must always be accepted | Toggle consents off in admin if they don't apply to the event; all shown consents are mandatory |
| Reminder on every bot access for non-completers | "Keep pushing them until they finish" | Users will block the bot; Telegram penalizes bots with high block rates | Single reminder N hours after dropout; non-intrusive with direct continue link |
| Timezone selector in broadcast scheduler | "Support multiple timezones" | Adds UI complexity for a team that all operates in one timezone | Hardcode to Moscow time (UTC+3); document clearly; revisit if needed |
| Multi-level approval (two managers must approve) | "More oversight" | Dramatically increases approval latency; AIESEC events don't have compliance requirements needing dual approval | Single manager approval with full audit log of who approved |
| Duplicate bot instances per event | "Cleaner separation" | Two bots = two codebases, two deployments, doubled bugs | One bot with module toggles per event type (already decided) |
| Recurring broadcast schedules (cron) | "Send every Monday" | AIESEC events are short (1 season); organizers think in specific dates, not recurrence patterns | One-time scheduled broadcasts; if recurrence is needed, schedule multiple |

---

## Feature Dependencies

```
[Verified List Gate (Google Sheet)]
    └──gates──> /start entry point
                    └──leads to──> [Channel Subscription Gate]
                                       └──leads to──> [Registration Form]

[Registration Form]
    ├──optional step──> [Resume Upload]         (if full-form + resume toggle on)
    ├──injects step──> [Consent Module]          (each enabled consent before finalize)
    └──leads to──> [Approval Flow]              (if approval_mode=manual)
                       └──unlocks──> [Payment Module]  (payment after approval)

[Approval Flow (Tinder UI)]
    └──shares UI pattern with──> [Payment Receipt Review Tinder]
    └──status field enables──> [Filtered Broadcasts]

[APScheduler / Persistent Scheduler]
    ├──required by──> [Scheduled Broadcasts]
    ├──required by──> [Re-registration Reminders]
    ├──required by──> [Payment Auto-reminders T-3/T-1]
    └──required by──> [Manager Pending-Queue Reminders]

[Filtered Broadcasts]
    └──extended by──> [Scheduled Broadcasts]
    └──reads fields from──> [Approval Flow] (status)
    └──reads fields from──> [Payment Module] (payment_status)

[Coins / Transactions Log]
    └──foundation for──> Gamification (future milestone, out of scope now)
    └──read by──> [Leaderboard]

[Resume Upload]
    └──displayed in──> [Approval Flow Tinder Card]

[Payment Module]
    ├──requires──> [APScheduler] (auto-reminders)
    ├──reuses pattern from──> [Approval Flow Tinder UI]
    └──receipt file handled like──> [Resume Upload] (file_id storage)
```

### Dependency Notes

- **APScheduler is a cross-cutting prerequisite.** It must be set up before any of: scheduled broadcasts, re-registration reminders, payment auto-reminders, manager queue reminders. One infrastructure investment, four features benefit.
- **Approval flow must precede Payment module.** Payment is triggered after approval (`status=approved`). Building payment without approval flow requires patching it later.
- **Tinder UI pattern is reusable.** Approval tinder and receipt-review tinder are the same UX pattern with different data sources. Build once as a generic paginated-card component; parameterize data source and button actions.
- **Filtered broadcasts gain value incrementally.** A filtered broadcast built before approval flow can only filter on existing fields (city, university, source). After approval flow, `status` becomes a filter. After payment module, `payment_status` becomes a filter. The filtered broadcast feature is additive.
- **Verification gate + channel gate ordering matters.** Verified-list check should happen first (cheapest: one Sheets read), then channel gate, then registration form. Avoids wasting channel subscription effort on non-selected users.
- **Consent module injects into REG_FLOW.** It is not a separate handler but a set of steps appended to the existing dynamic registration engine before the finalization step.
- **Resume upload is gated by full-form + toggle.** It should never appear in the short form (quick RSVP). The toggle lives in full-form settings alongside other question toggles.

---

## MVP Definition (This Milestone)

### Phase 1 — Launch With (Quick Wins, highest ROI)

- [ ] **Channel subscription gate** — lowest effort, immediately blocks non-subscribers
- [ ] **Resume file upload** — LOW complexity, directly unblocks approval flow review
- [ ] **Coins transactions log + admin commands** — foundation feature, prevents known data-loss bug
- [ ] **Leaderboard** — completes the coins feature; LOW complexity once transactions table exists

### Phase 2 — Launch With (Core Milestone)

- [ ] **Approval flow: status field + submit/approve split** — the central milestone requirement; must land before anything that reads `status=approved`
- [ ] **Tinder UI for applications** — required to make approval flow usable at scale
- [ ] **Separate moderation toggle per form variant** — completes approval flow; needed before rollout
- [ ] **Consent module** — legal requirement; inject before finalization; LOW-MEDIUM complexity
- [ ] **Persistent scheduler (APScheduler + DB)** — prerequisite for all reminder features; build once

### Phase 3 — Add After Core (Before Event Launch)

- [ ] **Verification against pre-selected list** — YL'26-specific gate; needs allowlist in Sheets
- [ ] **Filtered broadcasts** — adds significant value for targeted communications
- [ ] **Scheduled broadcasts** — depends on scheduler from Phase 2
- [ ] **Re-registration reminders** — depends on scheduler; low complexity once infra exists
- [ ] **Manager pending-queue reminder** — depends on scheduler; single scheduled job

### Phase 4 — Universal Module (For Conference Events)

- [ ] **Payment module settings** — enable/configure in admin
- [ ] **Payment flow (user side)** — receipt upload, status display
- [ ] **Receipt review tinder** — reuses tinder pattern from approval flow
- [ ] **Payment auto-reminders T-3/T-1** — depends on scheduler
- [ ] **Cancellation penalty scale display** — display only, no automation

### Future Consideration (Next Milestone)

- [ ] **Gamification task system** — explicitly deferred; 8 open questions to answer first
- [ ] **Role-based access (Delegate/Manager/Admin)** — deferred; doesn't block current milestone
- [ ] **Recurring broadcast schedules** — not needed for single-event season model

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Approval flow (status + split finalize) | HIGH | MEDIUM | P1 |
| Tinder UI for applications | HIGH | MEDIUM | P1 |
| Coins transactions log | HIGH | LOW | P1 |
| Channel subscription gate | HIGH | LOW | P1 |
| Resume upload (file_id) | MEDIUM | LOW | P1 |
| Consent module | HIGH | LOW-MEDIUM | P1 |
| Persistent scheduler (APScheduler) | HIGH (enabler) | MEDIUM | P1 |
| Verification against pre-selected list | HIGH (YL-specific) | LOW | P2 |
| Filtered broadcasts | HIGH | MEDIUM | P2 |
| Scheduled broadcasts | MEDIUM | MEDIUM | P2 |
| Re-registration reminders | MEDIUM | LOW (after scheduler) | P2 |
| Manager pending-queue reminder | MEDIUM | LOW (after scheduler) | P2 |
| Payment module (full) | HIGH (conferences) | HIGH | P2 |
| Leaderboard | MEDIUM | LOW | P2 |
| Admin coin history per user | LOW | LOW | P3 |
| Bulk "Approve all" | MEDIUM | LOW | P2 |
| Cancellation penalty scale display | LOW | LOW | P3 |
| Export payment status CSV | LOW | LOW | P3 |

**Priority key:**
- P1: Must have for milestone launch
- P2: Should have; add within milestone
- P3: Nice to have; future if time allows

---

## Complexity and Implementation Notes Per Feature

### Approval Flow — MEDIUM
The complexity is not in any single piece but in the coordinated change across multiple files:
`db.py` (schema migration), `registration.py` (split finalize), `user_actions.py` (ensure_registered guard),
`admin.py` (tinder section). The migration must add `status TEXT DEFAULT 'approved'` to preserve
existing records — all current users are implicitly approved.

### Tinder UI (Shared Pattern) — MEDIUM
The paginated card pattern (show card N of M, edit message in place, action buttons) must be
implemented as a reusable component. The same pattern serves both application review and receipt
review. Key detail: use `callback_query` with `message.edit_text` — never send new messages per
card action or the chat fills up.

### APScheduler Persistence — MEDIUM
Use `APScheduler` with `SQLAlchemyJobStore` pointed at the existing SQLite DB, or a separate
`scheduled_jobs` table with a custom polling loop. The polling loop approach is simpler and avoids
SQLAlchemy as an extra dependency. Critical: load all pending jobs on bot startup.

### Payment Module — HIGH
The highest-complexity feature due to: multiple status transitions, receipt file handling,
tinder review UI (separate from application tinder), auto-reminders with per-user deadlines,
cancellation penalty schedule configuration, and overdue detection. Build last; all prior
features are prerequisites or share patterns with it.

### Verified List Gate — LOW
One `gspread` read of one column. The main edge cases: (1) user has no Telegram username
(~5-10% of users); graceful handling needed — either prompt them to set a username or
contact organizer; (2) column values may have leading/trailing spaces — strip and lowercase
both sides before comparison.

### Re-registration Reminders — LOW-MEDIUM
The key insight: FSM MemoryStorage is volatile. Tracking must happen in DB. Add
`reg_started_at DATETIME` column to `users` (or a separate `reg_sessions` table). Set it on
`/start` before FSM begins. Scheduled job queries `WHERE reg_started_at IS NOT NULL AND status IS NULL AND reminder_sent = 0`.

---

## Sources

- `PROJECT.md` — Active scope, constraints, key decisions (HIGH confidence)
- `PLAN_YOULEAD_TZ.md` — UI mockups, architecture decisions, known failure modes (HIGH confidence)
- `README.md` — Existing capabilities, DB schema, FSM architecture (HIGH confidence)
- Production failure note: "слетали баллы" (points were lost) → drives INSERT-only audit trail design
- Scale note: YL'26/1 — 1072 selected, 590 started bot (55%) → tinder UI must handle 1000+ queue

---
*Feature research for: AIESEC Event Bot — subsequent milestone new features*
*Researched: 2026-06-25*
