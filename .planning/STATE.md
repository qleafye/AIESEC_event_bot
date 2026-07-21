---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: verifying
stopped_at: Completed 05-06-PLAN.md (checkpoint deferred to end-of-phase UAT)
last_updated: "2026-07-21T00:18:44.526Z"
last_activity: 2026-07-21
progress:
  total_phases: 5
  completed_phases: 1
  total_plans: 24
  completed_plans: 6
  percent: 25
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-25)

**Core value:** Менеджер DXP может полностью провести регистрацию делегатов через бота — от заявки до одобрения — без ручного учёта в таблицах и без перезапуска кода между событиями.
**Current focus:** Phase 05 — participant-tracks-party-delegates

## Current Position

Phase: 05 (participant-tracks-party-delegates) — EXECUTING
Plan: 6 of 6
Status: Phase complete — ready for verification
Last activity: 2026-07-21

Progress: [███░░░░░░░] 25%

## Performance Metrics

**Velocity:**

- Total plans completed: 0
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**

- Last 5 plans: —
- Trend: —

*Updated after each plan completion*
| Phase 05 P01 | 7min | 3 tasks | 3 files |
| Phase 05 P02 | 8min | 3 tasks | 2 files |
| Phase 05 P04 | 9min | 3 tasks | 2 files |
| Phase 05 P03 | 7min | 3 tasks | 2 files |
| Phase 05 P05 | 12min | 2 tasks | 3 files |
| Phase 05 P06 | 11min | 3 tasks | 3 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Roadmap init: 4 phases (research recommendation confirmed). DB + Quick Wins + Coins as Phase 1 — safe against 590 live users.
- Roadmap init: APP-08 (manager periodic reminder) placed in Phase 2 — can be implemented as a simple asyncio periodic task without APScheduler dependency.
- Roadmap init: APScheduler persistence approach (Option A: SQLAlchemyJobStore vs Option B: MemoryJobStore + DB restore) is an open decision to confirm before Phase 3 planning. Does not block Phases 1 or 2.
- [Phase 05]: D-01/D-02 (05-01): users.participant_type TEXT DEFAULT 'full' additive migration; track persisted at TWO points (reg_started at flow start, users at finalize) since clear_reg_started deletes the reg_started row on completion
- [Phase 05]: D-10 (05-01): _extract_party_track matches ONLY the two literal deep-link tokens via a fixed dict, no prefix matching, keeping referrer_id and src_ tag extraction untouched
- [Phase 05]: D-11a (05-01): party_enabled master gate placed AFTER the already-registered branch in cmd_start so a returning delegate on a stale party link never sees the closed message; fail-soft, never silently reroutes into full
- [Phase 05]: D-03/D-04 (05-02): _is_step_enabled_for_track reads {setting_key}__party only for party tracks; is-not-None (not truthiness) distinguishes explicit off from inherit
- [Phase 05]: D-08 (05-02): housing/bed_sharing/bed_partner skip rule gated on _is_party_track AND participant_type != 'party_overnight' — structurally cannot fire for full/None
- [Phase 05]: D-05 (05-02): _prompt wording override uses truthiness (not is-not-None) so an empty __party override falls back to global text instead of a blank message
- [Phase 05]: D-07 (05-02): REG_PRESETS['party'] carries no payment_enabled key by design — plan 05-03's shared preset_apply/preset_confirm handlers must tolerate its absence
- [Phase 05]: D-13/D-14 (05-04): _decide_status party branch resolves status from party_approval alone (party_setting or 'manual' fail-closed default), placed before the reg_mode logic with no fallthrough; D-14 compliance achieved by adding no participant_type predicate anywhere in the pending queue
- [Phase 05]: D-15 (05-04): _approve_text_for uses truthiness fallback (empty override falls back to global text); approve_user resolves participant_type ONCE via get_user before the payment_enabled early-return branch, fail-soft to 'full', so plan 05-05 can reuse the same value
- [Phase 05]: D-09/D-10 (05-04): party fork question implemented as a pre-flow InlineKeyboardMarkup in cmd_start (party_pick: callback), never a REG_FLOW step; _should_show_fork is a pure five-condition gate, false by default, suppressed by an authoritative deep-link/recovered track or a closed party_enabled toggle
- [Phase 05]: D-04/D-06 (05-03): tri-state party question toggle reads the raw __party setting (never collapsed through the 2-state boolean helper), delete_setting represents inherit; track switcher re-renders the same questions-screen message via callback_data, no new FSM state
- [Phase 05]: D-07 (05-03): preset_apply/preset_confirm widened with preset.get(payment_enabled) to tolerate the party preset's missing key, and key == party routes to _apply_party_preset() instead of _apply_event_preset() to avoid wiping global reg_q_* settings
- [Phase 05]: D-13/D-19 (05-03): party_approval wired independently of full_approval/short_approval via _toggle_approval_setting; participant_type whitelisted in BOTH database.db._FILTER_COLUMNS and handlers.admin._PICKER_FIELDS so it becomes a broadcast filter with a DB-distinct value picker
- [Phase 05]: D-16 (05-05): _parse_options widened to 3-tuples via unlimited pipe-split; blank third field resolves to None (all tracks), never an empty set
- [Phase 05]: D-17/T-05-05-07 (05-05): _visible_options is a standalone pure function for testable index-preservation; single/free fallback reads visible[0] never options[0], closing a wrong-price bug for track-split tariffs
- [Phase 05]: T-05-05-03 (05-05): process_payment_option re-resolves the caller's CURRENT track via get_user and rejects an ineligible tapped index before any charge runs; skipped entirely when the option's tracks is None
- [Phase 05]: D-11 (05-06): second _named_sheets cache keyed by tab name, parallel to the main-tab _sheet global -- neither the single-tab cache nor the full-overwrite sync_named_worksheet fit incremental per-row append to a second tab
- [Phase 05]: D-12 (05-06): finalize_registration sheet append is an exclusive if/else on _is_party_track -- party track appends to append_to_party_sheet only, else branch (full delegates) is byte-identical to pre-Phase-5 append_to_sheet; verified by integration tests, not just a grep
- [Phase 05]: T-05-06-01 (05-06): party_sheet_row applies database.db._csv_safe to every cell (reused, not reimplemented); the main sheet's active_sheet_row does NOT yet apply _csv_safe -- flagged as a pre-existing follow-up gap, out of this plan's scope
- [Phase 05]: Task 4 (05-06 checkpoint) deferred to a single consolidated end-of-phase human UAT pass per coordinator decision, carrying forward its 10-step live-bot/live-Sheet checklist -- same deferral pattern as 05-03/05-04

