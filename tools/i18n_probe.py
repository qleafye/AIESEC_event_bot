"""Phase 27 (27-01, LANG-10) — оффлайн замер движка перевода ru->en на РЕАЛЬНОМ корпусе
делегатских текстов анкеты, до того как в образ бота лягут ~90 МБ колёс и 156 МБ модели.

Не часть рантайма бота — CLI-инструмент, запускается руками (или из `--measure` на стенде).
Своего перечисления источников текста не имеет: корпус собирает `services.i18n_sources.corpus()`
— второго списка источников в проекте быть не должно.

Запуск:
    python tools/i18n_probe.py --help
    python tools/i18n_probe.py --db data/forum.db --out report.md
    python tools/i18n_probe.py --db data/forum.db --driver http --url http://localhost:5000 --out report.md
    python tools/i18n_probe.py --measure --probe-glossary --out 27-PROBE-REPORT.md

Без установленного движка (`--driver embedded`, дефолт) и без готового `.argosmodel` скрипт
печатает ОДНУ инструкцию и завершается кодом 2 — без трассировки (это ожидаемое, а не
аварийное состояние: движок ставится один раз на стенде, не в этой среде разработки).

Ленивый импорт `argostranslate` — идиома уже есть в `services/scheduler.py`; тесты и
`--help` не должны требовать 90 МБ колёс на диске.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DB_PATH = "data/forum.db"
BATCH_SIZE = 32
GLOSSARY_TERMS = ("ЛК", "ЛС", "трек", "АЙСЕК", "Юлид")

# Русские дефолты вопроса «город» (reg_engine.SELECT_CONFIG["city"][1]) — используются только
# для раздела отчёта «термины АЙСЕК», чтобы owner увидел худшие случаи (переведённые названия
# городов) первыми, не заводя здесь второе перечисление реестра.
CITY_NAME_HINTS = (
    "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург",
    "Казань", "Нижний Новгород", "Красноярск", "Уфа",
)

ENGINE_INSTALL_HINT = (
    "Движок перевода не установлен или модель не найдена. Поставьте: "
    "pip install argos-translate-lt==1.12.1 -- и положите translate-ru_en-1_9.argosmodel "
    "в каталог, на который указывает переменная окружения ARGOS_PACKAGES_DIR "
    "(по умолчанию — распакованный пакет ищется рядом с site-packages; см. 27-RESEARCH.md "
    "§ Standard Stack)."
)


class EngineMissing(RuntimeError):
    """Единая ошибка «нечем переводить» — и для отсутствующего пакета, и для отсутствующей
    модели: пользователю нужна одна и та же инструкция в обоих случаях (план, задача 2)."""


class EmbeddedDriver:
    """Драйвер поверх `argos-translate-lt` (пакет всё ещё импортируется как `argostranslate`
    — торч-фри форк того же вендора, drop-in замена). Ленивая загрузка: модель поднимается при
    первом батче, не при импорте модуля (тесты/--help не должны требовать 90 МБ колёс)."""

    def __init__(self) -> None:
        self._translation = None

    def _boot(self) -> None:
        try:
            import argostranslate.translate as at  # ленивый импорт
        except ImportError as exc:
            raise EngineMissing(ENGINE_INSTALL_HINT) from exc

        languages = at.get_installed_languages()
        from_lang = next((lang for lang in languages if lang.code == "ru"), None)
        to_lang = next((lang for lang in languages if lang.code == "en"), None)
        if from_lang is None or to_lang is None:
            raise EngineMissing(ENGINE_INSTALL_HINT)
        translation = from_lang.get_translation(to_lang)
        if translation is None:
            raise EngineMissing(ENGINE_INSTALL_HINT)
        self._translation = translation

    def translate_batch(self, texts: list[str]) -> list[str]:
        if self._translation is None:
            self._boot()
        return [self._translation.translate(t) for t in texts]


class HttpDriver:
    """Драйвер поверх сайдкара LibreTranslate (`POST {url}/translate`, батч-массив — API
    принимает и отдаёт список). `httpx` уже в зависимостях проекта (Mini App), лениво не
    прячем — в отличие от `argostranslate` он не тяжёлый и не опционален."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def translate_batch(self, texts: list[str]) -> list[str]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — httpx уже в requirements.txt
            raise EngineMissing(
                "httpx не установлен — он уже должен быть в requirements.txt проекта."
            ) from exc
        try:
            resp = httpx.post(
                f"{self.url}/translate",
                json={"q": texts, "source": "ru", "target": "en", "format": "text"},
                timeout=60,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineMissing(
                f"Сайдкар LibreTranslate недоступен по адресу {self.url!r}: {exc}"
            ) from exc
        data = resp.json()
        translated = data.get("translatedText")
        if isinstance(translated, str):
            return [translated]
        if isinstance(translated, list):
            return translated
        raise EngineMissing(f"Неожиданный ответ LibreTranslate: {data!r}")


def _make_driver(args: argparse.Namespace):
    if args.driver == "http":
        if not args.url:
            raise SystemExit("--driver http требует --url (адрес сайдкара LibreTranslate)")
        return HttpDriver(args.url)
    return EmbeddedDriver()


def _translate_all(driver, texts: list[str]) -> list[str]:
    out: list[str] = []
    for i in range(0, len(texts), BATCH_SIZE):
        chunk = texts[i:i + BATCH_SIZE]
        out.extend(driver.translate_batch(chunk))
    return out


async def _load_corpus(db_path: str, limit: int | None) -> list[tuple[str, str]]:
    from config import config
    config.DB_PATH = db_path
    import services.i18n_sources as i18n_sources
    items = await i18n_sources.corpus()
    if limit:
        items = items[:limit]
    return items


def _has_term(text: str) -> bool:
    return any(term in text for term in GLOSSARY_TERMS) or any(
        city in text for city in CITY_NAME_HINTS
    )


def _md_table(rows: list[tuple[str, str, str]]) -> str:
    lines = ["| origin_key | русский | английский |", "|---|---|---|"]
    for origin_key, ru, en in rows:
        ru_cell = ru.replace("|", "\\|").replace("\n", " ")
        en_cell = (en or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{origin_key}` | {ru_cell} | {en_cell} |")
    return "\n".join(lines)


def _build_report(corpus: list[tuple[str, str]], translations: list[str]) -> str:
    triples = [(origin, ru, en) for (origin, ru), en in zip(corpus, translations)]

    matched_source = [t for t in triples if not t[2] or t[2].strip() == t[1].strip()]
    terms = [t for t in triples if _has_term(t[1])]

    total_chars = sum(len(ru) for _origin, ru, _en in triples)
    unique_ru = {ru for _origin, ru, _en in triples}

    parts = [
        "# Отчёт замера перевода (Phase 27, план 27-01)\n",
        "## Корпус\n",
        f"- строк: {len(triples)}\n"
        f"- уникальных (по тексту): {len(unique_ru)}\n"
        f"- символов: {total_chars}\n",
        "## Совпало с исходником / перевод пуст\n",
        (
            "Движок либо ничего не поменял (транслитерация/незнакомое слово), либо вернул "
            "пустую строку — кандидаты на ручную правку в первую очередь.\n"
        ),
        _md_table(matched_source) if matched_source else "_таких строк нет._\n",
        "\n## Термины АЙСЕК (ЛК/ЛС/трек/АЙСЕК/Юлид/города)\n",
        (
            "Короткие и многозначные термины — движок их гарантированно испортит без "
            "глоссария. Смотреть в первую очередь.\n"
        ),
        _md_table(terms) if terms else "_таких строк нет._\n",
        "\n## Полная таблица «русский → английский»\n",
        _md_table(triples),
    ]
    return "\n".join(parts) + "\n"


# ── Глоссарий: сентинелы DNT (--probe-glossary) ──────────────────────────────────────────────

_SENTINEL_MAP = {
    "АЙСЕК": "ZQ1Z",
    "Юлид": "ZQ2Z",
    "трек": "ZQ3Z",
}

# (позиция, шаблон с {s} на месте сентинела) — начало/середина/склонённое словосочетание
# (сентинел + русское окончание слитно — самый жёсткий случай для NMT-токенизатора).
_SENTINEL_TEMPLATES = (
    ("начало", "{s} проводит форум для делегатов каждый год."),
    ("середина", "Мы уже третий год участвуем в {s} как волонтёры."),
    ("склонение", "Расскажи о деятельности {s}а за последний сезон."),
)


def _glossary_section(driver) -> str:
    probe_lines: list[tuple[str, str, str]] = []  # (term, position, source_line)
    for term, sentinel in _SENTINEL_MAP.items():
        for position, template in _SENTINEL_TEMPLATES:
            probe_lines.append((term, position, template.format(s=sentinel)))

    sources = [line for _term, _pos, line in probe_lines]
    translated = _translate_all(driver, sources)

    rows = []
    survived = 0
    for (term, position, source_line), en in zip(probe_lines, translated):
        sentinel = _SENTINEL_MAP[term]
        ok = sentinel in en
        survived += int(ok)
        rows.append((term, position, source_line, en, "да" if ok else "НЕТ"))

    total = len(rows)
    verdict = (
        f"DNT-сентинелы применимы: {survived}/{total} пережили перевод дословно."
        if survived == total
        else (
            f"DNT-сентинелы НЕ полностью применимы: только {survived}/{total} пережили "
            "перевод дословно — нужен откат на пост-замены (POST) и ручную правку "
            "оставшихся случаев."
        )
    )

    lines = [
        "\n## Сентинелы DNT (--probe-glossary)\n",
        (
            "ASCII-сентинел подставляется вместо термина АЙСЕК/Юлид/трек ДО перевода; если "
            "движок копирует его дословно — можно защищать термины сентинелами, а не только "
            "постфактум регуляркой.\n"
        ),
        "| термин | позиция | русский (с сентинелом) | английский | сентинел уцелел |",
        "|---|---|---|---|---|",
    ]
    for term, position, source_line, en, ok in rows:
        lines.append(f"| {term} | {position} | {source_line} | {en} | {ok} |")
    lines.append(f"\n**Вывод:** {verdict}\n")

    lines.append(
        "\n### Черновик глоссария (`services/i18n_glossary.py`, план 27-03 создаёт по "
        "утверждённому черновику)\n"
    )
    lines.append(
        "```python\n"
        "DNT = {                       # защищаем ДО перевода (подстановка сентинелов)\n"
        '    "АЙСЕК": "AIESEC",\n'
        '    "Юлид":  "YouLead",\n'
        '    "РеалТок": "RealTalk",\n'
        "}\n"
        "POST = [                      # чиним ПОСЛЕ перевода (частые кальки движка)\n"
        '    (r"\\bLC\\b|\\blocal committee\\b", "Local Committee"),\n'
        '    (r"\\bИК\\b", "Local Committee"),   # ЛК\n'
        '    (r"\\bpersonal message\\b", "direct message"),  # ЛС\n'
        "]\n"
        "```\n"
    )
    return "\n".join(lines)


# ── Замеры RSS/латентности (--measure) ───────────────────────────────────────────────────────

def _rss_kb() -> int | None:
    """`/proc/self/status` (Linux-стенд, надёжнее) с фоллбэком на `resource.getrusage` —
    оба недоступны на Windows (dev-машина), тогда возвращаем `None` и отчёт это помечает."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])  # килобайты
    except OSError:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # Linux: КБ
    except (ImportError, AttributeError):
        return None


def _fmt_mb(kb: int | None) -> str:
    if kb is None:
        return "н/д (не POSIX — снято не на этой платформе)"
    return f"{kb / 1024:.1f} МБ"


def _measure_section(driver, corpus: list[tuple[str, str]]) -> str:
    rss_before = _rss_kb()
    t0 = time.monotonic()
    driver.translate_batch([corpus[0][1]] if corpus else ["тест"])
    cold_load_s = time.monotonic() - t0
    rss_after = _rss_kb()

    sample = corpus[: min(50, len(corpus))]
    per_line_ms: list[float] = []
    for _origin, ru in sample:
        t = time.monotonic()
        driver.translate_batch([ru])
        per_line_ms.append((time.monotonic() - t) * 1000)

    avg_ms = statistics.mean(per_line_ms) if per_line_ms else 0.0
    p95_ms = (
        statistics.quantiles(per_line_ms, n=20)[18]
        if len(per_line_ms) >= 20
        else (max(per_line_ms) if per_line_ms else 0.0)
    )

    texts = [ru for _origin, ru in corpus]
    t0 = time.monotonic()
    _translate_all(driver, texts)
    total_batch_s = time.monotonic() - t0

    lines = [
        "\n## Замеры (RSS/латентность, --measure)\n",
        "| Метрика | Значение |",
        "|---|---|",
        f"| RSS до загрузки модели | {_fmt_mb(rss_before)} |",
        f"| RSS после загрузки модели | {_fmt_mb(rss_after)} |",
        f"| Время холодной загрузки | {cold_load_s:.2f} с |",
        f"| Среднее время на строку (выборка {len(sample)}) | {avg_ms:.0f} мс |",
        f"| p95 на строку (выборка {len(sample)}) | {p95_ms:.0f} мс |",
        f"| Общее время батча ({len(texts)} строк, по {BATCH_SIZE}) | {total_batch_s:.1f} с |",
    ]
    return "\n".join(lines) + "\n"


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Оффлайн замер движка перевода ru->en на корпусе делегатских текстов анкеты "
            "(Phase 27, план 27-01)."
        )
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"путь к forum.db (по умолчанию {DEFAULT_DB_PATH})")
    parser.add_argument(
        "--driver", choices=("embedded", "http"), default="embedded",
        help="embedded — argos-translate-lt в процессе; http — сайдкар LibreTranslate (--url)",
    )
    parser.add_argument("--url", default=None, help="URL сайдкара LibreTranslate (для --driver http)")
    parser.add_argument("--limit", type=int, default=None, help="ограничить корпус первыми N строками")
    parser.add_argument("--out", default=None, help="путь markdown-отчёта (по умолчанию — печать в stdout)")
    parser.add_argument("--probe-glossary", action="store_true", help="проверить DNT-сентинелы глоссария")
    parser.add_argument("--measure", action="store_true", help="снять RSS/латентность движка")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    import asyncio

    args = _parse_args(argv)
    corpus = asyncio.run(_load_corpus(args.db, args.limit))
    print(
        f"Корпус: строк={len(corpus)}, уникальных={len({t for _o, t in corpus})}, "
        f"символов={sum(len(t) for _o, t in corpus)}"
    )

    driver = _make_driver(args)
    try:
        translations = _translate_all(driver, [ru for _origin, ru in corpus])
    except EngineMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = _build_report(corpus, translations)

    if args.probe_glossary:
        try:
            report += _glossary_section(driver)
        except EngineMissing as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.measure:
        try:
            report += _measure_section(driver, corpus)
        except EngineMissing as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Отчёт записан: {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
