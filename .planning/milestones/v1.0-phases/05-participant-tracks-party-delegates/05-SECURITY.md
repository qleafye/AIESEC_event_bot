# Phase 5 — Participant Tracks (Party Delegates): Security Audit

**Verdict: SECURED — all declared threat mitigations verified present in code.**

**Threats closed:** 33/33 (25 `mitigate` verified in code + 8 `accept` documented below)
**Open (BLOCKER):** 0
**Unregistered flags:** 0
**ASVS target:** L1 (no config declared; L1 baseline applied)

Audit method: every `mitigate` threat was confirmed by locating the actual mitigation call/branch
in the implementation (not documentation). Implementation files were treated as read-only and the
code is the source of truth. Deep-link parsing, track persistence, per-track setting keys, admin
authorization, cross-track pricing, approval independence, and Sheets injection were each traced end
to end.

---

## Severity Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW | 2 (pre-existing / out-of-scope, noted for tracking) |

No CRITICAL or HIGH findings. No BLOCKER. Phase may ship.

---

## Focus-area verdicts

### 1. Deep-link `/start` param parsing — SAFE
- `_extract_party_track` (`handlers/registration.py:846-852`) is an **exact-match** lookup in the
  fixed 2-entry `_PARTY_TAG_MAP` (`registration.py:843`) after `.strip()`. No prefix/startswith/regex,
  so no crafted payload yields a track outside the closed vocabulary. No arbitrary string reaches
  `participant_type`.
- `_extract_referrer_id` (`registration.py:795-807`) requires `arg.isascii() and arg.isdigit()`,
  rejecting Unicode digits and non-numeric input, and drops self-referral (`referrer_id == current_user_id`).
  No type confusion, no crash.
- `_extract_source_tag` (`registration.py:826-832`) only strips a literal `src_` prefix; the three
  extractors are mutually exclusive by construction.
- All DB writes of these values are parameterized (`add_user` `registration.py`/`database/db.py:213-352`,
  `mark_reg_started` `db.py:566-576`) — no SQL injection surface.

### 2. Track persistence / self-assignment — SAFE
- `participant_type` is written only from the closed vocabulary (deep-link map or `party_pick`
  handler). There is **no user-facing handler that mutates `participant_type` post-registration**.
- `party_pick` (`registration.py:1445-1474`) maps the token through the SAME `_PARTY_TAG_MAP` and
  rejects any unmapped token (`registration.py:1457-1459`); it also re-checks `party_enabled`
  (`registration.py:1461-1464`), closing the render-then-flip window.
- Bare-repeat `/start` recovery reads `participant_type` back from `reg_started` (`get_reg_started_track`,
  `db.py:588-591`) — a column only ever written from the closed vocabulary. A user cannot inject an
  arbitrary track by replaying `/start`.

### 3. Per-track question override keys (`reg_q_<step>__party`) — SAFE
- The suffixed key is always built from a `setting_key` sourced from the hardcoded `REG_FLOW` list
  (`_apply_party_preset` `registration.py:339-357`; `_is_step_enabled_for_track` `registration.py:411-423`;
  `_prompt` `registration.py:386-401`).
- The one place a key derives from client input — `reg_q_ptoggle:{setting_key}` — validates it against
  the `REG_FLOW` whitelist **before** suffixing or writing
  (`handlers/admin.py:2154-2160`: `valid_keys = {sk for _, sk, *_ in REG_FLOW}`; unknown key → alert, no write).
- Even absent validation, `get_setting`/`set_setting` are fully parameterized (`db.py:184-199`) — the
  key is a bound value, never interpolated into SQL. No injection via step name.

### 4. Authorization on party settings — SAFE
- Every party-settings callback handler opens with `callback.from_user.id not in config.ADMIN_IDS`
  → `show_alert` denial: `toggle_party_enabled` path (`admin.py`), `toggle_party_approval`
  (`admin.py:613-617`), `reg_q_track_switch` (`admin.py:2134-2136`), `toggle_party_question`
  (`admin.py:2151-2153`), party preset apply (`admin.py:2267`). A non-admin crafted callback cannot
  flip party settings or write a `__party` override.

### 5. Cross-track pricing — SAFE
- Rendered keyboard is track-filtered (`_visible_options` `handlers/payment.py:136-151`) but
  `pay_option:{i}` indexes the **unfiltered** list, preserving index↔tariff binding under settings edits
  (`start_payment_step` `payment.py:185-226`).
- `process_payment_option` (`payment.py:241-282`) re-parses options, guards the index
  (`ValueError` + `0 <= idx < len(options)`, `payment.py:246-253`), then **re-resolves the caller's track
  server-side via `get_user`** and rejects a tapped index whose track set excludes the caller
  (`payment.py:264-271`) before any status write or details render. A full delegate cannot buy a
  party-only tariff and vice-versa. Untracked (`tracks is None`) options are intentionally offered to all.
- Single/free path reads `visible[0]`, not `options[0]` (`payment.py:221`), so a party-only tariff at a
  non-zero index is the one charged.

