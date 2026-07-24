# Анализ улучшений и план развития — AIESEC Event Bot

**Дата:** 2026-07-24
**Модель анализа:** Opus 4.8 (main thread)
**Источники:** `.planning/codebase/*` (карта, 7 док.), `STATE.md`, `ROADMAP.md`, `IMPROVE-LOG.md`, memory

---

## 0. Ключевой вывод: состояние проекта ≠ трекинг

Бот **функционально закончен** для milestone v1.0. По коду присутствуют и работают: coins-ledger, approval/tinder-модерация, APScheduler (persistent), фильтрованные/отложенные рассылки, payment-flow + чеки, consent, allowlist/pre-selection, party-треки, Nextcloud-резюме, Google Sheets sync.

**Но** `ROADMAP.md` (Progress-таблица) помечает фазы 1/3/4 как `Planned 0/N`, а `STATE.md` даёт milestone 25% / `verifying`. Реальность: почти всё внедрено, значительная часть — через quick-tasks (22 записи в STATE), а не через формальный execute-phase. → **Трекинг устарел, требует сверки перед любым «новым milestone».**

**Действие:** аудит milestone (`/gsd-audit-milestone`) для сверки фактического состояния с исходным замыслом, затем синхронизация ROADMAP checkboxes.

---

## P0 — Быстрое усиление (низкий риск, высокий эффект)

| # | Проблема | Файл | Фикс |
|---|----------|------|------|
| P0-1 | `aiogram>=3.0.0` без верхней границы → fresh install может подтянуть aiogram 4.x (hard-break всех handlers) | `requirements.txt` | добавить `,<4.0.0`; запинить прямые зависимости (`pip freeze`, обрезать до прямых) |
| P0-2 | Резюме принимается без лимита размера (у чеков лимит `_RECEIPT_MAX_BYTES=10MB` есть) | `handlers/registration.py:1604` vs `handlers/payment.py:377` | вынести общий `_MAX_UPLOAD_BYTES`, применить в `process_resume` |
| P0-3 | Провал Google Sheets append после ретраев — тихий (только `logger.error`), Sheets-зеркало молча дрейфует | `services/sheets.py:115-133` | админ-алерт на финальном фейле (паттерн `allowlist_refresh_job`) |
| P0-4 | `docker-compose.yml` → `version: 'version'` (мусорное значение) | `docker-compose.yml:1` | убрать ключ (compose v2 не нужен) или `"3.8"` |
| P0-5 | Nextcloud TLS-проверка по умолчанию OFF (`ssl=False`) — резюме с PII под MITM | `config.py:46`, `services/nextcloud.py:43` | подтвердить реальный TLS-статус прод-эндпоинта; если за доверенным CA → `NEXTCLOUD_VERIFY_TLS=true` в live `.env` |

---

## P1 — Процесс и надёжность (нет CI/линта — главный пробел)

| # | Проблема | Фикс |
|---|----------|------|
| P1-1 | **Нет CI** — 336 тестов гоняются только вручную; регрессия проходит в `main` без прогона | GitHub Actions: `pytest` на push/PR |
| P1-2 | **Нет линта/форматтера** — дрейф стиля в 3000+ строк handler'ов ловит только ревью | `ruff` + `ruff format`, pre-commit hook |
| P1-3 | Нет общей изоляции БД в тестах — каждый файл дублирует `_use_tmp_db`, `config.DB_PATH` — мутируемый глобал → блокирует `pytest -n auto` | `conftest.py` с `autouse`-фикстурой save/restore `DB_PATH` |
| P1-4 | Broadcast застревает в статусе `sending` навсегда при краше mid-send, без индикатора | админ-вью помечает `sending`-строки старше N минут как «возможно застрял, переслать» |
| P1-5 | Album-broadcast staging (`pending_albums` in-memory dict) теряется молча при рестарте mid-collection; нет теста | тест на album-путь; при рефакторе сохранить pop-then-check-None guard |

---

## P2 — Архитектура (по мере роста, не срочно)

- **Settings-schema реестр** (keystone из memory `admin-config-backlog`): ~70 ключей `bot_settings`, парсинг/дефолты размазаны по call-site'ам (`_reminder_interval`, `_int_or_default`, инлайн `== "on"`). Риск: новый call-site копирует неверную идиому дефолта → рассинхрон UI vs поведение. Фикс: единая таблица метаданных ключа (parse+default+label) — уже отмечен как post-SumMeet keystone для inline-UI.
- **God-файлы**: `handlers/admin.py` (3120 строк), `handlers/registration.py` (2446). Не резать mid-feature (риск router-order регрессий); группировать новый код рядом с однотипным, сплит — отдельной запланированной фазой.
- **WAL для SQLite**: проверить `PRAGMA journal_mode=WAL` в `init_db` — дешёвая защита от writer-serialization при пиковых mass-approve.
- **Индексы**: `users.status`, `users.payment_status` без индекса (full-scan). Не проблема на 1000-1500, добавить при росте на порядок.

---

## P3 — Развитие (v2 backlog + новые направления)

- **Bitrix multichannel + web-канал регистрации** (memory `bitrix-multichannel-focus`): near-term запрос заказчика, драйвер — троттлинг Telegram в RU. Крупное направление, кандидат №1 в новый milestone.
- **Геймификация** GAME-01..04 (task-система, механика монет) — deferred в v2. Нужен `coin_rules` реестр. Блокирована до settings-schema реестра (coupling tax, memory `post-summeet-sequence`).
- **Роли** ROLE-01..02 (Delegate/Manager/Admin) — deferred v2. Сейчас плоский admin-allowlist по ID, без иерархии.
- **Web App** (Telegram Mini App) — deferred, после реестра+inline-UI.

**Рекомендуемый порядок v2** (из memory, coupling-driven): settings-schema реестр → inline-UI → города → геймификация → Web App. Bitrix/web-канал — параллельный трек по запросу заказчика.

---

## Что уже сделано в этой сессии

1. **Model profile → balanced** (Opus: planner/eval-planner; Sonnet: execute/review/audit; mapper: sonnet). GSD config.
2. **База для нейросети:** `.planning/codebase/` — 7 документов (STACK, INTEGRATIONS, ARCHITECTURE, STRUCTURE, CONVENTIONS, TESTING, CONCERNS), закоммичены. Плюс `MEMORY.md` (persistent user memory).
3. **Knowledge graph:** флаг включён; CLI `graphifyy` не установлен (`uv pip install graphifyy && graphify install` для сборки).
4. **Этот анализ** (`.planning/IMPROVEMENTS.md`).

---

## Рекомендуемые следующие шаги

1. `/gsd-audit-milestone` — сверить факт vs замысел, синхронизировать ROADMAP.
2. Быстрый quick-фикс-пакет P0 (aiogram-pin, resume-size, sheets-alert, compose-version) — один `/gsd-quick`, низкий риск.
3. `/gsd-new-milestone` v2 — направления: Bitrix/web-канал (заказчик) + settings-schema реестр (разблокирует геймификацию/inline-UI).
4. CI + ruff (P1-1, P1-2) — отдельный quick, окупается сразу.
