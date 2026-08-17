# AIESEC Event Bot

Telegram-бот, который целиком закрывает регистрацию делегатов на мероприятие — от заявки
и модерации до оплаты, чеков и выгрузки в Google-таблицу, — чтобы менеджеру события не
приходилось вести участников вручную в переписке и в таблицах.

## Для кого сделано

Заказчик — **AIESEC в России**, направление DXP. До бота регистрация на форум шла через
Google-форму: заявки разбирали руками, статусы и оплату сводили в таблице, напоминания
писали лично каждому.

Бот покрывает и форумы (YouLead), и конференции (RusCo/Summit) — код один, различия между
событиями заданы настройками. Работает на нескольких городах сразу (Москва, СПб, Тюмень) с
отдельной deep-link-ссылкой и своей вкладкой таблицы у каждого.

Ключевое требование заказчика: **менеджер события настраивает всё сам, кнопками в
Telegram** — без разработчика, без правки кода, без перезапуска бота. Поэтому тексты,
даты, набор вопросов анкеты, тарифы и тумблеры модулей живут не в конфиге, а в БД и
редактируются из админки. Масштаб — сотни заявок на событие, порядка 1000–1500
пользователей за сезон.

## Документация

**→ [ADMIN_GUIDE.md](ADMIN_GUIDE.md) — полный гайд менеджера события.** Как провести
мероприятие через бота от настройки до закрытия: 22 раздела, включая роли и доступы,
модерацию, оплату, рассылки и раздел «Грабли и ограничения».

| Кому | Файл |
|------|------|
| Менеджеру события — полный гайд | **[ADMIN_GUIDE.md](ADMIN_GUIDE.md)** |
| Менеджеру события — шпаргалка на одну страницу | [ADMIN_CHEATSHEET.md](ADMIN_CHEATSHEET.md) |
| Тестировщику — чек-лист «что потыкать» | [BOT_GUIDE.md](BOT_GUIDE.md) |
| Про трек «вечеринка» | [docs/party-flow-guide.md](docs/party-flow-guide.md) |
| Разработчику | этот файл |

---

## Оглавление

