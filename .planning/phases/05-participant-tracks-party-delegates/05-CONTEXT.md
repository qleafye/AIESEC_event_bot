# Phase 5: Participant Tracks (Party Delegates) - Context

**Gathered:** 2026-07-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 5 adds a **participant-track dimension** to the existing registration engine so one bot serves several audiences of the same event at the same time:

1. **`full`** — полное участие (текущее поведение, ничего не меняется)
2. **`party_overnight`** — только вечеринка, с ночёвкой
3. **`party_noovernight`** — только вечеринка, без ночёвки

Each track gets its own question set, its own question wording, its own approval toggle, its own tariffs, and its own Google-Sheets tab — all configured from the admin panel with no redeploy. Party tracks run **in parallel** with normal full registrations; this is NOT an event-type switch that flips the whole bot.

**Requirements (6):** TRACK-01, TRACK-02, TRACK-03, TRACK-04, TRACK-05, TRACK-06.

**Correction to TRACK-06 as written in REQUIREMENTS.md:** the phrase «отдельной колонкой в Google Sheet» is superseded by D-11 — party delegates go to a **separate worksheet tab with its own header**, not a column on the main sheet. Track visibility in the admin card and broadcast filters stands as written.

**Out of scope:** capacity limits / auto-close on sellout / waitlist (see Deferred); gamification (v2); roles (v2).

</domain>

<decisions>
## Implementation Decisions

### Track model & persistence (TRACK-01)
- **D-01:** Track is a single column `users.participant_type TEXT DEFAULT 'full'` added via the existing `_ensure_column` additive-migration pattern (`database/db.py:31`). Three values: `full` / `party_overnight` / `party_noovernight`. The ~590 live users all land on `full` — no data loss, no behavior change.
- **D-02:** The track must survive a repeat `/start` that carries no deep-link parameter. FSM (`MemoryStorage`) is not sufficient. Track is written at **flow start** to the `reg_started` row (which already exists for dropout analytics, `db.py:555`) and at **finalization** to `users.participant_type`. Note for the planner: `clear_reg_started()` (`db.py:568`) deletes the row on completion, so these are two distinct write points, not one.

### Per-track question configuration (TRACK-02)
- **D-03:** One override namespace `__party` covers **both** party tracks — there is no `__party_overnight` / `__party_noovernight` split for question toggles. The с-ночёвкой / без difference is expressed as a conditional skip inside `_get_enabled_steps` (`registration.py:348`), the same way `housing`, `bed_partner` and the edu-conditional steps already work.
- **D-04:** Override resolution is **tri-state**, not boolean. Setting key absent → inherit the global `reg_q_<step>`; present → explicit `on` / `off`. The admin toggle cycles ➕Наследует → ✅Вкл → ❌Выкл so the manager can see at a glance what is actually overridden versus inherited.
- **D-05:** Question **wording** is overridable per track too: `reg_prompt_<step>__party` with fallback to the global `reg_prompt_<step>`. `_prompt()` is already the single resolution point (`registration.py:329`), so this is one function change. Rationale: «Где будешь жить» reads wrong for a one-night guest.
- **D-06:** Admin edits the party set from the **existing** «📋 Вопросы регистрации» screen (`handlers/admin.py:1940`), with a track switcher as the first row («Трек: [Полный] [Party]»). Tapping it re-renders the same toggle list in the context of that track. One screen, one mental model — not a duplicated menu entry.
- **D-07:** Seed values come from a **third preset button «🎉 Party»** next to the existing Форум / Конференция presets (`registration.py:281`). It writes only `__party` keys — global settings are never touched, so applying it cannot disturb a live full-registration flow. Seed set: возраст, телефон, ВК, город, аллергии, питание. Nothing changes until an admin taps it (Phase-4 D-15 default-OFF posture preserved).
- **D-08:** Overnight-specific questions **reuse the existing steps** `housing` / `bed_sharing` / `bed_partner` gated on `participant_type == 'party_overnight'`, added alongside the current conditions in `_get_enabled_steps`. No new step keys, no new DB columns, no new sheet columns.
- **D-09:** No new `REG_FLOW` step keys are needed — the existing 42 steps cover the party case. This phase adds track mechanics only.

### Entry & master toggle (TRACK-03)
- **D-10:** Entry into a party track is by **deep link only**: `?start=party_over` → `party_overnight`, `?start=party_noover` → `party_noovernight`. Must not disturb the two existing deep-link forms — a bare numeric arg stays `referrer_id` (`_extract_referrer_id`, `registration.py:690`) and `src_*` stays a source tag (`_extract_source_tag`, `registration.py:721`). An in-flow fork question exists behind setting `party_fork_question`, default `off`; while off, an ordinary delegate sees no extra screen at all.
- **D-11a:** Master toggle `party_enabled` (default `off`) turns the whole track on/off. When `off`, a user arriving on a party link gets a **configurable «регистрация на вечеринку закрыта» message plus a button** that drops them into the normal full-participation flow. Never silently reroute a party visitor into the full form — the price difference makes a silent fallthrough a real harm.

