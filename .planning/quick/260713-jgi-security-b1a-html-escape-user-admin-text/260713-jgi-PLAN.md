---
phase: quick-260713-jgi
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - handlers/admin.py
  - handlers/user_actions.py
  - handlers/registration.py
  - database/db.py
  - tests/test_csv_injection.py
autonomous: true
requirements: [CR-2, CR-3, CR-4, CR-5, C-WR-02, A-WR-03, CR-6]
must_haves:
  truths:
    - "A registrant with < > & in full_name/username/email cannot break the admin /find message"
    - "A registrant with markup in university/source cannot break /stats or 📈 Источники"
    - "A referrer's «Мои приглашённые» renders safely regardless of invitee names"
    - "Admin-set event_date/place/captions/consent text cannot break user info messages"
    - "CSV export cells starting = + - @ TAB CR are neutralized so Excel treats them as text"
    - "Full test suite (121 + new) stays green"
  artifacts:
    - path: "database/db.py"
      provides: "_csv_safe per-cell neutralizer applied in export_users_csv"
      contains: "_csv_safe"
    - path: "tests/test_csv_injection.py"
      provides: "unit test for _csv_safe"
  key_links:
    - from: "database/db.py::export_users_csv"
      to: "_csv_safe"
      via: "per-cell map over rows before return"
      pattern: "_csv_safe"
---

<objective>
Security batch B1a — close the HTML-injection (CR-2/3/4/5, C-WR-02, A-WR-03) and
CSV-injection (CR-6) CRITICAL findings from the all-phases code review. `main.py:61`
sets bot-wide `parse_mode=HTML`, so every unescaped user/admin string interpolated into a
send either breaks delivery (unclosed tag → Telegram 400) or injects markup.

Purpose: prevent applicant-triggered message-delivery DoS / markup injection reaching
admins and referrers, and prevent formula injection in the admin CSV export.
Output: escaping added at each named interpolation site; a single `_csv_safe` neutralizer
on the export path; one focused unit test.

CONSTRAINTS (do not violate):
- ONLY add escaping/neutralization. Do NOT change any message wording or formatting.
- Reuse the EXISTING pattern — do not invent a new one:
  - `handlers/admin.py` imports `html as html_module` → use `html_module.escape(...)`.
  - `handlers/user_actions.py` and `handlers/registration.py` import `html` → use `html.escape(...)`.
- No new dependencies (`html`, `csv` are stdlib). Preserve fail-soft conventions.
- CSV neutralizer is WRITE-side (export only) — never mutate stored data.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@.planning/reviews/260713-all-phases/CONSOLIDATED-REVIEW.md
@.planning/reviews/260713-all-phases/A-REGISTRATION-REVIEW.md

<interfaces>
Reference pattern (already correct) — handlers/payment.py:
  import html
  text += f"\n\n📋 Реквизиты:\n{html.escape(requisites)}"
  name = html.escape(str((user or {}).get("full_name") or telegram_id))

admin.py already uses `html_module.escape(...)` (import is `import html as html_module`).
user_actions.py already uses `html.escape(...)` (render_leaderboard line 67).
registration.py already uses `html.escape(...)` (import at line 3).

export_users_csv (database/db.py:442) returns `(headers, rows)`; `rows` are aiosqlite
tuples. Callers (handlers/admin.py:1063, :1183) feed rows straight into `csv.writer`.
Cells may be str / int / None. Neutralize str cells only so ints (telegram_id) pass through.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: HTML-escape all named user/admin interpolation sites</name>
  <files>handlers/admin.py, handlers/user_actions.py, handlers/registration.py</files>
  <action>
Add `html_module.escape(...)` / `html.escape(...)` at exactly these interpolation points.
Escape ONLY the interpolated values — do not touch surrounding literal text/tags/emoji.

handlers/admin.py — use `html_module.escape`:
  - CR-2 `cmd_find_user` (~:189-196): wrap `user['full_name']`, `user['username']`,
    `user['email']` (the `telegram_id` and `registration_date` are bot-generated — leave
    them). Guard for None consistent with the file, e.g. `html_module.escape(str(user['full_name'] or ''))`.
  - CR-3 `cmd_stats` (~:277-278) AND `show_admin_stats` (~:301-302): in the
    `for i, (uni, count) ...` loop, escape `uni` → `html_module.escape(str(uni))`. Both
    loops are identical — fix both.
  - CR-4 `show_admin_source_stats` (~:329-330): in `for source, count in rows`, escape
    `source` → `html_module.escape(str(source))`.

handlers/user_actions.py — use `html.escape`:
  - CR-5 `my_referrals` (~:281): change the join to
    `"\n".join(f"• {html.escape(str(name))}" for name in referrals)` — mirrors
    render_leaderboard (:67). (The "• None" cosmetic is out of scope; only add escaping.)
  - C-WR-02 `show_info_menu` (~:123-126): escape `event_date`, `event_time`, `place_name`.
  - C-WR-02 `info_date` (~:139,141): escape `event_date`, `event_time`.
  - C-WR-02 `info_place` (~:152,154): escape `place_name`, `place_address`.
  - C-WR-02 captions: `show_program` (~:191,198) escape `program_caption`; `show_speakers`
    (~:215 and its fallback send) escape `speakers_caption`. These are admin-set free text
    sent with parse_mode HTML. Escape the value where interpolated / passed as `caption=`.
    Guard None (a missing caption should stay falsy/empty, not the string "None").

