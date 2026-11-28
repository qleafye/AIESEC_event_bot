<!-- generated-by: gsd-doc-writer -->
# Разработка

Как поднять бота локально, куда класть код и как оформлять коммит. Слои и поток апдейта — в [ARCHITECTURE.md](ARCHITECTURE.md). Потолок размера модуля, параллельный прогон тестов и правило «на прод только из коммита» — в [CONVENTIONS.md](CONVENTIONS.md), здесь они не повторяются. Секреты и переменные — в [CONFIGURATION.md](CONFIGURATION.md) и в комментариях `.env.example`.

В репозитории нет `package.json`, `pyproject.toml`, `Makefile` и настроенного линтера. Команды ниже — то, чем проект реально запускается.

## Локальная настройка

Нужен Python 3.11 или новее. Образ бота и дашборда — `python:3.11-slim`. CI (`.github/workflows/tests.yml`) гоняет тесты на 3.11 и 3.12.

Отдельного шага компиляции нет: бот — `python main.py`. Зависимости рантайма — `requirements.txt` (их ставит корневой `Dockerfile`). Зависимости тестов — `requirements-dev.txt` (`pytest`, `pytest-xdist`; туда же продублированы пакеты дашборда и Mini App, чтобы тесты собирались на Windows без вложенного `-r`).

1. Клонировать и перейти в каталог:

```bash
git clone https://github.com/qleafye/AIESEC_event_bot.git
cd AIESEC_event_bot
```

2. Окружение и пакеты (для разработки ставятся оба файла, не только рантайм):

```bash
python -m venv .venv
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
pip install -r requirements.txt -r requirements-dev.txt
```

Linux и macOS:

```bash
source .venv/bin/activate
cp .env.example .env
pip install -r requirements.txt -r requirements-dev.txt
```

3. В `.env` заполнить `BOT_TOKEN` и `ADMIN_IDS`. Остальное для первого запуска не обязательно: пустые `GOOGLE_SHEET_ID` и ключи Nextcloud оставляют бота на SQLite. Образец значений — `.env.example`, не этот файл. `.env` и `google_credentials.json` в git не входят (`.gitignore`).

4. Запуск бота:

```bash
python main.py
```

При первом старте `database/db.py::init_db` создаёт `data/forum.db` (путь — `DB_PATH`, по умолчанию этот). Планировщик пишет джобы в `data/jobs.sqlite`. Каталог `data/*` в git не входит, кроме `data/lookup/` (справочники вузов и городов). `logs/bot.log` пишется с уровнем `LOG_LEVEL`; в корневой stdout уходят только WARNING и выше. Логгер `chat_recon` (`main.py::_configure_logging`) пишет INFO в stdout и в `logs/chat_recon.log`.

Mini App и дашборд файл `.env` сами не читают. `dashboard/config.py::load_config` берёт `os.environ`. В `docker-compose.yml` переменные подставляет `env_file: .env`. Локальный `uvicorn` так не делает: без `DASHBOARD_SESSION_SECRET` в окружении процесс падает с `RuntimeError` (пустой секрет сессии cookie `yl_dash` не подставляется сам). Схему базы Mini App не мигрирует: сначала хотя бы один старт бота, затем

```bash
uvicorn miniapp.main:app --host 127.0.0.1 --port 8001
```

`GET /app/health` отвечает 503, пока `PRAGMA user_version` ниже `database/schema_ready.py::SCHEMA_READY_USER_VERSION`. Дашборд (только чтение, другой набор пакетов в `dashboard/requirements.txt`; после `requirements-dev.txt` они уже стоят):

```bash
uvicorn dashboard.main:app --host 127.0.0.1 --port 8000
```

Стек `docker compose` порты наружу не публикует. Это форма сервера, не обычный цикл правки. Как код попадает на прод — [CONVENTIONS.md](CONVENTIONS.md).

### Если не стартует

- Импорт `config.py` падает сразу: `Settings` требует `BOT_TOKEN` и `ADMIN_IDS`. То же при `pytest`: `tests/conftest.py` импортирует конфиг. `ADMIN_IDS` — JSON-список чисел, как в `.env.example` (`[12345678, 87654321]` — формат, не рабочие id).
- Google не обязателен. Ключ сервисного аккаунта нужен только если заполнен `GOOGLE_SHEET_ID`. Файл по умолчанию — `google_credentials.json` рядом с `main.py`.
- Полный `pytest` в один поток долгий. Параллельный запуск и правило «один полный прогон на машину» — в [CONVENTIONS.md](CONVENTIONS.md). Два `-n auto` одновременно упираются в память и диск.
- Отдельный тест внутри файла иногда рассчитан на базу, которую открыл соседний тест того же файла. `conftest` подставляет пустую схему в свой временный каталог, чтобы не читать чужой `data/forum.db`, но такой тест надёжнее гонять файлом целиком.
- Локального хука сообщений коммита нет. Шаг `commit-trailers` отклоняет новые коммиты со строкой `Claude-Session:`. Трейлер `Co-authored-by` этот шаг не проверяет: такую строку в сообщение не пишут вручную.

