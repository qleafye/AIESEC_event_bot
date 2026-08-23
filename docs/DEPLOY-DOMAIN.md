# Дашборд и Nextcloud на своём домене — инструкция владельцу

Что получится в итоге: `https://yl26.<домен>` открывает дашборд статистики, `https://cloud.<домен>` —
Nextcloud с резюме, оба с нормальным сертификатом, и при этом **на сервере не открыт ни один
входящий порт**. Трафик приходит через Cloudflare Tunnel: контейнер `cloudflared` сам
устанавливает исходящее соединение к Cloudflare, а Cloudflare терминирует HTTPS и передаёт
запросы внутрь docker-сетей по простому HTTP.

Зачем домен вообще: кнопка входа в дашборд (Telegram Login Widget) работает только по HTTPS на
домене, зарегистрированном в BotFather. IP-адрес и самоподписанный сертификат не подходят.

## Где что

- Сервер: `ssh leaf@100.106.218.33` (Tailscale). Docker работает без sudo; ufw/iptables трогать
  не нужно и нечем — пароля sudo нет, и он не требуется.
- **Сначала — тест-стенд** `/home/leaf/YouLead26-test/` (бот `@YouLead_test_bot`), решение D-20.
  Прод `/home/leaf/YouLead26/` — только после того, как всё проверено на тесте. Перед любым
  `docker compose up` — `pwd`, чтобы не поднять не тот стек.
- Туннель: `/home/leaf/tunnel/` — уже создан, `cloudflared` работает, токен лежит в его `.env`.
  Шаг 2 ниже — для нового сервера или если туннель пришлось пересоздать.
- Сеть `edge` — уже создана с подсетью `172.31.0.0/16` (не `.30` — она занята тест-стеком).
- Имя `dashboard` на сервере занято чужим проектом (`/home/leaf/dashboard/`, сеть
  `dashboard_default`). Наш сервис — `yl26-dashboard`, контейнер `youlead26-dashboard`.

Плейсхолдеры: `<домен>` — ваш домен в Cloudflare (например `alekseev.info`), `<IP>` — адрес
сервера, с которого сейчас раздаётся Nextcloud.

---

## Шаг 1. Сеть `edge` (один раз на сервер)

```bash
docker network create --subnet 172.31.0.0/16 edge
```

Подсеть фиксируется не просто так: на неё настроен `DASHBOARD_TRUSTED_PROXIES` — дашборд верит
заголовкам `X-Forwarded-Proto/For` только от адресов из этой подсети, то есть только от
`cloudflared`. Если подсеть другая — поменять и её, и значение в `.env` стека.

**Проверка:** `docker network ls | grep edge` показывает сеть. Если она уже есть (наш случай) —
команда скажет `already exists`, это нормально; убедиться, что подсеть та:
`docker network inspect edge --format '{{(index .IPAM.Config 0).Subnet}}'` → `172.31.0.0/16`.

## Шаг 2. Туннель в Cloudflare Zero Trust

Уже сделано на leafye — пропустить, если `docker ps | grep cloudflared` показывает контейнер.

1. Cloudflare → Zero Trust → **Networks → Tunnels → Create a tunnel → Cloudflared**, имя
   например `leafye`. На экране установки скопировать токен — длинную строку после
   `cloudflared service install`. Сам установщик НЕ запускать.
2. На сервере:
   ```bash
   mkdir -p /home/leaf/tunnel && cd /home/leaf/tunnel
   # сюда — docker-compose.yml из каталога tunnel/ репозитория (scp или git show > файл)
   printf 'TUNNEL_TOKEN=%s\n' 'ВСТАВИТЬ_ТОКЕН' > .env
   docker compose up -d
   ```
   Токен — полноценный секрет: кто им владеет, тот подключит свой контейнер к туннелю.
   Он живёт только в этом `.env`, в репозиторий не попадает.

**Проверка:** в панели CF туннель показывает **HEALTHY**; `docker compose logs cloudflared`
содержит `Registered tunnel connection` и не содержит `Unauthorized`.

## Шаг 3. Public hostnames (это ещё не сделано)

В том же туннеле → вкладка **Public Hostname → Add a public hostname**, две записи:

| Subdomain | Domain   | Type   | URL                   |
|-----------|----------|--------|-----------------------|
| `yl26`    | `<домен>`| `HTTP` | `yl26-dashboard:8000` |
| `cloud`   | `<домен>`| `HTTP` | `nextcloud-app:80`    |

