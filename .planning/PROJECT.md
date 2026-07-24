# AIESEC Event Bot

## What This Is

Универсальный Telegram-бот (aiogram 3, SQLite, Google Sheets) для мероприятий AIESEC — форумов (YouLead) и конференций (RusCo). Один модульный бот для всех событий: регистрация, реферальная система, рассылки, админка, аналитика. Масштаб — 1000–1500 пользователей за сезон. Этот milestone расширяет существующий бот фичами «до запуска» YL'26 и универсальными модулями для конференций.

## Core Value

Менеджер DXP может полностью провести регистрацию делегатов мероприятия через бота — от заявки до одобрения — без ручного учёта в таблицах и без перезапуска кода между событиями.

## Current Milestone: v2 Registry & Multichannel

**Goal:** Снять инженерный долг конфигурации (единый реестр настроек) и расширить каналы привлечения за пределы Telegram, разблокировав давно отложенные геймификацию и роли.

**Target features:**
- Settings-schema реестр (SETTINGS_SCHEMA — единый источник метаданных ~70 ключей `bot_settings`: parse+default+label+group; keystone для inline-UI и геймификации)
- Bitrix CRM multichannel + web-канал регистрации (драйвер — троттлинг Telegram в RU)
- Геймификация (система заданий по трекам, монеты за задания — фундамент COIN уже есть)
- Роли (Делегат/Менеджер/Администратор с разграничением доступа)

## Requirements

### Validated

<!-- Существующий функционал бота (brownfield). -->

- ✓ Регистрация с динамическим flow (короткая/полная форма, настраиваемые вопросы, зависимые шаги) — existing
- ✓ Синхронизация с Google Sheets (запись при реге + кнопка догона) — existing
- ✓ Реферальные ссылки (`?start=USER_ID`, список приглашённых, статистика) — existing
- ✓ Рассылки (всем / по файлу ID, текст/фото/альбом/видео/документы) — existing
- ✓ Админ-панель (статистика, экспорт CSV, настройки форума, конструктор вопросов) — existing
- ✓ Метки источников (`/create_link`, `?start=src_TAG`) — existing
- ✓ Поиск пользователя (`/find @username`); ответы на вопросы пользователей — existing

<!-- Отгружено в v1.0 YouLead'26 MVP (2026-07-24). -->

- ✓ Подтверждение регистрации + проверка подписки на канал (`getChatMember`) — v1.0 (QW-01/02)
- ✓ Резюме PDF/DOCX при регистрации (`file_id` + Nextcloud share) — v1.0 (QW-03)
- ✓ Монеты + лидерборд (append-only ledger `coins`, `/coins`, `/рейтинг`) — v1.0 (COIN-01..03)
- ✓ Approval flow: `status`, submit/approve split, `ensure_registered` гейт, тиндер-очередь заявок, атомарные guard'ы, раздельные toggle модерации, напоминалка менеджеру — v1.0 (APP-01..08)
- ✓ Отложенные/фильтрованные рассылки (persistent APScheduler), 429-устойчивость, дропаут-напоминания, pre-selection allowlist — v1.0 (COMM-01..04, SCHED-01/03, VERIF-01/02)
- ✓ Модуль оплаты + чеки (тиндер, статусы, авто-напоминания T-3/T-1), модуль согласий, типы вопросов `date`/`consent`, event-type toggles — v1.0 (PAY-01..06, CONS-01/02, MOD-01..03)
- ✓ Party-треки: per-track вопросы/модерация/тарифы/Sheet-вкладка — v1.0 (TRACK-01..06)

### Active

<!-- Scope v2 — детально в REQUIREMENTS.md. -->

**Settings-schema реестр:**
- [ ] Единый реестр SETTINGS_SCHEMA (metadata на ключ: parse/default/label/group/type), инкрементальная миграция call-site'ов на него

**Bitrix + web-канал:**
- [ ] Интеграция с Bitrix CRM (лиды/контакты из регистраций)
- [ ] Web-канал регистрации (форма вне Telegram, единый бэкенд)

