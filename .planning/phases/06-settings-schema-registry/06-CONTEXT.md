# Phase 6: Settings-schema Registry - Context

**Gathered:** 2026-07-24
**Status:** Ready for planning

<domain>
## Phase Boundary

Единый `SETTINGS_SCHEMA`-реестр становится источником метаданных (`type`, `group`, `label`, `default`, `parse`) для ключей `bot_settings`. Существующие потребители (`services/reminders.py`, `services/scheduler.py`, `handlers/admin.py`, `keyboards/builders.py`) и админ-UI настроек читают значения через реестр — **инкрементально, группа-за-группой, без ломки** текущего поведения на ~590 живых юзерах.

**North star (сформулировал пользователь):** суть переезда — убрать координацию 4 файлов при добавлении/правке настройки; сделать всю настройку через **один файл**.

**В scope:** REG-01 (реестр), REG-02 (потребители читают через реестр), REG-03 (admin-UI рендерится из реестра). Реестр самодостаточен — не зависит от остальных групп v2.

**НЕ в scope (свои будущие фазы):** Bitrix CRM, web-канал, города, геймификация, роли. Не трогать в этой фазе.
</domain>

<decisions>
## Implementation Decisions

### Форма реестра
- **D-01:** Реестр живёт в **новом модуле `settings_schema.py`** (не в `db.py`, не в `admin.py`). Причина: `db.py` — infra-слой (не место для UI-текстов label/prompt); `admin.py` уже 3120 строк, и `registration.py` уже импортирует из `admin` → риск циклов. Новый модуль импортируется потребителями, сам ни от кого не зависит → циклов нет.
- **D-02:** Структура — **dict-by-key**: `SETTINGS_SCHEMA = {key: {type, group, label, default, prompt, parse?}}`. O(1) lookup по ключу. Plain dict, без dataclass (конвенция проекта — домен-сущности как plain dict, см. `CONVENTIONS.md`).
- **D-03:** Парсинг — **type-driven**: запись хранит `type` (`toggle`/`int`/`list`/`date`/`text`/`enum`/`photo`/`file`); общий `parse_value(key, raw)` смотрит `type` и парсит правильно. Единая семантика on/off/default на тип. Редкие спецслучаи — опциональный `parse`-callable override в записи (не правило, а исключение).
- **D-04:** `type` taxonomy: `toggle` (on/off), `int` (напр. `pending_reminder_interval`), `list` (перенос строк / `;`-разделитель для option-списков), `date`, `text`, `enum` (напр. `event_type`), `photo` / `file` (passthrough `file_id`).

