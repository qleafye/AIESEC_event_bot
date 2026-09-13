"""Phase 30 (30-02, A2-03; хвост 30-08, A2-03): разовая выгрузка справочников ВУЗ/город в
офлайн-снапшоты `data/lookup/universities_ru.json` / `data/lookup/cities_ru.json` —
`lookup_entries` (`database/db.py`) стартует БЕЗ обращения к внешним API в рантайме
(30-CONTEXT.md решение владельца №1, «второе поверх 03.09»: ДаData/Getgeo не используем ни
в каком виде).

Запуск (сухой прогон по умолчанию — печатает статистику, файлы не трогает):

    python tools/seed_universities.py                 # живая выгрузка из Викиданных (30-02)
    python tools/seed_universities.py --apply          # записывает оба снапшота
    python tools/seed_universities.py --from-config     # без сети: вузы из config.UNIVERSITIES
    python tools/seed_universities.py --curated --apply # кураторская выгрузка (30-08, ниже)

Города СЕГОДНЯ всегда собираются офлайн-фолбэком (города бота + топ-8 `SELECT_CONFIG`) —
лицензия `arbaev/russia-cities` проверена и НЕ подтверждена (нет LICENSE в репозитории,
GitHub API возвращает `license: null` → по умолчанию все права защищены, копировать датасет
в репозиторий нельзя, см. `data/lookup/README.md`).

Методика выгрузки вузов (`--apply` без `--curated`) — 30-CONTEXT.md § «Решения по итогам
30-RESEARCH.md», п. 3 (ОБЯЗАТЕЛЬНА, не переизобретается здесь):
  (1) основной запрос — transitive-closure по подклассам `wdt:P31/wdt:P279* wd:Q38723`
      («higher education institution»), фильтр страны `wdt:P17`;
  (2) контрольная сверка — прямой запрос из 30-RESEARCH.md § Code Examples
      (`VALUES ?class { wd:Q3918 wd:Q38723 wd:Q1371037 }`, verified live 472 записи
      12.09.2026) — обе цифры (`count_base`/`count_closure`) пишутся в снапшот и в README;
  (3) закрытые/расформированные (`P576` задан) — исключаются, количество исключённых
      считается;
  (4) филиалы (`P749` — головная организация) — не отдельной записью, а псевдонимом
      головной, если головная попала в выборку.

Методика `--curated` (30-CONTEXT.md § «Хвост после 30-02: кураторский сид вузов», уточнена
планом 30-08) — тот же transitive-closure запрос, но с фильтром ПРЯМО в SPARQL, вместо
публикации сырых 1187 строк с мусором (30-02 нашла в них адреса, комитеты, факультеты без
`P749`):
  (1) обязательная русская метка (`rdfs:label` с `LANG(...) = "ru"`, НЕ запасной вариант
      label-сервиса, который тихо подставляет английскую метку);
  (2) исключение записей, чей `P31` — факультет/кафедра/филиал
      (`Q180958`/`Q2467461`/`Q1077036`, если такой класс встречается);
  (3) `P576`/`P749` — как в методике выше (исключить закрытые, филиалы — псевдонимом головной);
  (4) объём — ожидание 500–800 записей; допустимый диапазон 300–1500 — вне него снапшот НЕ
      перезаписывается (см. `main()`), результат фиксируется в README/SUMMARY, а не
      выдумывается.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows-консоль по умолчанию открывает stdout/stderr в cp1251 — кириллица в `--help`
# (argparse печатает докстринг модуля) и в статистике падает `UnicodeEncodeError` на первом
# же «→»/«—». `reconfigure` есть с Python 3.7, no-op при уже-UTF-8 потоке (Linux/Docker-прод).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "AIESECEventBot/1.0 (tools/seed_universities.py, one-off lookup seed)"
TIMEOUT_SECONDS = 90
COUNTRY_RUSSIA = "Q159"

LOOKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "lookup"

# Verified live 2026-09-12 (30-RESEARCH.md § Code Examples): 472 строки, HTTP 200, ~3.5с.
# Контрольная сверка объёма с transitive-closure запросом ниже — расхождение больше чем в
# полтора раза фиксируется в SUMMARY исполнителя, не замалчивается.
_CONTROL_QUERY_TEMPLATE = """
SELECT ?item ?itemLabel ?itemAltLabel WHERE {{
  VALUES ?class {{ wd:Q3918 wd:Q38723 wd:Q1371037 }}
  ?item wdt:P31 ?class ; wdt:P17 wd:{country} .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
"""

# Transitive-closure — основной запрос методики (п.1): подклассы Q38723, а не фиксированный
# список трёх классов — ловит вузы, заведённые в Wikidata под более специфичным подклассом
# (консерватория/семинария/академия конкретного профиля), которого нет в контрольном запросе.
# P576/P749 — опционально, разбираются в Python (исключение закрытых / псевдоним филиала).
_CLOSURE_QUERY_TEMPLATE = """
SELECT ?item ?itemLabel ?itemAltLabel ?dissolved ?parent ?parentLabel WHERE {{
  ?item wdt:P31/wdt:P279* wd:Q38723 .
  ?item wdt:P17 wd:{country} .
  OPTIONAL {{ ?item wdt:P576 ?dissolved }}
  OPTIONAL {{ ?item wdt:P749 ?parent }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
"""


RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 65  # 30-02 столкнулась ровно с этим окном outage-режима


def _sparql_get(query: str) -> dict:
    """30-02 нашла `query.wikidata.org` в режиме «активный outage, 1 запрос/минуту» — ретрай
    здесь встроен в скрипт (было — ручное ожидание исполнителем между вызовами), чтобы
    следующий менеджер/разработчик, запускающий `--curated` вручную, не столкнулся с тем же
    вручную. HTTP 429 — единственный код, который повторяем; прочие ошибки сети/HTTP
    поднимаются сразу (решает вызывающий `main()` — офлайн-фолбэк)."""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{WIKIDATA_ENDPOINT}?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == RATE_LIMIT_MAX_RETRIES:
                raise
            wait_s = RATE_LIMIT_DEFAULT_WAIT_SECONDS
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                try:
                    wait_s = max(wait_s, int(retry_after))
                except ValueError:
                    pass
            print(
                f"HTTP 429 (rate-limit), попытка {attempt}/{RATE_LIMIT_MAX_RETRIES} — "
                f"жду {wait_s}с…",
                file=sys.stderr,
            )
            time.sleep(wait_s)
    raise AssertionError("unreachable")  # цикл всегда либо возвращает, либо бросает выше


def _rows(payload: dict) -> list[dict]:
    return payload.get("results", {}).get("bindings", [])


def _split_alt_labels(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _qid(uri: str | None) -> str | None:
    if not uri:
        return None
    return uri.rsplit("/", 1)[-1]


def _reduce_closure_rows(rows: list[dict], source_label: str) -> tuple[list[dict], int, int]:
    """Общая часть методики п.3-4 (закрытые исключаются, филиалы — псевдонимом головной),
    переиспользуется живой (`fetch_universities_from_wikidata`) и кураторской
    (`fetch_universities_curated`) выгрузкой — единственное место, где правило применяется,
    вторая копия не заводится. Возвращает `(items, excluded_dissolved, merged_branches)`."""
    by_qid: dict[str, dict] = {}
    dissolved_qids: set[str] = set()
    branch_of: dict[str, str] = {}

    for row in rows:
        qid = _qid(row.get("item", {}).get("value"))
        if not qid:
            continue
        label = row.get("itemLabel", {}).get("value") or qid
        alt = _split_alt_labels(row.get("itemAltLabel", {}).get("value"))
        if row.get("dissolved"):
            dissolved_qids.add(qid)
        parent_qid = _qid(row.get("parent", {}).get("value"))
        if parent_qid:
            branch_of[qid] = parent_qid
        entry = by_qid.setdefault(qid, {"canonical": label, "aliases": set()})
        entry["aliases"].update(alt)

    excluded_dissolved = 0
    merged_branches = 0
    kept: list[dict] = []
    for qid, entry in by_qid.items():
        if qid in dissolved_qids:
            excluded_dissolved += 1
            continue
        parent_qid = branch_of.get(qid)
        if parent_qid and parent_qid in by_qid and parent_qid not in dissolved_qids:
            # Методика п.4: филиал — псевдоним головной, не отдельная запись.
            parent_entry = by_qid[parent_qid]
            parent_entry["aliases"].add(entry["canonical"])
            parent_entry["aliases"].update(entry["aliases"])
            merged_branches += 1
            continue
        kept.append(entry)

    items = [
        {
            "canonical": entry["canonical"],
            "aliases": sorted(a for a in entry["aliases"] if a and a != entry["canonical"]),
            "source": source_label,
            "dissolved": False,
        }
        for entry in kept
    ]
    items.sort(key=lambda x: x["canonical"])
    return items, excluded_dissolved, merged_branches


def fetch_universities_from_wikidata(country: str = COUNTRY_RUSSIA) -> dict:
    """Живая выгрузка (методика п.1-4). Бросает `urllib.error.URLError`/`TimeoutError`/
    `OSError`, если сеть недоступна — вызывающий (`main`) решает откатиться на
    `_universities_from_config`, план не блокируется отсутствием сети у исполнителя."""
    control_payload = _sparql_get(_CONTROL_QUERY_TEMPLATE.format(country=country))
    count_base = len(_rows(control_payload))

    closure_payload = _sparql_get(_CLOSURE_QUERY_TEMPLATE.format(country=country))
    closure_rows = _rows(closure_payload)
    count_closure = len(closure_rows)

    items, excluded_dissolved, _merged = _reduce_closure_rows(closure_rows, "wikidata")

    return {
        "items": items,
        "count_base": count_base,
        "count_closure": count_closure,
        "excluded_dissolved": excluded_dissolved,
        "source_query": _CLOSURE_QUERY_TEMPLATE.format(country=country).strip(),
    }


# 30-08 (хвост 30-02): диапазон валидности объёма кураторской выгрузки. 500-800 — ожидание
# методики; 300-1500 — жёсткая граница, вне которой результат не публикуется (см. main()).
CURATED_EXPECTED_MIN = 500
CURATED_EXPECTED_MAX = 800
CURATED_VOLUME_MIN = 300
CURATED_VOLUME_MAX = 1500

# Q180958 — факультет (faculty), Q2467461 — кафедра (academic department), Q1077036 —
# приведён планом «если есть» (запасной Q-код на случай, если в Wikidata встретится под этим
# идентификатором ещё один структурный класс подразделения вуза; FILTER NOT EXISTS с
# несуществующим Q-кодом просто не матчит ни одной строки, ошибки не бросает).
_CURATED_BAD_CLASSES = ("wd:Q180958", "wd:Q2467461", "wd:Q1077036")

_CURATED_QUERY_TEMPLATE = """
SELECT ?item ?itemLabel ?itemAltLabel ?dissolved ?parent ?parentLabel WHERE {{
  ?item wdt:P31/wdt:P279* wd:Q38723 .
  ?item wdt:P17 wd:{country} .
  FILTER EXISTS {{ ?item rdfs:label ?ruLabel . FILTER(LANG(?ruLabel) = "ru") }}
  FILTER NOT EXISTS {{
    ?item wdt:P31 ?badClass .
    VALUES ?badClass {{ {bad_classes} }}
  }}
  OPTIONAL {{ ?item wdt:P576 ?dissolved }}
  OPTIONAL {{ ?item wdt:P749 ?parent }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
"""


def fetch_universities_curated(country: str = COUNTRY_RUSSIA) -> dict:
    """Кураторская выгрузка (30-CONTEXT.md «Хвост после 30-02», уточнено планом 30-08) — та
    же transitive-closure методика, но с двумя дополнительными фильтрами ПРЯМО в SPARQL
    (обязательная русская метка, исключение факультета/кафедры/филиала как самостоятельной
    записи), а не постобработкой уже готового мусора в Python. `P576`/`P749` — та же общая
    часть методики (`_reduce_closure_rows`), что и у `fetch_universities_from_wikidata`.

    Бросает `urllib.error.URLError`/`HTTPError`/`TimeoutError`/`OSError`, если сеть
    недоступна — `main()` не перезаписывает снапшот и объясняет причину, не выдумывая
    данные (тот же принцип, что офлайн-фолбэк обычной живой выгрузки)."""
    query = _CURATED_QUERY_TEMPLATE.format(
        country=country, bad_classes=" ".join(_CURATED_BAD_CLASSES)
    )
    payload = _sparql_get(query)
    rows = _rows(payload)
    count_curated_raw = len(rows)

    items, excluded_dissolved, merged_branches = _reduce_closure_rows(rows, "wikidata_curated")

    return {
        "items": items,
        "count_base": 0,
        "count_closure": count_curated_raw,
        "excluded_dissolved": excluded_dissolved,
        "merged_branches": merged_branches,
        "source_query": query.strip(),
        "curated": True,
    }


# 30-08 задача B (30-CONTEXT.md Open Question 2 — «более полный источник городов с чистой
# лицензией»): Викиданные CC0 — тот же принцип, что и вузы, но БЕЗ transitive-closure (город —
# листовой класс Q7930989, не иерархия подклассов) и с порогом населения `P1082 ≥ 50 тыс.`
# (задача B, дословно). Диапазон валидности объёма — по факту живого прогона 13.09.2026 (323
# записи) с запасом на колебание данных Wikidata между прогонами; вне диапазона — снапшот
# городов НЕ трогаем (тот же принцип «не перезаписывать», что и у вузов).
CITIES_VOLUME_MIN = 100
CITIES_VOLUME_MAX = 600
CITIES_POPULATION_MIN = 50000
CITY_CLASS_RUSSIA = "Q7930989"

_CITIES_QUERY_TEMPLATE = """
SELECT ?item ?itemLabel ?itemAltLabel (MAX(?pop) AS ?maxpop) WHERE {{
  ?item wdt:P31 wd:{city_class} .
  ?item wdt:P1082 ?pop .
  FILTER EXISTS {{ ?item rdfs:label ?ruLabel . FILTER(LANG(?ruLabel) = "ru") }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
GROUP BY ?item ?itemLabel ?itemAltLabel
HAVING (MAX(?pop) >= {population_min})
"""


def fetch_cities_curated(city_class: str = CITY_CLASS_RUSSIA, population_min: int = CITIES_POPULATION_MIN) -> dict:
    """Кураторская выгрузка городов (30-08 задача B) — Викиданные, класс «город России»
    (`Q7930989`), порог населения `P1082 ≥ {population_min}`, обязательная русская метка.
    Данные Wikidata — CC0 (см. `data/lookup/README.md`), в отличие от непроверенной лицензии
    `arbaev/russia-cities` (30-02). Без transitive-closure и без P576/P749-обработки — город
    не имеет ни «закрытых» версий, ни «филиалов» в смысле методики вузов; `P1082` может иметь
    несколько значений (переписи разных лет) — берём максимум через `MAX(?pop)`/`GROUP BY`,
    порог достаточно перевалить хоть одним значением.

    Бросает `urllib.error.URLError`/`HTTPError`/`TimeoutError`/`OSError` при недоступности
    сети — `main()` в этом случае оставляет `cities_ru.json` нетронутым (задача B: «при
    неудаче — оставить как есть»)."""
    query = _CITIES_QUERY_TEMPLATE.format(city_class=city_class, population_min=population_min)
    payload = _sparql_get(query)
    rows = _rows(payload)

    seen: set[str] = set()
    items: list[dict] = []
    for row in rows:
        qid = _qid(row.get("item", {}).get("value"))
        if not qid or qid in seen:
            continue
        seen.add(qid)
        label = row.get("itemLabel", {}).get("value") or qid
        alt = _split_alt_labels(row.get("itemAltLabel", {}).get("value"))
        items.append(
            {
                "canonical": label,
                "aliases": sorted(a for a in set(alt) if a and a != label),
                "source": "wikidata_curated",
                "dissolved": False,
            }
        )
    items.sort(key=lambda x: x["canonical"])

    return {
        "items": items,
        "count_base": 0,
        "count_closure": len(items),
        "excluded_dissolved": 0,
        "source_query": query.strip(),
        "curated": True,
        "license": "CC0 (Wikidata)",
    }


def _universities_from_config() -> dict:
    """Офлайн-фолбэк (задача 3, «если сеть недоступна — не выдумывать данные и не откладывать
    план»): снапшот из `config.UNIVERSITIES` (11 строк дефолта бота), без псевдонимов —
    `count_base`/`count_closure` = 0, объяснено в README как «сид не выполнен полностью»."""
    from config import config

    items = [
        {"canonical": name, "aliases": [], "source": "config_fallback", "dissolved": False}
        for name in config.UNIVERSITIES
    ]
    return {
        "items": items,
        "count_base": 0,
        "count_closure": 0,
        "excluded_dissolved": 0,
        "source_query": (
            "офлайн-фолбэк: config.UNIVERSITIES (сеть до query.wikidata.org недоступна "
            "на момент выгрузки — см. data/lookup/README.md)"
        ),
    }


def cities_fallback_snapshot() -> dict:
    """Города СЕГОДНЯ всегда фолбэк, не живой запрос (лицензия `arbaev/russia-cities` не
    подтверждена — README): каноники — города текущих мероприятий бота (реестр `cities.py`,
    дата в подписи отрезается по первой запятой) + восемь городов топ-8 из
    `reg_engine.SELECT_CONFIG['city']` (сегодняшний список кнопок делегата на шаге «Город»).
    Псевдонимов нет — короткая база, поиск по ней работает просто на меньшем числе записей
    (задача 3, «поиск города при этом работает»).

    Города берутся через `cities.all_cities()` — тот же публичный аксессор кэша `CITIES`,
    которым пользуются `handlers/admin_cities.py` и `handlers/registration.py`, а не
    прямым чтением .env-значения (сторож `tests/test_cities_registry_260818.py` запрещает
    читать это значение где-либо, кроме `cities.py`). Скрипт не вызывает `reload_cities()`,
    поэтому список — тот же холодный фолбэк из `.env`, что и раньше, если БД недоступна/пуста."""
    import cities as cities_module
    import reg_engine

    names: list[str] = []
    for entry in cities_module.all_cities():
        name = entry["label"].split(",")[0].strip()
        if name:
            names.append(name)
    _, select_default = reg_engine.SELECT_CONFIG["city"]
    names.extend(select_default)

    seen: set[str] = set()
    items: list[dict] = []
    for name in names:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append({"canonical": name, "aliases": [], "source": "bot_cities_fallback", "dissolved": False})

    return {
        "items": items,
        "count_base": 0,
        "count_closure": 0,
        "excluded_dissolved": 0,
        "source_query": (
            "офлайн-фолбэк: города бота (cities.all_cities()) + SELECT_CONFIG['city'] — "
            "лицензия arbaev/russia-cities не подтверждена (см. data/lookup/README.md)"
        ),
    }


def _print_stats(label: str, stats: dict) -> None:
    items = stats["items"]
    with_aliases = sum(1 for i in items if i["aliases"])
    print(f"— {label} —")
    print(f"Записей: {len(items)}, с непустыми псевдонимами: {with_aliases}")
    print(
        f"count_base={stats['count_base']} count_closure={stats['count_closure']} "
        f"excluded_dissolved={stats['excluded_dissolved']}"
    )
    print("Первые 5:")
    for item in items[:5]:
        alias_preview = ", ".join(item["aliases"][:5])
        print(f"  {item['canonical']}" + (f" — {alias_preview}" if alias_preview else ""))
    print()


def _write_snapshot(path: Path, stats: dict, fetched_at: str) -> None:
    meta = {
        "source_query": stats["source_query"],
        "count_base": stats["count_base"],
        "count_closure": stats["count_closure"],
        "excluded_dissolved": stats["excluded_dissolved"],
        "fetched_at": fetched_at,
    }
    # Поля кураторской выгрузки — необязательное расширение схемы, ничего не потребляет их
    # обязательно (`database.db.seed_lookup_from_snapshot` читает только `items`), но полезны
    # человеку, читающему JSON/`data/lookup/README.md`.
    for optional_key in ("merged_branches", "curated", "license"):
        if optional_key in stats:
            meta[optional_key] = stats[optional_key]
    payload = {"meta": meta, "items": stats["items"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# 30-08 задача B, акс. критерий «выборочная проверка 20 записей (10 известных вузов —
# найдены по сокращению?)». Сокращение ищется ТОЧНЫМ совпадением (casefold) целой строки
# alias/canonical, не подстрокой — подстрока даёт ложные срабатывания («МГУ» — подстрока
# внутри «АмГУ», сокращения Амурского гос. университета) и не отражает, как реально ищет
# делегат (`services.lookup.normalize_alias`/`search_lookup` работают со строкой целиком,
# не с произвольной подстрокой внутри чужого алиаса).
KNOWN_UNIVERSITY_ABBREVIATIONS = [
    "МГУ", "СПбГУ", "ВШЭ", "МФТИ", "МГТУ", "ИТМО", "ЛЭТИ", "ТюмГУ", "НГУ", "УрФУ",
]


def check_known_universities(items: list[dict], abbreviations: list[str]) -> list[tuple[str, bool, str]]:
    """Возвращает `(сокращение, найдено, каноника-если-найдено)` для каждого сокращения —
    используется и выводом скрипта, и SUMMARY исполнителя (акс. критерий задачи B)."""
    result: list[tuple[str, bool, str]] = []
    for abbr in abbreviations:
        needle = abbr.casefold()
        match = ""
        for item in items:
            haystack = [item["canonical"], *item["aliases"]]
            if any(needle == h.casefold() for h in haystack):
                match = item["canonical"]
                break
        result.append((abbr, bool(match), match))
    return result


def find_alias_collisions(items: list[dict]) -> list[tuple[str, list[str]]]:
    """Один нормализованный псевдоним (`services.lookup.normalize_alias`) у нескольких разных
    каноник — при посеве в `lookup_entries` (`UNIQUE INDEX (kind, alias_norm)`) выживет
    только первая по алфавиту вставка, остальные молча проигнорирует `INSERT OR IGNORE`
    (30-08 задача B, найдено при выборочной проверке: «МГУ» — легальное сокращение и у
    Московского, и у Морского государственного университета одновременно). Методика
    фильтрации Викиданных это не устраняет — данные реальные, коллизия языковая, не баг
    скрипта; диагностика для README/SUMMARY, решается вручную через экран менеджера
    «Справочники» (мёрдж-очередь), если конкретная коллизия окажется проблемой на практике."""
    from services.lookup import normalize_alias

    seen: dict[str, list[str]] = {}
    for item in items:
        for alias in [item["canonical"], *item["aliases"]]:
            norm = normalize_alias(alias)
            if not norm:
                continue
            bucket = seen.setdefault(norm, [])
            if item["canonical"] not in bucket:
                bucket.append(item["canonical"])
    return [(norm, canons) for norm, canons in seen.items() if len(canons) > 1]


async def main(apply: bool, from_config: bool, curated: bool) -> int:
    from services.timeutil import msk_now

    fetched_at_stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")

    if curated:
        exit_code = 0

        try:
            uni_stats = fetch_universities_curated()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(
                f"Кураторская выгрузка вузов недоступна ({exc}) — снапшот НЕ трогаем, "
                "оставлен прежний data/lookup/universities_ru.json.",
                file=sys.stderr,
            )
            uni_stats = None
            exit_code = 1

        if uni_stats is not None:
            count = len(uni_stats["items"])
            _print_stats("Вузы (кураторская выгрузка)", uni_stats)
            print(
                f"Объём: {count} (ожидание {CURATED_EXPECTED_MIN}–{CURATED_EXPECTED_MAX}, "
                f"допустимый диапазон {CURATED_VOLUME_MIN}–{CURATED_VOLUME_MAX})"
            )
            print("Выборочная проверка по сокращению:")
            for abbr, found, canonical in check_known_universities(
                uni_stats["items"], KNOWN_UNIVERSITY_ABBREVIATIONS
            ):
                mark = f"найдено ({canonical})" if found else "НЕ найдено"
                print(f"  {abbr}: {mark}")

            collisions = find_alias_collisions(uni_stats["items"])
            print(f"Коллизии псевдонимов (один нормализованный alias у ≥2 каноник): {len(collisions)}")
            for norm, canons in collisions[:10]:
                print(f"  «{norm}»: {' / '.join(canons)}")

            if not (CURATED_VOLUME_MIN <= count <= CURATED_VOLUME_MAX):
                print(
                    f"Объём {count} вне допустимого диапазона "
                    f"[{CURATED_VOLUME_MIN}, {CURATED_VOLUME_MAX}] — снапшот НЕ перезаписан, "
                    "см. data/lookup/README.md.",
                    file=sys.stderr,
                )
                exit_code = 1
            elif apply:
                _write_snapshot(LOOKUP_DIR / "universities_ru.json", uni_stats, fetched_at_stamp)
                print(f"Записано: {LOOKUP_DIR / 'universities_ru.json'}")
            else:
                print("Сухой прогон — файл не изменён. Повторите с --apply, чтобы записать снапшот.")

        print()
        try:
            city_stats = fetch_cities_curated()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(
                f"Кураторская выгрузка городов недоступна ({exc}) — снапшот НЕ трогаем, "
                "оставлен прежний data/lookup/cities_ru.json (задача B: «при неудаче — "
                "оставить как есть»).",
                file=sys.stderr,
            )
            return exit_code

        city_count = len(city_stats["items"])
        _print_stats("Города (кураторская выгрузка)", city_stats)
        print(
            f"Объём: {city_count} (допустимый диапазон {CITIES_VOLUME_MIN}–{CITIES_VOLUME_MAX})"
        )
        if not (CITIES_VOLUME_MIN <= city_count <= CITIES_VOLUME_MAX):
            print(
                f"Объём {city_count} вне допустимого диапазона "
                f"[{CITIES_VOLUME_MIN}, {CITIES_VOLUME_MAX}] — cities_ru.json НЕ перезаписан, "
                "оставлен прежний офлайн-фолбэк.",
                file=sys.stderr,
            )
            return exit_code
        if apply:
            _write_snapshot(LOOKUP_DIR / "cities_ru.json", city_stats, fetched_at_stamp)
            print(f"Записано: {LOOKUP_DIR / 'cities_ru.json'}")
        else:
            print("Сухой прогон — файл не изменён. Повторите с --apply, чтобы записать снапшот.")
        return exit_code

    if from_config:
        uni_stats = _universities_from_config()
    else:
        try:
            uni_stats = fetch_universities_from_wikidata()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(
                f"Сеть до query.wikidata.org недоступна ({exc}) — офлайн-фолбэк "
                "config.UNIVERSITIES.",
                file=sys.stderr,
            )
            uni_stats = _universities_from_config()

    city_stats = cities_fallback_snapshot()

    _print_stats("Вузы", uni_stats)
    _print_stats("Города", city_stats)

    if apply:
        _write_snapshot(LOOKUP_DIR / "universities_ru.json", uni_stats, fetched_at_stamp)
        _write_snapshot(LOOKUP_DIR / "cities_ru.json", city_stats, fetched_at_stamp)
        print(f"Записано: {LOOKUP_DIR / 'universities_ru.json'}")
        print(f"Записано: {LOOKUP_DIR / 'cities_ru.json'}")
    else:
        print("Сухой прогон — файлы не изменены. Повторите с --apply, чтобы записать снапшоты.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="перезаписать data/lookup/*.json")
    parser.add_argument(
        "--from-config", action="store_true",
        help="собрать снапшот вузов из config.UNIVERSITIES без сети (города всегда офлайн-фолбэк)",
    )
    parser.add_argument(
        "--curated", action="store_true",
        help=(
            "кураторская выгрузка вузов (30-08): русская метка обязательна, факультет/"
            "кафедра/филиал исключены прямым SPARQL-фильтром; несовместимо с --from-config"
        ),
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply, args.from_config, args.curated)))
