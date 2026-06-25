# Phase 1: DB Foundation + Quick Wins + Coins - Context

**Gathered:** 2026-06-25
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 1 delivers: (1) safe SQLite schema migrations against the live `data/forum.db` (~590 users), (2) three UX quick wins — registration confirmation, channel-subscription check, resume upload, and (3) the coins economy foundation (append-only ledger + admin command + leaderboard + user-facing balance).

**Scope adjustment from discussion (IMPORTANT — affects roadmap):** The user redefined QW-02 from a hard subscription *gate* into *subscription tracking + an admin broadcast to non-subscribers*, and explicitly pulled two broadcast **audience segments** into Phase 1:
- Non-subscribers (those not subscribed to the configured TG channel)
- Incomplete registrations (users who started `/start` but never finished)

The "incomplete registrations" segment requires dropout tracking — the `reg_started` mechanism that the roadmap originally scheduled as **SCHED-02 in Phase 3**. Per explicit user direction ("всё сейчас", "нам нужно ВИДЕТЬ тех, кто зарегался НЕ ДО КОНЦА... отдельная каста в рассылке"), this tracking + these two broadcast segments are now **in Phase 1 scope**. See `<deferred>` / roadmap-impact note. Phase 1 success criterion 5 (hard gate at `/start`) is **superseded** — no gate.

</domain>

<decisions>
## Implementation Decisions

### QW-01 — Registration confirmation
- **D-01:** After the form is filled, show a summary of all answers + `get_confirm_kb()` (`Всё верно ✓` / `Изменить`). Keyboard already exists in `keyboards/builders.py:136`.
- **D-02:** `Изменить` = **restart the whole registration flow** (`/start` over). No per-field editing — user judged single-field edits a "1 из 100" case not worth the complexity / DB-sync. No background worker, no field-level sync.
- **D-03:** Summary+confirm applies to the **full form**. Short form (name only) needs no meaningful summary — minimal/no confirm there.

### QW-02 — Channel subscription (NOT a gate)
- **D-04:** Do **not** block bot access. Check subscription status via `getChatMember` against the channel already configured in settings (`contact_tg` in `bot_settings`) — reuse that field, no new channel config.
- **D-05:** Identify non-subscribers and surface them to the admin: admin receives a prompt like "N пользователей не подписаны на канал — разослать им напоминание?".
- **D-06:** Sending the reminder = a **broadcast targeting a segment** (non-subscribers). Build it into the broadcast flow as a separate target/field. ("Всё сейчас" — the reminder broadcast ships in Phase 1, not deferred to Phase 3.)
- **D-07:** fail-open — if the bot is not admin in the channel (cannot read membership), do not penalize the user.

### QW-03 — Resume upload
- **D-08:** Resume is a **toggleable registration step** controlled via the existing registration-questions configurator (like other REG_FLOW toggles). When enabled, it applies to the full form.
- **D-09:** When enabled, resume is **mandatory** and accepts **PDF/DOCX only**.
- **D-10:** Hard requirement: the bot must **not crash on a wrong file type** — validate the document type gracefully and re-prompt. Store `file_id` only (no download, no OCR).
- **D-11:** Short-form question-configurator parity is **deferred** (see deferred). Only the resume toggle ships now.

