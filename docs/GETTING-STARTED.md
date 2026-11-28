<!-- generated-by: gsd-doc-writer -->
# С чего начать

Локальный запуск бота на машине разработчика под Windows (PowerShell). Что умеет бот — [README.md](../README.md). Как устроены процессы и база — [ARCHITECTURE.md](ARCHITECTURE.md). Какие ключи лежат в `.env`, а какие менеджер меняет из бота — [CONFIGURATION.md](CONFIGURATION.md).

Менеджеру события этот файл не нужен. Ему — [ADMIN_GUIDE.md](ADMIN_GUIDE.md).

Серверный стек (`docker compose`, туннель, сеть `edge`) здесь не поднимается. Схема выкладки — раздел «Деплой» в README и [DEPLOY-DOMAIN.md](DEPLOY-DOMAIN.md).

## Что нужно заранее

- **Python 3.11 или 3.12.** Образ бота — `python:3.11-slim` (`Dockerfile`). CI гоняет тесты на 3.11 и 3.12 (`.github/workflows/tests.yml`). В README указано «Python 3.11 или новее». Команды ниже зовут интерпретатор из `.venv`, не системный `python` после создания окружения.
- **Git.** Репозиторий: `https://github.com/qleafye/AIESEC_event_bot.git`.
- **Токен бота** от @BotFather и **свой числовой Telegram ID**. Как узнать ID — комментарий в [`.env.example`](../.env.example) (бот @userinfobot). Оба значения обязательны для процесса бота. Полный список ключей и дефолтов — в [CONFIGURATION.md](CONFIGURATION.md), сюда их не копируем.
- Доступ к Bot API с этой машины **или** прокси. Без прокси код стартует, но апдейты не придут, если `api.telegram.org` с машины не открывается. Форматы `PROXY_URL` — в `.env.example`.

Docker, Google-таблица и Nextcloud для первого локального запуска не нужны. Пустые `GOOGLE_SHEET_ID` и `NEXTCLOUD_*` регистрацию не ломают: источник истины — SQLite.

Справочники вузов и городов уже в клоне: `data/lookup/`. Каталог `data/` из‑за них есть сразу. Сами `data/forum.db` и `data/jobs.sqlite` в git не входят (`.gitignore`).

## Установка

Команды — из корня репозитория, в PowerShell. Активировать `.venv` не нужно: дальше везде `.\.venv\Scripts\python.exe`.

1. Клонировать и перейти в каталог:

```powershell
git clone https://github.com/qleafye/AIESEC_event_bot.git
cd AIESEC_event_bot
```

2. Окружение и зависимости бота. Список пакетов — [`requirements.txt`](../requirements.txt) (aiogram 3, aiosqlite, gspread, APScheduler 3.11.2 и остальные рантайм-зависимости, включая Mini App в том же файле). Отдельный `dashboard/requirements.txt` в это окружение вложенным `-r` не подключают: комментарий в `requirements-dev.txt` фиксирует кодировочный баг pip на Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

3. Файл окружения. Если `.env` уже заполнен, не копируйте поверх — затрёте токен.

```powershell
Copy-Item -Path .env.example -Destination .env
```

Файл должен остаться в корне, рядом с `main.py`, в кодировке UTF-8: `Settings` в `config.py` читает `.env` именно так.

4. В `.env` заполнить два поля, без которых процесс не стартует:

- `BOT_TOKEN` — токен от BotFather.
- `ADMIN_IDS` — JSON-список целых чисел в квадратных скобках, как в шаблоне `.env.example`. Не одно голое число.

Остальные ключи можно оставить как в шаблоне. Секреты в репозиторий не кладутся: `.env` и `google_credentials.json` в `.gitignore`.

Тесты на этом шаге не ставятся. Они нужны только если будете гонять pytest — см. ниже.

## Первый запуск

Из корня репозитория:

```powershell
.\.venv\Scripts\python.exe main.py
```

Что должно произойти:

1. Процесс **не завершается**. Это long polling, не одноразовый скрипт. Остановка — Ctrl+C в том же окне.
2. В консоли при штатном старте почти пусто. Корневой хендлер stdout в `main.py` пишет только `WARNING` и выше, но логгер `chat_recon` добавляет свой `StreamHandler(sys.stdout)` на уровне `INFO`. Подробный лог — в `logs/bot.log` (уровень по умолчанию `INFO`).
3. В `logs/bot.log` есть строки `Starting bot...` и `Scheduler + allowlist refresh + heartbeat started`. Пока их нет, бот ещё не дошёл до поллинга.
4. Появляются `data/forum.db` (её создаёт `init_db`) и `data/jobs.sqlite` (её создаёт планировщик, каталог `data` он при необходимости создаёт сам).

