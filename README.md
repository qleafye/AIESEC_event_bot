# AIESEC Event Bot

Универсальный Telegram-бот для мероприятий AIESEC: форумов (YouLead) и конференций
(RusCo/Summit). Регистрация делегатов, модерация заявок, оплата и чеки, согласия,
рассылки, реферальная система, аналитика — всё внутри Telegram. Один бот на все события:
что именно спрашивать и какие модули включать, настраивается кнопками в админке без
перезапуска и без правки кода.

**Документация по ролям:**

| Кому | Файл |
|------|------|
| Менеджеру события (полный гайд) | [ADMIN_GUIDE.md](ADMIN_GUIDE.md) |
| Менеджеру события (одна страница) | [ADMIN_CHEATSHEET.md](ADMIN_CHEATSHEET.md) |
| Тестировщику (чек-лист «потыкать») | [BOT_GUIDE.md](BOT_GUIDE.md) |
| Про трек «вечеринка» | [docs/party-flow-guide.md](docs/party-flow-guide.md) |
| Разработчику | этот файл |

---

## Оглавление

- [Что умеет бот](#что-умеет-бот)
- [Быстрый старт](#быстрый-старт)
- [Переменные окружения](#переменные-окружения)
- [Google Sheets](#google-sheets)
- [Хранение резюме (Nextcloud)](#хранение-резюме-nextcloud)
- [Архитектура](#архитектура)
  - [Структура проекта](#структура-проекта)
  - [Стек](#стек)
  - [Настройки: SETTINGS_SCHEMA](#настройки-settings_schema)
  - [База данных](#база-данных)
  - [Планировщик и фоновые задачи](#планировщик-и-фоновые-задачи)
  - [Порядок обработки сообщений](#порядок-обработки-сообщений)
  - [FSM](#fsm)
- [Тесты](#тесты)

---

## Что умеет бот

| Модуль | Кратко |
|--------|--------|
| Регистрация | Динамическая анкета: ~43 вопроса, каждый включается тумблером. Типы: кнопки, текст, дата, мультивыбор, файл. Зависимые вопросы пропускаются автоматически, в конце — сводка ответов |
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
PROXY_RECHECK_SECONDS=600                 # сколько секунд сидеть на резерве до попытки вернуться на основной
DB_PATH=data/forum.db
LOG_LEVEL=INFO                            # DEBUG | INFO | WARNING | ERROR

# --- Google Sheets (пусто = бот работает только на своей БД) ---
GOOGLE_SHEET_ID=
GOOGLE_CREDENTIALS_FILE=google_credentials.json
GOOGLE_SHEET_TAB=                         # имя вкладки; пусто = первая вкладка

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

**Резервный прокси (failover).** `PROXY_URL` можно указывать как HTTP-прокси (напр.
`http://host.docker.internal:8118`, если используется privoxy-обвязка), так и напрямую как
`socks5://` эндпоинт — `aiohttp-socks` уже в зависимостях, промежуточный privoxy-хоп не
обязателен. При сетевой ошибке к Telegram (`TelegramNetworkError`) бот автоматически
переключается на `PROXY_URL_BACKUP` — без рестарта, без пересоздания сессии — и остаётся
на нём (sticky), пока не пройдёт `PROXY_RECHECK_SECONDS`: тогда бот сам пробует вернуться
на основной канал, и если тот всё ещё недоступен — снова уходит на резерв. Если оба звена
мертвы одновременно, наружу летит тот же исходный `TelegramNetworkError`, что и раньше —
`dp.start_polling` ретраит с тем же бэкоффом, ничего не меняется в этом сценарии. Каждая
смена звена пишет `WARNING` в лог и уходит админам (`ADMIN_IDS`) одним уведомлением в
Telegram; креды прокси (`user:pass@`) в логах и уведомлениях всегда маскируются.

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
├── tests/                   # pytest, ~400 тестов
├── resources/               # Опциональные fallback-фото
└── data/                    # Рантайм: forum.db, jobs.sqlite, broadcast_target.txt
```

### Стек

| Компонент | Технология |
|-----------|------------|
| Фреймворк | [aiogram 3](https://docs.aiogram.dev/), long polling |
| БД | SQLite через [aiosqlite](https://github.com/omnilib/aiosqlite) |
| Планировщик | [APScheduler 3.x](https://apscheduler.readthedocs.io/en/3.x/) `AsyncIOScheduler` + `SQLAlchemyJobStore` (`data/jobs.sqlite`) |
| Конфигурация | pydantic-settings + `.env` |
| Google Sheets | [gspread](https://docs.gspread.org/) + сервисный аккаунт |
| Файлы | Telegram `file_id` + Nextcloud (WebDAV) для резюме |

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

Схема расширяется только аддитивно: `CREATE TABLE IF NOT EXISTS` + `_ensure_column()`
(`ALTER TABLE ... ADD COLUMN` с проверкой существования). Никаких деструктивных миграций
на живой базе.

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
| Registration | `admin_rereg` → `full_name` → динамический поток из включённых вопросов |
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

~400 тестов: чистые хелперы (парсинг настроек, рендеры карточек и клавиатур), миграции БД,
фильтры рассылок, планировщик, Sheets-хелперы, Nextcloud, CSV-инъекции. Внешние сервисы
(Telegram, Google, Nextcloud) не дёргаются — используются фейки.
