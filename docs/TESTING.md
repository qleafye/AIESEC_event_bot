<!-- generated-by: gsd-doc-writer -->
# Тесты

Проверки лежат в каталоге `tests/` и гоняются pytest. Сеть не нужна: свою SQLite во временном файле заводит не каждый тест — [`tests/conftest.py`](../tests/conftest.py) держит одну временную базу на процесс для тех, кто не задаёт `config.DB_PATH` (например [`tests/test_content_percity_consumers.py`](../tests/test_content_percity_consumers.py)::`test_approve_text_party_track_no_party_override_still_ignores_city`), а Google Sheets подменяется в том тесте, который вообще доходит до клиента таблицы. Плагина pytest-asyncio нет — корутины тесты запускают сами через `asyncio.run()`.

## Фреймворк и подготовка

Фреймворк — pytest, диапазон в [`requirements-dev.txt`](../requirements-dev.txt): `pytest>=7.1.3,<9`. Параллельный прогон — `pytest-xdist>=3.6,<4`. Отдельного `pytest-asyncio`, `pytest-cov` и файла конфигурации покрытия нет.

[`pytest.ini`](../pytest.ini) задаёт только каталог:

```ini
[pytest]
testpaths = tests
```

Свои маркеры не объявлены. Подмножества выбираются путём к файлу, node id или `-k`, а не маркером.

Перед первым прогоном нужны зависимости бота и тестовый набор. FastAPI, uvicorn, Jinja2, itsdangerous и httpx уже запинены в `requirements.txt`; `requirements-dev.txt` среди этого списка дописывает PyJWT, fonttools и brotli. fonttools импортируется только внутри теста через `pytest.importorskip` ([`tests/test_web_assets_260823.py`](../tests/test_web_assets_260823.py)), а не на сборке каждого теста. Пины дашборда продублированы в этом файле нарочно, а не через `-r dashboard/requirements.txt`.

Из корня репозитория, тем интерпретатором, которым запускаются тесты:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

Импорт `config.Settings` требует `BOT_TOKEN` и `ADMIN_IDS` ещё до первого теста (`config.py`). Локально их обычно даёт `.env`. На чистой машине без `.env` сборка падает, пока переменные не заданы. В CI стоят фиктивные значения, сеть ими не открывается:

```powershell
$env:BOT_TOKEN = "123456:dummy-test-token"
$env:ADMIN_IDS = "[1]"
```

Рабочий токен бота и боевой `data/forum.db` для прогона не нужны. Каталог `tests/` должен оставаться пакетом: пустой [`tests/__init__.py`](../tests/__init__.py) не даёт колесу `argos-translate-lt` затенить его своим пакетом `tests` в site-packages.

## Запуск тестов

Полный набор (каталог берётся из `pytest.ini`):

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Полный прогон в один поток упирается в таймаут CI (45 минут, см. комментарий в [`.github/workflows/tests.yml`](../.github/workflows/tests.yml)). Локально его запускают параллельно. Один полный прогон на машину: два сразу делят диск и память. Отдельный файл без `-n` — накладные расходы воркеров больше выигрыша.

```powershell
.\.venv\Scripts\python.exe -m pytest -q -n auto
```

Один файл, один тест, отбор по имени:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_db_phase1.py
.\.venv\Scripts\python.exe -m pytest tests/test_db_phase1.py::test_add_user_persists_resume_file_id
.\.venv\Scripts\python.exe -m pytest -k sheet
```

Режима watch в `pytest.ini` и скриптах нет.

`tmp_path` pytest складывает во временный каталог пользователя (`pytest-of-<имя>`). Шаблон схемы и дефолтная база процесса — отдельные каталоги с префиксами из [`tests/_dbtpl.py`](../tests/_dbtpl.py) и [`tests/conftest.py`](../tests/conftest.py); по завершении процесса они удаляются. Исключения антивируса на каталог репозитория и на `%TEMP%\pytest-of-<имя>` ускоряют файловые тесты.

## Новые тесты

Файл — `tests/test_*.py` в корне `tests/`, без вложенных пакетов. Имя описывает тему; у части файлов в суффиксе дата `YYMMDD`. Функции — `test_*`. Общие куски, которые pytest не должен собирать как тесты, лежат в модулях с подчёркиванием и в `tests/fixtures/`.

Корутина не помечается `async def` на уровне pytest. Внутри теста — вложенная `async def`, снаружи — `asyncio.run(...)`, как в [`tests/test_admin_actions_db.py`](../tests/test_admin_actions_db.py).

### База

Это не мок драйвера, а настоящий SQLite. До сборки [`tests/conftest.py`](../tests/conftest.py) один раз на процесс (и на каждый воркер pytest-xdist) кладёт пустую схему во временный файл и записывает путь в `config.DB_PATH`. Иначе одиночный тест, который сам путь не меняет, открыл бы `data/forum.db`. Свою базу в `tmp_path` заводит не каждый тест: кто не задаёт `config.DB_PATH`, пользуется этой общей временной базой (например [`tests/test_content_percity_consumers.py`](../tests/test_content_percity_consumers.py)::`test_approve_text_party_track_no_party_override_still_ignores_city`).

Свежую схему даёт `fast_init_db()` из [`tests/_dbtpl.py`](../tests/_dbtpl.py): один раз на процесс вызывается боевой `database.db.init_db()`, дальше тест получает копию файла. Вызывать после присвоения `config.DB_PATH`. Так делают тесты, которым нужна пустая актуальная схема, а не проверка самой миграции.

`init_db()` напрямую оставляют там, где тест собирает старую схему и проверяет `ALTER` / бэкафилл, либо где второй вызов `init_db()` не должен стереть уже записанные строки. Такие файлы перечислены в докстринге `tests/_dbtpl.py`. Повтор одного и того же крупного засева — `replay_seed()` в том же модуле; засев, который удаляет строки или зависит от автоинкремента, туда не кладут.

Каркас:

```python
import asyncio