Дальше откройте бота в Telegram под ID из `ADMIN_IDS` и отправьте `/start`. Админка — команда `/admin`. Тексты и тумблеры после этого правятся в боте, не в `.env`.

Прогон тестов к первому запуску бота не относится. Если нужен, зависимости разработки — [`requirements-dev.txt`](../requirements-dev.txt), команда локального параллельного прогона — из `docs/CONVENTIONS.md`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q -n auto
```

`pytest.ini` уже смотрит в каталог `tests`. Плагин `pytest-asyncio` не используется. Один полный прогон на машину: два параллельных упираются в память и диск. Отдельный файл гоняют без `-n`. В README та же установка и вариант `python -m pytest -n 8`.

Локальный `uvicorn` дашборда файл `.env` сам не читает: `dashboard/config.load_config` берёт только `os.environ`. `uvicorn miniapp.main:app` при импорте вызывает `Settings()` из `config.py`, а тот читает `env_file=".env"`. Для первого знакомства с ботом оба процесса не нужны; отличия — в [CONFIGURATION.md](CONFIGURATION.md).

## Частые проблемы при настройке

**Консоль молчит, кажется, что ничего не запустилось.** Так и задумано: INFO уходит в `logs/bot.log`, не в окно PowerShell. Смотрите файл лога и что процесс всё ещё жив. Запускать надо из корня, где лежат `main.py` и `.env`. Из другого каталога pydantic не найдёт `.env`, а база уедет в относительный `data/forum.db` не туда.

**Падение сразу, до поллинга, ошибка валидации pydantic.** Нет `BOT_TOKEN` или `ADMIN_IDS` ни в `.env`, ни в окружении процесса. Либо `ADMIN_IDS` не JSON-список (`[12345678]`), либо файл сохранён не в UTF-8. Своей русской фразы на этот случай в коде нет — текст ошибки от pydantic. Пока этих двух полей нет, `config.py` не импортируется.

**Бот «запущен», но в Telegram молчит.** Код не требует прокси. Если до Bot API достучаться нельзя, задайте `PROXY_URL` (и при необходимости `PROXY_URL_BACKUP`) по комментариям `.env.example` и перезапустите процесс. Разбор полей — в [CONFIGURATION.md](CONFIGURATION.md).

**Нет `google_credentials.json`.** Для локального старта это нормально, файл в git не входит. Пока `GOOGLE_SHEET_ID` пустой и в базе нет своей таблицы, запись в Google выключена. Ключ кладут рядом с `main.py` только когда таблица нужна; шаги сервисного аккаунта — в README, раздел «Конфигурация».

**Английский перевод менеджерских текстов не появляется.** Пакет `argos-translate-lt` ставится из `requirements.txt`, модель — нет. Её нет в `data/argos` — драйвер пишет в лог, что пакет ru→en не найден, и отключается до конца процесса. Делегат при этом видит русский, бот не падает. Файл модели: `translate-ru_en-1_9.argosmodel` в каталоге `data/argos` (или в каталоге из `ARGOS_PACKAGES_DIR`, если переменная задана в окружении).

**Тесты на Windows идут очень долго или диск захлёбывается.** Полный однопоточный `python -m pytest` на Windows в `docs/CONVENTIONS.md` оценён примерно в 34 минуты. Параллельно — `.\.venv\Scripts\python.exe -m pytest -q -n auto`. Исключения Защитника Windows для каталога репозитория и для `%TEMP%\pytest-of-<user>` ускоряют файловые тесты. Не добавляйте в `requirements-dev.txt` строку `-r dashboard/requirements.txt`: вложенный `-r` на Windows ломает pip по кодировке, пины дашборда в том файле уже продублированы.

**`Activate.ps1` не запускается.** Для команд из этого файла активация не нужна. Если всё же включаете окружение скриптом, политика выполнения PowerShell может запретить `.\.venv\Scripts\Activate.ps1`. Зовите `.\.venv\Scripts\python.exe` напрямую.

## Дальше

- [README.md](../README.md) — что бот делает для делегата и менеджера, быстрый старт и деплой, без повторения таблиц настроек.
- [ARCHITECTURE.md](ARCHITECTURE.md) — процессы `bot`, Mini App и дашборд, куда пишется SQLite и как идёт апдейт.
- [CONFIGURATION.md](CONFIGURATION.md) — обязательные и необязательные переменные, что сеется в базу один раз и что локальный uvicorn не подхватывает из файла `.env`.

Код и сообщения коммитов — [CONVENTIONS.md](CONVENTIONS.md). Кнопки админки — [ADMIN_GUIDE.md](ADMIN_GUIDE.md).