URL — имя docker-сервиса и порт, без `http://` и без слэша. Ни `Path`, ни дополнительные
настройки не нужны. Cloudflare сам создаст CNAME-записи в DNS домена — руками в DNS ничего
добавлять не нужно; **оранжевое облако здесь штатно** (оно и есть путь через туннель).

Для тест-стенда, если дашборд поднимается там, — свой поддомен (например `yl26-test`) и своё имя
сервиса; на момент написания на тест идёт именно `yl26.<домен>` (D-20), прод получит домен позже.

**Проверка:** в разделе DNS домена появились две CNAME `yl26` и `cloud` вида `<id>.cfargotunnel.com`.
Пока стек не поднят, `https://yl26.<домен>` отдаёт 502 от Cloudflare — это ожидаемо до шага 4.

## Шаг 4. Стек бота: sidecar дашборда

В каталоге стека (**сначала тест**: `cd /home/leaf/YouLead26-test && pwd`) дописать в `.env`:

```
DASHBOARD_PUBLIC_URL=https://yl26.<домен>
DASHBOARD_SESSION_SECRET=<длинная случайная строка>
DASHBOARD_BOT_USERNAME=YouLead_test_bot
DASHBOARD_TRUSTED_PROXIES=172.31.0.0/16
DASHBOARD_DB_PATH=/app/data/forum.db
```

Секрет сгенерировать так: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
Свой на каждый стек, смена значения разлогинивает всех.

Затем:

```bash
git pull
docker compose up -d --build
```

Дашборд работает от UID 1000 и читает `data/` только на чтение; если папки создавались от root,
права поправить без sudo одноразовым контейнером (проверенный на этом сервере приём):

```bash
mkdir -p data logs
docker run --rm -v "$PWD/data:/d" -v "$PWD/logs:/l" alpine chown -R 1000:1000 /d /l
```

**Проверка:** `docker compose ps` — `bot` и `yl26-dashboard` в статусе `running`;

```bash
docker compose exec yl26-dashboard python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
```

печатает `200`; `https://yl26.<домен>/login` открывается в браузере со страницей входа.

## Шаг 5. Nextcloud за туннелем (на тесте пропускается)

Стек Nextcloud живёт на сервере, не в репозитории. Найти его каталог:

```bash
docker inspect nextcloud-app --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

Сети трогать не нужно: `nextcloud-app` уже в `nextcloud_internal`, к которой подключён
`cloudflared`. Настраивается сам Nextcloud через `occ` (из того каталога):

```bash
docker compose exec -u www-data nextcloud-app php occ config:system:set trusted_domains 1 --value=cloud.<домен>
docker compose exec -u www-data nextcloud-app php occ config:system:set overwritehost --value=cloud.<домен>
docker compose exec -u www-data nextcloud-app php occ config:system:set overwriteprotocol --value=https
docker compose exec -u www-data nextcloud-app php occ config:system:set overwrite.cli.url --value=https://cloud.<домен>
```

Без `overwritehost`/`overwriteprotocol` Nextcloud за прокси, который терминирует TLS, начнёт
собирать внутренние ссылки с `http://`, и браузер заблокирует их как смешанное содержимое.
Индекс `1` в `trusted_domains` — следующий свободный; посмотреть текущие:
`... php occ config:system:get trusted_domains`.

**Проверка:** `https://cloud.<домен>` открывается без предупреждения браузера и без баннера
«доступ через недоверенный домен»; ссылки на страницах — `https`.

## Шаг 6. BotFather: домен для кнопки входа

В BotFather: `/setdomain` → выбрать бота этого стека (на тесте `@YouLead_test_bot`) → ввести
ровно `https://yl26.<домен>` — без пути и без слэша в конце.

При смене домена шаг повторяется, иначе кнопка входа молча ничего не делает.

**Проверка:** на `https://yl26.<домен>/login` кнопка Telegram открывает окно подтверждения, а не
«Bot domain invalid».

## Шаг 7. `.env` стека: ссылки на новый Nextcloud (после шага 5)

```
NEXTCLOUD_PUBLIC_URL=https://cloud.<домен>
NEXTCLOUD_VERIFY_TLS=true
NEXTCLOUD_CA_BUNDLE=
```

Самоподписанный CA больше не нужен — сертификат выдаёт Cloudflare. **`NEXTCLOUD_WEBDAV_URL` не
трогать:** загрузка идёт изнутри по `http://nextcloud-app:80/...`, это быстрее и не зависит от
туннеля. Затем `docker compose up -d`.