## Команды

| Команда | Описание |
|---|---|
| `pip install -r requirements.txt` | Рантайм бота и Mini App. То же ставит корневой образ. |
| `pip install -r requirements-dev.txt` | Тесты: `pytest>=7.1.3,<9`, `pytest-xdist`, пакеты дашборда, `fonttools` / `brotli` для сторожа шрифтов. |
| `pip install -r dashboard/requirements.txt` | Только дашборд, как в `dashboard/Dockerfile`. Для локальных тестов не обязателен, если уже стоит `requirements-dev.txt`. |
| `python main.py` | Long polling, миграция схемы, планировщик. |
| `uvicorn miniapp.main:app --host 127.0.0.1 --port 8001` | Mini App. В compose та же команда на `0.0.0.0:8001`, сервис `miniapp`. |
| `uvicorn dashboard.main:app --host 127.0.0.1 --port 8000` | Дашборд. В compose — сервис `yl26-dashboard`, порт 8000 внутри сети, наружу не опубликован. |
| `python -m pytest -q -n auto` | Полный набор, `testpaths = tests` в `pytest.ini`. Плагин `pytest-asyncio` не используется: тесты зовут `asyncio.run()` сами. |
| `python -m pytest tests -q -n auto --durations=30` | То, что запускает CI. |
| `python -m pytest tests/test_layout_guard.py -q` | Один файл, без `-n`. Так же гоняют любой `tests/test_*.py`. |
| `python -m services.heartbeat --check` | Проверка живости long polling. `HEALTHCHECK` корневого образа. |

Отдельного скрипта линта, формата и сборки артефакта нет.

## Куда класть новый код

Корень репозитория — не место для нового модуля. `tests/test_layout_guard.py` разрешает в корне только `main.py` и `config.py`. Зависимости слоёв односторонние: `domain/` не импортирует `aiogram` и `handlers`; `shared/` — только стандартная библиотека (его целиком копирует образ дашборда). Импорт любого `handlers.*` исполняет `handlers/__init__.py` и поднимает роутеры бота, поэтому Mini App и дашборд в `handlers` не ходят. Сервисы тоже не должны: исключения перечислены в `HANDLERS_IMPORT_ALLOWLIST` того же теста, список только сокращается.

Новый экран Telegram — в `handlers/`, в каталог своей темы, не в новый файл корня.

| Куда | Когда |
|---|---|
| `handlers/reg/`, шов у `handlers/registration.py` | Шаг анкеты в чате. |
| `handlers/applications/` | Очередь и карточка заявки. Швы админки импортируются из хвоста `handlers/admin.py`. |
| `handlers/payment.py` | Тариф и чек. Свой `Router`, в `main.py` он раньше регистрации. |
| `handlers/settings/`, `handlers/admin.py` | Экран `/admin` и настроек. |
| `handlers/access/` | Роли, `/uat`, ответ без прав. |
| `handlers/comms/` | Рассылки и опросы. |
| `handlers/amb/`, `handlers/game/` | Амбассадоры, задания, монеты. |
| `handlers/forum/` | Программа, чекин, сертификаты, день форума. |
| `handlers/chat/` | Чат города. |
| `handlers/cities/`, `handlers/delegations/`, `handlers/ext_forms/`, `handlers/sheets/`, `handlers/vk/`, `handlers/i18n/` | Города, делегации, внешние формы, таблица, ВК, экраны перевода. |
| `handlers/user_actions.py` | Меню делегата. Тоже владелец роутера. |
| `keyboards/` | Сборка клавиатур Telegram и ВК, не сценарий. |

Четыре владельца роутеров — `registration`, `user_actions`, `admin`, `payment`. Их первыми импортирует `handlers/__init__.py` (тот же порядок, что в `main.py`). Новый кусок существующего экрана — отдельный модуль, который вешает хендлеры на **уже созданный** `router` владельца. Импорт шва — одна строка в хвосте владельца, в том порядке, в каком хендлеры должны срабатывать. Склеивать швы в один `import` или менять строки местами нельзя: в aiogram побеждает первый совпавший фильтр. Зачем так и какой потолок строк — [CONVENTIONS.md](CONVENTIONS.md). Инвариант порядка — `tests/test_seam_import_order_260819.py`.