### Аксессор для потребителей (REG-02)
- **D-05:** Новый **async `get_setting_typed(key)`**: читает raw через существующий `get_setting`, если `None` — берёт `default` из реестра, парсит по `type`. Call-site'ы меняют паттерн `get_setting(k)` + ручной парс → один вызов.
- **D-06:** `REG_DEFAULTS` (registration.py:197) **поглощён реестром** — `default` каждого reg_q_* ключа переезжает в его запись; `REG_DEFAULTS` не дублируется (SC#1).
- **D-07:** Старый `get_setting`/`set_setting`/`delete_setting` (db.py:184–205) **остаются** — raw string I/O нужен для write-пути (admin пишет raw) и мест, где сырое значение уместно. `get_setting_typed` — тонкая обёртка над ними, не замена.
- **D-08:** (конвенция проекта, не override выбора) `get_setting_typed` внутри делегирует чистому sync-хелперу `_parse_setting(key, raw)` (или `parse_value`) — pure-функция парсит без БД, чтобы parse-логика была юнит-тестируема через `asyncio.run`-free тесты. Соответствует паттерну «извлекай тестируемый `_private` хелпер» из `CONVENTIONS.md`.

### Охват типов (что заходит в реестр)
- **D-09:** **ВСЕ ключи** в одном реестре: text/int/list (`SETTINGS_FIELDS`) **+ toggle-кнопки** (`party_enabled`, `edu_conditional`, `reg_university_mode`, все `reg_q_*` из `REG_DEFAULTS`) **+ photo/file** (`PHOTO_FIELDS`: program/speakers/start/venue; `FILE_FIELDS`: reg_bonus; PDF согласий). Один файл описывает ВСЕ ключи — это и есть цель переезда; оставить toggle/photo хардкодом = сохранить ту же 4-файловую боль.
- **D-10:** photo/file — `type: photo|file`, `parse` = passthrough (raw `file_id`). **Сбор значения** (upload UI в admin) остаётся спец-механикой; из реестра берутся только metadata (`group`/`label`/`type`). Реестр описывает ключ, не заменяет upload-flow.

### Порядок миграции (инкрементально, non-breaking — SC#3)
- **D-11:** **Пилот — группа `event` (🎪 Событие/Медиа)**: 10 чистых text-ключей (event_date/time/place_name/place_address/contacts/start_text/event_name/event_type). Простейший тип, мало call-site'ов, низкий риск — доказывает реестр+рендер+тест на лёгкой группе перед сложными.
- **D-12:** toggle-ключи (+ поглощение `REG_DEFAULTS`) мигрируются **отдельной волной** внутри фазы, после того как паттерн доказан на text-группах. Конечная цель — всё в реестре.
- **D-13:** **Реестр = источник; старые списки становятся generated views.** Для мигрированного ключа значение метаданных живёт ТОЛЬКО в реестре; `SETTINGS_FIELDS` / `SETTINGS_GROUPS` (admin.py:341/397) для мигрированных групп **генерируются из реестра** (computed) — один source of truth сразу, нет двоения данных. Немигрированные ключи остаются literal-записями в старых списках до своей волны. На любом промежуточном шаге бот рабочий, ни одна из ~590 записей не теряется, ни одна настройка не сбрасывается.
- **D-14:** Потребители `SETTINGS_FIELDS`/`SETTINGS_GROUPS` продолжают читать те же имена (теперь generated) — не переписываются массово; admin-UI рендер для мигрированных групп идёт по `type` из реестра (render-by-type), заменяя ручные списки (SC#4).

### Сеть регресса (доказать «байт-в-байт» — SC#2)
- **D-15:** **parse-equivalence тест** (основной): для каждого мигрированного ключа — набор raw-входов (вкл. `None` / пусто / мусор) → реестр-parse даёт то же, что старый ручной парс. Ловит сдвиг семантики default/on/off. Чистый sync, без БД.
- **D-16:** **snapshot рендера настроек**: зафиксировать текст/клавиатуру `render_settings` ДО миграции, сравнить ПОСЛЕ — порядок/группы/label не съехали (UI-регресс от generated view).
- **D-17:** **coverage-тест реестра**: каждый ключ ровно в одной группе, нет осиротевших, `type` ∈ допустимый набор, `default` парсится без ошибки. Расширение `tests/test_settings_groups_c0x.py`.
- **D-18:** **+ ручной smoke** на реальном экране настроек после каждой мигрированной группы (env без CI/линтера — ручная проверка обязательна).

### Claude's Discretion
- Точные имена: `parse_value` vs `_parse_setting`, `get_setting_typed` vs альтернатива — planner выбирает по стилю файла.
- Механика генерации `SETTINGS_FIELDS`/`SETTINGS_GROUPS` из реестра (computed property / функция-билдер) — реализация на усмотрение planner, инвариант — один source of truth (D-13).
- Порядок волн после `event` (reg → pay → party → consent → toggle → photo/file) — planner уточняет по анализу call-site'ов; инвариант — начинать с низкорисковых.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Требования / roadmap
- `.planning/ROADMAP.md` §«Phase 6: Settings-schema Registry» — Goal, 4 Success Criteria (байт-в-байт, инкрементально, generated view сосуществует, admin-UI из реестра)
- `.planning/REQUIREMENTS.md` §«Settings-schema Registry» — REG-01 / REG-02 / REG-03

### Код — точки миграции (source of truth по текущему поведению)
- `handlers/admin.py:341` — `SETTINGS_FIELDS` (text-настройки: key, label, prompt) → generated view
- `handlers/admin.py:397` — `SETTINGS_GROUPS` (group→keys, из quick 260724-c0x — временная группировка, которую реестр заменяет) → generated view
- `handlers/admin.py:389` — `_SETTINGS_DISPLAY_DEFAULTS` (fallback-тексты рендера) → переезжает в `default` реестра
- `handlers/admin.py:450/457` — `PHOTO_FIELDS` / `FILE_FIELDS` → в реестр как `type: photo/file`
- `handlers/admin.py` `build_settings_keyboard` — хардкод toggle-кнопок (party_enabled, edu_conditional, reg_university_mode) → в реестр как `type: toggle`
- `handlers/registration.py:197` — `REG_DEFAULTS` (дефолты reg_q_* toggle) → поглощается реестром (D-06)
- `database/db.py:184` — `get_setting` / `set_setting` / `delete_setting` (raw I/O; остаются, D-07)
- Потребители значений (REG-02): `services/reminders.py`, `services/scheduler.py`, `handlers/admin.py`, `keyboards/builders.py`

### Тесты (регресс-набор)
- `tests/test_settings_groups_c0x.py` — coverage группировки (расширить, D-17)
- `tests/test_registration_phase4.py` / `test_registration_phase5.py` — существующие проверки `REG_DEFAULTS` (должны остаться зелёными после поглощения)
- `.planning/codebase/CONVENTIONS.md` — naming, pure-helper extraction, import/циклы, тесты через `asyncio.run` (нет pytest-asyncio)

### Провенанс
- `.planning/quick/260724-c0x-ux/` — откуда пришли `SETTINGS_GROUPS`/флаги задано-не-задано (временная группировка, которую эта фаза заменяет)
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `SETTINGS_GROUPS` shape `(label, token, [keys])` уже зеркалит `REG_CATEGORIES` (registration.py) — форма записи реестра может унаследовать эту привычную структуру группировки.
- `_settings_group_keys` / `_settings_group_label` / `_settings_nav_groups` (admin.py:420–448) с leftover-safety («📦 Прочие») — паттерн «ни один ключ не теряется» переиспользуется генератором из реестра.
- Пример типизированного парса уже есть: `is_on = (v == "on") if v is not None else (REG_DEFAULTS.get(sk, "on") == "on")` (admin.py:501/2097/2248) — эталон семантики toggle, который parse-equivalence тест должен воспроизвести байт-в-байт.

### Established Patterns
- Pure `_private` helper extraction для тестируемости (CONVENTIONS.md §Function Design) — обосновывает D-08 (`get_setting_typed` делегирует sync `_parse_setting`).
- Lazy (function-local) import для разрыва циклов (`keyboards/builders.py` ↔ `handlers.payment`) — если возникнет цикл с `settings_schema.py`, применять тот же приём; но новый модуль спроектирован без обратных зависимостей.
- Комментарии-провенанс с тегом-ID (`# REG-…:`) — помечать мигрированные ключи/строки для аудит-трейла.

### Integration Points
- `settings_schema.py` (новый) ← импортируется `handlers/admin.py`, `handlers/registration.py`, `services/reminders.py`, `services/scheduler.py`, `keyboards/builders.py`. Сам импортирует максимум `database.db` (raw I/O) — направление зависимостей однонаправленное.
- `get_setting_typed` встраивается рядом с `get_setting` (db.py) ИЛИ в `settings_schema.py` — planner решает; инвариант — обёртка не дублирует raw I/O.
</code_context>

<specifics>
## Specific Ideas

- Прямая цитата-намерение пользователя: «вся суть переезда на реестр чтобы убрать взаимодействие 4 файлов, и сделать настройку через единый файл». Любое решение проверять против неё: добавить/поправить настройку должно требовать правки ТОЛЬКО записи в `settings_schema.py`.
- Регресс — многослойный по явному запросу: parse-equivalence + snapshot рендера + coverage-тест + ручной smoke (не один тест, а все четыре).
</specifics>

<deferred>
## Deferred Ideas

- Bitrix CRM (CRM-01/02), web-канал (WEB-01/02), города (CITY-01/02), геймификация (GAME-01..04), роли (ROLE-01/02) — отдельные группы v2, свои фазы, ждут ТЗ. Реестр разблокирует их (единый config-слой), но их scope сюда не тянуть.
- Настройки как значения в реестре могут позже питать inline-UI редактор и `coin_rules` для геймификации — вне этой фазы (см. memory `admin-config-backlog`).

None иных — обсуждение осталось в рамках scope фазы.
</deferred>

---

*Phase: 6-settings-schema-registry*
*Context gathered: 2026-07-24*
