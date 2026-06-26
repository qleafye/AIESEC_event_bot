# Phase 4 — Payment Model Notes (design seed)

**Captured:** 2026-06-26
**Status:** DESIGN SEED ONLY — not full phase context. Deep payment flow (requisites message, receipt verification, cancellation/penalty timings) deliberately deferred per user ("по оплате поговорим позже"). Capture here so the direction isn't lost.

## Decisions seeded (to confirm at Phase 4 discuss/plan)

- **N-01 — Payment is a settings toggle.** Consistent with the bot's settings-driven design (`bot_settings`). A `payment_enabled` toggle: OFF → current free registration flow; ON → registration gains a payment step. Aligns with ROADMAP Phase 4 SC#1 (module toggles, no code deploy).

- **N-02 — Model "options/tickets", NOT "days".** Do not introduce a day/calendar entity for pricing/access logic. Days belong on the info page, not in payment logic.
  - An **option** = `{name, price}`; `price = 0` means free.
  - Event = a list of options. Most events = a single option.
  - "Multi-day event where only one day is paid" = two options (free base + paid day) — no special multi-day machinery.
  - Single paid day = just an option with a price. No separate "day" mechanic.
  - Universal across YouLead (forum) and RusCo (conference) — one abstraction.

- **N-03 — Reuse the existing configurable-list pattern.** Store the options list as editable text in `bot_settings`, same as `source_options` today; admin edits via the existing settings configurator. No new concept/UI primitive.

- **N-04 — Flow sketch (when `payment_enabled=on`):** user picks an option → if `price > 0` → payment step (show requisites + upload receipt into `receipt_file_id`, field already planned for Phase 4) → manager verifies in a tinder receipt queue (ROADMAP SC#4). Ties into approval flow (payment triggered on approval per Phase 4 depends-on).

## Context: RusCo
RusCo is a **paid** event, but **payment collection is NOT being enabled yet** (launching RusCo now on the free/open flow, no moderation). Payment is finished later as Phase 4. This note exists so the options-vs-days direction is settled before that work starts.

## Parked for the "payment talk" (explicitly later)
- Requisites/bank-details message content and formatting
- Receipt verification flow + re-upload on rejection (PAY-04)
- Cancellation scope + penalty schedule + deadline reminders (PAY-06; STATE.md pre-Phase-4 blocker)
- Consent module texts (CONS-01/02; STATE.md pre-Phase-4 blocker — organizer must provide)

Relates to ROADMAP Phase 4 (MOD-01..03, CONS-01/02, PAY-01..06).
