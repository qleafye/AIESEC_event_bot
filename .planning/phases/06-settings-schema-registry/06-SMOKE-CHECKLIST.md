# Phase 6 Plan 7: Final Coverage + Smoke Checklist

**Status:** Automated results recorded. Human smoke section below is UNCHECKED — pending live-bot verification.

## 1. Automated Regression Results (Task 1)

Run 1 — plan-scope suite:
```
python -m pytest tests/test_settings_groups_c0x.py tests/test_settings_consumers_phase6.py tests/test_registration_phase4.py tests/test_registration_phase5.py tests/test_payment_phase5.py -q
```
Result: **149 passed** in 62.03s (0 failed, 0 skipped).

Run 2 — full suite (cross-suite regression check):
```
python -m pytest -q
```
Result: **395 passed** in 122.32s (0 failed, 0 skipped).

Both exit 0. No unrelated regression. This matches the state carried forward from 06-06 (395/395) — no drift introduced by this plan (no source files were modified by Task 1; it is a read-only aggregation/checklist task).

## 2. Flagged-Boundary Coverage Sweep (06-05 → 06-06 → 06-07 carry-forward)

06-05's Summary flagged an open question for this plan: does `render_settings_text`'s own `registration_mode` read, plus the generic multi-key toggle helpers `_toggle_module_setting` / `_toggle_approval_setting` / `_toggle_value_setting`, need migration, or are they correctly out of scope? 06-06's Summary restated this as 06-07's one remaining piece of REG-02 scope: "confirm no consumer anywhere still hand-rolls a `get_setting(...) or "<default>"` idiom for a key present in SETTINGS_SCHEMA."

**Sweep performed:** grepped every `await get_setting(` call site (not `get_setting_typed`) across `handlers/admin.py`, `handlers/registration.py`, `handlers/payment.py`, `services/scheduler.py`, `services/reminders.py`, `keyboards/builders.py`, and cross-referenced each key against `settings_schema.SETTINGS_SCHEMA`.

**Finding — genuine gap, NOT closed by this plan (scope decision deferred to orchestrator/user):**

Four read-sites in `handlers/admin.py` still use the raw `get_setting(key) or "<default>"` idiom for keys that ARE present in `SETTINGS_SCHEMA` (type `toggle`/`enum`, defaults already registered in the 06-04 wave):

| Line | Site | Keys affected | Current idiom |
|------|------|----------------|----------------|
| 466 | `render_settings_text`'s own `registration_mode` read | `registration_mode` | `await get_setting("registration_mode") or "short"` |
| 716 | `_toggle_approval_setting` (shared helper) | `full_approval`, `short_approval`, `party_approval` | `await get_setting(key) or default` |
| 748 | `_toggle_module_setting` (shared helper) | `payment_enabled`, `consent_enabled`, `party_enabled`, `party_fork_question` | `await get_setting(key) or "off"` |
| 793 | `_toggle_value_setting` (shared helper) | `reg_university_mode`, `edu_conditional`, `reg_show_progress`, `payment_reminders_enabled` | `await get_setting(key) or default` |

This is **not a behavior bug** — 06-01/D-15's parse-equivalence tests already prove that for every one of these keys, the registry's `default` is byte-identical to the hardcoded literal each site falls back to (that is precisely what makes the registry a safe drop-in). So there is zero live-behavior risk from these four sites remaining as they are. What is open is a **completeness question against the phase's own north star** ("настройка через один файл" — one file should own metadata for a key, including every consumer read of it), not a correctness question.

Both 06-05 and 06-06 independently treated these as deliberately out-of-scope for their own waves (explicit interfaces-list boundary, not an oversight), and both summaries deferred the final call to 06-07. **06-07's own Task 1 (this task) is itself scoped only to test-running + checklist authoring — it does not include a code-migration task**, and the plan's `<tasks>` block has exactly one `auto` task (this one) followed directly by the human-verify checkpoint. Migrating these four shared, multi-key-parameterized helpers would touch behavior shared by 12 different toggle buttons at once — exactly the kind of "significant structural" change do-not-silently-expand-scope guidance calls out. Per the explicit instruction for this execution ("document clearly ... so the user/orchestrator can decide — do NOT silently expand scope"), **no code change was made in this plan.**

**Recommendation for the orchestrator/user:** if full closure of the "one file, no exceptions" principle is wanted, open a small follow-up plan (would not require a new phase — 4 read-site swaps, same byte-for-byte-preserving pattern as 06-05/06-06, each already covered by existing parse-equivalence tests) to finish migrating these 4 shared read-sites. If the current state (registry owns metadata + defaults; these 4 shared write-path helpers still do their own `get_setting(key) or default` read before flipping) is acceptable as the phase's final state, no further action needed — REG-01/REG-02/REG-03 can still be marked complete since:
- REG-01 (registry exists, is the metadata source of truth) — fully met.
- REG-02 (consumers read via registry) — met for all *display* and all *behavioral gate* reads (06-05, 06-06); the 4 flagged sites are *write-path pre-read-before-flip* sites in shared toggle handlers, not the setting's authoritative read path (the authoritative read for all of these keys, in `render_settings_text`/`build_settings_keyboard`, already migrated in 06-05).
- REG-03 (admin-UI renders from registry) — fully met (06-01..06-05).