### Roadmap Evolution

- Phase 5 added (2026-07-20): Participant Tracks (Party Delegates) — регистрация делегатов «только на вечеринку» (с ночёвкой / без). Требования TRACK-01..06. Причина: новый запрос заказчика после закрытия scope YL'26; расширяет, а не меняет фазы 1–4.

### Pending Todos

None yet.

### Blockers/Concerns

- **Phase 3 plan override (2026-06-27):** Mechanical decision-coverage gate scored 2/16 (plans cite decisions by description, not literal D-NN IDs). Semantic plan-checker confirmed all 16 decisions (D-01..D-16) have implementing tasks — accepted as false-negative, proceeded by user. verify-phase should re-confirm decision coverage semantically.
- **Pre-Phase 3:** Confirm APScheduler persistence approach (Option A vs B) before Phase 3 planning begins.
- **Pre-Phase 3:** Confirm Google Sheet structure (sheet name, tab, column) for VERIF-01 username verification with the AIESEC manager.
- **Pre-Phase 4:** Confirm payment cancellation scope — does the bot handle user-initiated cancellation or only display the penalty schedule?
- **Pre-Phase 4:** Consent texts for YL'26 (data processing, photo/video rights, event rules) must be provided by the organizer before Phase 4.

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260702-vr1 | fix payment receipt-upload trap plus pay-later button and upload-receipt menu entry | 2026-07-02 | 5aacb1b | [260702-vr1-fix-payment-receipt-upload-trap-plus-pay](./quick/260702-vr1-fix-payment-receipt-upload-trap-plus-pay/) |
| 260702-w50 | event-type presets (forum/conference) + categorized question toggles in admin | 2026-07-02 | a9b3c74 | [260702-w50-event-type-presets-forum-conference-cate](./quick/260702-w50-event-type-presets-forum-conference-cate/) |
| 260702-wf6 | dynamic google sheet columns (only enabled questions) + rename duplicate labels | 2026-07-02 | d78dae6 | [260702-wf6-dynamic-google-sheet-columns-only-enable](./quick/260702-wf6-dynamic-google-sheet-columns-only-enable/) |
| 260702-wqi | background sheet append + cache gspread client + sync payment deadline into reg question | 2026-07-02 | 21a127f | [260702-wqi-background-sheet-append-cache-gspread-cl](./quick/260702-wqi-background-sheet-append-cache-gspread-cl/) |
| 260702-wwu | admin receipt-arrival notification + full readable CSV export (RU headers, all columns) | 2026-07-02 | af8a846 | [260702-wwu-admin-receipt-arrival-notification-full-](./quick/260702-wwu-admin-receipt-arrival-notification-full-/) |
| 260702-x01 | conference bed-sharing question with dynamic follow-up (share bed? if yes, with whom) | 2026-07-02 | a432b81 | [260702-x01-conference-bed-sharing-question-with-dyn](./quick/260702-x01-conference-bed-sharing-question-with-dyn/) |
| 260703-00o | custom city text via «Другое» prompt + export incomplete registrations to «Незавершённые» tab | 2026-07-03 | eb2f087 | [260703-00o-custom-city-text-via-prompt-export-incom](./quick/260703-00o-custom-city-text-via-prompt-export-incom/) |
| 260703-06r | «Другое» guard on department/aiesec_role + scheduled auto-sync of incomplete regs (every 2h) | 2026-07-03 | 428b391 | [260703-06r-handle-on-department-and-aiesec-role-plu](./quick/260703-06r-handle-on-department-and-aiesec-role-plu/) |
| 260703-0mj | make /create_link source tag authoritative (skip «Источник» question for tagged users) | 2026-07-03 | f38b0c3 | [260703-0mj-make-source-tag-authoritative-skip-quest](./quick/260703-0mj-make-source-tag-authoritative-skip-quest/) |
| 260704-378 | consent list showed only 1 entry — accept «;» separator (Telegram Enter=send trap) | 2026-07-04 | 0bdf07f | [260704-378-fix-consent-list-only-one-entry-shown-sp](./quick/260704-378-fix-consent-list-only-one-entry-shown-sp/) |
| 260709-mog | +departments (F&L/LCP/EwA) + payment reminders now fire for «оплачу позже» (not_paid) + overdue final ping + admin reminder-text settings | 2026-07-09 | 5d19291 | [260709-mog-customer-fixes-departments-payment-reminders](./quick/260709-mog-customer-fixes-departments-payment-reminders/) |
| 260709-n19 | payment auto-reminders on/off toggle (admin) + non-payer broadcast segment (payment_status filter) | 2026-07-09 | d797549 | [260709-n19-payment-reminders-toggle-and-nonpayer-broadcast](./quick/260709-n19-payment-reminders-toggle-and-nonpayer-broadcast/) |
| 260710-nkk | sheet columns in form order + «Статус»/«Резюме (текст)» columns + resume required (no skip) + resume text in admin card + status autosync on approve/reject + rebuild-sheet button with dropdown/color formatting | 2026-07-10 | 3c6c347 | [260710-nkk-sheet-cols-resume-status](./quick/260710-nkk-sheet-cols-resume-status/) |
| 260710-wk6 | Nextcloud resume upload — file resume → WebDAV PUT + OCS password-protected public share link stored in `resume_url` column + «Резюме (ссылка)» sheet column; awaited-with-timeout, fully fail-soft (Nextcloud down never breaks reg); share password never persisted | 2026-07-10 | 37c3297 | [260710-wk6-nextcloud-resume-upload](./quick/260710-wk6-nextcloud-resume-upload/) |
| 260711-0c7 | Backfill script `scripts/backfill_resumes.py` — uploads OLD delegates' resumes (have resume_file_id, no resume_url) to Nextcloud, writes resume_url in DB; `--dry-run`/`--limit`, per-user fail-soft, DB-only (reminds to press «Пересобрать таблицу») | 2026-07-11 | 7718a91 | [260711-0c7-backfill-old-resumes-to-nextcloud](./quick/260711-0c7-backfill-old-resumes-to-nextcloud/) |
| 260711-16c | Sharing redesign — dropped per-file OCS shares; sheet links are now deep-links into ONE manual password-protected folder share (`{PUBLIC_URL}/s/{TOKEN}/download?path=/&files=`). ФИО-based filenames, text resumes uploaded as `.txt`. New config `NEXTCLOUD_PUBLIC_URL` + `NEXTCLOUD_FOLDER_SHARE_TOKEN`; backfill handles both types | 2026-07-11 | fe5cb96 | [260711-16c-resume-links-via-one-folder-share-deep-l](./quick/260711-16c-resume-links-via-one-folder-share-deep-l/) |
| 260713-i4p | Payment UX — renamed menu button «💳 Загрузить чек» → «💳 Оплата» (label now reads as "payment", not just "upload"); requisites now surfaced in the pay-later defer message AND the multi-option picker (no longer hidden behind option selection); synced overdue-reminder + docstring button-name references. Reply-keyboard button text and handler filter kept byte-identical | 2026-07-13 | 41b8dfe | [260713-i4p-payment-ux-fixes-rename-zagruzit-chek-bu](./quick/260713-i4p-payment-ux-fixes-rename-zagruzit-chek-bu/) |
| 260713-w9h | Logic B1b — 4 logic criticals from all-phases review. CR-1: receipt queue got `offset`, both moderation/receipt tinder queues page past first 50 (no false «нет заявок» at 1000+ scale). CR-7: consent bypass closed — `process_consent_accept` gates on `_consent_key_matches`, clears used keyboard (stale scroll-up re-tap can't skip a consent). CR-8: `_extract_referrer_id`+`_parse_age` require ASCII digits (²/①/fullwidth no longer crash int()); global `@dp.errors()` handler logs exception+update (was silent-drop). CR-9: `active_sheet_row` projects onto frozen `bot_settings` header snapshot (set at startup+rebuild), mid-event toggle no longer misaligns rows, live fallback = no migration break. 140 tests. NOTE: executor died mid-run on billing error after CR-1 commit; CR-7/8/9 finished inline. Manual-UAT pending for CR-1/CR-7 (behavioral) | 2026-07-13 | 4a9aed1 | [260713-w9h-logic-b1b-pagination-offset-in-admin-mod](./quick/260713-w9h-logic-b1b-pagination-offset-in-admin-mod/) |
| 260713-jgi | Security B1a — fixed all HTML-injection + CSV-injection criticals from all-phases review. html.escape'd registrant-controlled full_name/username/email/university/source in admin /find,/stats,«Источники» (CR-2/3/4), full_name in user_actions referrals (CR-5), admin event_date/place/captions in info msgs (C-WR-02), consent card admin string (A-WR-03); added `_csv_safe` formula-injection neutralizer (prefix `'` on cells starting =+-@\t\r) to db.export_users_csv (CR-6) + 10-case unit test. 131 tests pass (+10). admin.py uses `html as html_module` — kept distinct from plain `import html` elsewhere | 2026-07-13 | 215db42 | [260713-jgi-security-b1a-html-escape-user-admin-text](./quick/260713-jgi-security-b1a-html-escape-user-admin-text/) |
| 260721-msh | Новый вопрос регистрации «Аламни/айсекер» (`alumni_status`) — клавиатура Аламни/Айсекер/Ни то, ни другое, спрашивается после телефона, дефолт OFF, входит в пресет 🎉 Party. Колонки в основном и party-листе, строка в карточке модерации, CSV-label, +10 тестов. ⚠️ после деплоя нужна «♻️ Пересобрать таблицу» (колонка вставлена в середину шапки) | 2026-07-21 | dd1aca9 | [260721-msh-add-alumni-status-question](./quick/260721-msh-add-alumni-status-question/) |
| 260721-fst | Party fork inline buttons renamed «Вечеринка с/без ночёвки» → «Гости с/без ночёвки» (`_party_fork_kb`, handlers/registration.py). callback_data tokens untouched | 2026-07-21 | d69abf4 | — (fast) |

## Deferred Items

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| Gamification | GAME-01..04 (task system, coins mechanics) | v2 backlog | Roadmap init |
| Roles | ROLE-01..02 (Delegate/Manager/Admin roles) | v2 backlog | Roadmap init |

## Session Continuity

Last session: 2026-07-21T00:18:44.507Z
Stopped at: Completed 05-06-PLAN.md (checkpoint deferred to end-of-phase UAT)
Resume file: None
