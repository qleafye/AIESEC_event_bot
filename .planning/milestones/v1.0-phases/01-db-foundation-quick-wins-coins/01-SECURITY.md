# Security Audit — Phase 1: DB Foundation + Quick Wins + Coins

**Audited:** 2026-07-24
**Scope:** `database/db.py`, `handlers/{admin,registration,user_actions,payment}.py`, `services/allowlist.py`, `config.py`, `main.py`
**Method:** Code is source of truth. Each declared threat in the phase PLAN threat registers (01-01..01-04) was verified against the actual implementation by grep + read. Implementation files are read-only; no code was modified.

---

## Verdict: PASS (SECURED)

Every declared mitigation for Phase 1 is present and correctly located in the shipped code. No CRITICAL or HIGH findings. The SQL layer is fully parameterized, admin authorization is enforced on every coins/admin entry point (message filter + per-callback re-check), the coins ledger is append-only (no read-modify-write race), the subscription check is genuinely fail-open, and migrations are additive/idempotent (safe against the ~590-user live DB). A small number of LOW / informational hardening notes are listed below.

**Findings by severity:** CRITICAL 0 · HIGH 0 · MEDIUM 0 · LOW 3 · INFO 2

---

## Threat Register Verification

| Threat | Category | Disposition | Status | Evidence |
|--------|----------|-------------|--------|----------|
| T-01-01 | Tampering — coins ledger | mitigate | CLOSED | `add_coins` INSERT-only (`db.py:498-506`); balance = `SUM(delta)` (`db.py:509-515`). No `UPDATE coins` anywhere. |
| T-01-02 | Injection — all new db helpers | mitigate | CLOSED | All queries use `?` placeholders. Only identifier interpolation is column names from whitelists (`_FILTER_COLUMNS` `db.py:808`, `update_payment_status` col tuple `db.py:931`) guarded by `_assert_identifier`/whitelist. |
| T-01-03 | Elevation/DoS — status migration | mitigate | CLOSED | `status TEXT DEFAULT 'approved'` (`db.py:76`); `add_user` ON CONFLICT DO UPDATE omits `status` (`db.py:232-291`). |
| T-01-04 | Info disclosure — reg_started | accept | CLOSED | Table stores only `telegram_id, username, timestamp, nudged_at, last_step` (`db.py:117-128`). No PII. |
| T-01-05 | Tampering — init_db idempotency | mitigate | CLOSED | `_ensure_column` guards via `PRAGMA table_info` (`db.py:24-35`); all `CREATE TABLE IF NOT EXISTS`. No DROP/DELETE/REPLACE of live data. |
| T-02-01 | DoS/Tampering — resume upload | mitigate | CLOSED | `_is_allowed_resume` extension gate (`registration.py:1186-1191`); only `file_id` stored, no download/disk write (`registration.py:1560-1563`). See LOW-1. |
| T-02-02 | Info disclosure — summary render | mitigate | CLOSED | Free-text rendered to Telegram HTML is `html.escape`d (`render_leaderboard` `user_actions.py:67`; `/coins` confirm `admin.py:171`; `/find` `admin.py:193-196`). |
| T-02-03 | Info disclosure — reg_started | accept | CLOSED | Same minimal-data table as T-01-04. |
| T-02-04 | Availability — dropout hooks | mitigate | CLOSED | `mark_reg_started` call wrapped in try/except at flow start (`registration.py:1204-1205`); DB helpers fail-soft. |
| T-03-01 | Elevation — /coins authorization | mitigate | CLOSED | `@router.message(Command("coins"), is_admin)` (`admin.py:149`); `is_admin` = `from_user.id in config.ADMIN_IDS` (`admin.py:80-81`). Keyed on TG user id, not spoofable @username. |
| T-03-02 | Injection — username lookup + reason | mitigate | CLOSED | `get_user_by_username` parameterized + COLLATE NOCASE (`db.py:372`); `reason` bound as `?` (`db.py:503`); amount via `_parse_coins_amount` (ascii-digit only, `admin.py:84-93`). |
| T-03-03 | Stored-XSS-style — reason/name display | mitigate | CLOSED | `html_module.escape` on username/name in `/coins` output (`admin.py:171`); `html.escape` in leaderboard (`user_actions.py:67`). |
| T-03-04 | Tampering — balance manipulation | mitigate | CLOSED | Balance derived from append-only ledger; `/coins` only INSERTs. No user-facing grant path (see Authorization note). |
| T-04-01 | DoS — subscription check at /start | mitigate | CLOSED | `is_subscribed` try/except → None on error (`registration.py:1301-1308`); outer try/except around whole check (`registration.py:1319-1326`). Fail-open, never blocks/crashes /start. |
| T-04-02 | Info disclosure — membership leakage | mitigate | CLOSED | Result only written to internal `subscribed` flag (`registration.py:1324`). User never told status; None writes nothing. |
| T-04-03 | Elevation — segment broadcast | mitigate | CLOSED | Both segment callbacks re-check `callback.from_user.id not in config.ADMIN_IDS` (`admin.py:1400`, `1413`). Callback-level auth present despite message-filter gap (WR-01). |
| T-04-04 | Info disclosure — incomplete segment | accept | CLOSED | Segment resolves to telegram_ids only (`get_incomplete_user_ids` `db.py:597-600`); no PII exposed to other users. |