Свой `include_router` в `main.py` — только если апдейт обязан встать в очередь раньше или позже чужого роутера. Сейчас порядок такой: `group_chat.router`, `group_chat.private_router`, `uat_seed.router`, `admin.router`, `admin_no_access.router`, `payment.router`, `onsite_reg.router`, `telegram_link.router`, `registration.router`, `user_actions.router`, `polls.router`, последним `reg_silence_fallback.router`. Последний отвечает лишь тогда, когда апдейт никто не забрал. Групповой роутер стоит первым, потому что он забирает сообщения групп и не пускает их в личные хендлеры.

Сценарий, который нужен и чату, и Mini App, и джобе — в `services/`, в подкаталог той же темы (`registration/`, `applications/`, `comms/`, `forum/`, `sheets/`, `access/`, `i18n/` и соседние). Инфраструктура процесса, не экран: `services/scheduler.py` (единственный планировщик, джобы в `data/jobs.sqlite`), `services/infra/`, `services/heartbeat.py`, `services/messenger_router.py`. Веб может вызвать сервис и записать строку в базу. Сессию long polling и планировщик он не поднимает. Сообщение и сброс FSM Mini App кладёт в `miniapp/outbox.py`; разбирает очередь `services/infra/miniapp_outbox.py`. Вкладку магазина процесс Mini App пересобирает сам: `miniapp/main.py` зовёт `services.sheets.shop_sheet.enable_shop_sheet_sync`, а `database/shop_db.py` после выдачи — `request_shop_sheet_sync()`.

Правило без aiogram — в `domain/`. Туда: ключ реестра в `domain/settings/schema.py` (`SETTINGS_SCHEMA`), шаг анкеты в `domain/regform/`, код права в `domain/access/capabilities.py` (`ALL_CAPABILITIES`, подписи ролей в `ROLES`), город в `domain/cities.py`, разбор тарифа в `domain/payment.py`. `domain` может читать `database` и `config`, роутеры — нет. Движок анкеты один на чат и Mini App.

Схема и запросы — в `database/`. Новая таблица: `CREATE TABLE IF NOT EXISTS` в `init_db` или в соседнем модуле с `ensure_schema(db)`, который `init_db` вызывает сам (так устроены `amb_status_db`, `shop_db`, `certificates_db` и остальные рядом). Новая колонка на уже живой базе — `database/db.py::_ensure_column`, не отдельный мигратор. Одноразовый перенос данных — константа вида `*_USER_VERSION` и `PRAGMA user_version` в модуле каталога `database/`. `schema_ready.py` сам ничего не меняет: максимум этих констант становится `SCHEMA_READY_USER_VERSION`, по нему Mini App понимает, что бот уже мигрировал. Старые карточки ломать нельзя: отсутствие колонки — повод добавить её, не пересоздать таблицу.

Новый HTTP-экран Mini App — модуль в `miniapp/routers/` и строка в `ALL_ROUTERS` (`miniapp/routers/__init__.py`). Кто спрашивает и какое право нужно — `miniapp/deps.py`. Дашборд новый слой записи не заводит: `dashboard/db.py::read_conn` открывает файл с `mode=ro`. Правка данных для страницы статистики живёт в боте или в Mini App. Общая формула бота и дашборда на чистой stdlib — `shared/`. Разовый скрипт, не путь апдейта — `tools/` или `scripts/`.

Тест — `tests/test_*.py`. Общие помощники: `tests/conftest.py` (канонический импорт роутеров до сбора тестов), `tests/_dbtpl.py`, `tests/_paths.py`. Базу тест заводит сам во временном каталоге и подменяет `config.DB_PATH`. Каждый хендлер админки объявляет право одним декоратором: полноту сверяет `tests/test_roles_phase8.py`.

## Стиль кода

Отдельного форматтера и линтера в репозитории нет: нет `pyproject.toml`, `ruff.toml`, `.prettierrc`, `biome.json`, `.editorconfig`, pre-commit. CI стиль не гоняет, только тесты и проверку сообщений коммитов. `.ruff_cache` и `.mypy_cache` в `.gitignore` — следы локальных запусков, не настройка проекта.

Как принято в коде:

- Идентификаторы, ключи настроек и коды прав — по-английски (`event_date`, `moderate_reg`). Текст, который видит делегат или менеджер, — по-русски.
- Комментарий объясняет, почему так, а не пересказывает строку. Длинные комментарии в существующих файлах часто ссылаются на старые задачи; в новый код такое не копировать.
- Аннотации типов используются там, где сигнатуру читает и другой слой (`domain`, `database`, `miniapp`).
- Новый файл `handlers/*.py` держать около ориентира из [CONVENTIONS.md](CONVENTIONS.md). Жёсткий потолок для файла вне списка исключений — `DEFAULT_CEILING` в `tests/test_module_size_convention_260816.py`. Поднимать потолок осознанно, в том же коммите, одной своей строкой `KNOWN_OVERAGES`.

