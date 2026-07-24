# Roadmap: AIESEC Event Bot

## Milestones

- ✅ **v1.0 YouLead'26 MVP** — Phases 1-5 (shipped 2026-07-24)
- 📋 **v2** — Settings registry + Bitrix/web channel + gamification + roles (planned)

## Phases

<details>
<summary>✅ v1.0 YouLead'26 MVP (Phases 1-5) — SHIPPED 2026-07-24</summary>

Full detail archived at `.planning/milestones/v1.0-ROADMAP.md`; phase artifacts at `.planning/milestones/v1.0-phases/`.

- [x] Phase 1: DB Foundation + Quick Wins + Coins — safe migrations, coins ledger, subscription check, reg_started dropout tracking (DB-01..03, QW-01..03, COIN-01..03, SCHED-02) — verified 2026-07-24
- [x] Phase 2: Approval Flow — paginated tinder moderation queue, atomic approve guards, per-form toggles (APP-01..08) — verified 2026-07-24
- [x] Phase 3: Scheduler + Communications + Verification — persistent APScheduler, filtered/scheduled broadcasts, dropout nudge, pre-selection gate (COMM-01..04, SCHED-01/03, VERIF-01/02) — verified 2026-07-24
- [x] Phase 4: Universal Modules — payment flow + receipts, consent module, event-type/module toggles (MOD-01..03, CONS-01/02, PAY-01..06) — verified 2026-07-24
- [x] Phase 5: Participant Tracks (Party Delegates) — per-track questions/approval/tariffs/Sheet-tab (TRACK-01..06) — verified 2026-07-21 (live-bot UAT deferred; accepted at close)

**Post-ship hardening:** cross-phase party-payment re-entry blocker fixed (quick 260724-dnm); P0 pack — aiogram pin, resume size guard, Sheets fail-alert, compose fix (quick 260724-dw1). Full suite 359/359 pass.

</details>

### 📋 v2 Registry & Multichannel — backlog-mode (started 2026-07-24)

Scope зафиксирован в `.planning/REQUIREMENTS.md` (15 требований). Детальные фазы **не строятся**, пока нет точного ТЗ (решение пользователя). Разработка — позже, по группам; реестр можно начинать первым (самодостаточен, разблокирует остальное). Каждой группе — свой `/gsd-plan-phase`, когда появится ТЗ.

Группы (порядок по связности, не жёсткие фазы):
1. **Settings-schema реестр** (REG-01/02/03) — keystone, стартовать первым
2. **Bitrix CRM** (CRM-01/02)
3. **Web-канал регистрации** (WEB-01/02)
4. **Города** (CITY-01/02)
5. **Геймификация** (GAME-01..04) — фундамент COIN уже есть
6. **Роли** (ROLE-01/02) — ⚠️ нужен ТЗ, модель разграничения TBD

Future (за пределами v2): Telegram Mini App (WEBAPP-01).

## Progress

| Phase | Milestone | Plans | Status | Completed |
|-------|-----------|-------|--------|-----------|
| 1. DB Foundation + Quick Wins + Coins | v1.0 | 4 | Complete (verified) | 2026-07-24 |
| 2. Approval Flow | v1.0 | 4 | Complete (verified) | 2026-07-24 |
| 3. Scheduler + Communications + Verification | v1.0 | 5 | Complete (verified) | 2026-07-24 |
| 4. Universal Modules | v1.0 | 5 | Complete (verified) | 2026-07-24 |
| 5. Participant Tracks (Party Delegates) | v1.0 | 6 | Complete (verified; live UAT deferred) | 2026-07-24 |
