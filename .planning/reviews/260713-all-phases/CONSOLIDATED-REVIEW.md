---
status: issues_found
scope: all-phases (whole codebase, deep)
date: 2026-07-13
zones: 4
files_reviewed: 19
findings:
  critical: 9
  warning: 23
  info: 17
  total: 49
---

# Consolidated Code Review — All Phases (2026-07-13)

Deep review of the whole app (~19 source files) split across 4 parallel zones.
Per-zone detail: `A-REGISTRATION-REVIEW.md`, `B-ADMIN-REVIEW.md`, `C-USERFLOW-REVIEW.md`, `D-DATA-SERVICES-REVIEW.md`.

**Total: 9 Critical / 23 Warning / 17 Info.**

---

## ⚠️ Systemic theme #1 — HTML injection (bot-wide parse_mode=HTML)

`main.py:61` sets `DefaultBotProperties(parse_mode=ParseMode.HTML)`, so **every** `.answer()`/`.edit_text()`
parses HTML by default. Any user- or admin-controlled string interpolated without `html.escape()` either
breaks message delivery (unclosed tag → Telegram 400) or injects markup. This bug class recurs in all 4 zones
and accounts for 5 of the 9 Criticals. **Fix once, systematically** — audit every f-string that reaches a send,
add `html.escape()` at every interpolation of a DB/user/admin field. Consider a lint/helper.

Affected: B-CR-02, B-CR-03, B-CR-04, C-CR-01, C-WR-02, A-WR-03.

---

## Critical (9) — fix before next event

| # | Zone | Location | Problem |
|---|------|----------|---------|
| CR-1 | B | `admin.py:2204-2223`, `:2438-2452` | **Moderation queue breaks at scale.** `get_pending_users`/`get_receipt_pending_users` fetch fixed `limit=50`, never advance `offset`. Manager skipping past 50 items gets a false "нет заявок". Breaks the exact 1000+ scale the pagination was built for (core project constraint). |
| CR-2 | B | `admin.py:188-197` (`/find`) | Unescaped registrant `full_name`/`username`/`email` in HTML message → stored HTML injection / delivery DoS. Any applicant can trigger. |
| CR-3 | B | `admin.py:267-305` (`/stats`, «📊») | Unescaped registrant-controlled `university` (free-text) in HTML. |
| CR-4 | B | `admin.py:318-334` («📈 Источники») | Unescaped registrant-controlled `source` in HTML. |
| CR-5 | C | `user_actions.py:281-285` (`my_referrals`) | Unescaped `full_name` in HTML — a registrant with `<`/`&` in name breaks the **referrer's** message. Sibling `render_leaderboard` escapes; this one doesn't. |
| CR-6 | D | `db.py::export_users_csv()` | **CSV/formula injection** (CWE-1236). `full_name`/comments starting `= + - @` execute as live formula when admin opens export in Excel. |
| CR-7 | A | `registration.py::process_consent_accept` | **Consent bypass.** No validation that tapped consent matches active `_consent_key`; prior inline button not disabled → stale re-tap skips a required consent. Compliance-relevant. |
| CR-8 | A | `registration.py` `_extract_referrer_id`, `process_age` | `str.isdigit()` accepts Unicode digits (`²`,`①`) that crash `int()` with unhandled `ValueError`. No global error handler → update silently dropped. Reachable via `/start <arg>` and age step. |
| CR-9 | A | `registration.py::active_sheet_row()` | Recomputed per-registration against **live** `reg_q_*` toggles, not a frozen snapshot. Admin toggling a question mid-event silently misaligns subsequent rows vs header + earlier rows. |

---

## Warning (23)

**Zone A** — WR-01 stale `_reg_total` → wrong progress numbering · WR-02 reserved words ("Отмена"/"Другое"/"Пропустить") collide with admin option text · WR-03 consent card HTML unguarded · WR-04 `_ask_step` 215-line if/elif · WR-05 unscoped `reg_cancel_yes/no` clears unrelated FSM.

**Zone B** — inconsistent `ADMIN_IDS` gate on 4 callbacks (`process_broadcast_all`, `process_broadcast_local_file`, `settings_cancel`, `broadcast_cancel`) · unguarded `int()` in `sched_cancel` · scheduled photo broadcast loses caption formatting (`caption` vs `html_text`) · `_wait_and_send_album` FSM stuck on unsupported media early-return · `reject_text` used but no edit UI + inconsistent escape.

**Zone C** — WR-01 `process_payment_option`/`process_pay_later` fully unguarded, no `callback.answer()` guarantee (breaks file's fail-soft) · WR-02 admin `event_date`/`place_name`/`place_address`/captions unescaped in HTML · WR-03 defer-from-picker leaves `payment_option`/`payment_due` NULL → permanently excluded from overdue sweep · WR-04 unvalidated admin URLs in `InlineKeyboardButton(url=...)` · WR-05 requisites resolve/escape/format duplicated ×3.

**Zone D** — WR-01 `sync_incomplete_sheet_job` (every 2h) 3-col header vs 4-col rows + skips `dropout_step_label()` → overwrites sheet with mismatched data each cycle · WR-02/03 `main.py` 3× `create_task` with no stored ref (GC risk) + no graceful shutdown · WR-04 APScheduler `misfire_grace_time=3600` → jobs during >1h downtime silently dropped, DB stuck `pending` · WR-05 `sheets.py` cached `_sheet` TOCTOU race across `to_thread` (no lock) · WR-06 full PII (phone/email) logged at INFO/ERROR on every append · WR-07 `NEXTCLOUD_VERIFY_TLS` defaults `False` (creds+PII over unverified TLS) · WR-08 `_ensure_column`/`_column_exists` f-string SQL identifiers (safe today, landmine).

---

## Info (17) — see per-zone files

Dead/vestigial: `Registration.payment_option` state, `expectations_ar` always-empty column, `status_msg` in `process_broadcast`, `NEXTCLOUD_BASE_URL`. Cosmetic: `"• None"`/`"None"` for NULL `full_name`, ternary in `get_main_menu_kb`, unused `bot` params, `/scheduled` double-escape. Latent: unreachable `..` gap in `_safe_name`, `IndexError` in `_parse_coins_amount`, unbounded conditional-format rules, redundant exception tuple, unpinned gspread, pydantic-v1 config, misleading `approve_all_pending` docstring.

---

## Recommended fix order

1. **Security batch (1 pass):** CR-2/3/4/5 + C-WR-02 + A-WR-03 — the systemic HTML-escape audit. Cheap, high impact.
2. **CR-6** CSV injection — prefix `'` on cells starting `= + - @` in `export_users_csv`.
3. **CR-1** pagination offset — real blocker at event scale.
4. **CR-7** consent bypass — compliance.
5. **CR-8** Unicode-digit crash + add a global error handler (missing entirely — worth its own task).
6. **CR-9** freeze `active_sheet_row` snapshot at registration start.
7. Warnings by zone.

## Verified clean
Parameterized SQL paths (no SQLi), blocking I/O offloaded via `to_thread` in sheets/allowlist, reply-button↔handler text pairs byte-match (incl. renamed «💳 Оплата»), receipt catch-all lets `/commands` through, `pay_option:` parsing bounds-checked, sheet writes use RAW input-option (Sheets-side formula-injection not exploitable — only the CSV export path is, CR-6).