**Проверка:** пробная загрузка резюме тестовым делегатом — ссылка в таблице и в карточке начинается
с `https://cloud.<домен>`.

## Шаг 8. Бэкап и перенос старых ссылок

Старые ссылки в БД смотрят на `https://<IP>:8443/...`. Сначала бэкап — **вместе с `-wal`**:

```bash
cp data/forum.db data/forum.db.bak-$(date +%F) && cp data/forum.db-wal data/forum.db-wal.bak-$(date +%F) 2>/dev/null || true
```

Точный старый префикс взять из БД, не по памяти:

```bash
docker compose exec bot python -c "import sqlite3; print(sqlite3.connect('data/forum.db').execute('select resume_url from users where resume_url is not null limit 3').fetchall())"
```

Сухой прогон, сверить цифры, потом боевой:

```bash
docker compose exec bot python -m scripts.backfill_nextcloud_urls --old-base https://<IP>:8443 --new-base https://cloud.<домен> --dry-run
docker compose exec bot python -m scripts.backfill_nextcloud_urls --old-base https://<IP>:8443 --new-base https://cloud.<домен>
```

Меняется только префикс; токен шары и имя файла остаются. Затем из бота пересобрать основную
вкладку таблицы — колонка «Резюме (ссылка)» подтянет новые адреса.

**Проверка:** повторный `--dry-run` показывает `Найдено: 0`; случайная старая ссылка из таблицы
открывается.

## Шаг 9. Закрыть лишнее

Когда оба адреса работают через туннель, в compose Nextcloud убрать публикацию портов у
`nextcloud-caddy` (`ports:` с `80` и `8443`) и поднять стек заново — снаружи они больше не нужны,
а сейчас 80 виден из интернета.

**Проверка:** `docker ps --format '{{.Names}} {{.Ports}}'` — ни одной строки с `0.0.0.0:`.

## Шаг 10. Проверка дашборда с точки зрения людей

- Из бота: ⚙️ Настройки → 📊 Статистика → кнопка «🌐 Открыть дашборд» ведёт на `https://yl26.<домен>`.
- Суперадмин входит и видит блоки.
- Человек без права `stats` видит страницу «нет доступа», а суперадминам приходит уведомление о
  запросе доступа.
- Права и блоки настраиваются в боте (⚙️ Настройки → 📊 Дашборд), не в файлах.

## Если не получилось

| Симптом | Причина | Как проверить / починить |
|---|---|---|
| Туннель не HEALTHY | неверный или отозванный токен | в `docker compose logs cloudflared` есть `Unauthorized` → взять токен заново, перезаписать `.env`, `docker compose up -d` |
| Адрес отдаёт 502 | origin не в одной сети с cloudflared или имя сервиса в URL написано не так | `docker network inspect edge` и `docker network inspect nextcloud_internal` — cloudflared должен быть в обеих, `youlead26-dashboard` в `edge`, `nextcloud-app` в `nextcloud_internal` |
| Открывается не тот дашборд / 502 | в public hostname написано `dashboard` вместо `yl26-dashboard` | на сервере есть чужой проект с именем `dashboard` — исправить URL в CF |
| Кнопка входа ничего не делает / «Bot domain invalid» | домен в BotFather не совпал с `DASHBOARD_PUBLIC_URL` | повторить шаг 6, сверить бота и адрес посимвольно |
| После входа снова страница входа | cookie не считается защищённой: дашборд не доверяет `X-Forwarded-Proto` | `DASHBOARD_TRUSTED_PROXIES` = подсеть сети `edge` (шаг 1), в логе дашборда нет WARNING про `*` |
| Nextcloud: «недоверенный домен» или ссылки по `http` | не сделан шаг 5 | выполнить `occ`-команды, перезагрузить страницу |
| `PermissionError` / `unable to open database file` | `data/` не принадлежит UID 1000 | `chown` через alpine-контейнер из шага 4 |

## Чего делать НЕ нужно

Старая схема с обратным прокси на сервере больше не используется, и её шаги теперь вредны:

- **не** заводить A-записи на IP сервера — DNS ведёт на туннель через CNAME, который создаёт Cloudflare;
- **не** переключать облако в «серое» (DNS only) — через серое облако туннель не работает;
- **не** открывать порты 80/443 на сервере и **не** ставить Caddy/nginx с Let's Encrypt — портов
  наружу нет, сертификат выдаёт Cloudflare;
- **не** класть в `tunnel/` никаких `config.yml`/`credentials.json` — туннель remotely-managed,
  всё в панели.