handlers/registration.py — use `html.escape` (A-WR-03, consent card ~:641-649):
  - Escape the resolved prompt and remove the now-redundant inner escape:
    `caption = html.escape(await _prompt(f'consent_{consent_key}', label))`
    (was `_prompt(..., html.escape(label))` — moving the escape outward covers the
    admin `reg_prompt_consent_<key>` override too, and avoids double-escaping the default).
  - Guard the two sends (`answer_document` / `answer` at :647/:649) fail-soft, matching the
    file's convention: wrap in try/except and on failure resend the same caption with
    `parse_mode=None` (plain text). Do NOT change wording or the keyboard.
  </action>
  <verify>
    <automated>cd "C:/Users/alexe/Desktop/work/AIESEC_event_bot" && python -m pytest -q 2>&1 | tail -5</automated>
Manual grep confirmation (each must show the escape present at the fixed site):
  grep -n "html_module.escape(str(user\['full_name'\]\|html_module.escape(str(uni)\|html_module.escape(str(source)" handlers/admin.py
  grep -n "html.escape(str(name))\|html.escape(str(event_date\|html.escape(str(place" handlers/user_actions.py
  grep -n "html.escape(await _prompt" handlers/registration.py
  </verify>
  <done>
Every listed site interpolates via html(_module).escape; full pytest suite green; no
wording/formatting changed; consent sends are fail-soft with plain-text fallback.
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: CSV formula-injection neutralizer + test (CR-6)</name>
  <files>database/db.py, tests/test_csv_injection.py</files>
  <behavior>
    - `_csv_safe("=SUM(A1)")` → `"'=SUM(A1)"` (prefixes single quote)
    - `_csv_safe("+1")`, `_csv_safe("-1")`, `_csv_safe("@x")` → each prefixed with `'`
    - `_csv_safe("\tx")` and `_csv_safe("\rx")` (leading TAB / CR) → each prefixed with `'`
    - `_csv_safe("normal")` → `"normal"` (unchanged)
    - `_csv_safe("")` → `""` (unchanged)
    - `_csv_safe(123)` → `123` (non-str passthrough, unchanged type)
    - `_csv_safe(None)` → `None` (passthrough)
  </behavior>
  <action>
In database/db.py add a module-level helper and apply it per-cell inside export_users_csv:

  _CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

  def _csv_safe(value):
      """Neutralize CSV/Excel formula injection (CWE-1236): prefix a single quote to any
      STRING cell that begins with a formula trigger so spreadsheet apps treat it as text.
      Non-string cells (int/None) pass through unchanged. Export-side only — never mutates
      stored data."""
      if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
          return "'" + value
      return value

In `export_users_csv` (~:449-450), before returning, map every cell:
  rows = [tuple(_csv_safe(cell) for cell in row) for row in rows]
  return headers, rows
Do NOT neutralize `headers` (bot-generated RU labels). `str.startswith` accepts the tuple
directly — no per-prefix loop needed. Keep this the single sanitizer; do not scatter
inline checks in the admin.py CSV writers.

Create tests/test_csv_injection.py: a pure unit test importing `_csv_safe` from
database.db, asserting each case in <behavior>. No bot/DB/async fixtures — plain functions.
Follow the style of an existing test file (e.g. tests/test_db_phase1.py) for imports/layout.
  </action>
  <verify>
    <automated>cd "C:/Users/alexe/Desktop/work/AIESEC_event_bot" && python -m pytest tests/test_csv_injection.py -q 2>&1 | tail -5</automated>
Then full suite: python -m pytest -q  (must stay green, 121 prior + new)
  </verify>
  <done>
`_csv_safe` exists in database/db.py and is applied per-cell in export_users_csv (str cells
only); tests/test_csv_injection.py passes; full suite green; stored data untouched.
  </done>
</task>

</tasks>

<verification>
- `python -m pytest -q` → all green (121 existing + new csv test).
- Grep confirms escapes present at every CR-2/3/4/5, C-WR-02, A-WR-03 site.
- `git diff` shows ONLY added escaping/neutralization + the new test — no wording/format changes.
</verification>

<success_criteria>
- CR-2/3/4/5, C-WR-02, A-WR-03: every named interpolation of a user/admin string into a
  parse_mode=HTML send is wrapped in html(_module).escape.
- A-WR-03 consent sends are fail-soft with a plain-text fallback.
- CR-6: export_users_csv neutralizes str cells starting = + - @ \t \r via `_csv_safe`.
- One focused pure-function test added; whole suite stays green; no wording changes; no
  new dependencies; stored data unchanged.
</success_criteria>

<output>
After completion, create `.planning/quick/260713-jgi-security-b1a-html-escape-user-admin-text/260713-jgi-SUMMARY.md`
</output>
