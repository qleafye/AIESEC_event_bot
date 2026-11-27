<!-- generated-by: gsd-doc-writer -->
# Выкладка

На каждое мероприятие — свой каталог и свой `docker compose`. В каталоге три процесса вокруг одного SQLite: бот (long polling), Mini App и дашборд. Общий [`docker-compose.yml`](../docker-compose.yml) **не публикует порты на хост**. Как домен, туннель и loopback доходят до этих процессов, расписано в [DEPLOY-DOMAIN.md](DEPLOY-DOMAIN.md) — этот файл ту схему не дублирует и не подставляет адреса, которых в репозитории нет.

Код прода YouLead попадает на сервер **только из закоммиченного git**. Сначала commit, потом в каталоге стека `git pull` и сборка. Запрещены сборка из грязного дерева, копирование `.py` в уже запущенный контейнер и «срочно на форуме — без коммита». Одобрение заявки, баллы и рассылка — это записи в SQLite, не выкладка: в git они не пишутся и откатом образа не отменяются.

Полный список переменных — [CONFIGURATION.md](CONFIGURATION.md).

## Куда выкладывается

Реестра образов и манифестов Vercel, Fly, Netlify, Railway или Kubernetes в репозитории нет. Образы собираются на той машине, где лежит checkout.

| Куда | Что в репозитории | Что поднимается |
|---|---|---|
| Стек события | [`docker-compose.yml`](../docker-compose.yml), [`Dockerfile`](../Dockerfile), [`dashboard/Dockerfile`](../dashboard/Dockerfile) | `bot`, `yl26-dashboard`, `miniapp`. Сайдкар `libretranslate` — только профиль `i18n`, по умолчанию не стартует |
| Публичный HTTPS | не этот compose | Cloudflare Tunnel и проброс на loopback. Пошагово, с занятыми портами и именами контейнеров — [DEPLOY-DOMAIN.md](DEPLOY-DOMAIN.md) |
| Файл [`tunnel/docker-compose.yml`](../tunnel/docker-compose.yml) | образ `cloudflare/cloudflared`, сеть `edge` и `nextcloud_internal` | На сервере бота этим файлом коннектор **не** поднимают. Живая схема — в DEPLOY-DOMAIN, шаги 1–3. Токен в git не кладётся, шаблон — [`tunnel/.env.example`](../tunnel/.env.example) |
| Супердашборд | тот же образ `dashboard/`, отдельный checkout | Каталог и вход через Cloudflare Access описаны в DEPLOY-DOMAIN, раздел «Супердашборд». Стек события его не содержит |
| Nextcloud | в этом репозитории стека нет | Публикация `cloud.<домен>` — DEPLOY-DOMAIN, шаг 5 |

Прод YouLead — каталог `/home/leaf/YouLead26` на машине, куда заходят как `ssh leafye`. Сначала проверяют тест-стенд `/home/leaf/YouLead26-test` (бот `@YouLead_test_bot`). Перед любым `docker compose` смотрят `pwd`: на одном демоне несколько стеков, не тот каталог поднимет не то событие. Имена, поддомены и порты уже развёрнутых стеков — таблица «Что уже развёрнуто» в [DEPLOY-DOMAIN.md](DEPLOY-DOMAIN.md). <!-- VERIFY: адрес ssh-алиаса leafye и состав машин туннеля задаются вне git; в репозитории зафиксированы только алиас, пути каталогов и отсылка к DEPLOY-DOMAIN.md -->

Базовый compose рассчитан на Юлид: у дашборда `container_name: youlead26-dashboard`, у Mini App — `youlead26-miniapp`. Имя сервиса `yl26-dashboard`, не `dashboard`: короткое имя на общем демоне занято чужим проектом. Второй стек на том же Docker не копирует этот файл как есть. Публикация порта на loopback, своё `container_name` и тома — в `docker-compose.override.yml` **этого** каталога. Файл в git не входит (`.gitignore`). Правка общего `docker-compose.yml` подняла бы юлидовские имена на каждом стеке, который тянет репозиторий.

Сеть `edge` (`172.31.0.0/16`) объявлена как `external`. Без неё `docker compose up` не стартует, даже если трафик наружу идёт не через неё:

```bash
docker network create --subnet 172.31.0.0/16 edge
```

