---
phase: 05-participant-tracks-party-delegates
reviewed: 2026-07-21T00:00:00Z
depth: deep
files_reviewed: 6
files_reviewed_list:
  - database/db.py
  - handlers/admin.py
  - handlers/payment.py
  - handlers/registration.py
  - main.py
  - services/sheets.py
findings:
  critical: 1
  warning: 5
  info: 2
  total: 8
status: issues_found
---

# Phase 5: Code Review Report — participant-tracks-party-delegates

**Reviewed:** 2026-07-21
**Depth:** deep (cross-file trace of `?start=` parsing → `_start_registration_flow` → FSM → `add_user`/sheet routing, and of `approve_user`/`start_payment_step` → `send_completion_and_bonus` across handlers/registration.py, handlers/payment.py, handlers/admin.py)
**Files Reviewed:** 6
**Status:** issues_found

## Summary

Reviewed the diff `00e8c70..HEAD` restricted to `database/db.py`, `handlers/admin.py`, `handlers/payment.py`, `handlers/registration.py`, `main.py`, `services/sheets.py`. The full-track regression surface is mostly clean: `_is_step_enabled_for_track`, `_decide_status`, `add_user`'s column/placeholder count, and the `pay_option:{i}` index-preservation contract (`_visible_options`) are all correct and byte-identical for `participant_type in (None, "full")`. Exclusive sheet routing in `finalize_registration` is genuinely exclusive (single `if/else`, no path writes both tabs). Every new admin callback checks `ADMIN_IDS`. Deep-link track parsing is a closed 2-entry map — no injection path to an arbitrary `participant_type`.

One **Critical** bug was found: enabling `party_fork_question` (a documented, supported Phase-5 admin toggle) silently discards `referrer_id`/`source_tag` deep-link attribution for every fresh user who lands on the fork screen, including users who ultimately choose the ordinary "full" track. This is a real regression to a pre-existing, revenue-relevant feature (referral coins), not merely a party-track edge case.

Several **Warning**-level gaps were also found in the "per-track approval text" (D-15) feature: it is wired for reads but (a) is never applied on the two payment completion paths most party delegates will actually hit, and (b) has no admin-facing UI to configure it at all, contradicting the plan's own summary claim that plan 05-03 "exposes `approve_text__party` as admin toggles/editors."

## Critical Issues

### CR-01: `party_fork_question` silently drops referrer_id/source_tag for every fresh user routed through the fork

**File:** `handlers/registration.py:1394-1409` (fork branch) and `handlers/registration.py:1412-1441` (`party_pick` handler)

**Issue:** `cmd_start` extracts `referrer_id`/`source_tag` from `command.args` at line 1335-1336, *before* the fork gate. When `_should_show_fork(...)` returns `True` (party_fork_question=on, party_enabled=on, user not yet registered, no party deep-link token resolved — i.e. exactly the case of a bare `/start` or a `?start=<referrer_id>` / `?start=src_...` deep link), the handler sends the fork keyboard and `return`s at line 1405 **without ever calling `_start_registration_flow`**. `referrer_id`/`source_tag` are therefore never persisted into FSM state.

When the user later taps a button, `party_pick` (line 1441) or `party_fallback_full` calls `_start_registration_flow(tap_message, state, participant_type=...)` with no `referrer_id=`/`source_tag=` argument and an FSM state that is still empty (this is the *first* call for the session). Inside `_start_registration_flow`, `saved_referrer_id = referrer_id or existing_data.get("referrer_id")` resolves to `None`, and the same for `saved_source_tag`. The referral/campaign attribution captured by a real deep link (`?start=123456`, `?start=src_vk`) is permanently lost — `add_user` will store `referrer_id=NULL`/`source='-' `for a user who was, in fact, referred.

This is not a party-track-only concern: it fires for any organic referred user who happens to hit the fork screen, including one who picks "Полная регистрация" (full track). Not covered by any existing or Phase-5 test (`tests/test_registration_phase5.py` never asserts on `referrer_id`/`source_tag` around the fork).

**Fix:** Persist the deep-link values into FSM state before showing the fork keyboard (or thread them through the callback), e.g.:
```python
if show_fork:
    await state.update_data(referrer_id=referrer_id, source=source_tag,
                             _source_from_tag=bool(source_tag))
    fork_text = await get_setting("party_fork_text") or DEFAULT_PARTY_FORK_TEXT
    await message.answer(fork_text, reply_markup=_party_fork_kb())
    return
```
and read them back out of `state.get_data()` in `party_pick`/`party_fallback_full` before calling `_start_registration_flow`, the same way `process_confirm_edit` already recovers them from `existing_data`.

## Warnings