**All 17 declared threats: CLOSED.**

---

## Focus-Area Findings (per audit request)

### SQL Injection — CLEAN
`database/db.py` is fully parameterized. Every user/admin value binds as `?`. The only places a name is composed into a SQL string are identifiers (never values):
- `_column_exists` / `_ensure_column` — table/column names guarded by `_assert_identifier` (`db.py:18-35`); every caller passes a hardcoded literal.
- `_build_filter_clause` / `get_distinct_filter_values` — column names constrained to `_FILTER_COLUMNS` whitelist + literal `registration_date`; non-whitelisted fields dropped (`db.py:817-859`).
- `update_payment_status` — `col` interpolated only from the hardcoded tuple `("receipt_file_id","paid_at","payment_option","payment_due")` (`db.py:931-933`).
- `add_user` ON CONFLICT and `reg_started` upsert use `excluded.` / `?` throughout — no f-string of user data.

No f-string/`.format()` of user-controlled data into SQL was found.

### Authorization — ENFORCED
- `/coins` gated by `is_admin` message filter (`admin.py:149`). A normal user cannot reach it.
- `add_coins` is called from exactly one site — the admin `/coins` handler (`admin.py:168`). Grep confirms no other production call site. **A normal user has no path to grant themselves coins.** `user_actions.show_my_coins` only reads the caller's own balance (`user_actions.py:78`).
- All 40+ admin callback handlers re-check `callback.from_user.id in config.ADMIN_IDS` (message filters do not cover callbacks — WR-01/D-06 is correctly applied throughout `admin.py`).
- `changed_by=message.from_user.id` is recorded on every ledger row → audit trail exists (`db.py:498-506`, `admin.py:168`).

### Coins Integrity — SOUND
- **Race:** append-only ledger has no read-modify-write, so concurrent `/coins` just insert independent rows — no lost-update. Balance is always a fresh `SUM(delta)`.
- **Negative amounts:** allowed by design (admin debit, D-14); only reachable by an admin.
- **Crafted username:** lookup parameterized; display escaped.
- **Overflow:** see INFO-1.

### getChatMember — FAIL-OPEN CONFIRMED
`is_subscribed` returns `None` on any exception and the caller only writes the flag when `result is not None` (`registration.py:1322-1324`). A failure can **never block** a user and **never leaks** channel/membership data to the user (observe-only internal flag). Verified: no branch raises or returns a blocking value on the subscription path.

### Migration Safety — SAFE
All Phase 1 schema changes are additive: `_ensure_column` (PRAGMA-guarded ALTER ADD) and `CREATE TABLE IF NOT EXISTS`. `status` lands with `DEFAULT 'approved'` so no existing user loses access. No `DROP`, no `DELETE`, no `INSERT OR REPLACE` against live rows. Re-running `init_db()` on the ~590-user DB is non-destructive.

### Secrets — CLEAN
- `BOT_TOKEN`, `PROXY_URL`, Nextcloud passwords typed as `pydantic.SecretStr` (`config.py:6-43`); loaded from `.env`.
- No hardcoded tokens/credentials in source.
- `.env` and `google_credentials.json` are git-ignored (`.gitignore`) and **not tracked** (`git ls-files` returns neither). Confirmed no secret is committed.

---

## LOW / Informational Hardening Notes

**LOW-1 — Resume type check is extension-only, no MIME validation.**
`_is_allowed_resume` (`registration.py:1186-1191`) accepts by filename suffix (`.pdf`/`.docx`) while the receipt path validates `mime_type == "application/pdf"` (`payment.py:365`). A user can upload an arbitrary file renamed to `x.pdf`. Impact is LOW: the bot never downloads/executes it in the reg flow — it stores `file_id` and forwards to Nextcloud. Risk is only to a human who later opens the file.
*Fix (optional):* also check `message.document.mime_type in {"application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}` to match the receipt path's rigor.

**LOW-2 — `/coins` amount is unbounded.**
`_parse_coins_amount` accepts any-length digit string (`admin.py:84-93`). An admin could set an absurd balance. Admin-only, so trust-bounded; no privilege escalation.
*Fix (optional):* clamp to a sane range (e.g. |amount| ≤ 1_000_000) and reject otherwise.

**LOW-3 — Receipt/consent MIME is client-declared.**
`mime_type` on `process_receipt_document` (`payment.py:365`) is supplied by the Telegram client and could be spoofed. Impact LOW — file is stored as `file_id` and reviewed by an admin, never executed. Acceptable for this queue.

**INFO-1 — SQLite 64-bit integer bound on coins delta.**
A `delta` above 2^63-1 would raise "Python int too large to convert to SQLite INTEGER" (`db.py:503`). Admin-only, surfaces as a handled error via the global error handler — no corruption. Bounded by LOW-2's suggested clamp.

**INFO-2 — `google_credentials.json` present in working tree.**
The file exists on disk (needed at runtime) but is correctly git-ignored and untracked. Ensure deployment/backup tooling does not inadvertently include it in any published artifact.

---

## Notes on Scope
This phase was built via ad-hoc quick-tasks; the PLAN threat registers exist and every declared mitigation was verified present. No unregistered new attack surface was found beyond what the registers cover. No implementation changes were made — all findings above are either already-mitigated (CLOSED) or optional LOW/INFO hardening.