### COIN — Coins + leaderboard
- **D-12:** `coins` table = **append-only ledger** (`user_id, delta, reason, changed_by, timestamp`). Balance = `SUM(delta)`. Never UPDATE a balance (root cause of prior "слетали баллы" bug).
- **D-13:** `/coins @username +N` / `-N` — admin only. **reason is optional** (free text after the amount).
- **D-14:** Negative balances are **allowed**.
- **D-15:** Users see their own balance via a **main-menu button** (`🪙 Мои монеты`) AND `/рейтинг`. `/рейтинг` shows top-10 + the requester's rank.
- **D-16:** Add English aliases for the leaderboard command (`/rating`, `/leaderboard`) alongside `/рейтинг`. (Claude's discretion — confirm during planning.)

### DB Foundation (locked by roadmap, restated)
- **D-17:** `add_user()` must switch from `INSERT OR REPLACE` to `ON CONFLICT(telegram_id) DO UPDATE SET` — `REPLACE` = DELETE+INSERT and would wipe new columns (`status`, `resume_file_id`, coins linkage) on re-registration. Current code at `database/db.py:93`.
- **D-18:** New columns added via existing `_ensure_column()` (`database/db.py:16`). `status` column added with `DEFAULT 'approved'` so existing ~590 users keep access.

### Claude's Discretion
- Exact ledger schema/indexing, summary message formatting, getChatMember caching strategy, command alias naming, resume step placement within REG_FLOW order, graceful file-type rejection copy.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Plan & requirements
- `PLAN_YOULEAD_TZ.md` — source plan: confirmation, subscription check, coins+ledger, resume, broadcast segments
- `.planning/REQUIREMENTS.md` §Foundation/QuickWins/Coins — DB-01..03, QW-01..03, COIN-01..03
- `.planning/ROADMAP.md` §Phase 1 — goal + success criteria (criterion 5 superseded; see domain note)

### Research (brownfield, grounded in actual code)
- `.planning/research/STACK.md` — getChatMember + file_id are native aiogram 3; no new deps for Phase 1; `_ensure_column` + PRAGMA user_version migration pattern
- `.planning/research/ARCHITECTURE.md` — `_ensure_column` migration path; `status DEFAULT 'approved'`; where new subsystems plug into existing files
- `.planning/research/PITFALLS.md` — coins read-modify-write race (INSERT-only ledger); `INSERT OR REPLACE` destroying `status`; FSM MemoryStorage volatility → `reg_started` must be a DB table

### Existing code (source of truth)
- `README.md` — architecture, DB schema, FSM, router order
- `database/db.py` — `add_user` (line 93), `_ensure_column` (16), `bot_settings` get/set
- `handlers/registration.py` — `REG_FLOW`, `_get_enabled_steps`, `finalize_registration` (650)
- `handlers/user_actions.py` — `ensure_registered` (19)
- `keyboards/builders.py` — `get_confirm_kb` (136), menu builders
- `ТЗ_Бот_YouLead.docx` — original requirements doc (binary; reference only)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `get_confirm_kb()` (`keyboards/builders.py:136`) — ready-made `Всё верно ✓` / `Изменить` reply keyboard for QW-01. No new keyboard needed.
- `_ensure_column()` / `_column_exists()` (`database/db.py:10-18`) — additive migration primitive for all new columns.
- `get_setting()` / `set_setting()` (`database/db.py:67-82`) — key-value `bot_settings` store; `contact_tg` already holds the channel link for QW-02; resume toggle + channel-check toggle live here too.
- REG_FLOW engine (`registration.py:44-134`) — `_get_enabled_steps` + `_is_step_enabled` is exactly the toggle mechanism the resume step plugs into (add `("resume", "reg_q_resume")` + a step in `_ask_step`).
- `get_user_by_username()` (`db.py:147`) — for `/coins @username` lookup.
- Existing admin broadcast flow — the non-subscriber reminder extends this with a new target segment.

### Established Patterns
- Registration uses FSM states (`handlers/states.py:Registration`) with `MemoryStorage` — **volatile**. Dropout tracking (incomplete-registration segment) CANNOT rely on FSM; needs a `reg_started` DB row written at flow start, deleted at `finalize_registration`.
- `finalize_registration` (`registration.py:650`) is a monolith (DB + Sheets + admin notify + complete text + bonus). QW-01 inserts a confirm step *before* it; resume step is the last REG_FLOW step before confirm. Touch carefully (Phase 2 will split it further).
- Settings toggles default via `REG_DEFAULTS` dict (`registration.py:66`) — add `reg_q_resume` default `off`.

### Integration Points
- `cmd_start` (`registration.py:343`) — subscription check hook point; also where `reg_started` row is created.
- `add_user` (`db.py:93`) — switch to ON CONFLICT; add `status`, `resume_file_id` columns.
- Main menu builder (`keyboards/builders.py`) — add `🪙 Мои монеты` button (respect existing menu-toggle pattern).

</code_context>

<specifics>
## Specific Ideas

- Admin non-subscriber prompt phrasing: "N пользователей не подписаны на канал, давайте пришлём им уведомление" → leads into a segmented broadcast.
- Two named broadcast audience segments the user wants visible as "касты": **non-subscribers** and **incomplete registrations**.
- Coins menu button label: `🪙 Мои монеты`.
- Confirmation keyboard text already chosen: `Всё верно ✓` / `Изменить`.

</specifics>

<deferred>
## Deferred Ideas

- **Short-form question configurator parity** — extend the registration-questions configurator (currently full-form only) to the short form. Belongs with Phase 2 (APP-07 separate short/full settings) / Phase 4 modularity. Only the resume toggle ships in Phase 1.
- **Per-field editing of registration answers** — rejected for now (rare case, high complexity). Could revisit if users complain.

### Roadmap impact (for `/gsd-phase` / planner)
- **SCHED-02 (incomplete-registration / `reg_started` tracking)** was scheduled in Phase 3 but is pulled into Phase 1 by user direction (dropout segment must exist now). Recommend updating ROADMAP traceability so SCHED-02 (and the two broadcast segments) reflect Phase 1, OR plan Phase 1 to deliver a minimal `reg_started` + segment-targeting and leave scheduled/automated reminders (SCHED-01/03) in Phase 3.
- **Phase 1 success criterion 5** (hard subscribe gate at `/start`) is superseded by the no-gate / notify-admin decision (D-04..07). Update the criterion to: "subscription status is checked against the configured channel; non-subscribers can be targeted by an admin reminder broadcast; fail-open when bot is not channel admin."

</deferred>

---

*Phase: 1-db-foundation-quick-wins-coins*
*Context gathered: 2026-06-25*