### Google Sheets (TRACK-06)
- **D-11:** Party registrations are written to a **separate worksheet tab** with its **own header** built from the party question set — no empty ВУЗ/резюме columns. The tab keeps its own frozen header snapshot, independent of the main sheet's. Precedent: Phase 3 D-09 put the pre-selection allowlist on a separate tab and left `sheet1` untouched. This also decouples party from the known mid-event header-drift hazard documented at `handlers/admin.py:_refresh_sheet_header`.
- **D-12:** A party delegate is written to the party tab **only** — no duplicate row on the main sheet. The main tab stays purely full delegates. A combined view, if ever wanted, is a manual formula on the organizers' side.

### Approval (TRACK-04)
- **D-13:** `party_approval` is its own setting, independent of `full_approval` / `short_approval`, resolved in `_decide_status` (`registration.py:64`). `auto` and `manual` behave as they do for the other forms.
- **D-14:** When `party_approval=manual`, party applications land in the **shared** «Заявки» tinder queue with a track line in the card («🎉 Трек: вечеринка с ночёвкой»). When `party_approval=auto` they are approved on submit and **never enter the queue at all** — do not implement "always enqueue, then auto-approve".
- **D-15:** Approval message is overridable per track: `approve_text__party` with fallback to the global approve text. A party guest getting the three-day-forum welcome text is wrong on dates, times and venue.

### Payment (TRACK-05)
- **D-16:** `payment_options` gains an **optional third field**: `label|price|track`. `track` accepts specific track values, comma-separated (`party_overnight,party_noovernight`) because the overnight and no-overnight prices genuinely differ. A line with **no** third field remains valid and is offered to **all** tracks — existing RusCo configuration keeps working untouched.
- **D-17:** `pay_option:{i}` callback data keeps indexing the **full unfiltered** option list; only the rendered keyboard is filtered. Filtering the list before indexing would shift indices and make a keyboard sent before a settings edit select the wrong tariff.
- **D-18:** If no tariff matches the user's track, treat participation as **free** — skip the payment step, go straight to the main menu, same as `payment_enabled=off`.

### Broadcast & visibility (TRACK-06)
- **D-19:** `participant_type` is added to `_PICKER_FIELDS` (`handlers/admin.py:1641`) so track becomes a broadcast filter field; the DB-distinct value picker added in commit `7ddb9b5` then populates its values with no extra work.

### Claude's Discretion
- Exact setting key names and the separator convention for the `__party` suffix.
- Whether the tri-state toggle stores `inherit` explicitly or represents inheritance as key-absence (D-04 only fixes the observable behavior).
- Party worksheet tab name and whether it is admin-configurable via `bot_settings`.
- Whether `participant_type` on `reg_started` is a new column or the track is resolved another way — the requirement is only that a repeat `/start` without the parameter preserves the track (D-02).
- Where exactly the track switcher row renders in the questions keyboard, and the emoji/labels used.
- Whether the party preset lives in `REG_PRESETS` with a track marker or as a separate structure.
- What a party delegate sees in the main menu after approval, and whether coins/referrals apply to party tracks — not discussed; pick the least-surprising behavior (treat as a normal approved user) and flag it in the plan.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` § "Phase 5: Participant Tracks (Party Delegates)" — goal, 8 success criteria, requirement list, depends-on.
- `.planning/REQUIREMENTS.md` § "Participant Tracks (Фаза 5 — party-делегаты)" — TRACK-01..06 text. **Note the D-11 correction to TRACK-06 above.**

### Upstream phase context (depended-on)
- `.planning/phases/04-universal-modules/04-CONTEXT.md` — payment module (D-08 options/tariff model, D-09 payment-after-approval), consent steps, module-toggle posture, and D-15 "new modules default OFF".
- `.planning/phases/03-scheduler-communications-verification/03-CONTEXT.md` — D-09 separate-worksheet-tab precedent (reused by D-11 here); broadcast filter builder; settings-toggle default-off posture.
- `.planning/phases/02-approval-flow/02-CONTEXT.md` — approval flow, atomic approve guards, tinder queue shape reused by D-14.

### Tech-stack & compatibility locks
- `CLAUDE.md` § "Fixed Core Stack" / Constraints — additive migrations must not break ~590 live users; aiogram 3 + SQLite + long polling unchanged; pagination mandatory at 1000+ application scale.

### Source of truth in code
- `handlers/registration.py` — `REG_FLOW` (:77), `REG_DEFAULTS` (:185), `REG_PRESETS` (:281), `REG_CATEGORIES` (:307), `_prompt` (:329), `_is_step_enabled` (:335), `_get_enabled_steps` (:348), `_decide_status` (:64), `_extract_referrer_id` (:690), `_extract_source_tag` (:721), `_start_registration_flow` (:974), `cmd_start` (:1074), `SHEET_COLUMNS` (:787), `active_sheet_headers` (:855).
- `handlers/admin.py` — questions toggle screen (:1940-1965), `_refresh_sheet_header` header-drift note, `_PICKER_FIELDS` (:1641).
- `handlers/payment.py` — `_parse_options` (:98), `start_payment_step` (:149) and the `pay_option:{i}` index contract (:157).
- `database/db.py` — `_ensure_column` (:31), `add_user` (:203), `mark_reg_started` (:555), `clear_reg_started` (:568).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_get_enabled_steps` (`registration.py:348`) already hosts six conditional-skip rules (edu-conditional, housing-on-arrival, bed_partner-on-bed_sharing, source-from-tag, informal_day-on-Online, work_sphere-on-work_status). Track skips (D-08) are one more rule of exactly the same shape.
- `_source_from_tag` (`registration.py:361`, `:986`) is a working precedent for "deep-link value is authoritative, so skip the question that would let the user overwrite it" — D-10's fork-question skip is the same mechanism.
- `_prompt` (`registration.py:329`) is the single wording-resolution point → D-05 is one function change, not a sweep.
- `REG_PRESETS` + the preset-apply handler (`admin.py:2041`) — the bulk-write shape D-07 clones for `__party` keys.
- `_decide_status` (`registration.py:64`) already routes on form type to a per-form moderation setting — D-13 adds a third branch.
- DB-distinct broadcast value pickers (commit `7ddb9b5`) — D-19 gets its value list free once `participant_type` joins `_PICKER_FIELDS`.