## Тексты для менеджера

Менеджер настраивает событие кнопками в `/admin` и в Mini App, без правки кода и без рестарта. В интерфейс не попадают коды.

- Кнопка — готовая русская подпись. Право `moderate_reg` на экране называется «📋 Модерация заявок» (`CAP_LABELS` в `handlers/access/admin_caps.py`). Роль `reg_manager` называется «🛂 Менеджер регистраций» (`ROLES` в `domain/access/capabilities.py`). Ключ `event_date` на экране — «🗓 Дата» (`label` в `SETTINGS_SCHEMA`). Код города, имя ключа и capability в подпись кнопки не подставлять и ввести у человека не просить.
- Свободный текст — только там, где значение правда произвольное: имя, сумма, дата, текст письма. Рядом пример формата. У `forum_date` подсказка просит `ДД.ММ.ГГГГ` и приводит пример в `prompt` той же записи схемы.
- Ошибка говорит, что сделать («пришлите пересланное сообщение или @username»), а не `invalid input`.
- Действие, которое что-то стирает, сначала подтверждается, и в тексте подтверждения сказано, что пропадёт.
- Проверка любой админской правки: менеджер делает это один, ночью, без разработчика.

Новая настройка — запись в `SETTINGS_SCHEMA` с человеческими `label` и `prompt`, не новый абзац в `.env`. В `.env` остаются секрет запуска, `ADMIN_IDS` и то, что бот не может спросить в Telegram.

## Сообщения коммитов

Язык — русский. По-английски остаются идентификаторы и термины без принятого перевода (`payment_status`, `SETTINGS_SCHEMA`, deep-link).

Формат — Conventional Commits:

```
тип(скоуп): тема
```

Тип — `feat`, `fix`, `docs`, `test`, `refactor`, `chore`. Скоуп — модуль: `admin`, `registration`, `payment`, `ui`, `miniapp`, `dashboard`, `db`, `sheets`, `chat`, `game`, `config`, `tests` и соседние каталоги по тому же принципу. Затронуто много модулей — скоуп опустить. Тема — что изменилось для читателя истории, без номера задачи и без шифра плана.

```
fix(cities): перевод города оставляет одну строку и одно письмо
```

Тело — только если из темы не ясно, почему так. Не пересказывать дифф.

В сообщение не входят номер плана, фазы или внутренний id ревью. Не входят строка `Claude-Session:` и трейлер `Co-authored-by` от Cursor, Claude или Anthropic: репозиторий публичный. Локального хука, который вырезал бы их сам, нет. Шаг `commit-trailers` в `.github/workflows/tests.yml` не пропускает новые коммиты со строкой `Claude-Session:`. При cherry-pick чужого коммита сообщение проверить руками.

## Ветки

Письменного соглашения об именах веток в репозитории нет. Ветка по умолчанию — `main`: `origin/HEAD` смотрит на неё, CI запускается на push и pull request в `main`.

В локальной истории встречаются префиксы `fix/`, `hotfix/`, `quick/`, `docs` и датированные имена. Это наблюдение, не шаблон, которому нужно следовать. В имя ветки не кладут токен, пароль и адрес с секретом.

## Pull request

Шаблона pull request и шаблонов issue в `.github/` нет. Код поведения — в `CODE_OF_CONDUCT.md` его тоже нет.

- Ветка от актуального `main`, запрос в `main`.
- Сообщения коммитов — как в разделе выше. Хук подключён, в истории ветки нет `Claude-Session:`.
- Рядом с правкой поведения — тест в `tests/test_*.py`. Сторож раскладки и потолок размера модуля не обходятся «заодно» чужой строкой allowlist.
- Перед запросом имеет смысл прогнать затронутые файлы без `-n`. Если задеты роутеры, схема или реестр — полный `python -m pytest tests -q -n auto`.
- CI на pull request в `main`: job `commit-trailers` и job `pytest` независимы, между ними нет `needs`. `pytest` идёт на Python 3.11 и 3.12 с командой `python -m pytest tests -q -n auto --durations=30`. Оба должны быть зелёными. Секреты в описание запроса не вставлять: в прогоне CI стоят фиктивные `BOT_TOKEN` и `ADMIN_IDS`, не боевые.

Баг и пожелание без готовой правки — GitHub Issues этого репозитория: что сделали, что ждали, что вышло, какое событие и город. Секреты, `.env` и выгрузку персональных данных в issue не класть.