- [Путь заявки](#путь-заявки)
- [Что умеет бот](#что-умеет-бот)
- [Стек](#стек)
- [Быстрый старт](#быстрый-старт)
- [Переменные окружения](#переменные-окружения)
- [Что в .env и почему](#что-в-env-и-почему)
- [Google Sheets](#google-sheets)
- [Хранение резюме (Nextcloud)](#хранение-резюме-nextcloud)
- [Архитектура](#архитектура)
  - [Структура проекта](#структура-проекта)
  - [Настройки: SETTINGS_SCHEMA](#настройки-settings_schema)
  - [База данных](#база-данных)
  - [Планировщик и фоновые задачи](#планировщик-и-фоновые-задачи)
  - [Оплата и синхронизация с таблицей](#оплата-и-синхронизация-с-таблицей)
  - [Порядок обработки сообщений](#порядок-обработки-сообщений)
  - [FSM](#fsm)
- [Тесты](#тесты)

---

## Путь заявки

Дорожки: делегат, бот, менеджер. Две развилки зависят от настроек события (предотбор и
оплата включаются тумблерами), решают либо бот сам, либо менеджер на карточке заявки и на
карточке чека. Всё, что в дорожке «Бот», идёт без участия людей.

![Путь заявки: BPMN с дорожками Делегат / Бот / Менеджер](docs/diagrams/application-flow-bpmn.svg)

Рядом с процессом крутятся четыре фоновых цикла: догон брошенных анкет, напоминания об
оплате, сводка админам по заявкам в ожидании и обновление кэша предотбора. Подробности —
в разделе [Планировщик](#планировщик-и-фоновые-задачи).

---

## Что умеет бот

| Модуль | Кратко |
|--------|--------|
| Регистрация | Динамическая анкета: ~43 вопроса, каждый включается тумблером. Типы: кнопки, текст, дата, мультивыбор, файл. Зависимые вопросы пропускаются автоматически, в конце — сводка ответов |
| Города мероприятия | Делегат перед анкетой выбирает город (Москва/СПб/Тюмень) — одна форма на все города, заявка едет на свой лист; у каждого города своя deep-link ссылка. Список городов — из `.env`, вкл/выкл модуля и города и подпись — из админки. Оплата и напоминания общие для всех городов |
| Погородная админка | Переключатель «🏙 Город» в шапке `/admin` сужает по выбранному городу обе очереди модерации (заявки и чеки), массовое одобрение и экспорт CSV; `event_city` доступен как фильтр рассылки. Намеренно НЕ сужаются: статистика (блок «По городам» для сравнения), «Незавершённые → таблица» (пишет все города) и напоминание админам о заявках |
| Треки участников | Полный делегат + вечеринка с ночёвкой / без. У трека свои вопросы, модерация, тарифы и вкладка в таблице |
| Модерация | Очередь `status=pending`, пагинированная карточка (одна заявка за раз): одобрить / отклонить с причиной / пропустить / одобрить всех |
| Оплата | Тарифы (в т.ч. по трекам), реквизиты общие и по ЛК, дедлайн, штрафы, загрузка чека (PDF/фото), проверка чеков менеджером, автонапоминания T-3 / T-1 |
| Согласия | Список согласий с PDF, обязательное принятие кнопкой, аудит принятия в БД |
| Рассылки | Все / файл / не подписаны на канал / не завершили анкету / AND-конструктор фильтров. Превью количества, отправка сейчас или по расписанию (переживает рестарт), flood-safe |
| Коины | Append-only леджер (баланс = SUM(delta)), `/coins`, рейтинг, кнопка «🪙 Мои монеты» |
| Аналитика | Статистика, регистрации по месяцам, источники, CSV-экспорт, выгрузка незавершённых анкет с шагом отвала |
| Источники | Метки кампаний `/create_link` + личные реферальные ссылки участников |
| Google Sheets | Дублирование регистраций, динамические колонки по включённым вопросам, статус заявки с выпадашкой и цветом, синхронизация / пересборка / дедупликация |
| Резюме | PDF/DOCX или текст; файл заливается в Nextcloud, в таблицу пишется ссылка |
| Предотбор | Allowlist `@username` из Google-таблицы, fail-open с алертом админу |
| Автоматика | Догон брошенных анкет, напоминания об оплате, сводка по заявкам админам |

---

## Стек

| Компонент | Технология |
|-----------|------------|
| Фреймворк | [aiogram 3](https://docs.aiogram.dev/), long polling |
| БД | SQLite через [aiosqlite](https://github.com/omnilib/aiosqlite) |
| Планировщик | [APScheduler 3.x](https://apscheduler.readthedocs.io/en/3.x/) `AsyncIOScheduler` + `SQLAlchemyJobStore` (`data/jobs.sqlite`) |
| Конфигурация | pydantic-settings + `.env` |
| Google Sheets | [gspread](https://docs.gspread.org/) + сервисный аккаунт |
| Файлы | Telegram `file_id` + Nextcloud (WebDAV) для резюме |
| Тесты | pytest, ~1370 тестов без обращений к внешним сервисам |
| Деплой | Docker + docker-compose |

---

## Быстрый старт

```bash
git clone <url>
cd AIESEC_event_bot

python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS

pip install -r requirements.txt
cp .env.example .env            # заполнить, см. ниже
python main.py
```

При первом запуске бот сам создаёт `data/forum.db` и `data/jobs.sqlite`. Миграции
аддитивные и идемпотентные (`CREATE TABLE IF NOT EXISTS` + `_ensure_column`) — обновление
на живой базе с ~600 пользователями безопасно.

**Свой Telegram ID:** `/start` боту [@userinfobot](https://t.me/userinfobot).

### Docker

```bash
docker-compose up -d --build
docker-compose logs -f bot
```

Compose подхватывает `.env`, монтирует `data/` (БД и job store), `logs/`, `resources/`
(фото) и `google_credentials.json`.

### Логи

`logs/bot.log` — ротация 10 МБ × 5 файлов, уровень задаётся `LOG_LEVEL`. В stdout
(`docker logs`) уходит только `WARNING+`, чтобы контейнерные логи оставались читаемыми.

---

## Переменные окружения

```env
# --- Обязательное ---
BOT_TOKEN=123456789:ABCDefGHIjkLLmnoPQRstuVWxyz
ADMIN_IDS=[12345678, 87654321]

# --- Опционально ---
PROXY_URL=socks5://user:pass@host:port   # если Telegram API заблокирован
PROXY_URL_BACKUP=socks5://user:pass@host2:port  # резервный канал (см. описание ниже)
PROXY_RECHECK_SECONDS=600                 # только первичный seed, дальше правится в боте: «⚙️ Настройки → 🔧 Система»
PROXY_CONNECT_TIMEOUT=5                   # то же самое, см. «Что в .env и почему»
DB_PATH=data/forum.db
LOG_LEVEL=INFO                            # DEBUG | INFO | WARNING | ERROR

# --- Google Sheets (пусто = бот работает только на своей БД) ---
GOOGLE_SHEET_ID=
GOOGLE_CREDENTIALS_FILE=google_credentials.json
GOOGLE_SHEET_TAB=                         # первичный seed, дальше «⚙️ Настройки → 📄 Вкладки таблицы»; пусто = первая вкладка

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
NEXTCLOUD_VERIFY_TLS=false                # true, если сертификат доверенный
```

`ADMIN_IDS` — JSON-список. `GOOGLE_SHEET_TAB` стоит задавать явно: иначе бот пишет в
первую вкладку по позиции, и перестановка вкладок перенаправит запись.

`ADMIN_IDS` — это **bootstrap-суперадмины**: полный доступ всегда, не снимаются из бота
(снять можно только правкой `.env` и рестартом). Остальных менеджеров с ограниченным
доступом заводит сам бот — таблица `staff`, экран «👥 Роли и доступы» (`/admin` → ⚙️
Настройки → 👥 Роли и доступы), без `.env` и передеплоя. Подробности и таблица прав —
ADMIN_GUIDE.md, §22.

`EVENT_CITIES` — список городов мероприятия, формат `код|подпись|база_вкладки`, разделитель
между городами — `;` (та же схема, что у списков согласий/тарифов — см. «Enter = отправка» в
ADMIN_GUIDE.md). `код` — латиница/цифры/`_`, до 16 символов, используется в deep-link
(`?start=city_код`) и в ключах настроек (`city_enabled__код`, `city_label__код`). `база_вкладки`
— опциональна: пустая база (как у Москвы) означает, что город работает на легаси-вкладках
(`GOOGLE_SHEET_TAB` и т.д.) без изменения имён; непустая база (`СПб`, `Тюмень`) даёт городу
свои вкладки — «СПб», «СПб Акция», «СПб Party», «СПб Незавершённые». `EVENT_CITY_DEFAULT` —
код города, который подставляется на чтении, если у записи `event_city` не задан (без
бэкфилла старых строк). Список городов правится только через `.env` и рестарт — из админки
меняются лишь подпись, вкл/выкл модуля и вкл/выкл отдельного города (см. ADMIN_GUIDE.md,
раздел «Города мероприятия»).

Выбранный админом город хранится в `bot_settings` под служебным ключом
`admin_city__{telegram_id}` (один ключ на админа, переживает рестарт — FSM не переживает).
Читается только через `cities.admin_selected_city()`, который возвращает `None` при
выключенном модуле — это единственная точка, где «модуль выключен» превращается в «ничего не
фильтруется». Сам SQL-фильтр строится из дескриптора `cities.city_scope()` →
`database.db._city_clause()`; `database/db.py` намеренно не импортирует `cities` (цикл), скоуп
передаётся значением.

**Резервный прокси (failover).** `PROXY_URL` можно указывать как HTTP-прокси (напр.
`http://host.docker.internal:8118`, если используется privoxy-обвязка), так и напрямую как
`socks5://` эндпоинт — `aiohttp-socks` уже в зависимостях, промежуточный privoxy-хоп не
обязателен. Установка соединения ограничена `PROXY_CONNECT_TIMEOUT` (по умолчанию 5 сек) —
мёртвый канал теперь стоит боту секунды, а не полного тайм-аута запроса (20–90 сек). При
сетевой ошибке к Telegram (`TelegramNetworkError`) бот автоматически переключается на
`PROXY_URL_BACKUP` — без рестарта, без пересоздания сессии — и остаётся на нём (sticky).
Возврат на основной канал больше не проверяется на живых запросах пользователей: в фоне
раз в `PROXY_RECHECK_SECONDS` крутится отдельная проверка (фоновый пробник) — она стучится
на основной канал сама, и только когда он реально отвечает, бот переключается обратно. Пока
пробник не подтвердил, что основной канал жив, ни один запрос пользователя на него не
попадёт. Если оба звена мертвы одновременно, наружу летит тот же исходный
`TelegramNetworkError`, что и раньше — `dp.start_polling` ретраит с тем же бэкоффом, ничего
не меняется в этом сценарии. Каждая смена звена пишет `WARNING` в лог (с указанием причины
обрыва) и уходит админам (`ADMIN_IDS`) одним уведомлением в Telegram; креды прокси
(`user:pass@`) в логах и уведомлениях всегда маскируются.

⚠️ **Резерв защищает, только если он ФИЗИЧЕСКИ независим от основного канала** — другой
сервер, другой туннель, другой провайдер. Второй порт того же VPS или второй privoxy поверх
того же `ssh -N -D` туннеля упадёт вместе с основным и ничего не даст.

Известное ограничение: `gspread` (Google Sheets) и загрузка резюме в Nextcloud ходят через
переменные окружения контейнера `HTTP(S)_PROXY`, а не через `PROXY_URL` — этот failover их
не покрывает.

Всё остальное (тексты, даты, вопросы анкеты, тарифы, тумблеры модулей) живёт **не в
`.env`**, а в таблице `bot_settings` и меняется из админки.

В `.env.example` лежат развёрнутые комментарии по деплою Nextcloud (docker-сеть, WebDAV-эндпоинт,
`occ`-настройки `trusted_domains` / `overwritehost`) — при подключении облака читать оттуда.

## Что в .env и почему

Полная сверка «поле `config.Settings` → где реально живёт → почему» — чтобы не гадать, что
можно смело перенести в реестр бота, а что обязано остаться в `.env` навсегда.

| Поле | Где живёт | Почему |
|------|-----------|--------|
| `BOT_TOKEN` | `.env` | секрет |
| `PROXY_URL`, `PROXY_URL_BACKUP` | `.env` | в URL пароль |
| `PROXY_RECHECK_SECONDS` | реестр `proxy_recheck_seconds`; `.env` = разовый seed | не секрет, но применяется только после перезапуска бота |
| `PROXY_CONNECT_TIMEOUT` | реестр `proxy_connect_timeout`; `.env` = разовый seed | то же |
| `ADMIN_IDS` | `.env` | bootstrap-суперадминов; дальнейшие роли выдаются из бота («👥 Роли и доступы») |
| `UNIVERSITIES` | код `config.py` (аварийный фолбэк) | список ВУЗов уже настраивается ключом реестра `university_options` — поле в `.env` не выносится |
| `DB_PATH` | `.env` | путь к файлу БД, инфраструктура |
| `LOG_LEVEL` | `.env` | уровень логов, инфраструктура |
| `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_FILE` | `.env` | доступ к таблице |
| `GOOGLE_SHEET_TAB` | DEPRECATED → разовый seed в `bot_settings.main_sheet_tab` | дальше вкладка правится из «⚙️ Настройки → 📄 Вкладки таблицы» |
| `EVENT_CITIES`, `EVENT_CITY_DEFAULT` | `.env` | пока единственное место: список городов читается на старте. Из бота меняются подпись города и тумблеры вкл/выкл |
| `NEXTCLOUD_*` | `.env` | секреты и адреса самохостинга |

Правило простое: **настройки живут внутри бота**, в `.env` остаются секреты и bootstrap
первого запуска. `.env` трогает тот, кто разворачивает сервер. Всё остальное менеджер
мероприятия настраивает сам, кнопками, без разработчика и без рестарта.

---

## Google Sheets

Опционально: без `GOOGLE_SHEET_ID` бот работает только на локальной БД. При ошибке
записи — до 3 повторов (5 / 15 / 30 сек), запись идёт в фоне и не блокирует анкету.

1. [Google Cloud Console](https://console.cloud.google.com/) → создать проект.
2. **APIs & Services → Library** → включить **Google Sheets API** и **Google Drive API**.
3. **Credentials → Create Credentials → Service Account** → вкладка **Keys** →
   **Add Key → Create new key → JSON**.
4. Переименовать скачанный файл в `google_credentials.json`, положить рядом с `main.py`.
   Файл секретный, в git его быть не должно.
5. Создать таблицу, взять ID из URL:
   `https://docs.google.com/spreadsheets/d/ЭТОТ_ID/edit` → в `.env`.
6. Расшарить таблицу на `client_email` из `google_credentials.json` с правами
   **Редактор**.

Заголовки бот проставляет сам при старте и перестраивает под текущий набор включённых
вопросов; колонки выключенных вопросов не выводятся. Столбец «Статус» получает выпадающий
список и цветовую заливку (Новая / Одобрена / Отклонена). Party-заявки пишутся в отдельную
вкладку (по умолчанию `Party`), обычные — только в основную.

Обслуживание — кнопками в админке: «🔄 Синхронизация таблицы» (дописать пропущенные
строки), «♻️ Пересобрать таблицу» (перезаписать лист целиком под новый порядок колонок),
«🧹 Убрать дубли», «📝 Незавершённые → таблица».

---

## Хранение резюме (Nextcloud)

Файлы резюме не хранятся на диске бота: `file_id` остаётся в БД (Telegram как хранилище),
а сам файл заливается в Nextcloud по WebDAV и переименовывается по схеме
`ФИО + @username`. В БД (`users.resume_url`) и в таблицу попадает ссылка вида
`{NEXTCLOUD_PUBLIC_URL}/s/{FOLDER_SHARE_TOKEN}/download?path=%2F&files=<имя файла>`.

Загрузка fail-soft: недоступное облако не ломает регистрацию — участник проходит анкету,
файл остаётся доступен через `file_id`. Бэкфилл ранее загруженных резюме —
`scripts/backfill_resumes.py`.

---

## Архитектура

### Структура проекта

```
AIESEC_event_bot/
├── main.py                  # Точка входа: роутеры, планировщик, фоновые циклы, логи
├── config.py                # pydantic-settings поверх .env
├── settings_schema.py       # SETTINGS_SCHEMA — реестр настроек bot_settings
│
├── handlers/
│   ├── admin.py             # Админ-панель, настройки, модерация заявок, чеки, рассылки, фильтры
│   ├── registration.py      # Движок анкеты (REG_FLOW), /start, треки, согласия, предотбор
│   ├── payment.py           # Выбор тарифа, реквизиты, загрузка чека
│   ├── user_actions.py      # Кнопки меню, вопросы организаторам, коины, рейтинг
│   └── states.py            # FSM-состояния
│
├── keyboards/builders.py    # Клавиатуры, динамическое меню
├── database/db.py           # SQLite через aiosqlite, миграции, запросы
│
├── services/
│   ├── sheets.py            # Google Sheets: заголовки, запись, пересборка, дедуп
│   ├── scheduler.py         # APScheduler: отложенные рассылки, напоминания об оплате
│   ├── reminders.py         # Периодическая сводка админам по заявкам
│   ├── allowlist.py         # Кэш списка отобранных (предотбор)
│   ├── nextcloud.py         # Загрузка резюме по WebDAV
│   └── background.py        # fire-and-forget с strong-ref (защита от GC)
│
├── scripts/                 # Разовые утилиты (бэкфилл резюме, диагностика колонок)
├── tests/                   # pytest, ~1370 тестов
├── resources/               # Опциональные fallback-фото
└── data/                    # Рантайм: forum.db, jobs.sqlite, broadcast_target.txt
```

### Настройки: SETTINGS_SCHEMA

`settings_schema.py` — единственный источник правды по ключам `bot_settings`:
тип / группа / подпись / подсказка / значение по умолчанию.

```python
SETTINGS_SCHEMA = {
    "payment_deadline": {
        "type": "date", "group": "pay", "label": "📅 Дедлайн оплаты",
        "prompt": "Крайний срок оплаты в формате ДД.ММ.ГГГГ ЧЧ:ММ...", "default": None,
    },
    ...
}
```

- Типы: `toggle | int | list | date | text | enum | photo | file`; парсинг диспетчеризуется
  по типу в чистой функции `_parse_setting(key, raw)` (это и есть unit-тестовая поверхность).
- Читать настройку в коде: `await get_setting_typed(key)` — один сырой `get_setting` +
  разбор. Сырой `get_setting` остаётся для нерегистрированных ключей.
- Незарегистрированный ключ возвращается как есть (fail-soft) — миграция инкрементальная.
- Добавление настройки = одна запись в реестре; экраны админки (`SETTINGS_FIELDS`,
  `SETTINGS_GROUPS`) и дефолты анкеты (`REG_DEFAULTS`) выводятся из него.

Группы настроек: `event`, `reg`, `pay`, `party`, `consent`, `reg_questions`, `toggles`.

### База данных

| Таблица | Назначение |
|---------|-----------|
| `users` | Все участники: анкета, `status` (pending/approved/rejected), `participant_type` (трек), поля оплаты (`payment_status`, `payment_option`, `receipt_file_id`, `payment_due`, `paid_at`), `resume_file_id` / `resume_text` / `resume_url`, `referrer_id`, `source` |
| `bot_settings` | key-value: все настройки из админки |
| `coins` | Append-only леджер: `delta`, `reason`, `changed_by`, `timestamp`. Баланс = `SUM(delta)`, `UPDATE` не используется |
| `reg_started` | Персистентный трекинг начатых анкет: `started_at`, `last_step`, `nudged_at`, `participant_type` |
| `scheduled_broadcasts` | Payload отложенной рассылки (текст, фото, фильтр, статус). Триггером владеет APScheduler |
| `user_consents` | Аудит принятых согласий, `UNIQUE(user_id, consent_key)` |
| `staff` | Менеджеры, добавленные из бота: `telegram_id`, `role`, `added_by`, `added_at`, композитный PK `(telegram_id, role)`. Состав прав роли и тумблер «роль выключена» живут в `bot_settings` (`role_caps_<role>`, `role_<role>_enabled`), не здесь. Не путать с `ADMIN_IDS` — суперадминами из `.env`, см. ниже |
| `delegate_questions` | Вопросы делегатов организаторам + атомарный захват ответа: `id`, `user_id`, `question_text`, `asked_at`, `answered_by`, `answered_by_name`, `answered_at`, `answer_text`. Второй менеджер, попытавшийся ответить на уже отвеченный вопрос, видит имя первого |

Схема расширяется только аддитивно: `CREATE TABLE IF NOT EXISTS` + `_ensure_column()`
(`ALTER TABLE ... ADD COLUMN` с проверкой существования). Никаких деструктивных миграций
на живой базе.

ER-схема (связи логические — SQLite-таблицы без `FOREIGN KEY`, целостность держит код;
показаны ключевые поля, у `users` ~60 колонок анкеты опущены):

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
        int created_by
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
    staff |o--o{ game_submissions : "проверил"
    users ||--o{ game_submissions : "сдал"
    game_tasks ||--o{ game_submissions : "по заданию"
    game_submissions ||--o{ game_submission_parts : "части (альбом)"
```

Три инварианта, которые схема защищает на уровне данных: `coins` — только `INSERT`
(баланс всегда воспроизводим из истории); `staff` — составной PK допускает несколько
ролей у одного человека без отдельной таблицы; `delegate_questions.answered_by`
захватывается атомарным `UPDATE ... WHERE answered_by IS NULL` — двое менеджеров не
ответят на один вопрос.

### Планировщик и фоновые задачи

`main.py` при старте поднимает:

- **APScheduler** (`services/scheduler.py`) с job store в `data/jobs.sqlite` — отложенные
  рассылки (`date`-триггер), напоминания об оплате T-3 / T-1 и финальный пинг после
  дедлайна. Джобы переживают перезапуск и сверяются с БД при старте.
- **`pending_reminder_loop`** (`services/reminders.py`) — сводка админам по заявкам в
  ожидании, интервал из `pending_reminder_interval`.
- **Догон брошенных анкет** — скан `reg_started`, одно напоминание на человека
  (`nudged_at`).
- **`refresh_allowlist`** — периодическое обновление кэша предотбора.

Фоновые задачи запускаются через `services/background.spawn`, который держит strong-ref,
иначе GC может убить приостановленную корутину.

### Оплата и синхронизация с таблицей

Отрезок процесса, где раньше всё ломалось: внешний API (Google Sheets) стоял на
критическом пути регистрации, и любой сбой квоты/сети ронял анкету делегата. Сейчас
источник истины — SQLite, а таблица — производная: пишется в фоне, с повторами, и её
недоступность делегат не замечает.

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
        Note over B,G: до 3 попыток (5/15/30 с),<br/>после 3-й — алерт менеджерам,<br/>SQLite не затронут, догоняется<br/>кнопкой «Синхронизация»
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
    Note over S,U: джоба сама проверяет статус —<br/>после paid молчит, даже если<br/>отмена не успела
```

Файл чека не скачивается: в БД лежит Telegram `file_id`, менеджеру бот пересылает его по
запросу. Так же устроены резюме — см. [Nextcloud](#хранение-резюме-nextcloud).

### Порядок обработки сообщений

Роутеры подключаются в `main.py` в фиксированном порядке — aiogram 3 идёт сверху вниз и
останавливается на первом совпадении:

1. **admin** — команды админов и reply-ответы на вопросы участников
2. **registration** — `/start`, анкета, согласия
3. **payment** — выбор тарифа, загрузка чека
4. **user_actions** — кнопки меню, вопросы, коины

### FSM

| Группа | Состояния |
|--------|-----------|
| Registration | `full_name` → динамический поток из включённых вопросов (перерегистрация админа — инлайн-кнопка `admin_rereg`, не состояние) |
| Question | `waiting_for_question` |
| Broadcast | `target_selection` → фильтры → `message` → (отправка / планирование) |
| EditSetting | `waiting_for_value`, `waiting_for_photo`, `waiting_for_file` |
| Payment | Выбор тарифа → загрузка чека |

Поток анкеты собирается движком `REG_FLOW` на лету: набор шагов зависит от включённых
вопросов, трека участника и предыдущих ответов. Отмена доступна везде — кнопка «Отмена»,
`/cancel` или inline «❌ Отмена».

FSM-хранилище — `MemoryStorage`: при перезапуске незавершённые анкеты сбрасываются
(готовые регистрации не теряются, они уже в БД). Именно поэтому отложенные рассылки и
напоминания живут в БД, а не в FSM.

---

## Тесты

```bash
python -m pytest tests/ -q
```

~1370 тестов: чистые хелперы (парсинг настроек, рендеры карточек и клавиатур), миграции БД,
фильтры рассылок, планировщик, Sheets-хелперы, Nextcloud, CSV-инъекции. Внешние сервисы
(Telegram, Google, Nextcloud) не дёргаются, вместо них фейки.
