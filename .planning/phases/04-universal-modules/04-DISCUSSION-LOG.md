# Phase 4: Universal Modules - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-29
**Phase:** 04-universal-modules
**Areas discussed:** Consent (storage + config), Event type ↔ module toggles, Payment (model + flow), Build order (MVP slicing)

---

## Consent — list config

| Option | Description | Selected |
|--------|-------------|----------|
| Кастомный список + toggle каждого | Configurable list, reuse source_options; default 3, admin adds/disables | ✓ |
| Фиксированные 3 | Simpler code, no custom items | |
| Ты решай | Claude discretion | |

**User's choice:** Кастомный список + toggle каждого
**Notes:** Reuse the existing `source_options` newline-text settings pattern.

## Consent — acceptance storage

| Option | Description | Selected |
|--------|-------------|----------|
| Таблица user_consents | (user_id, consent_key, accepted_at) — timestamped audit | ✓ |
| JSON-колонка в users | consents TEXT via _ensure_column, simpler, no per-item time | |
| Ты решай | Claude discretion | |

**User's choice:** Таблица user_consents
**Notes:** Per-item timestamp wanted for personal-data audit.

## Consent — display & enforcement

| Option | Description | Selected |
|--------|-------------|----------|
| Каждое отдельно, кнопка «Принимаю» | Each tapped separately before finalize (SC#2, RusCo UX) | ✓ |
| Одно сообщение, «Принимаю всё» | Faster UX, weaker legally | |
| Ты решай | Claude discretion | |

**User's choice:** Каждое отдельно, кнопка «Принимаю»

## Consent — texts & links

| Option | Description | Selected |
|--------|-------------|----------|
| Текст в bot_settings + опц. ссылка | text + optional policy/PDF link | |
| Только текст, без ссылок | simpler | |
| Ты решай | Claude discretion | |

**User's choice:** "только пдф" (free text) — full consent/policy text delivered as an attached PDF (file_id in bot_settings) + short caption + «Принимаю», no long inline text.

## Event type ↔ module toggles

| Option | Description | Selected |
|--------|-------------|----------|
| Тип = пресет + ручная донастройка | Type presets toggles, each overridable | ✓ |
| Тип и тумблеры независимы | Type is a label only | |
| Ты решай | Claude discretion | |

**User's choice:** Тип = пресет + ручная донастройка

## Modularity — storage & apply

| Option | Description | Selected |
|--------|-------------|----------|
| bot_settings, читается на лету | event_type/payment_enabled/consent_enabled; immediate effect, no deploy (SC#1) | ✓ |
| Ты решай | Claude discretion | |

**User's choice:** bot_settings, читается на лету

## Modularity — REG_FLOW question types

| Option | Description | Selected |
|--------|-------------|----------|
| Расширить шаг полем type | text/date/consent dispatcher; date validates ДД.ММ.ГГГГ; backward compatible | ✓ |
| Ты решай | Claude discretion | |

**User's choice:** Расширить шаг полем type

## Payment — tariff model

| Option | Description | Selected |
|--------|-------------|----------|
| Да, options + bot_settings | options-not-days {name,price}, newline text | ✓ |
| Ты решай | Claude discretion | |

**User's choice:** Да, options + bot_settings (confirms seed N-02)

## Payment — step timing

| Option | Description | Selected |
|--------|-------------|----------|
| После одобрения | SC#3, ties to approval flow; works with auto-approve | ✓ |
| Сразу после регистрации | RusCo TZ flow, ignores moderation | |
| Ты решай | Claude discretion | |

**User's choice:** После одобрения

## Payment — field storage

| Option | Description | Selected |
|--------|-------------|----------|
| Колонки в users | payment_status/payment_option/receipt_file_id/payment_due/paid_at via _ensure_column | ✓ |
| Отдельная таблица payments | history of multiple payments | |
| Ты решай | Claude discretion | |

**User's choice:** Колонки в users

## Payment — receipt + verify + reminders

| Option | Description | Selected |
|--------|-------------|----------|
| PDF-only + реюз тиндера + APScheduler | PDF reject-by-MIME, tinder queue (APP-04), Phase-3 scheduler | ✓ |
| Ты решай | Claude discretion | |

**User's choice:** PDF-only + реюз тиндера + APScheduler
**Notes:** LATER amended by user — receipt also accepts a photo/screenshot, not only PDF (D-11). Relaxes ROADMAP SC#3's literal "PDF-only".

## Build order (MVP)

| Option | Description | Selected |
|--------|-------------|----------|
| Модульность+согласия сначала | Payment last; RusCo on free-flow, payment content not ready | ✓ |
| Оплата сначала | If payment urgent | |
| Всё в один заход | Whole phase at once | |

**User's choice:** Модульность+согласия сначала

## Build order — organizer-content blocker

| Option | Description | Selected |
|--------|-------------|----------|
| Строить с плейсхолдерами, toggle OFF | Code doesn't wait on content; modules default OFF | ✓ |
| Ждать контент | Blocks the phase | |
| Ты решай | Claude discretion | |

**User's choice:** Строить с плейсхолдерами, toggle OFF

---

## Claude's Discretion

- bot_settings key naming; per-consent PDF key scheme.
- Consent-list serialization format (`label|pdf_key` pairs vs separate keyed settings).
- Penalty-schedule (PAY-01) serialization in bot_settings.
- Admin-UI placement of the event-type/module panel + receipt tinder queue.
- `overdue` transition mechanism (scheduler sweep vs lazy-on-read).

## Deferred Ideas

- Resume disk-storage / File Browser module (04-RESUME-STORAGE-NOTES.md, R-01..R-04) — separate optional module, not needed for conference-readiness.
- Deep user-initiated cancellation/refund workflow — default to display-only penalty schedule; full self-service deferred (STATE.md pre-Phase-4 question).
- Gamification + Roles — already v2 backlog.