Один раз на демон. В `edge` входят `yl26-dashboard` и `miniapp`. Сервис `bot` — нет.

Внутри контейнера дашборд слушает `8000` (`/health`), Mini App — `8001` (`/app/health`). Это порты процесса, не публикация на хост. Какой loopback-порт какому событию принадлежит, в общем compose не записан — только в DEPLOY-DOMAIN.

## Как собирается

workflow выкладки в `.github/workflows/` нет. [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) на push и pull request в `main` гоняет pytest (Python 3.11 и 3.12) и проверяет, что в новых коммитах нет строки `Claude-Session:`. Образы он не собирает и на сервер ничего не отправляет. 3.11 — та же база, что `FROM python:3.11-slim` в обоих Dockerfile.

Сборка ручная, из каталога стека, и только когда `git diff-index --quiet HEAD` молчит (неотслеживаемые файлы и gitignore эта команда не считает).

1. Закоммитить и отправить на ту ветку, которую сервер забирает `git pull`. URL remote в репозитории не зафиксирован. <!-- VERIFY: конкретный remote и ветка checkout на сервере в git не записаны; workflow тестов смотрит ветку main -->
2. На сервере зайти в каталог **этого** стека и убедиться, что `pwd` верный.
3. `git pull`. Грязные отслеживаемые правки сначала убрать в коммит или отложить, а не собирать поверх них.
4. Дождаться тишины в анкетах, если пересборка заденет бота на живой регистрации (ниже).
5. Собрать и поднять, проставив метку коммита:

```bash
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build
```

6. Сверить запущенное с диском:

```bash
python tools/check_prod_git.py
```

Код 0 — дерево чистое и у `bot`, `miniapp`, `yl26-dashboard` метка `org.opencontainers.image.revision` равна `HEAD`. Код 1 — расхождение. Скрипт ничего не пересобирает и секреты не печатает. Пустая метка и значение `unknown` считаются провалом: так бывает, если `GIT_SHA` в окружение сборки не передали (в compose подставится `unknown`).

`docker compose up` без профиля `i18n` не запускает `libretranslate`. Поднимать его вручную (`docker compose --profile i18n up -d libretranslate`) для обычной выкладки не нужно: перевод по умолчанию внутри процесса бота, а сервис `bot` в сеть `edge`, где сидит сайдкар, не входит.

Что попадает в образ. Корневой Dockerfile ставит `requirements.txt`, копирует дерево от пользователя `appuser` (UID/GID 1000) и запускает `python main.py`. [`.dockerignore`](../.dockerignore) не пускает в слой `.env`, `google_credentials.json`, `.git`, `data/`, `logs/`, `tests/`, `docs/`, `refs/`, `tunnel/`. Секрет, попавший в слой, из контейнера уже не стереть. Дашборд — другой образ: только `dashboard/requirements.txt`, каталоги `dashboard/` и `shared/`, растры `miniapp/static/pattern/`. Кода бота там нет.

Три сервиса — три процесса. Пересборка бота Mini App и дашборд не обновляет.

| Задача | Команда |
|---|---|
| Весь стек из чистого HEAD | `GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build` |
| Только бот, после тихой минуты | `tools/deploy_bot.sh` — внутри `docker compose up -d --build bot`. `GIT_SHA` скрипт сам не выставляет: без него в окружении метка будет `unknown`, и `check_prod_git.py` этот сервис не примет |
| Только Mini App | `docker compose up -d --build --no-deps miniapp` |
| Не ждать тишины | `FORCE=1 tools/deploy_bot.sh` — осознанно. Окно ожидания: `QUIET` (по умолчанию 120 с), `MAX_WAIT` (900 с) |

`tools/deploy_bot.sh` сначала зовёт `tools/quiet_minute.py` по `data/forum.db` (только чтение). Код 0 — в черновиках анкеты не было свежих правок, можно пересобирать бота. Код 2 — тишины не дождались. Код 1 — база недоступна. Состояние диалога бота хранится в `fsm_storage` той же базы (`database/fsm_storage.py`); скрипт тишины смотрит не на него, а на `reg_drafts.updated_at`. Строка про «Продолжить» печатается только если `quiet_minute.py` вышел с ненулевым кодом, и тогда `deploy_bot.sh` заканчивается кодом 2, не собирая образ. `FORCE=1` пропускает `quiet_minute.py` без этого предупреждения и выкладывает сразу.