### Established Patterns
- Additive idempotent migrations (`_ensure_column`, `CREATE TABLE IF NOT EXISTS`) — mandatory; ~590 live users must survive untouched.
- New capability defaults **OFF**; the live flow stays byte-identical until an admin deliberately turns it on (Phase-3 preselect gate, Phase-4 D-15).
- Settings read on the fly from `bot_settings` — no redeploy between events.
- Per-callback `config.ADMIN_IDS` re-check on every admin callback.
- Fail-soft on non-critical side paths (Sheets, Nextcloud, analytics) — never block a registration.

### Integration Points
- `cmd_start` (`registration.py:1074`) — parse the party deep-link alongside referrer/src; enforce the `party_enabled` gate (D-11a).
- `_start_registration_flow` (`registration.py:974`) — persist the track at flow start (D-02).
- `finalize_registration` / `submit_application` — persist `participant_type` on `users`; route the sheet write to the party tab (D-11/D-12).
- `approve_user()` — per-track approve text (D-15) and per-track tariff filtering (D-16/D-17/D-18).
- Admin questions screen — track switcher (D-06); admin settings — `party_enabled`, `party_approval`, `party_fork_question`, closed-message text.

### Known hazards surfaced during scouting
- `pay_option:{i}` is a **positional index** — naive filtering silently mis-selects tariffs (addressed by D-17).
- Inserting a sheet column mid-list only realigns rows appended after the change (`admin.py:_refresh_sheet_header` docstring). D-11's separate tab sidesteps this for party but the main sheet keeps the constraint.
- `reg_started` rows are deleted on completion — a single write point is not enough to persist the track (D-02).

</code_context>

<specifics>
## Specific Ideas

- Party tracks run **in parallel** with full registrations — explicitly not an event-type switch. Both funnels must be live at the same time (user's words: «party идет ПАРАЛЛЕЛЬНО со всеми регами полными»).
- Organizers must be able to open and close the party track at will (`party_enabled`) — sales for the party night are expected to close before the forum's.
- A visitor who arrives on a closed party link must be told it is closed and offered the full-participation flow — never silently registered into a different (more expensive) product.
- The party sheet tab is for the party team to work from directly; it should not carry columns that are always empty for party guests.

</specifics>

<deferred>
## Deferred Ideas

- **Capacity limit / auto-close on sellout / waitlist for the party** — `party_capacity` setting, live counter, race handling on simultaneous registrations, waitlist promotion. A real new capability; belongs in its own phase. For now manual `party_enabled=off` covers the need.
- **Track transfer** — moving an already-registered delegate between tracks from the admin panel (raised as a possible gray area, not selected for discussion). Note for a follow-up phase.
- **Party-specific main-menu content** after approval, and whether coins/referrals apply to party guests — flagged as Claude's Discretion for this phase; if it grows, it is its own scope.
- Gamification (GAME-01..04) and Roles (ROLE-01..02) — already v2 backlog.

</deferred>

---

*Phase: 5-participant-tracks-party-delegates*
*Context gathered: 2026-07-20*
