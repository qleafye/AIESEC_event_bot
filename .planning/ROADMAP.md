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
7. **Рефакторинг god-файлов** (REFAC-01/02/03) — разбить `admin.py`/`registration.py`; ТЗ не нужен, лучше после/в связке с реестром (он ужимает admin.py); поведение байт-в-байт, порядок роутеров сохранён, опора на 359 тестов

Future (за пределами v2): Telegram Mini App (WEBAPP-01).

### 🔨 Активные фазы v2

- [ ] **Phase 6: Settings-schema Registry** — единый `SETTINGS_SCHEMA` реестр как источник метаданных настроек; потребители и админ-UI читают из него; инкрементальная миграция без ломки (REG-01, REG-02, REG-03)

### Phase 6: Settings-schema Registry
**Goal:** Единый `SETTINGS_SCHEMA`-реестр становится источником метаданных (parse-fn, default, label, group, type) для ключей `bot_settings`; существующие потребители и админ-UI читают значения через него — инкрементально, группа-за-группой, без ломки текущего поведения на ~590 живых юзерах
**Mode:** mvp
**Depends on:** Nothing (v1.0 отгружен; keystone самодостаточен). Осознанно: `SETTINGS_GROUPS`/флаги задано-не-задано из quick 260724-c0x — временная группировка, которую этот реестр заменяет
**Requirements:** REG-01, REG-02, REG-03
**Success Criteria** (what must be TRUE):
  1. `SETTINGS_SCHEMA` реестр существует: каждый мигрированный ключ `bot_settings` описан как `{type: toggle/text/int/enum/list/date/photo/file, group, label, default, parse}` — один справочник вместо парсеров/дефолтов, разбросанных по call-site'ам. `REG_DEFAULTS` поглощён реестром (не дублируется)
  2. Реестр — источник истины для парсинга/дефолтов: существующие потребители (`services/reminders.py`, `services/scheduler.py`, `handlers/admin.py`, `keyboards/builders.py`) для мигрированных ключей читают значение через реестр, а семантика on/off/default остаётся байт-в-байт как сейчас (регресс-набор зелёный, поведение не меняется)
  3. Миграция инкрементальная и не-ломающая: старый (`SETTINGS_GROUPS`/`SETTINGS_FIELDS`) и новый (генерируемый из реестра) рендер сосуществуют; на любом промежуточном шаге бот рабочий, ни одна из ~590 пользовательских записей не теряется, ни одна настройка не сбрасывается
  4. Админ-UI настроек рендерится из реестра (порядок/группы/label/рендер-по-типу) для мигрированных групп; добавление/правка одного ключа требует правки только записи реестра, а не нескольких хендлеров
**Note:** ТЗ на остальные группы v2 (Bitrix/web/города/гейма/роли) ещё нет — реестр планируется и исполняется отдельно как самодостаточный keystone. Исполнение — после SumMeet (форум 31 июля–2 авг); сейчас только план.

**Plans:** 7 plans
Plans:
- [ ] 06-01-PLAN.md — Registry foundation + `event` pilot slice (settings_schema.py machinery + get_setting_typed + event generated view)
- [ ] 06-02-PLAN.md — Migrate reg/pay/party/consent text groups to the registry (int/date/list types + render snapshots)
- [ ] 06-03-PLAN.md — REG-02 consumers (reminders/scheduler/builders) read int/date/list via get_setting_typed
- [ ] 06-04-PLAN.md — Register reg_q toggles + feature-switch enums, absorb REG_DEFAULTS, wire the 3 reg_q read-sites + full-registry coverage
- [ ] 06-05-PLAN.md — Wire admin feature-switch reads (render + toggle-button block) to the registry (button structure preserved, D-12)
- [ ] 06-06-PLAN.md — Wire registration/payment/scheduler feature-switch gates to the registry byte-for-byte
- [ ] 06-07-PLAN.md — Consolidated manual smoke checkpoint incl. mandatory per-toggle-button comparison (D-18)

## Progress

| Phase | Milestone | Plans | Status | Completed |
|-------|-----------|-------|--------|-----------|
| 1. DB Foundation + Quick Wins + Coins | v1.0 | 4 | Complete (verified) | 2026-07-24 |
| 2. Approval Flow | v1.0 | 4 | Complete (verified) | 2026-07-24 |
| 3. Scheduler + Communications + Verification | v1.0 | 5 | Complete (verified) | 2026-07-24 |
| 4. Universal Modules | v1.0 | 5 | Complete (verified) | 2026-07-24 |
| 5. Participant Tracks (Party Delegates) | v1.0 | 6 | Complete (verified; live UAT deferred) | 2026-07-24 |
| 6. Settings-schema Registry | v2 | 7 | Planning | — |
