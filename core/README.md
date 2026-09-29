# core/ — общие модули проекта

Здесь живут модули, которые раньше лежали россыпью в корне репозитория. Их объединяет одно:
они **не зависят от aiogram** и нужны сразу нескольким процессам — боту (`handlers/`,
`services/`), Mini App (`miniapp/`, FastAPI) и частично дашборду (`dashboard/`).

Что здесь:

- **Настройки:** `settings_schema.py` (реестр `SETTINGS_SCHEMA`), `settings_ops.py`,
  `settings_validation.py`, `settings_synonyms.py`, `settings_audit.py`.
- **Анкета:** `reg_engine.py` (ядро анкеты), `reg_labels.py`, `reg_options.py`, `reg_presets.py`,
  `payment_options.py`, `moderation_card.py`, `i18n_ui_en.py`.
- **Города:** `cities.py`.
- **Геймификация:** `game_labels.py`, `chat_score.py`.
- **Веб-слой и дашборд:** `web_theme.py`, `tg_media.py`, `arrival_stats.py`, `secret_redact.py`,
  `dashboard_favicon.py`.

В корне остались только `main.py` (точка входа, её запускает Dockerfile) и `config.py`
(загрузка `.env`). Новый общий модуль кладите сюда, а не в корень — это проверяет
`tests/test_root_layout.py`.

Как импортировать:

```python
from core import cities                     # вызовы остаются cities.normalize_city(...)
from core.settings_schema import SETTINGS_SCHEMA
```

Два правила:

1. **`core/__init__.py` пустой и должен оставаться пустым.** Образ дашборда
   (`dashboard/Dockerfile`) копирует из `core/` только пять модулей на чистой stdlib
   (`web_theme`, `tg_media`, `arrival_stats`, `secret_redact`, `chat_score`). Любой импорт
   в `__init__.py` потянет туда `database.db`/aiosqlite, и дашборд не запустится.
   Если дашборду понадобился ещё один модуль отсюда — добавьте строку `COPY` в
   `dashboard/Dockerfile` (сторож: `tests/test_dashboard_docker.py`).
2. **Порядок импортов между модулями — намеренный.** Некоторые модули нарочно не импортируют
   друг друга или импортируют лениво внутри функции (например, `database/db.py` не импортирует
   `cities`), чтобы не было циклов. Переставлять такие импорты нельзя.
