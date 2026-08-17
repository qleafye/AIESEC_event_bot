<!-- GSD:project-start source:PROJECT.md -->
## Project

**AIESEC Event Bot**

Универсальный Telegram-бот (aiogram 3, SQLite, Google Sheets) для мероприятий AIESEC — форумов (YouLead) и конференций (RusCo). Один модульный бот для всех событий: регистрация, реферальная система, рассылки, админка, аналитика. Масштаб — 1000–1500 пользователей за сезон. Этот milestone расширяет существующий бот фичами «до запуска» YL'26 и универсальными модулями для конференций.

**Core Value:** Менеджер DXP может полностью провести регистрацию делегатов мероприятия через бота — от заявки до одобрения — без ручного учёта в таблицах и без перезапуска кода между событиями.

### Constraints

- **Tech stack**: aiogram 3 + SQLite + long polling — сохранить, не переписывать. Новые фичи встраивать в существующую модульную архитектуру.
- **Compatibility**: миграции БД (поле `status`, таблица `coins`, поля оплаты) должны не ломать существующие записи пользователей.
- **Scheduler**: отложенные рассылки/напоминания требуют персистентного хранилища job'ов (БД), т.к. FSM — MemoryStorage и сбрасывается при перезапуске.
- **Telegram Bot API**: кастомные эмодзи Premium не пересылаются ботом без коллектируемого юзернейма (Fragment). Ограничение API, не бота.
- **Масштаб модерации**: при 1000+ заявках UI заявок обязан быть пагинированным (не сообщение на заявку — засорит чат).
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Fixed Core Stack (Do Not Change)
| Technology | Version in use | Notes |
|------------|---------------|-------|
| aiogram | >=3.0.0 (latest stable: 3.29.0) | Async Telegram framework |
| aiosqlite | latest | Async SQLite driver |
| pydantic-settings | latest | `.env` config |
| gspread + google-auth | latest | Google Sheets sync |
| aiohttp-socks | latest | Proxy support |
| long polling | — | No webhooks, no change needed |
## New Libraries to Add
### Scheduler: APScheduler 3.x
# Integration pattern in main.py startup
- `date` trigger — one-time delayed broadcast at scheduled datetime
- `interval` trigger — pending-application reminder every N minutes
- `date` trigger — payment reminder at T-3 days and T-1 day
### DB Migration: Extend Existing Pattern (No New Library)
# In init_db(), add new tables using CREATE TABLE IF NOT EXISTS:
# Add new columns to existing tables using _ensure_column():
### Telegram Bot API: getChatMember (No New Library)
### File/Receipt Storage: file_id Pattern (No New Library)
# In registration handler, when user sends resume:
- `resume_file_id TEXT` — PDF/DOCX resume
- `receipt_file_id TEXT` — payment receipt
## Complete Addition to requirements.txt
## Supporting Libraries (Supporting Tables)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| apscheduler | 3.11.2 | Persistent scheduled jobs (broadcasts, reminders) | Required for delayed broadcasts + payment reminders |
| sqlalchemy | >=2.0,<3.0 | APScheduler's SQLAlchemyJobStore backend | Required alongside apscheduler |
## Alternatives Considered
| Recommended | Alternative | Why Not |
|-------------|-------------|---------|
| APScheduler 3.11.2 | APScheduler 4.0 | Still alpha (4.0.0a6). Breaking API changes vs 3.x. Not production-ready as of June 2026. |
| APScheduler 3.x `AsyncIOScheduler` | `BackgroundScheduler` + thread | Background thread cannot safely call `await bot.send_message()` from thread context in asyncio. Requires `run_coroutine_threadsafe` — fragile and error-prone. |
| SQLAlchemyJobStore (SQLite) | Custom job table in aiosqlite | Custom approach requires building serialization, recovery, and idempotency from scratch. SQLAlchemyJobStore is battle-tested. |
| SQLAlchemyJobStore (SQLite) | Redis / PostgreSQL | Requires additional infrastructure (Redis/PG container). Single-instance bot on 1000-user scale does not need distributed coordination. |
| Extend `_ensure_column` pattern | Alembic | Alembic requires SQLAlchemy ORM models for all tables. Massive refactor. Wrong abstraction level for this project. |
| Extend `_ensure_column` pattern | yoyo-migrations | yoyo is synchronous. No async support. Adds a dependency for trivial functionality already present in the codebase. |
| PRAGMA user_version | Custom migrations table | SQLite has a built-in user_version pragma. No extra table needed. |
| aiogram 3 getChatMember built-in | python-telegram-bot | Different framework. Cannot mix with aiogram. |
| file_id in DB column | Download files to disk | Disk storage requires Docker volume management, file naming, cleanup. file_id is permanent, requires no disk I/O, lets bot forward files instantly. |
## What NOT to Use
| Avoid | Why | Use Instead |
|-------|-----|-------------|
| APScheduler 4.0 | Pre-release alpha; API changed completely from 3.x; no stable release path | APScheduler 3.11.2 |
| `MemoryJobStore` for scheduled broadcasts | Jobs lost on restart/crash; violates the persistence requirement | `SQLAlchemyJobStore` with SQLite |
| `BackgroundScheduler` | Runs in a thread; async job functions (coroutines) cannot be scheduled; bot.send_message must cross thread boundary | `AsyncIOScheduler` |
| Alembic | Requires SQLAlchemy ORM; introduces ORM models for all existing tables; overkill for raw-SQL project | Inline `_ensure_column` + `CREATE TABLE IF NOT EXISTS` pattern |
| yoyo-migrations | Synchronous only; no aiosqlite compatibility; adds dependency for 30-line solution | PRAGMA user_version + inline ALTER TABLE |
| aiojobs | Manages concurrent coroutines, not scheduled delayed jobs; no persistence | APScheduler 3.x |
| Storing files on disk | Requires volume management, naming, cleanup; files deleted if volume unmounted | Store `file_id` TEXT in DB, use Telegram servers as storage |
## Version Compatibility
| Package | Compatible With | Notes |
|---------|----------------|-------|
| apscheduler==3.11.2 | sqlalchemy>=1.4,<3.0 | Use sqlalchemy 2.x (current stable 2.0.51). APScheduler 3.x works with both SQLAlchemy 1.4 and 2.x. |
| apscheduler==3.11.2 | Python >=3.8 | Bot requires Python 3.10+ (aiogram constraint); no conflict. |
| sqlalchemy>=2.0 | aiosqlite | aiosqlite is NOT used by APScheduler's job store — APScheduler uses sync SQLAlchemy for job store I/O. aiosqlite remains the async driver for all application DB queries. No conflict. |
| aiogram 3.29.0 | Python >=3.10, <3.15 | No change — already in use. |
## Sources
- Context7 `/agronholm/apscheduler` — APScheduler 3.x SQLAlchemyDataStore docs, async scheduler patterns (HIGH confidence)
- Context7 `/websites/aiogram_dev_en_v3_27_0` — getChatMember, ChatMemberStatus, MEMBERS group, document file_id (HIGH confidence)
- [APScheduler PyPI page](https://pypi.org/project/APScheduler/) — confirmed 3.11.2 stable, 4.0.0a6 pre-release (HIGH confidence)
- [APScheduler 3.x user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html) — AsyncIOScheduler + SQLAlchemyJobStore patterns (HIGH confidence)
- [SQLAlchemy PyPI / changelog](https://www.sqlalchemy.org/changelog/CHANGES_2_0_49) — confirmed 2.0.51 stable as of June 2026 (HIGH confidence)
- [aiogram PyPI page](https://pypi.org/project/aiogram/) — confirmed 3.29.0 latest stable June 2026 (HIGH confidence)
- Existing `database/db.py` — existing `_ensure_column` migration pattern already in production (source of truth)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

### Главное правило продукта

**МЫ ДЕЛАЕМ БОТА ДЛЯ ЛЮДЕЙ, НЕ ДЛЯ ПРОГЕРОВ.** Менеджер должен зайти в админку и суметь
настроить всё сам — без разработчика, без документации, ночью перед мероприятием.

Практически это значит:

- **Выбор из готового набора — кнопками.** Тумблеры и чекбоксы с человеческими подписями,
  а не «введите `moderate_reg;settings` через точку с запятой». Кодовые значения (capability,
  ключи настроек, коды городов) человеку не показываем и ввести не просим.
- **Текстовый ввод — только для действительно произвольных значений** (имя, сумма, дата, текст
  сообщения). Там же в подсказке — пример правильного формата.
- **Ошибка объясняет, что сделать.** «Не понял, пришлите пересланное сообщение или @username»,
  а не «invalid input».
- **Разрушительные операции — с подтверждением**, в котором написано, что именно пропадёт.
- Проверочный вопрос к любой админ-фиче: *сможет ли менеджер сделать это один, без меня?*

## Сообщения коммитов

**Язык — русский.** Английскими остаются только идентификаторы кода и термины, у которых нет
принятого перевода (`payment_status`, `SETTINGS_SCHEMA`, deep-link, fail-soft).

**Формат:** `тип(скоуп): тема` (Conventional Commits). Тип — `feat`, `fix`, `docs`, `test`,
`refactor`, `chore`. Скоуп — **модуль проекта**: `admin`, `registration`, `payment`,
`user_actions`, `db`, `sheets`, `scheduler`, `reminders`, `ui`, `config`, `tests`, `docs`.
Тронуто много модулей — скоуп опустить.

**Служебного — только необходимое.** В сообщение не идут номера планов и фаз (`14-02`,
`quick-260817-jas`, `09.2-06`) и ID из ревью и очередей (`GAME-10`, `CR-03`, `findings #3`):
они ссылаются на `.planning/`, которого в репозитории нет, и для читателя истории это шифр
без ключа. Привязка к плану живёт в `.planning/`, а не в git.

**Тело — только когда «почему» не видно из темы:** что именно ломалось, почему выбрано такое
решение, какие остаются ограничения. Пересказывать диф не нужно.

```
feat(payment): тарифы по трекам + напоминания T-3 / T-1

Джобы ставятся при выборе тарифа и снимаются при подтверждении чека. Сама джоба
перед отправкой перечитывает payment_status — иначе гонка между отменой и запуском
даёт напоминание уже оплатившему делегату.
```

Прочие конвенции — по мере появления паттернов в коде.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
