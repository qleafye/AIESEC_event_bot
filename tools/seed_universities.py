"""Phase 30 (30-02, A2-03): разовая выгрузка справочников ВУЗ/город в офлайн-снапшоты
`data/lookup/universities_ru.json` / `data/lookup/cities_ru.json` — `lookup_entries`
(`database/db.py`) стартует БЕЗ обращения к внешним API в рантайме (30-CONTEXT.md решение
владельца №1, «второе поверх 03.09»: ДаData/Getgeo не используем ни в каком виде).

Запуск (сухой прогон по умолчанию — печатает статистику, файлы не трогает):

    python tools/seed_universities.py                 # живая выгрузка из Викиданных
    python tools/seed_universities.py --apply          # записывает оба снапшота
    python tools/seed_universities.py --from-config     # без сети: вузы из config.UNIVERSITIES

Города СЕГОДНЯ всегда собираются офлайн-фолбэком (города бота + топ-8 `SELECT_CONFIG`) —
лицензия `arbaev/russia-cities` проверена и НЕ подтверждена (нет LICENSE в репозитории,
GitHub API возвращает `license: null` → по умолчанию все права защищены, копировать датасет
в репозиторий нельзя, см. `data/lookup/README.md`).

Методика выгрузки вузов — 30-CONTEXT.md § «Решения по итогам 30-RESEARCH.md», п. 3
(ОБЯЗАТЕЛЬНА, не переизобретается здесь):
  (1) основной запрос — transitive-closure по подклассам `wdt:P31/wdt:P279* wd:Q38723`
      («higher education institution»), фильтр страны `wdt:P17`;
  (2) контрольная сверка — прямой запрос из 30-RESEARCH.md § Code Examples
      (`VALUES ?class { wd:Q3918 wd:Q38723 wd:Q1371037 }`, verified live 472 записи
      12.09.2026) — обе цифры (`count_base`/`count_closure`) пишутся в снапшот и в README;
  (3) закрытые/расформированные (`P576` задан) — исключаются, количество исключённых
      считается;
  (4) филиалы (`P749` — головная организация) — не отдельной записью, а псевдонимом
      головной, если головная попала в выборку.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
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


def _sparql_get(query: str) -> dict:
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{WIKIDATA_ENDPOINT}?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


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


def fetch_universities_from_wikidata(country: str = COUNTRY_RUSSIA) -> dict:
    """Живая выгрузка (методика п.1-4). Бросает `urllib.error.URLError`/`TimeoutError`/
    `OSError`, если сеть недоступна — вызывающий (`main`) решает откатиться на
    `_universities_from_config`, план не блокируется отсутствием сети у исполнителя."""
    control_payload = _sparql_get(_CONTROL_QUERY_TEMPLATE.format(country=country))
    count_base = len(_rows(control_payload))

    closure_payload = _sparql_get(_CLOSURE_QUERY_TEMPLATE.format(country=country))
    closure_rows = _rows(closure_payload)
    count_closure = len(closure_rows)

    by_qid: dict[str, dict] = {}
    dissolved_qids: set[str] = set()
    branch_of: dict[str, str] = {}

    for row in closure_rows:
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
            continue
        kept.append(entry)

    items = [
        {
            "canonical": entry["canonical"],
            "aliases": sorted(a for a in entry["aliases"] if a and a != entry["canonical"]),
            "source": "wikidata",
            "dissolved": False,
        }
        for entry in kept
    ]
    items.sort(key=lambda x: x["canonical"])

    return {
        "items": items,
        "count_base": count_base,
        "count_closure": count_closure,
        "excluded_dissolved": excluded_dissolved,
        "source_query": _CLOSURE_QUERY_TEMPLATE.format(country=country).strip(),
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
    payload = {
        "meta": {
            "source_query": stats["source_query"],
            "count_base": stats["count_base"],
            "count_closure": stats["count_closure"],
            "excluded_dissolved": stats["excluded_dissolved"],
            "fetched_at": fetched_at,
        },
        "items": stats["items"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def main(apply: bool, from_config: bool) -> int:
    from services.timeutil import msk_now

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
    fetched_at = msk_now().strftime("%Y-%m-%d %H:%M:%S")

    _print_stats("Вузы", uni_stats)
    _print_stats("Города", city_stats)

    if apply:
        _write_snapshot(LOOKUP_DIR / "universities_ru.json", uni_stats, fetched_at)
        _write_snapshot(LOOKUP_DIR / "cities_ru.json", city_stats, fetched_at)
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
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply, args.from_config)))
