# Phase 1: DB Foundation + Quick Wins + Coins - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-25
**Phase:** 1-db-foundation-quick-wins-coins
**Areas discussed:** QW-01 confirmation, QW-02 subscription, QW-03 resume, COIN coins/leaderboard

---

## QW-01 — Registration confirmation

| Option | Description | Selected |
|--------|-------------|----------|
| Restart, full form only | Summary+confirm for full form; «Изменить» = redo form | ✓ |
| Per-field edit, full form | «Изменить» → field list, edit one without re-walking | |
| Restart, both forms | Summary for short + full | |

**User's choice:** Restart-based confirmation. Initially named "second option" but reasoning fully described restart ("случай 1 из 100, когда нужно менять поля... достаточно просто подтверждения, и если не так — пройти процесс заново"). Confirmed in follow-up: "Да, рестарт".
**Notes:** Rejected per-field editing as rare + complex (no DB field-sync, no background worker). Reused existing `get_confirm_kb()`.

---

## QW-02 — Channel subscription

| Option | Description | Selected |
|--------|-------------|----------|
| Block before reg, admin config | Hard gate at /start; channel_id+toggle in admin; recheck button | |
| Gate on menu | Free reg; subscription required for menu functions | |
| Block at /start, .env | Hard gate; channel_id in .env | |

**User's choice:** None of the above as written — **redefined**: no gate at all. Check subscription, surface non-subscribers to admin, offer an admin reminder broadcast. Channel taken from existing in-bot TG-channel setting (`contact_tg`). Follow-up: "Всё сейчас" — reminder broadcast ships in Phase 1.
**Notes:** Non-subscribers become a broadcast audience segment. fail-open when bot not channel admin.

---

## QW-03 — Resume upload

| Option | Description | Selected |
|--------|-------------|----------|
| Optional, PDF/DOCX | Skippable; full form toggle | |
| Mandatory, PDF/DOCX | Required when toggle on | ✓ |
| Optional, any file | Skippable; any document | |

**User's choice:** Toggle in registration-questions configurator; when enabled, **mandatory PDF/DOCX**. Follow-up: "Резюме-toggle сейчас, паритет позже".
**Notes:** Hard requirement — bot must not crash on wrong file type (graceful validation). Short-form configurator parity deferred. Store file_id only, no OCR.

---

## COIN — Coins + leaderboard

| Option | Description | Selected |
|--------|-------------|----------|
| Menu button + reason optional | User sees balance via `🪙 Мои монеты` + /рейтинг; optional reason; negatives allowed | ✓ |
| /рейтинг only + reason required | No menu button; reason mandatory | |
| Menu button + reason required | Button + mandatory reason | |

**User's choice:** Menu button + optional reason.
**Notes:** Append-only ledger (SUM(delta)); admin-only command; negative balances allowed; English aliases at Claude's discretion.

---

## Claude's Discretion

- Ledger schema/indexing, summary formatting, getChatMember caching, command alias names, resume step placement in REG_FLOW, file-type rejection copy.

## Deferred Ideas

- Short-form question configurator parity → Phase 2/4.
- Per-field registration editing → rejected, revisit if needed.
- **Roadmap impact:** SCHED-02 (incomplete-registration `reg_started` tracking) pulled from Phase 3 → Phase 1 by user direction; two broadcast segments (non-subscribers, incomplete registrations) now in Phase 1; Phase 1 success criterion 5 (hard gate) superseded.