`miniapp` зависит от `bot` условием `service_started`, не `service_healthy`. Здоровье бота — это ответ Telegram на getUpdates. Ждать его значило бы не поднять Mini App без сети. Пока схема базы не догнана, `/app/health` отвечает 503 само.

После сборки имеет смысл глянуть процессы:

```bash
docker compose ps
```

У `bot` и `yl26-dashboard` ожидается `running`. У Mini App свой healthcheck на `http://127.0.0.1:8001/app/health`.

## Окружение на сервере

Окружения — это разные каталоги, не файлы `.env.production` / `.env.development`. Их в репозитории нет. У каждого каталога свой `.env`, свой `./data` и свой `DASHBOARD_SESSION_SECRET`. Один секрет на два стека значил бы, что cookie одного события подходит к другому.

Перед первым `up`:

1. Скопировать [`.env.example`](../.env.example) в `.env`. В git файл не входит.
2. Положить `google_credentials.json` рядом с compose, если нужна таблица. Файл в образ не копируется, контейнеры бота и Mini App монтируют его `:ro`. Без файла и без ID таблицы бот работает на SQLite.
3. Создать каталоги данных и отдать их UID 1000 (пароль sudo на описанном сервере не используется; приём из DEPLOY-DOMAIN):

```bash
mkdir -p data logs
docker run --rm -v "$PWD/data:/d" -v "$PWD/logs:/l" alpine chown -R 1000:1000 /d /l
```

Ключ Google тоже должен читаться UID 1000. Нечитаемый ключ процесс не роняет: отсутствие файла и ошибка чтения в `services/sheets` — fail-soft. `unable to open database file` — ошибка тома `./data` и WAL в `docker-compose.yml`, не ключа.

Без этих переменных соответствующий процесс не начинает работу (тексты ошибок — в [CONFIGURATION.md](CONFIGURATION.md)):

| Процесс | Без чего не стартует |
|---|---|
| `bot` и `miniapp` | `BOT_TOKEN`, `ADMIN_IDS`. Mini App дополнительно требует `DASHBOARD_SESSION_SECRET` |
| `yl26-dashboard` | `DASHBOARD_SESSION_SECRET`. Токен бота образ дашборда для старта не требует; без него кнопка входа всё равно собирается, если заданы `public_url` и `bot_username` (`dashboard/templates/login.html`). `BOT_TOKEN` нужен позже, в `verify_login_payload` |
| Коннектор из `tunnel/` | `TUNNEL_TOKEN` в `tunnel/.env`. На сервере бота этот compose не запускают — см. DEPLOY-DOMAIN |

Для уже опубликованного дашборда в `.env` стека ещё нужны `DASHBOARD_PUBLIC_URL`, `DASHBOARD_BOT_USERNAME`, `DASHBOARD_TRUSTED_PROXIES=172.31.0.0/16` и `DASHBOARD_DB_PATH`. Набор для теста Юлида и отличия второго события (свой секрет, свой override, свой поддомен) — DEPLOY-DOMAIN, шаги 4 и «Второй ивент». Секрет сессии генерируют на сервере и в переписку не кладут:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Домен кнопки входа менеджера задаётся в BotFather (`/setdomain`) и должен совпасть с `DASHBOARD_PUBLIC_URL`. Кнопки `web_app` самого приложения этого шага не требуют.

`GIT_SHA` — аргумент сборки, не ключ `.env`. На поведение бота не влияет, только на метку образа.

Живые токены, пароли прокси, app-password Nextcloud и токен туннеля в репозитории не лежат. <!-- VERIFY: значения секретов и публичный домен прода задаются на сервере и в панели Cloudflare, не в git -->

## Откат

Автоматического отката в CI нет. Образы не тегируются в реестре: предыдущая выкладка — это предыдущий коммит, собранный заново на сервере. Локальные слои Docker сами по себе процедурой отката не считаются.

Код и данные откатываются по-разному. `./data` (в том числе `forum.db`, `forum.db-wal`, `jobs.sqlite`) и `./logs` — тома хоста. Новый образ их не затирает и старый образ их не возвращает. Старт бота схему только добавляет (`init_db`, новые колонки). Обратной миграции в репозитории нет: откат коммита не откатывает уже записанные строки и не снимает добавленные колонки.