from config import config
from tests._dbtpl import fast_init_db


def test_example(tmp_path):
    config.DB_PATH = str(tmp_path / "example.db")
    fast_init_db()

    async def go():
        ...

    asyncio.run(go())
```

`conftest` дополнительно, до сборки, импортирует `handlers.registration`, `handlers.user_actions`, `handlers.admin`, `handlers.payment` — в том же порядке, что `main.py`. Иначе порядок хендлеров на роутере зависит от того, какой файл попал в процесс первым, и одиночный прогон расходится с полным. После каждого теста autouse-фикстуры стирают устаревшие копии шаблонной базы и сбрасывают процессную память вечерней рассылки QR и подсказок SOS.

### Google Sheets

Общего фейка на весь набор нет. Тесты, которым важен отказ «таблицы нет», обнуляют `config.GOOGLE_SHEET_ID` и `config.GOOGLE_CREDENTIALS_FILE` через `monkeypatch` — запись в Sheets тогда не начинается. Тесты синхронной обёртки подменяют саму функцию (`_append_to_sheet_sync`, `_append_to_named_sheet_sync`, `update_status_in_sheet`, `sync_named_worksheet`) и не открывают gspread.

Там, где нужен объект листа, `sheets.gspread.service_account` подменяется локальным клиентом с `open_by_key`. Образец — `_FakeClient` / `_FakeSpreadsheet` / `_FakeWorksheet` в [`tests/test_sheets_phase5.py`](../tests/test_sheets_phase5.py) и [`tests/test_sheets_main_tab_pin_260813.py`](../tests/test_sheets_main_tab_pin_260813.py). В такие тесты передаются литералы вроде `fake-id` и `fake-creds.json`, не файл ключа сервисного аккаунта.

Узкий лист в памяти — [`tests/_fake_ws.py`](../tests/_fake_ws.py) (`FakeWS`, `probe_sheet`). Его методы повторяют те вызовы Worksheet, которые делает зеркало вкладки. `FakeWS` и `probe_sheet` оттуда импортирует [`tests/test_delegations_mirror.py`](../tests/test_delegations_mirror.py). [`tests/test_ext_forms_mirror.py`](../tests/test_ext_forms_mirror.py) объявляет свой `FakeWS` и `tests/_fake_ws` не импортирует.

### Прочие хелперы

| Модуль | Зачем |
|---|---|
| [`tests/_paths.py`](../tests/_paths.py) | `REPO_ROOT` — корень репозитория для чтения исходников по пути |
| [`tests/_enroll38.py`](../tests/_enroll38.py), [`tests/_enroll38_chat.py`](../tests/_enroll38_chat.py) | общая база и фейковые сообщения для записи на сессии |
| [`tests/_forum_day_world.py`](../tests/_forum_day_world.py) | сквозной стенд дня форума: временная база, часы МСК, `Dispatcher` без сети, планировщик на паузе |
| [`tests/_lang_on.py`](../tests/_lang_on.py), [`tests/_delegate_preview.py`](../tests/_delegate_preview.py) | язык делегата и шаг подтверждения анкеты |
| [`tests/fixtures/ext_forms/`](../tests/fixtures/ext_forms/) | синтетический JSON внешней формы, не живая выгрузка |

Маршруты дашборда поднимают приложение через `starlette.testclient.TestClient` и ту же временную базу (`tests/test_dashboard_routes.py`). Отдельного HTTP-сервера тест не слушает.

## Покрытие

Порог покрытия не задан: в репозитории нет `pytest-cov`, секции coverage и шага `--cov` в CI. Процент строк, веток, функций и инструкций нигде не проверяется.

Файлы с `coverage` в имени — это не отчёт coverage.py. Это проверки переводов: [`tests/test_i18n_form_manual_coverage_260917.py`](../tests/test_i18n_form_manual_coverage_260917.py) и [`tests/test_miniapp_i18n_coverage_260917.py`](../tests/test_miniapp_i18n_coverage_260917.py). У [`tests/test_settings_groups_c0x.py`](../tests/test_settings_groups_c0x.py) слова `coverage` в имени файла нет.

## CI

Workflow [`.github/workflows/tests.yml`](../.github/workflows/tests.yml), имя `tests`. Запуск — `push` и `pull_request` в ветку `main`. Группа `tests-${{ github.ref }}` с `cancel-in-progress: true`: новый пуш отменяет предыдущий прогон той же ветки.

Тесты выполняет job `pytest`:

1. `ubuntu-latest`, таймаут 45 минут. Матрица Python `3.11` и `3.12`, `fail-fast: false`. Образ бота — `python:3.11-slim`; 3.12 в матрице — вторая проверка, не версия прод-образа.
2. В окружение job кладутся фиктивные `BOT_TOKEN=123456:dummy-test-token`, `ADMIN_IDS=[1]` и `PYTHONDONTWRITEBYTECODE=1`.
3. `actions/setup-python@v5` кеширует pip по `requirements.txt` и `requirements-dev.txt`.
4. Установка: `python -m pip install --upgrade pip`, затем `pip install -r requirements.txt -r requirements-dev.txt`.
5. Команда: `python -m pytest tests -q -n auto --durations=30`. `-n auto` — потому что один поток не укладывается в 45 минут. `--durations=30` печатает тридцать самых медленных тестов.

Рядом job `commit-trailers` (таймаут 5 минут) тесты не запускает: он отклоняет новые коммиты, в сообщении которых есть строка `Claude-Session:`.
