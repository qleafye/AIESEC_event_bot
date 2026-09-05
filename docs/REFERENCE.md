# Справочник

Полные технические факты по проекту: переменные окружения, схема базы данных, FSM
регистрации, путь оплаты, дерево проекта. Обзор, возможности и быстрый старт живут в
[README.md](../README.md), этот файл про то, что искать по ходу разработки, а не что
делать сначала.

Факты сверены с кодом на коммите, который вводит этот файл: имена переменных
(`config.py`, `dashboard/config.py`, `.env.example`) и таблиц базы (`database/db.py`,
`CREATE TABLE`). Устаревшие поля помечены прямо в тексте, не удалены молча.

## Оглавление

- [Переменные окружения](#переменные-окружения)
- [Что в .env и почему](#что-в-env-и-почему)
- [Google Sheets](#google-sheets)
- [Хранение резюме (Nextcloud)](#хранение-резюме-nextcloud)
- [Структура проекта](#структура-проекта)
- [База данных](#база-данных)
- [Путь оплаты](#путь-оплаты)
- [FSM регистрации](#fsm-регистрации)

## Переменные окружения

```env
# --- Обязательное ---
BOT_TOKEN=123456789:ABCDefGHIjkLLmnoPQRstuVWxyz
ADMIN_IDS=[12345678, 87654321]

# --- Опционально ---
PROXY_URL=socks5://user:pass@host:port   # если Telegram API заблокирован
PROXY_URL_BACKUP=socks5://user:pass@host2:port, api:https://tg.example.com  # резервы через запятую, по приоритету
PROXY_RECHECK_SECONDS=600                 # только первичный seed, дальше правится в боте: /admin → 🔧 Управление → «🔧 Система»
PROXY_CONNECT_TIMEOUT=5                   # то же самое, см. «Что в .env и почему»
DB_PATH=data/forum.db
LOG_LEVEL=INFO                            # DEBUG | INFO | WARNING | ERROR

# --- Google Sheets (пусто = бот работает только на своей БД) ---
GOOGLE_SHEET_ID=
GOOGLE_CREDENTIALS_FILE=google_credentials.json
GOOGLE_SHEET_TAB=                         # первичный seed, дальше /admin → 📊 Данные → «📄 Вкладки таблицы»; пусто = первая вкладка

# --- Города мероприятия ---
EVENT_CITIES=msk|Москва, 30-31 октября|;spb|Санкт-Петербург, 3 октября|СПб;tyumen|Тюмень, 3 октября|Тюмень
EVENT_CITY_DEFAULT=msk                    # код города, применяемый при NULL/неизвестном event_city

# --- Nextcloud для резюме (все пустые = загрузка выключена) ---
NEXTCLOUD_WEBDAV_URL=https://cloud.example.org/remote.php/dav/files/botuser
NEXTCLOUD_USER=
NEXTCLOUD_APP_PASS=
NEXTCLOUD_FOLDER=resumes
NEXTCLOUD_PUBLIC_URL=https://cloud.example.org
NEXTCLOUD_FOLDER_SHARE_TOKEN=             # токен XXXX из публичной ссылки /s/XXXX
NEXTCLOUD_VERIFY_TLS=true                 # по умолчанию true; false, только осознанно
NEXTCLOUD_CA_BUNDLE=                      # PEM self-signed сертификата/CA, чтобы не выключать проверку

# --- Дашборд статистики и Mini App (читает dashboard/config.py, не config.py бота) ---
DASHBOARD_PUBLIC_URL=                     # публичный адрес стека, напр. https://yl26.example.org
DASHBOARD_DB_PATH=/app/data/forum.db      # путь к БД бота внутри контейнера дашборда
DASHBOARD_SESSION_SECRET=                 # секрет подписи cookie-сессии, свой на каждый стек
DASHBOARD_BOT_USERNAME=                   # username бота этого стека для Telegram Login Widget
DASHBOARD_TRUSTED_PROXIES=172.31.0.0/16   # подсеть docker-сети edge, от которой доверяем X-Forwarded-*
```

`NEXTCLOUD_SHARE_PASSWORD` и `NEXTCLOUD_BASE_URL` встречаются в старых `.env`, код их больше
не читает (пароль ставится один раз на публичной шаре папки в UI Nextcloud, базовый адрес
собирается из `NEXTCLOUD_WEBDAV_URL`/`NEXTCLOUD_PUBLIC_URL`). `extra="ignore"` в
`config.Settings` не даёт лишним ключам уронить старт бота.

`ADMIN_IDS`, JSON-список. `GOOGLE_SHEET_TAB` стоит задавать явно, иначе бот пишет в первую
вкладку по позиции, и перестановка вкладок перенаправит запись.

`ADMIN_IDS`, это bootstrap-суперадмины: полный доступ всегда, не снимаются из бота (снять
можно только правкой `.env` и рестартом). Остальных менеджеров с ограниченным доступом
заводит сам бот, таблица `staff`, экран «👥 Роли и доступы» (`/admin` → 🔧 Управление →
«👥 Роли и доступы»), без `.env` и передеплоя. Подробности и таблица прав, docs/ADMIN_GUIDE.md,
§22.

`EVENT_CITIES`, только начальный seed: читается один раз при первом старте с пустой таблицей
`cities`, дальше города живут в БД и правятся экраном `/admin` → 🔧 Управление → «🏙 Города
мероприятия» (добавить, переименовать, база вкладки, вкл/выкл, город по умолчанию, удалить).
Формат seed, `код|подпись|база_вкладки`, разделитель между городами, `;` (та же схема, что у
списков согласий/тарифов, см. «Enter = отправка» в docs/ADMIN_GUIDE.md). `код`,
латиница/цифры/`_`, до 16 символов, используется в deep-link (`?start=city_код`) и в ключах
настроек (`city_enabled__код`, `city_label__код`). `база_вкладки` опциональна: пустая база
(как у Москвы) означает, что город работает на легаси-вкладках (`GOOGLE_SHEET_TAB` и т.д.) без
изменения имён, непустая база («СПб», «Тюмень») даёт городу свои вкладки, «СПб», «СПб Акция»,
«СПб Party», «СПб Незавершённые». `EVENT_CITY_DEFAULT`, код города, который подставляется на
чтении, если у записи `event_city` не задан (без бэкфилла старых строк). Дальше всё, из
админки, «🏙 Города» (см. docs/ADMIN_GUIDE.md, раздел «Города мероприятия»).

Выбранный админом город хранится в `bot_settings` под служебным ключом
`admin_city__{telegram_id}` (один ключ на админа, переживает рестарт, FSM не переживает).
Читается только через `cities.admin_selected_city()`, который возвращает `None` при
выключенном модуле, это единственная точка, где «модуль выключен» превращается в «ничего не
фильтруется». Сам SQL-фильтр строится из дескриптора `cities.city_scope()` →
`database.db._city_clause()`, `database/db.py` намеренно не импортирует `cities` (цикл), скоуп
передаётся значением.

**Резервный прокси (failover).** `PROXY_URL` можно указывать как HTTP-прокси (напр.
`http://host.docker.internal:8118`, если используется privoxy-обвязка), так и напрямую как
`socks5://` эндпоинт, `aiohttp-socks` уже в зависимостях, промежуточный privoxy-хоп не
обязателен. Установка соединения ограничена `PROXY_CONNECT_TIMEOUT` (по умолчанию 5 сек), так
что мёртвый канал стоит боту секунды вместо полного тайм-аута запроса (20-90 сек). При
сетевой ошибке к Telegram (`TelegramNetworkError`) бот автоматически переключается на
`PROXY_URL_BACKUP`, без рестарта, без пересоздания сессии, и остаётся на нём (sticky).
Резервов может быть несколько (через запятую, порядок = приоритет), и резервом может быть не
только прокси, а другой хост Bot API: `api:https://tg.example.com`, прямое соединение на
Cloudflare Worker (reverse-proxy `api.telegram.org` на своём домене), доступный из РФ без
туннеля. Три пути, свой туннель, второй VPS, Worker, не делят инфраструктуру, поэтому
одновременный отказ всех трёх маловероятен.

Возврат на основной канал не проверяется на живых запросах пользователей. В фоне раз в
`PROXY_RECHECK_SECONDS` работает отдельный пробник: он стучится на основной канал сам, и
только когда тот реально отвечает, бот переключается обратно. Пока пробник не подтвердил, что
основной канал жив, ни один запрос пользователя на него не попадёт. Если оба звена мертвы
одновременно, наружу летит тот же исходный `TelegramNetworkError`, что и раньше, `dp.start_polling`
ретраит с тем же бэкоффом, ничего не меняется в этом сценарии. Каждая смена звена пишет
`WARNING` в лог (с указанием причины обрыва) и уходит админам (`ADMIN_IDS`) одним уведомлением
в Telegram, креды прокси (`user:pass@`) в логах и уведомлениях всегда маскируются.

Резерв защищает, только если он физически независим от основного канала: другой сервер,
другой туннель, другой провайдер. Второй порт того же VPS или второй privoxy поверх того же
`ssh -N -D` туннеля упадёт вместе с основным и ничего не даст.

Известное ограничение: `gspread` (Google Sheets) и загрузка резюме в Nextcloud ходят через
переменные окружения контейнера `HTTP(S)_PROXY`, а не через `PROXY_URL`, этот failover их не
покрывает.

Всё остальное (тексты, даты, вопросы анкеты, тарифы, тумблеры модулей) живёт не в `.env`, а в
таблице `bot_settings` и меняется из админки.

В `.env.example` лежат развёрнутые комментарии по деплою Nextcloud (docker-сеть, WebDAV-эндпоинт,
`occ`-настройки `trusted_domains` / `overwritehost`), при подключении облака читать оттуда.

## Что в .env и почему

Сверка «поле `config.Settings` → где реально живёт → почему», чтобы не гадать, что можно
перенести в реестр бота, а что остаётся в `.env` навсегда.

| Поле | Где живёт | Почему |
|------|-----------|--------|
| `BOT_TOKEN` | `.env` | секрет |
| `PROXY_URL`, `PROXY_URL_BACKUP` | `.env` | в URL пароль |
| `PROXY_RECHECK_SECONDS` | реестр `proxy_recheck_seconds`; `.env` = разовый seed | не секрет, но применяется только после перезапуска бота |
| `PROXY_CONNECT_TIMEOUT` | реестр `proxy_connect_timeout`; `.env` = разовый seed | то же |
| `ADMIN_IDS` | `.env` | bootstrap-суперадминов; дальнейшие роли выдаются из бота («👥 Роли и доступы») |
| `UNIVERSITIES` | код `config.py` (аварийный фолбэк) | список ВУЗов уже настраивается ключом реестра `university_options`, поле в `.env` не выносится |
| `DB_PATH` | `.env` | путь к файлу БД, инфраструктура |
| `LOG_LEVEL` | `.env` | уровень логов, инфраструктура |
| `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_FILE` | `.env` | доступ к таблице |
| `GOOGLE_SHEET_TAB` | DEPRECATED → разовый seed в `bot_settings.main_sheet_tab` | дальше вкладка правится из `/admin` → 📊 Данные → «📄 Вкладки таблицы» |
| `EVENT_CITIES`, `EVENT_CITY_DEFAULT` | `.env` → seed | читаются один раз при пустой таблице `cities`; дальше всё, экран «🏙 Города» в боте (добавление, подпись, вкладка, вкл/выкл, город по умолчанию, удаление) |
| `NEXTCLOUD_*` | `.env` | секреты и адреса самохостинга |
| `DASHBOARD_*` | `.env` | секреты и адрес самого стека дашборда/Mini App, читает `dashboard/config.py`, отдельная от бота конфигурация |

Правило простое: настройки живут внутри бота, в `.env` остаются секреты и bootstrap первого
запуска. `.env` трогает тот, кто разворачивает сервер. Всё остальное менеджер мероприятия
настраивает сам, кнопками, без разработчика и без рестарта.

## Google Sheets

Опционально: без `GOOGLE_SHEET_ID` бот работает только на локальной БД. При ошибке записи, до
3 повторов (5 / 15 / 30 сек), запись идёт в фоне и не блокирует анкету.

1. [Google Cloud Console](https://console.cloud.google.com/) → создать проект.
2. **APIs & Services → Library** → включить **Google Sheets API** и **Google Drive API**.
3. **Credentials → Create Credentials → Service Account** → вкладка **Keys** →
   **Add Key → Create new key → JSON**.
4. Переименовать скачанный файл в `google_credentials.json`, положить рядом с `main.py`.
   Файл секретный, в git его быть не должно.
5. Создать таблицу, взять ID из URL:
   `https://docs.google.com/spreadsheets/d/ЭТОТ_ID/edit` → в `.env`.
6. Расшарить таблицу на `client_email` из `google_credentials.json` с правами редактора.

**Колонки, включённые вопросы анкеты.** Список колонок собирается из `SHEET_COLUMNS`
(`handlers/reg_schema.py`, значения переехали в `reg_labels.py`/`reg_options.py`, схема
реэкспортирует их байт-в-байт): служебные (ID, username, дата, статус, ФИО, детали) плюс по
колонке на каждый включённый вопрос `reg_q_*` в порядке анкеты. Шапку пишет `ensure_sheet_header`
(`services/sheets.py`) при старте бота, при «🔄 Синхронизация таблицы» и при правке тумблеров
вопросов, колонки выключенных вопросов не выводятся. Столбец «Статус» получает выпадающий
список и цветовую заливку (Новая / Одобрена / Отклонена). Party-заявки пишутся в отдельную
вкладку (по умолчанию `Party`), краткая форма, в свою, города с заданной базой вкладки, в
свои. Исключение: `reg_q_age` (Возраст), вопрос без колонки в `SHEET_COLUMNS`.

Ловушка «пропали колонки»: в 9 случаях из 10 это скрытые колонки в интерфейсе Google Sheets
(правый клик по букве столбца → «Показать») или узкий диапазон `IMPORTRANGE` в копии таблицы,
а не бот. Проверять это раньше, чем лезть в код, «♻️ Пересобрать таблицу» вернёт колонки на
место по порядку вопросов.

Обслуживание, кнопками в админке: «🔄 Синхронизация таблицы» (дописать пропущенные строки),
«♻️ Пересобрать таблицу» (перезаписать лист целиком под новый порядок колонок), «🧹 Убрать
дубли из таблицы» (оставляет самую свежую строку по Telegram ID, старые дубли удаляет целиком,
вместе с ручными пометками на них), «📝 Незавершённые → таблица», «🔄 Таблица геймы». Обе
кнопки записи, «Синхронизация» и «Пересборка», маршрутизируют каждого пользователя на его
городскую вкладку через `city_row_tab` (батч-дозапись и чтение существующих id на именованной
вкладке, `services/sheets.py`), одна упавшая городская вкладка не отменяет остальные, отчёт
называет вкладки по-человечески.

**Вкладки геймификации.** Общие «Гейма» (матрица участники × задания) и «История сдач»
(`game_matrix_tab` / `game_history_tab`), а при включённом модуле городов, ещё пара на каждый
включённый город с непустой базой вкладки: «СПб Гейма» / «СПб История сдач» (приписки
`city_tab_suffix__game` / `city_tab_suffix__game_history` в «📄 Вкладки таблицы»). Город без
базы вкладки своей пары не получает, старые листы при переименовании не удаляются. План
вкладок строит `services/game_sheets.game_tab_plan()`, пересборка, кнопкой «🔄 Таблица геймы»
или автоматически (`services/game_sync.py`, debounce около 30 секунд после каждого задания,
решения или правки монет).

**Живая копия в другую таблицу, без кода.** В левой верхней ячейке пустого листа другой
таблицы: `=IMPORTRANGE("<url_или_id_таблицы_бота>"; "TG CANDIDATES!A1:Z10000")`. Первый раз
ячейка покажет `#REF!`, навести курсор → «Разрешить доступ». Копия обновляется раз в несколько
минут, только на чтение, диапазон строк/столбцов берётся с запасом (иначе «пропадут колонки»),
разделитель аргументов `;` или `,` зависит от локали таблицы. Выпадашки и цвета через
`IMPORTRANGE` не переносятся. Нужна редактируемая копия, Файл → Создать копию.

## Хранение резюме (Nextcloud)

Файлы резюме не хранятся на диске бота: `file_id` остаётся в БД (Telegram как хранилище), а
сам файл заливается в Nextcloud по WebDAV под уникальным именем
`ФИО_username_telegramid_YYYYMMDD-HHMMSS.pdf` (`_resume_file_stem` в `handlers/registration.py`,
без username, без него, id не дублируется): WebDAV PUT перезаписывает молча, и два тёзки или
повторная подача раньше затирали файл друг друга. В БД (`users.resume_url`) и в таблицу
попадает ссылка вида
`{NEXTCLOUD_PUBLIC_URL}/s/{FOLDER_SHARE_TOKEN}/download?path=%2F&files=<имя файла>`.

Загрузка fail-soft: недоступное облако не ломает регистрацию, участник проходит анкету, файл
остаётся доступен через `file_id`. Бэкфилл ранее загруженных резюме, `scripts/backfill_resumes.py`,
перенос уже сохранённых ссылок на новый домен Nextcloud, `scripts/backfill_nextcloud_urls.py`
(`--old-base`/`--new-base`, сначала `--dry-run`).

## Структура проекта

```
AIESEC_event_bot/
├── main.py                    # Точка входа: роутеры, планировщик, фоновые циклы, логи
├── config.py                  # pydantic-settings поверх .env
├── settings_schema.py         # SETTINGS_SCHEMA, реестр настроек bot_settings
├── settings_ops.py            # Правила настроек, общие для бота и Mini App, без aiogram
├── settings_validation.py     # Валидация значения настройки до записи в bot_settings
├── settings_synonyms.py       # Синонимы для поиска по настройкам в Mini App
├── cities.py                  # Реестр городов мероприятия (event_city)
├── reg_engine.py               # Ядро анкеты без aiogram, общее для бота и Mini App
├── reg_labels.py               # Подписи анкеты, корневой модуль без aiogram
├── reg_options.py              # Списки вариантов ответа анкеты
├── game_labels.py              # RU-подписи геймификации, корневой модуль без aiogram
├── moderation_card.py          # Карточка заявки для модератора: что показывать, как обрезать
├── web_theme.py                # Пресеты оформления Mini App и дашборда
│
├── handlers/                   # Модули ~800 строк, общий Router на группу (admin_*, reg_*)
│   ├── admin.py                # Агрегатор админки: импортирует admin_* «швы» в один router
│   ├── admin_core.py           # /admin, меню по правам, справка по настройкам
│   ├── admin_sections.py       # SECTIONS: 8 разделов /admin по пути делегата
│   ├── admin_settings.py       # Экраны настроек из SETTINGS_SCHEMA, вкладки таблицы, выгрузки
│   ├── admin_settings_lists.py # Списочные настройки: правка по одному пункту
│   ├── admin_moderation.py     # Заявки и чеки (appr_*/rcpt_*)
│   ├── admin_modcard.py        # Экран «Поля карточки заявки»
│   ├── admin_broadcasts.py     # Рассылки, фильтры, отложенные
│   ├── admin_reg_config.py     # Тумблеры и тексты вопросов анкеты
│   ├── admin_consent.py        # Версия согласия, напоминание о целях обработки
│   ├── admin_cities.py         # Города, сброс/импорт сезона, дедупликация таблицы
│   ├── admin_roles.py          # «Роли и доступы»; admin_caps.py, CapabilityMiddleware
│   ├── admin_questions.py      # Журнал вопросов делегатов, ответ прямо из экрана
│   ├── admin_polls.py          # Опросы: список, карточка с итогами
│   ├── admin_poll_wizard.py    # Мастер создания опроса
│   ├── admin_dashboard.py      # Экран «Дашборд», тумблеры блоков
│   ├── admin_sheet_logs.py     # Экран «Журналы в таблицу»
│   ├── admin_miniapp.py        # Экран «Оформление» Mini App: тумблеры и разделы
│   ├── admin_miniapp_theme.py  # Пресеты оформления и ручки кастома
│   ├── admin_gamification.py   # Задания, проверка сдач, монеты вручную, журнал, таблица геймы
│   ├── admin_game_tasks.py     # Карточка правки задания, пресеты дедлайна, «Как видит делегат»
│   ├── game_task_wizard.py     # Чистые рендеры визарда задания; game_review_render.py, карточки
│   ├── game_submit_counter.py  # Сообщение-счётчик сдачи
│   ├── registration.py         # /start, треки, согласия, предотбор, сводка
│   ├── reg_schema.py           # REG_FLOW, SHEET_COLUMNS, реэкспорт reg_labels/reg_options
│   ├── reg_steps.py            # process_*-хендлеры шагов
│   ├── reg_flow.py             # Форки, отмена, подтверждение и возврат к шагу
│   ├── reg_consent.py          # Пересогласие уже зарегистрированного делегата
│   ├── reg_handoff.py          # Возврат владения черновиком боту после Mini App («эстафета»)
│   ├── reg_resume.py           # Экран «Продолжить с шага N / Заново»
│   ├── payment.py              # Выбор тарифа, реквизиты, загрузка чека
│   ├── user_actions.py         # Меню делегата, вопросы, задания, монеты
│   ├── polls.py                # Делегатская сторона опросов: poll_answer, итоги
│   └── states.py               # FSM-состояния
│
├── keyboards/builders.py       # Клавиатуры, динамическое меню
├── database/db.py              # SQLite через aiosqlite, миграции, запросы
│
├── services/
│   ├── sheets.py                # Google Sheets: заголовки, запись, пересборка, дедуп
│   ├── sheet_logs.py            # Журнал операций записи в таблицу
│   ├── game_sheets.py           # План вкладок геймы (общие + по городам)
│   ├── game_sync.py             # Debounce-автосинк вкладок геймы
│   ├── game_digest.py           # Дайджест сдач менеджерам вместо сообщения на каждую
│   ├── proxy_session.py         # Failover прокси / Bot API хоста
│   ├── scheduler.py             # APScheduler: отложенные рассылки, напоминания об оплате
│   ├── reminders.py             # Периодическая сводка админам по заявкам
│   ├── allowlist.py             # Кэш списка отобранных (предотбор)
│   ├── nextcloud.py             # Загрузка резюме по WebDAV
│   ├── background.py            # fire-and-forget со strong-ref (защита от GC)
│   ├── consent.py               # Проверка принятых согласий
│   ├── questions.py             # Статус вопросов делегатов (без ответа/в работе/отвечен)
│   ├── quiet_hours.py           # Тихие часы для рассылок и напоминаний
│   ├── reg_finalize.py          # Финализация анкеты: запись в БД, резюме, вкладка таблицы
│   ├── reg_handoff.py           # Эстафета черновика анкеты между ботом и Mini App
│   ├── applications.py          # Решение по заявке (approve/reject), общее для бота и Mini App
│   ├── application_effects.py   # Побочные эффекты решения: уведомление, строка в таблице
│   ├── miniapp_outbox.py        # Очередь эффектов Mini App, разбирает бот
│   ├── polls.py                 # Отправка опроса, сбор ответов
│   ├── heartbeat.py             # Heartbeat-файл для Docker HEALTHCHECK
│   └── timeutil.py              # Общие хелперы работы с датой/временем
│
├── dashboard/                  # FastAPI: read-only статистика, см. README, «Дашборд статистики»
├── miniapp/                    # FastAPI: Mini App делегата и менеджера, см. README, «Mini App»
├── docs/                       # ADMIN_GUIDE, ADMIN_CHEATSHEET, BOT_GUIDE, DEPLOY-DOMAIN и другие
├── scripts/                    # Разовые утилиты (бэкфилл резюме, диагностика колонок)
├── tests/                      # pytest, около 3700 тестов; conftest.py фиксирует порядок импорта хендлеров
├── resources/                  # Опциональные fallback-фото
└── data/                       # Рантайм: forum.db, jobs.sqlite, broadcast_target.txt
```

## База данных

| Таблица | Назначение |
|---------|-----------|
| `users` | Все участники: анкета, `status` (pending/approved/rejected), `participant_type` (трек), поля оплаты (`payment_status`, `payment_option`, `receipt_file_id`, `payment_due`, `paid_at`), `resume_file_id` / `resume_text` / `resume_url`, `referrer_id`, `source` |
| `bot_settings` | key-value: все настройки из админки |
| `coins` | Append-only леджер: `delta`, `reason`, `changed_by`, `timestamp`. Баланс = `SUM(delta)`, `UPDATE` не используется |
| `reg_started` | Персистентный трекинг начатых анкет: `started_at`, `last_step`, `nudged_at`, `participant_type` |
| `scheduled_broadcasts` | Payload отложенной рассылки (текст, фото, фильтр, статус, `sending_since`). Триггером владеет APScheduler |
| `scheduled_broadcast_deliveries` | Чекпоинт по получателям отложенной рассылки: `(broadcast_id, chat_id)` → `ok` / `failed`. Рассылка, оборванная рестартом, на буте доставляется дальше с того же места (старые `sending` реклеймятся), уже получившие не дублируются, после `sent` строки удаляются |
| `user_consents` | Аудит принятых согласий, `UNIQUE(user_id, consent_key, consent_version)`, новая версия согласия принимается заново, старый акцепт остаётся в истории |
| `staff` | Менеджеры, добавленные из бота: `telegram_id`, `role`, `added_by`, `added_at`, композитный PK `(telegram_id, role)`. Состав прав роли и тумблер «роль выключена» живут в `bot_settings` (`role_caps_<role>`, `role_<role>_enabled`), не здесь. Не путать с `ADMIN_IDS`, суперадминами из `.env`, см. выше |
| `delegate_questions` | Вопросы делегатов организаторам плюс атомарный захват ответа: `id`, `user_id`, `question_text`, `asked_at`, `answered_by`, `answered_by_name`, `answered_at`, `answer_text`. Второй менеджер, попытавшийся ответить на уже отвеченный вопрос, видит имя первого |

Схема расширяется только аддитивно: `CREATE TABLE IF NOT EXISTS` плюс `_ensure_column()`
(`ALTER TABLE ... ADD COLUMN` с проверкой существования). Никаких деструктивных миграций на
живой базе.

Начиная с Phase 21 в `database/db.py` появились ещё несколько таблиц, которых нет в упрощённой
ER-схеме ниже (она сознательно показывает только ключевые сущности, не полный список): `cities`
(реестр городов), `reg_drafts` и `reg_answer_history` (общий черновик анкеты бота и Mini App,
журнал правок), `game_submit_digest_queue` и `delayed_notifications` (очереди дайджестов и
отложенных уведомлений), `miniapp_outbox` и `application_decisions` (эффекты и решения Mini
App), `reg_events` (события воронки для дашборда), `polls`/`poll_messages`/`poll_answers`
(опросы). Их поля смотреть напрямую в `database/db.py`.

ER-схема (связи логические, SQLite-таблицы без `FOREIGN KEY`, целостность держит код,
показаны ключевые поля, у `users` около 60 колонок анкеты опущены):

```mermaid
erDiagram
    users {
        int telegram_id PK
        text username
        text full_name
        text status "pending | approved | rejected"
        text participant_type "full | short | party_overnight | party_noovernight"
        text event_city
        text payment_status "not_paid | receipt_sent | paid | overdue"
        text payment_option
        text receipt_file_id
        text payment_due
        text paid_at
        text resume_file_id
        text resume_url "ссылка Nextcloud"
        int referrer_id "users.telegram_id"
        text source "метка кампании"
    }
    reg_started {
        int telegram_id PK
        text started_at
        text last_step
        text nudged_at
        text participant_type
    }
    user_consents {
        int id PK
        int user_id
        text consent_key
        text accepted_at
    }
    coins {
        int id PK
        int user_id
        int delta "append-only, баланс = SUM"
        text reason
        int changed_by
        text timestamp
    }
    delegate_questions {
        int id PK
        int user_id
        text question_text
        text asked_at
        int answered_by
        text answered_at
        text answer_text
        text delivered_at
    }
    staff {
        int telegram_id PK
        text role PK
        text city "NULL = все города"
        int added_by
        text added_at
    }
    scheduled_broadcasts {
        int id PK
        text text
        text photo_file_id
        text filter_spec "JSON AND-фильтров"
        text scheduled_at
        text status "pending | sending | sent | cancelled"
        text sending_since
        int created_by
    }
    scheduled_broadcast_deliveries {
        int broadcast_id PK
        int chat_id PK
        text status "ok | failed"
        text sent_at
    }
    game_tasks {
        int id PK
        text text
        text category
        int coins
        text proof_type
        text deadline_at
        text event_city
    }
    game_submissions {
        int id PK
        int task_id
        int user_id
        text status "pending | approved | rejected"
        int reviewed_by
        int coins_awarded
    }
    game_submission_parts {
        int id PK
        int submission_id
        int ord
        text kind
        text content
    }
    bot_settings {
        text key PK
        text value "все настройки админки"
    }

    users ||--o| reg_started : "начал анкету"
    users ||--o{ user_consents : "принял"
    users ||--o{ coins : "леджер"
    users ||--o{ delegate_questions : "задал"
    users |o--o{ users : "пригласил (referrer_id)"
    staff |o--o{ delegate_questions : "ответил"
    staff |o--o{ scheduled_broadcasts : "создал"
    scheduled_broadcasts ||--o{ scheduled_broadcast_deliveries : "чекпоинт по получателям"
    staff |o--o{ game_submissions : "проверил"
    users ||--o{ game_submissions : "сдал"
    game_tasks ||--o{ game_submissions : "по заданию"
    game_submissions ||--o{ game_submission_parts : "части (альбом)"
```

Что схема держит на уровне данных. В `coins` идут только `INSERT`, поэтому баланс всегда
воспроизводим из истории. Составной PK в `staff` допускает несколько ролей у одного человека
без отдельной таблицы. `delegate_questions.answered_by` захватывается атомарным
`UPDATE ... WHERE answered_by IS NULL`, так что двое менеджеров не ответят на один вопрос.

## Путь оплаты

Google Sheets убран с критического пути регистрации: раньше сбой квоты или сети ронял анкету
делегата. Источник истины теперь SQLite, таблица производная. Пишется в фоне, с повторами, и
её недоступность делегат не замечает.

```mermaid
sequenceDiagram
    autonumber
    actor U as Делегат
    participant B as Бот (aiogram)
    participant DB as SQLite
    participant S as APScheduler (jobs.sqlite)
    participant G as Google Sheets
    actor M as Менеджер

    U->>B: «Всё верно» (сводка анкеты)
    B->>DB: INSERT users (status=pending), DELETE reg_started
    par фон, делегат не ждёт
        B-)G: append строки заявки
        Note over B,G: до 3 попыток (5/15/30 с),<br/>после 3-й, алерт менеджерам,<br/>SQLite не затронут, догоняется<br/>кнопкой «Синхронизация»
    and
        B->>M: карточка заявки (кто имеет moderate_reg)
    end

    M->>B: одобрить
    B->>DB: UPDATE status=approved (атомарно, WHERE status=pending)
    B-)G: статус «Одобрена» в строке (фон)
    B->>U: поздравление + тарифы

    U->>B: тариф
    B->>DB: payment_option, payment_due
    B->>S: date-джобы T-3 / T-1 до дедлайна
    B->>U: реквизиты, «пришли чек»

    U->>B: чек (PDF / фото)
    B->>DB: payment_status=receipt_sent, receipt_file_id
    B->>M: «новый чек» (кто имеет moderate_receipts)
    B->>U: «чек получен, менеджер проверит»

    alt чек верный
        M->>B: подтвердить
        B->>DB: UPDATE payment_status=paid<br/>WHERE status=receipt_sent
        Note over B,DB: rowcount=0 → «уже обработан»:<br/>двое менеджеров не подтвердят<br/>один чек дважды
        B->>S: отменить джобы T-3 / T-1
        B->>U: оплата подтверждена
    else чек отклонён
        M->>B: отклонить (+причина)
        B->>DB: payment_status=not_paid
        B->>U: причина, «загрузи чек ещё раз»
    end

    S-->>B: (если оплаты нет) T-3, T-1: напоминание
    S-->>U: напоминание об оплате
    Note over S,U: джоба сама проверяет статус,<br/>после paid молчит, даже если<br/>отмена не успела
```

Файл чека не скачивается: в БД лежит Telegram `file_id`, менеджеру бот пересылает его по
запросу. Так же устроены резюме, см. [Хранение резюме](#хранение-резюме-nextcloud).

## FSM регистрации

| Группа | Состояния |
|--------|-----------|
| `Registration` | `full_name` → динамический поток из включённых вопросов → `payment_option` → `receipt_upload` (шаги оплаты, это состояния той же группы, не отдельная FSM `Payment`, `handlers/states.py`); перерегистрация админа, инлайн-кнопка `admin_rereg`, не состояние |
| `Question` | `waiting_for_question` |
| `Broadcast` | `target_selection` → фильтры → `message` → (отправка / планирование) |
| `EditSetting` | `waiting_for_value`, `waiting_for_photo`, `waiting_for_file` |

Кроме этих четырёх, в `handlers/states.py` есть ещё около десятка небольших групп под
конкретные визарды админки: `StaffAdd`, `GameTaskCreate`, `GameTaskEdit`, `GameReview`,
`GameSubmit`, `CoinsManual`, `CityForm`, `SeasonReset`, `SeasonImport`, `PollCreate`,
`MiniAppTheme`. Все устроены одинаково, одно состояние на шаг мастера, подтверждение,
последний шаг читает `state.get_data()` вместо отдельного состояния.

Поток анкеты собирается движком `REG_FLOW` (`reg_engine.py`) на лету: набор шагов зависит от
включённых вопросов, трека участника и предыдущих ответов. Отмена доступна везде, кнопка
«Отмена», `/cancel` или inline «❌ Отмена».

FSM-хранилище, `MemoryStorage`: при перезапуске незавершённые анкеты сбрасываются (готовые
регистрации не теряются, они уже в БД). Именно поэтому отложенные рассылки и напоминания живут
в БД, а не в FSM.