No other gaps found in the sweep: every other raw `get_setting(...)` call site inspected (text/int/list/photo/file fields, `payment_options`, `consent_list`, `university_options`, `sheet_header_schema`, party per-track overrides, preselect_*, nudge_*, `pending_reminder_enabled`) reads a key that is either (a) not yet in `SETTINGS_SCHEMA` at all (out of this phase's scope by design — e.g. `preselect_enabled`, `nudge_enabled`, `pending_reminder_enabled` were never part of the D-09 toggle set), or (b) a raw text/list/photo/file field for which raw I/O is the intended pattern per D-07 (registry only adds typed parsing for `toggle`/`int`/`enum`/`date` types; text/list/photo/file consumers legitimately keep reading raw where no typed parse is needed).

## 3. Human Smoke Checklist (Task 2 — checkpoint:human-verify)

**Instructions:** On a real bot instance (or a faithful copy of live `bot_settings` data), as an admin, walk through each step below and mark PASS/FAIL. This is the D-18 manual smoke gate — required because the project has no CI/linter (CLAUDE.md).

### 3.1 Landing screen

- [ ] Open `/admin` → `⚙️ Настройки форума`. Landing screen text (all `📝 Форма регистрации`, `✅ Модерация…`, `🎉 Трек вечеринки`, `⏰ Автонапоминания…` lines) reads exactly as before the migration.

### 3.2 MANDATORY per-toggle-button before/after comparison

For EVERY button below: record the button TEXT before and after the deploy (must be identical), then tap it once and confirm (a) the underlying setting flips, (b) the button text updates to reflect the new state, (c) no error/exception in logs.

| # | Button | Text before | Text after | Tap → flips? | Tap → text updates? | PASS/FAIL |
|---|--------|-------------|------------|----------------|----------------------|-----------|
| 1 | 📝 Регистрация toggle (`settings_toggle_reg`) | | | | | |
| 2 | 🎁 Бонус (`reg_bonus_enabled` toggle) | | | | | |
| 3 | ✅ Полная форма — модерация (`settings_toggle_full_approval`) | | | | | |
| 4 | ✅ Краткая форма — модерация (`settings_toggle_short_approval`) | | | | | |
| 5 | 🔔 Уведомление (`pending_notify_mode` toggle) | | | | | |
| 6 | 💳 Оплата (`toggle_payment_enabled`) | | | | | |
| 7 | ⏰ Автонапоминания (`toggle_payment_reminders`) | | | | | |
| 8 | 📋 Согласия (`toggle_consent_enabled`) | | | | | |
| 9 | 🏫 ВУЗ — режим списка/ввода (`toggle_uni_mode`) | | | | | |
| 10 | 🎓 ВУЗ/курс — условность (`toggle_edu_conditional`) | | | | | |
| 11 | 🔢 Нумерация (`toggle_show_progress`) | | | | | |
| 12 | 🎉 Трек вечеринки (`toggle_party_enabled`) | | | | | |
| 13 | 🔀 Вопрос-развилка (`toggle_party_fork_question`) | | | | | |
| 14 | ✅ Модерация вечеринки (`settings_toggle_party_approval`) | | | | | |

### 3.3 Migrated group sub-screens

- [ ] `🎪 Событие/Медиа` — field labels, order, `✏️ задано / — не задано / по умолчанию` flags, `── не настроено ──` collapse match prior behavior.
- [ ] `📝 Регистрация` — same checks.
- [ ] `💳 Оплата` — same checks.
- [ ] `🎉 Party` — same checks.
- [ ] `📋 Согласия` — same checks.

### 3.4 Edit round-trip (one field per group)

- [ ] Edit `🗓 Дата` (event group) → saves, flag flips to `задано`.
- [ ] Edit a payment field (e.g. `payment_deadline`) → saves, flag flips to `задано`.
- [ ] Edit a party text (e.g. `party_closed_text`) → saves, flag flips to `задано`.

### 3.5 Default-fallback display

- [ ] `party_closed_text` still shows `по умолчанию` when unset.
- [ ] `party_sheet_tab` still shows `по умолчанию` when unset.

### 3.6 reg_q_* question toggle

- [ ] Flip a `reg_q_*` question in `📋 Вопросы регистрации` → toggles correctly; default-off/on question set unchanged from pre-migration.

### 3.7 Scheduler timing (spot check)

- [ ] Pending-application reminder still fires on its interval (or confirm no error in logs).
- [ ] Payment-deadline / payment-reminders timing unchanged (spot check or confirm no error in logs).

### 3.8 Restart-persistence

- [ ] Restart the bot. Confirm NO setting reset to blank/default (spot-check a handful of previously-`задано` fields across all 5 migrated groups + the 14 toggle buttons above).

## 4. Sign-off

Result: **PENDING** — awaiting human execution of Section 3 on a real bot instance.

Type "approved" once all Section 3 boxes are checked PASS, or describe the specific drift/regression observed (which step, which field/button, expected vs. actual).