### WR-01: `approve_text__party` (D-15) is never applied on the payment paths most party delegates will actually hit

**File:** `handlers/payment.py:306-317` (`_show_payment_details`) and `handlers/payment.py:225-234` (`start_payment_step` exception fallback), and `handlers/admin.py:2760-2787` (`rcpt_confirm`)

**Issue:** `send_completion_and_bonus`/`_approve_text_for` correctly accept `participant_type` and are wired from `approve_user` (registration.py:2116-2129) and from `start_payment_step`'s "no visible tariff" branch (payment.py:211-217). But `_show_payment_details` — the function that actually delivers the completion text for (a) a single free tariff and (b) a user who manually picks a free option among several — calls `send_completion_and_bonus(bot, telegram_id)` at line 315 with **no `participant_type`**, so it always falls back to the global `approve_text`. `_show_payment_details` has no `participant_type` parameter at all, so neither of its two call sites (`start_payment_step:224`, `process_payment_option:272`) can pass it even though both have the value in scope.

The same gap exists in `handlers/admin.py:2784` — the manager's "confirm receipt" action (`rcpt_confirm`), which is how every *paid* party delegate actually completes registration, calls `send_completion_and_bonus(callback.bot, uid, with_menu=False)` with no track resolution at all (no `get_user(uid)` lookup for `participant_type`).

Net effect: a party-track delegate who pays (the primary path plan 05-05 was built for) or who picks a free option among several always receives the *full-track* `approve_text`, never `approve_text__party`. The only path where the override actually fires is payment disabled entirely, or a track with zero matching tariffs.

**Fix:** Add `participant_type: str | None = None` to `_show_payment_details` and thread it from both call sites; resolve `participant_type` via `get_user(uid)` in `rcpt_confirm` before calling `send_completion_and_bonus`, mirroring the pattern already used in `approve_user`.

### WR-02: `approve_text__party` and `reg_prompt_<step>__party` have no admin UI to set them

**File:** `handlers/admin.py:2305-2343` (`admin_reg_prompts`/`reg_prompt_edit`), `handlers/admin.py:340-366` (`SETTINGS_FIELDS`)

**Issue:** `05-03-SUMMARY.md` states plan 05-03 "exposes `approve_text__party` as admin toggles/editors", but no such editor exists in `admin.py`: `SETTINGS_FIELDS` has an entry for `approve_text` (line 352) but none for `approve_text__party`, and `admin_reg_prompts`/`reg_prompt_edit` only ever read/write `reg_prompt_{step_key}` — there is no track switcher analogous to `reg_q_track_switch` for the prompt-text screen. The tri-state question-*enable* toggles (`reg_q_ptoggle`) and the `party_closed_text`/`party_sheet_tab` fields were built, but the per-track *wording* (D-05) and per-track *approval message* (D-15) settings can currently only be populated by writing directly to `bot_settings` — there is no bot-driven path to configure them, contradicting the project's stated core value ("менеджер DXP может полностью провести регистрацию делегатов ... через бота").

**Fix:** Add an `approve_text__party` entry (own editor, or reuse `SETTINGS_FIELDS` machinery with an explicit key), and extend `admin_reg_prompts`/`reg_prompt_edit` with the same `track` parameter/switcher pattern already used for `admin_reg_questions`.

### WR-03: Applying the "🎉 Party" preset silently turns off housing/bed_sharing/bed_partner for the overnight sub-track, even in configs where those questions are globally enabled

**File:** `handlers/registration.py:309-323` (`REG_PRESETS["party"]["on"]`), `handlers/registration.py:327-344` (`_apply_party_preset`)

**Issue:** `_apply_party_preset` writes an explicit `on`/`off` `__party` override for *every* `REG_FLOW` key (line 341-344), not only the ones being enabled. `REG_PRESETS["party"]["on"]` does not include `reg_q_housing`/`reg_q_bed_sharing`/`reg_q_bed_partner`, so tapping the preset button unconditionally writes `reg_q_housing__party=off` etc. In a RusCo-style deployment where these questions are enabled globally for the full/conference form (a legitimate, expected configuration — these fields exist specifically for conferences), tapping the "Party" preset defeats D-08's entire purpose (overnight-sub-track housing/bed questions) with no warning in the confirm dialog (`preset_apply`'s text only says "⚠️ ... вопросов будут перезаписаны" is explicitly *suppressed* for the party preset at line ~2246-2249, on the stated rationale that "`__party` keys never overlap the globals" — which is true for the *global* keys but not for the *pre-existing* `__party` overrides an admin may have already set for the overnight track).

