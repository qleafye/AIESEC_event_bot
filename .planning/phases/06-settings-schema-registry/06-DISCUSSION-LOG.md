# Phase 6: Settings-schema Registry - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-24
**Phase:** 06-settings-schema-registry
**Areas discussed:** Форма реестра, Охват типов, Порядок миграции, Сеть регресса

**Framing note (пользователь):** «вся суть переезда на реестр чтобы убрать взаимодействие 4 файлов, и сделать настройку через единый файл» — north star для всех решений.

---

## Форма реестра — парсинг

| Option | Description | Selected |
|--------|-------------|----------|
| type гонит парсер | Запись хранит `type`; общий `parse_value(key, raw)` парсит по типу. Опциональный override-callable для редких случаев. | ✓ |
| parse-callable на каждый ключ | ~70 лямбд в реестре — шумно, противоречит единой семантике. | |

**User's choice:** type гонит парсер (+ optional per-key override).

## Форма реестра — где живёт / структура

| Option | Description | Selected |
|--------|-------------|----------|
| Новый `settings_schema.py`, dict-by-key | `SETTINGS_SCHEMA = {key: {type, group, label, default, prompt, parse?}}`. Один файл = единая точка правки. Нет циклов. | ✓ |
| Внутри `database/db.py` | Смешивает infra-слой с UI-текстами. | |
| Внутри `handlers/admin.py` | Риск циклов (registration импортирует из admin); admin.py уже 3120 строк. | |

**User's choice:** Новый `settings_schema.py`, dict-by-key.

## Форма реестра — аксессор для потребителей

| Option | Description | Selected |
|--------|-------------|----------|
| Новый `get_setting_typed(key)` | Async-обёртка: raw → default из реестра → parse по type. REG_DEFAULTS поглощён. `get_setting` остаётся для raw. | ✓ |
| Sync `parse_setting(key, raw)` | Реестр даёт только parse+default; call-site сам дёргает get_setting. | |
| Оба: typed async + внутри sync parse | Публичный async API + чистый sync для тестов. | |

**User's choice:** `get_setting_typed(key)`.
**Notes:** По конвенции проекта typed-обёртка внутри всё равно делегирует чистому sync-хелперу (`_parse_setting`) — parse-логика тестируема без БД (D-08). Не противоречит выбору.

## Охват типов — toggle-ключи

| Option | Description | Selected |
|--------|-------------|----------|
| Да, все toggle — один реестр | party_enabled/edu_conditional/reg_university_mode/reg_q_* → `type: toggle`. Поглощает REG_DEFAULTS и хардкод кнопок. Отдельной волной. | ✓ |
| Нет, только text/int/list на старте | REG_DEFAULTS не поглощён — SC#1 не выполнен полностью. | |
| Да, но последней волной | Сначала text, toggle — позже. | |

**User's choice:** Да, все toggle в реестр (мигрируются отдельной волной).

## Охват типов — photo/file поля

| Option | Description | Selected |
|--------|-------------|----------|
| В реестр как `type: photo/file` | metadata/группа/label из реестра, parse = passthrough file_id. Сбор (upload UI) остаётся спец-механикой. Единый файл описывает ВСЕ ключи. | ✓ |
| Отдельно — не трогаем | Остаётся 2 источника metadata. | |

**User's choice:** В реестр как `type: photo/file`.

## Порядок миграции — пилотная группа

| Option | Description | Selected |
|--------|-------------|----------|
| 🎪 Событие/Медиа (event) — чистый text | 10 text-ключей, простейший тип, мало call-site'ов, низкий риск. | ✓ |
| pending_reminder_interval + toggles (int/bool) | Сразу нетривиальный parse, где реальная ценность; выше риск. | |
| Ты решаешь | planner выбирает по анализу. | |

**User's choice:** Группа event (чистый text) первой.

## Порядок миграции — сосуществование old/new рендера

| Option | Description | Selected |
|--------|-------------|----------|
| Реестр — источник, старые списки генерятся из него | Мигрированный ключ живёт только в реестре; SETTINGS_FIELDS/GROUPS = generated view. Один source of truth сразу, нет двоения. | ✓ |
| Флаг на группу: рендер старый или новый | Два кодовых пути одновременно — больше ветвления. | |

**User's choice:** Реестр = источник, старые списки — generated views.

## Сеть регресса — чем доказываем «байт-в-байт»

| Option | Description | Selected |
|--------|-------------|----------|
| parse-equivalence тест | raw-входы (вкл None/пусто/мусор) → реестр-parse == старый ручной парс. Sync, без БД. | ✓ |
| snapshot рендера настроек | render_settings ДО == ПОСЛЕ (порядок/группы/label). | ✓ |
| coverage-тест реестра | Каждый ключ в одной группе, нет осиротевших, type валиден, default парсится. | ✓ |

**User's choice:** Все три + ручной smoke-тест на реальном экране настроек (multiSelect).
**Notes:** env без CI/линтера, тесты через `asyncio.run` (нет pytest-asyncio) — ручная проверка обязательна дополнением к автотестам.

---

## Claude's Discretion

- Точные имена (`parse_value` vs `_parse_setting`, `get_setting_typed`).
- Механика генерации `SETTINGS_FIELDS`/`SETTINGS_GROUPS` из реестра (computed property / билдер).
- Порядок волн после `event` (reg → pay → party → consent → toggle → photo/file).

## Deferred Ideas

- Bitrix CRM / web-канал / города / геймификация / роли — отдельные группы v2, свои фазы, ждут ТЗ.
- Реестр как питание для будущего inline-UI редактора настроек и `coin_rules` (геймификация) — вне фазы.
