---
id: 260721-msh
slug: add-alumni-status-question
description: Новый вопрос регистрации «Аламни/айсекер» (alumni_status)
date: 2026-07-21
mode: quick (executed inline — task fully specified, no research/planning delta)
---

# Quick 260721-msh — вопрос «Аламни/айсекер»

## Задача

Менеджер сверял список вопросов для гостей вечеринки (аламни) с фактическим
флоу бота. Единственный отсутствующий вопрос — «Аламни/айсекер». «Ник в тг»
решили не спрашивать: @username берётся автоматом из Telegram.

## Что добавляем

- step_key `alumni_status`, setting `reg_q_alumni_status`, DB-колонка `alumni_status TEXT`
- Тип `text` + reply-клавиатура `["Аламни", "Айсекер", "Ни то, ни другое"]`
- Дефолт **OFF** (как любой новый вопрос — живой флоу не меняется)
- Текст по умолчанию: «Ты аламни или айсекер?» (переопределяется `reg_prompt_alumni_status`,
  для party — `reg_prompt_alumni_status__party`)

Эталон для копирования — существующий `bed_sharing` (тот же тип, та же клавиатура,
тот же `_store_choice`).

## Задачи

### T1 — вопрос в движке регистрации
Файлы: `handlers/registration.py`, `handlers/states.py`

- `REG_FLOW`: `("alumni_status", "reg_q_alumni_status", "text")` сразу после `("phone", ...)`
- `REG_DEFAULTS["reg_q_alumni_status"] = "off"`
- `REG_LABELS["reg_q_alumni_status"] = "🎓 Аламни/айсекер"`
- `REG_CATEGORIES` → «🎤 Конфа», рядом с `reg_q_aiesec_role`
- `REG_PRESETS["party"]["on"]` += `reg_q_alumni_status`
- `_ask_step`: ветка `elif step_key == "alumni_status"` → `_prompt(...)` + `_reply_kb([...])`
- `states.py`: `Registration.alumni_status = State()`
- Хендлер `@router.message(Registration.alumni_status)` → `_store_choice("alumni_status", "alumni_status", ...)`
- `SHEET_COLUMNS`: колонка «Аламни/айсекер» после «Телефон»
- `PARTY_SHEET_COLUMNS`: та же колонка после «ВК»
- `_build_summary`: строка «Аламни/айсекер»

**verify:** `python -c "import handlers.registration"`; шаг присутствует в `_get_enabled_steps` при `reg_q_alumni_status=on`.

### T2 — персистенция
Файлы: `database/db.py`

- `_ensure_column(db, "users", "alumni_status", "TEXT")` (рядом с `bed_sharing`)
- INSERT: имя колонки + плейсхолдер `?` + `alumni_status=excluded.alumni_status` + `data.get('alumni_status')`
- CSV-label map: `"alumni_status": "Аламни/айсекер"`

**verify:** `add_user` c `alumni_status` → `get_user` возвращает значение; счёт колонок == счёт `?`.

### T3 — тесты
Файл: `tests/test_alumni_status.py`

- шаг включается/выключается через `reg_q_alumni_status`
- колонка есть в `SHEET_HEADERS` и в `party_sheet_headers()`
- round-trip `add_user` → `get_user`

**verify:** полный `pytest` зелёный.

## Риск

Вставка колонки в середину `SHEET_COLUMNS` сдвигает шапку Google-таблицы —
старые строки разъедутся. После деплоя нужна админ-кнопка «♻️ Пересобрать таблицу».
Заморозка шапки (CR-9 `sheet_header_schema`) защищает уже записанные строки до
пересборки.