**Fix:** Either exclude `housing`/`bed_sharing`/`bed_partner` from the blanket "set every key" pass in `_apply_party_preset` (leave them at `inherit` by default so the overnight/no-overnight distinction from D-08 still governs them), or explicitly surface them in the preset's "on" list with a note that overnight guests get asked and no-overnight guests don't.

### WR-04: Party sheet header is recomputed live on every append with no resync hook when a `__party` question toggle changes it — risk of column misalignment

**File:** `handlers/registration.py:1061-1094` (`party_sheet_headers`/`party_sheet_row`/`append_to_party_sheet`), `handlers/admin.py:2145-2173` (`toggle_party_question`)

**Issue:** The main sheet freezes its header via `set_sheet_schema` at startup and explicitly resyncs on every question toggle (`toggle_reg_question` calls `_refresh_sheet_header()` at admin.py:2126; `preset_confirm` calls it too). The party sheet deliberately does not freeze its header (acknowledged in the `party_sheet_row` docstring as "Claude's Discretion... party volume is low enough"), but `toggle_party_question` (the tri-state `__party` toggle handler) has **no equivalent resync call** for the party tab at all — not even an unfrozen best-effort one. If an admin flips a `reg_q_*__party` override mid-event, `party_sheet_headers()` on the *next* registration returns a different-length header than what is physically in row 1 of the "Party" tab (last synced at bot startup by `_maybe_ensure_party_sheet_header`, main.py:64-77) and than what earlier appended rows contain. `append_to_party_sheet` does a raw positional `append_row`, so this silently shifts columns for every row appended after the toggle relative to the header row and to prior rows — a real data-integrity risk, not merely cosmetic, despite being called out as an accepted low-volume risk.

**Fix:** At minimum, call `sheets_service.ensure_named_sheet_header(tab, await party_sheet_headers())` from `toggle_party_question` and `preset_confirm`'s party branch (mirroring `_refresh_sheet_header()`), so the physical header stays in sync even without a frozen schema.

### WR-05: `payment_options` admin help text was not updated to document the new track-filter syntax

**File:** `handlers/admin.py:359`

**Issue:** `_parse_options` (handlers/payment.py:98-133) now accepts an optional third `label|price|track1,track2` field to restrict a tariff to specific `participant_type` values — the entire mechanism behind D-16/D-17. The in-bot help text for `payment_options` shown to the admin (`SETTINGS_FIELDS`, line 359) still only documents `Название | Цена` with no mention of the third field, its accepted values (`full`, `party_overnight`, `party_noovernight`), or that omitting it makes a tariff visible to every track including party. An admin configuring party pricing through the bot UI (the only interface the project intends managers to use) has no way to discover this syntax.

**Fix:** Extend the help string, e.g. append: `"\n\nЧтобы ограничить вариант треком, добавь третье поле: Название | Цена | full  (или party_overnight,party_noovernight). Без третьего поля вариант виден всем трекам."`.

## Info

### IN-01: Broadcast filter value picker shows raw `participant_type` DB codes instead of translated labels

**File:** `handlers/admin.py:1808-1827` (`_show_value_picker`/`filter_pick_field`)

**Issue:** The new "🎉 Трек" broadcast filter (admin.py:1698, 1707, 1776) pulls its button values from `get_distinct_filter_values("participant_type")`, which returns the raw stored strings (`full`, `party_overnight`, `party_noovernight`) with no label translation, unlike `_render_application_card`'s track label map (registration.py `_render_application_card` diff, line ~2437-2440) or the `PARTY_SHEET_COLUMNS` "Трек" column, both of which translate to readable Russian. This matches the pre-existing pattern for other raw-value pickers (e.g. `status`), so it is not a regression, but it is a new user-facing surface that inherits the same readability gap.

**Fix:** Low priority; if addressed, add a label map similar to the one already written for `_render_application_card`.

### IN-02: `mark_reg_started`'s COALESCE-preserve-on-NULL branch is unreachable in production

**File:** `database/db.py:558-565`

**Issue:** `mark_reg_started(telegram_id, username, participant_type=None)` uses `COALESCE(excluded.participant_type, reg_started.participant_type)` to avoid clobbering a previously-recorded track when called with `participant_type=None`. The only production call site, `_start_registration_flow` (registration.py:1181), always resolves `saved_track` to a concrete string (`participant_type or existing_data.get("participant_type", "full")` — never `None`) before calling it, so the COALESCE-preserve path is exercised only by direct unit tests (`tests/test_db_phase5.py`), never by the running bot. Not a functional bug, but worth noting as defensive code with no live caller — a future refactor of `_start_registration_flow` that starts passing `None` through would silently change behavior at this call site without anyone reading `db.py`.

**Fix:** None required; documentation-only note for future maintainers.

---

_Reviewed: 2026-07-21_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