1. Не копировать файлы в контейнер и не чинить прод правкой дерева без коммита.
2. Если откат рискованный для базы — снять копию вместе с WAL, как в DEPLOY-DOMAIN (шаг 8). Префикс ниже — пример имени, не команда «на все случаи»:

```bash
cp data/forum.db data/forum.db.bak-$(date +%F)
cp data/forum.db-wal data/forum.db-wal.bak-$(date +%F) 2>/dev/null || true
```

3. В каталоге стека перейти на уже существующий коммит (`git checkout` этого коммита или `git pull` после того, как нужный коммит оказался на отслеживаемой ветке). Дерево отслеживаемых файлов должно быть чистым.
4. Собрать заново с меткой этого коммита:

```bash
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build
```

5. `python tools/check_prod_git.py` должен закончиться кодом 0. Пока пересобран не весь набор `bot` / `miniapp` / `yl26-dashboard`, проверка будет красной, даже если один сервис уже на нужном коммите.
6. Супердашборд — отдельный каталог. Его останов (`docker compose down` в том каталоге) ботов и дашборды событий не трогает. Точная команда и что останется в DNS — раздел «Супердашборд» в [DEPLOY-DOMAIN.md](DEPLOY-DOMAIN.md).

Откат правила в Cloudflare или строки релея этим репозиторием не делается. Симптомы 502, не того дашборда и сломанного `/app` — таблица «Если не получилось» в DEPLOY-DOMAIN.

## Наблюдение

Sentry, Datadog, New Relic и OpenTelemetry в зависимостях и конфигах нет. Наружу логи не отправляются. <!-- VERIFY: внешняя панель алертов, если она есть, в репозитории не описана -->

Живость процесса смотрит Docker healthcheck. `restart: always` поднимает контейнер заново, когда процесс **вышел**. Статус `unhealthy` сам по себе контейнер не перезапускает.

| Сервис | Проверка | Порог |
|---|---|---|
| `bot` | `python -m services.heartbeat --check`. Файл в temp контейнера обновляется раз в 30 с, только пока `getUpdates` отвечает. Нет файла или ему больше 120 с — exit 1 | interval 60 с, start-period 60 с, timeout 5 с, 3 попытки |
| `yl26-dashboard` | `GET http://127.0.0.1:8000/health` должен вернуть 200 | interval 60 с, start-period 20 с, timeout 5 с, 3 попытки |
| `miniapp` | `GET http://127.0.0.1:8001/app/health` (healthcheck образа бота здесь заменён: тот проверял бы long polling и всегда был бы красным) | те же 60 / 20 / 5 / 3 |

Статус:

```bash
docker inspect --format '{{.State.Health.Status}}' <container>
```

Имя контейнера дашборда Юлида в базовом compose — `youlead26-dashboard`, Mini App — `youlead26-miniapp`. У бота `container_name` не задан, имя выдаёт Compose.

Логи контейнеров — драйвер `json-file`, `max-size` 10 МБ, `max-file` 3, у каждого сервиса свои. Дальше Docker старые файлы выкидывает.

| Куда смотреть | Что там |
|---|---|
| `docker compose logs bot` | stdout бота: корневой логгер — `WARNING` и выше, но `chat_recon` пишет в stdout ещё и `INFO` |
| `logs/bot.log` на томе | тот же бот, уровень из `LOG_LEVEL` (по умолчанию `INFO`), ротация 10 МБ × 5 |
| `logs/chat_recon.log` | отдельный файл разведки чата, 2 МБ × 3 |
| `docker compose logs miniapp` | приложение Mini App пишет в stdout, не в файл на томе `./logs` |
| `docker compose logs yl26-dashboard` | stdout дашборда |

Совпадение кода с тем, что реально запущено, — снова `python tools/check_prod_git.py`. Это не мониторинг Telegram и не аптайм домена.

Туннель снаружи (HEALTHY в Zero Trust, 502 на публичном имени, заголовки `/app/health` и `/login`) проверяется по таблице в [DEPLOY-DOMAIN.md](DEPLOY-DOMAIN.md). Из репозитория эти проверки не запускаются.