**Города:**
- [ ] Управление городами/локациями из админки + город как фильтр аналитики/рассылок

**Геймификация:**
- [ ] Система заданий по трекам + начисление монет за выполнение + проверка менеджером + антидроп-лимит

**Роли (⚠️ нужен ТЗ — модель разграничения ещё не определена):**
- [ ] Три роли (Делегат/Менеджер/Администратор) с разграничением доступа + управление менеджерами из админки

> **Режим v2:** зафиксировано как scope/бэклог идей. Детальные фазы не планируются, пока нет точного ТЗ (решение 2026-07-24). Реестр можно начинать первым — самодостаточен, разблокирует остальное.

### Out of Scope

- Розыгрыши / бинго — непонятный scope.
- OCR для резюме — overthink, менеджер сам открывает файл.
- Админка в Google-таблице — двусторонняя синхра ложила прошлый бот. Админка остаётся в боте.
- Разделение на отдельные боты для форумов/конференций — решено: один модульный бот.
- Замена core-стека (aiogram/SQLite/polling) — brownfield, встраиваться, не переписывать.

## Context

- **Brownfield**: бот уже в продакшене. Архитектура задокументирована в `README.md` (структура, стек, схема БД, FSM, порядок роутеров). Источник истины по текущему коду — README + сам код.
- **Стек**: aiogram 3 (async), SQLite через aiosqlite, pydantic-settings, gspread, long polling. Запуск через `python main.py` или docker-compose.
- **Структура**: `handlers/` (admin, registration, user_actions, states), `keyboards/builders.py`, `database/db.py`, `services/sheets.py`, `config.py`, `main.py`.
- **ТЗ**: `ТЗ_Бот_YouLead.docx` — требования к боту форума YouLead. План доработок — `PLAN_YOULEAD_TZ.md`.
- **Масштаб реальный**: YL'26/1 — 1072 отобранных, в чат зашло 590 (55%). Целевой объём 1000–1500/сезон.
- **Референс модуля оплаты**: бот RusCo'26 (регистрация + оплата + напоминания + чеки).
- **Известные проблемы прошлого бота**: слетали баллы (отсюда требование transactions log для монет); двусторонняя синхра с Google-таблицей ложила бот.

## Constraints

- **Tech stack**: aiogram 3 + SQLite + long polling — сохранить, не переписывать. Новые фичи встраивать в существующую модульную архитектуру.
- **Compatibility**: миграции БД (поле `status`, таблица `coins`, поля оплаты) должны не ломать существующие записи пользователей.
- **Scheduler**: отложенные рассылки/напоминания требуют персистентного хранилища job'ов (БД), т.к. FSM — MemoryStorage и сбрасывается при перезапуске.
- **Telegram Bot API**: кастомные эмодзи Premium не пересылаются ботом без коллектируемого юзернейма (Fragment). Ограничение API, не бота.
- **Масштаб модерации**: при 1000+ заявках UI заявок обязан быть пагинированным (не сообщение на заявку — засорит чат).

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Один универсальный бот, не разделять на форумы/конференции | Движок реги уже динамический; рассылки/админка/Sheets идентичны; два бота = двойной код и баги | — Pending |
| Регистрация → заявки через `approval_mode` toggle (раздельный short/full) | ТЗ требует ручного одобрения менеджером; toggle сохраняет быстрый авто-режим где нужно | — Pending |
| UI заявок — пагинированный раздел «Заявки» (тиндер), не сообщение на заявку | При 1000 заявках inline-сообщения засорят чат | — Pending |
| Монеты с transactions log (таблица `coins` с reason/changed_by/timestamp) | В прошлом боте слетали баллы — нужна аудируемость каждого изменения | — Pending |
| Модульность мероприятия (toggle модулей оплаты/согласий) | Один бот обслуживает форумы и конференции переключением настроек | — Pending |
| Геймификацию отложить | Нет чёткого ТЗ на механику, 8 блоков открытых вопросов | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-07-24 after v1.0 YouLead'26 MVP milestone — v2 Registry & Multichannel started*