### 6. Approval independence (`party_approval`) — SAFE (fail-closed)
- `_decide_status` party branch (`registration.py:64-78`) resolves from `party_approval` alone via
  `party_setting or "manual"` — an unset/unreadable setting **moderates** (pending), never silently
  auto-approves. The auto-approve path (`finalize_registration` `registration.py:2338-2339` → `approve_user`)
  is the same shared path used by all tracks; it sends the welcome/payment step and skips no validation
  gate (the FSM flow already validated all answers). Double-approval is still guarded by the atomic
  `UPDATE … WHERE status='pending'` + `rowcount==1` check (`db.py:668-675`), which party rows share.

### 7. Google Sheet track column + broadcast filter — SAFE
- Formula injection: `party_sheet_row` (`registration.py:1097-1108`) passes **every** registrant cell
  through `database.db._csv_safe` (`db.py:474-481`), which prefixes `'` on values starting with
  `= + - @ \t \r` (`_CSV_INJECTION_PREFIXES`, `db.py:471`).
- Exclusive routing: `finalize_registration` (`registration.py:2291-2298`) schedules exactly one append
  via if/else — party → `append_to_party_sheet`, else → `append_to_sheet`. No dual-write, no cross-tab leak.
- PII-safe logging: `append_to_named_sheet` logs only `telegram_id` + tab name, never the row
  (`services/sheets.py:426-442`).
- Tab targeting: `party_sheet_tab` is admin-only and used solely as a gspread worksheet **title**
  (`sheets.py:397-412`, `append_to_party_sheet` `registration.py:1114-1118`) — never interpolated into a
  query or A1 range.
- Broadcast filter: `participant_type` is added to the `_FILTER_COLUMNS` whitelist (`db.py:808-814`);
  `_build_filter_clause` (`db.py:817-836`) only interpolates column names from that closed set and binds
  all values as `?`. `get_distinct_filter_values` (`db.py:839-859`) validates against the same whitelist.

---

## Threat Verification Table

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-05-01-01 | Tampering | mitigate | CLOSED | Exact-match `_PARTY_TAG_MAP` — registration.py:843, 846-852 |
| T-05-01-02 | EoP | mitigate | CLOSED | `party_enabled` gate before flow, early return — registration.py:1390-1402 |
| T-05-01-03 | Spoofing | mitigate | CLOSED | `party_fallback_full` starts FULL only, no params — registration.py:1477-1491 |
| T-05-01-04 | Tampering | mitigate | CLOSED | Additive `_ensure_column … DEFAULT 'full'`, no DROP — db.py:179-180 |
| T-05-01-05 | DoS | mitigate | CLOSED | `get_reg_started_track` in try/except fail-soft — registration.py:1408-1413 |
| T-05-01-06 | Info Disclosure | accept | CLOSED | Logs only telegram_id + exc text — registration.py:1401 (accepted, see below) |
| T-05-02-01 | Tampering | mitigate | CLOSED | `_apply_party_preset` writes only `__party` keys from REG_FLOW — registration.py:339-357 |
| T-05-02-02 | EoP | mitigate | CLOSED | `__party` read gated on `_is_party_track` — registration.py:411-423 |
| T-05-02-03 | Info Disclosure | mitigate | CLOSED | `_prompt` returns text to caller only, no cross-user interpolation — registration.py:386-401 |
| T-05-02-04 | DoS | accept | CLOSED | Extra `get_setting`/step, party-only, negligible at scale (accepted, see below) |
| T-05-02-05 | Tampering | mitigate | CLOSED | Empty override falls back via truthiness — registration.py:397-401 |
| T-05-03-01 | EoP | mitigate | CLOSED | ADMIN_IDS re-check on every party callback — admin.py:2134-2136, 2151-2153, 2267 |
| T-05-03-02 | Tampering | mitigate | CLOSED | `setting_key` validated against REG_FLOW before write — admin.py:2154-2160 |
| T-05-03-03 | Injection | mitigate | CLOSED | `participant_type` fallback `html_module.escape` — admin.py:2437-2442 |
| T-05-03-04 | Tampering | mitigate | CLOSED | `participant_type` in `_FILTER_COLUMNS` whitelist, params bound — db.py:808-836 |
| T-05-03-05 | Info Disclosure | accept | CLOSED | `get_distinct_filter_values` returns track constants only — db.py:839-859 (accepted) |
| T-05-03-06 | Repudiation | accept | CLOSED | No settings audit log; ADMIN_IDS trusted; deferred (accepted, see below) |
| T-05-04-01 | EoP | mitigate | CLOSED | `party_pick` maps token via `_PARTY_TAG_MAP`, re-checks `party_enabled` — registration.py:1452-1464 |
| T-05-04-02 | EoP | mitigate | CLOSED | `_decide_status` fail-closed `party_setting or "manual"` — registration.py:74-76 |
| T-05-04-03 | Spoofing | mitigate | CLOSED | Fork rendered only when `party_enabled == "on"` — registration.py:881-886 |
| T-05-04-04 | DoS | mitigate | CLOSED | `get_user` in approve_user wrapped, defaults `full` — registration.py:2157-2162 |
| T-05-04-05 | Info Disclosure | accept | CLOSED | `approve_text__party` admin-authored, no registrant data — registration.py:2109-2113 (accepted) |
| T-05-04-06 | Tampering | mitigate | CLOSED | Shared atomic `UPDATE … WHERE status='pending'`, rowcount==1 — db.py:668-675 |
| T-05-05-01 | Tampering | mitigate | CLOSED | Index preserved via enumerate over full list — payment.py:136-151, 185-204 |
| T-05-05-02 | Tampering | mitigate | CLOSED | ValueError guard + bounds check preserved — payment.py:246-253 |
| T-05-05-03 | EoP | mitigate | CLOSED | Server-side track re-resolve + reject — payment.py:264-271 |
| T-05-05-04 | DoS | mitigate | CLOSED | Empty `visible` → free completion path — payment.py:211-217 |
| T-05-05-05 | Tampering | mitigate | CLOSED | Malformed line falls back to price 0, never raises — payment.py:113-133 |
| T-05-05-07 | Tampering | mitigate | CLOSED | Single/free path reads `visible[0]` not `options[0]` — payment.py:221 |
| T-05-06-01 | Injection | mitigate | CLOSED | `_csv_safe` on every party cell — registration.py:1107; db.py:471-481 |
| T-05-06-02 | DoS | mitigate | CLOSED | Fire-and-forget task in try/except, MAX_RETRIES bounded — registration.py:2291-2298; sheets.py:431-442 |
| T-05-06-03 | Info Disclosure | mitigate | CLOSED | Logs only telegram_id + tab name — sheets.py:426, 434 |
| T-05-06-04 | Info Disclosure | mitigate | CLOSED | Exclusive if/else, one append per reg — registration.py:2291-2298 |
| T-05-06-05 | Tampering | mitigate | CLOSED | `party_sheet_tab` admin-only, used as worksheet title only — sheets.py:397-412 |
| T-05-06-06 | DoS | accept | CLOSED | Auto-create limited to 2 admin-named tabs (accepted, see below) |

---

## Accepted Risks Log

These `accept`-disposition threats are recorded here as required; each is a conscious, justified
acceptance, not an unmitigated gap.

- **T-05-01-06 (Info Disclosure — party gate logging):** Only `telegram_id` and exception text are
  logged, never raw `command.args`. Consistent with the repo PII-safe logging convention. No new
  disclosure surface. Accepted.
- **T-05-02-04 (DoS — extra `get_setting` per step):** At most one additional read per step for party
  users only, against local SQLite at 1000–1500-user season scale. No caching layer warranted. Accepted.
- **T-05-03-05 (Info Disclosure — distinct filter values):** `get_distinct_filter_values("participant_type")`
  returns only the fixed track constants, not user PII. Accepted.
- **T-05-03-06 (Repudiation — party settings changes):** No settings audit log exists repo-wide; adding
  one is out of Phase 5 scope. `ADMIN_IDS` is a small trusted set. Accepted; noted for a future audit-log
  phase.
- **T-05-04-05 (Info Disclosure — `approve_text__party`):** Admin-authored text sent only to the approved
  user; no registrant data interpolated. Accepted.
- **T-05-06-06 (DoS — unbounded tab auto-creation):** `_get_named_sheet` auto-creates a missing tab, but
  only two admin-controlled names (party, allowlist) are ever requested. No user action triggers repeated
  creation. Accepted.

---

## Additional (non-blocking) observations — outside Phase 5 declared scope

- **LOW / pre-existing — main-sheet path lacks CSV-formula neutralization.** `party_sheet_row` correctly
  applies `_csv_safe` (this phase's surface is mitigated), but the main-tab `active_sheet_row` does **not**
  apply `_csv_safe` (explicitly noted at `registration.py:1104-1105`). A crafted ФИО written to the main
  tab can still render as a formula when an organizer opens the sheet. This is a pre-existing gap outside
  the Phase 5 threat model but should be tracked and closed in a follow-up (mirror the `_csv_safe`
  treatment used here). **Fix:** wrap main-sheet cell values in `_csv_safe` in `active_sheet_row` /
  `_build_sheet_row`.
- **LOW / pre-existing — unreferenced fire-and-forget tasks.** `asyncio.create_task(append_to_party_sheet(...))`
  (`registration.py:2294`) keeps no reference to the task, so it can in principle be garbage-collected
  before completion (a known asyncio gotcha). This matches the existing `append_to_sheet` pattern and is
  fail-soft (a lost append only drops a spreadsheet row, never blocks/crashes registration), so impact is
  minimal. **Fix (optional):** retain task references in a module-level set until done.

No unregistered attack surface (`## Threat Flags` in all six SUMMARYs report "None"; each new callback,
setting key, DB column, and Sheets path maps to a registered threat ID above).
