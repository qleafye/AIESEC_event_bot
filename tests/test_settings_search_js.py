"""Фаза 22 (WEB-SET-02, D-15б/в): поведенческая проверка нестрогого поиска по настройкам —
чистых функций `searchFilter`/`highlightMatch`/`suggestTerms` из `miniapp/static/js/form.js`,
запущенных в node (в проекте нет JS-тестраннера; `form.js` намеренно импортируем в чистом
node без DOM — на уровне модуля он к `document`/`window` не обращается).

Таблица кейсов — контракт D-15: точное слово; синоним из `search_terms` (с сервера, не из
фронта); «дедлаин» → «дедлайн» (Левенштейн ≤ 1 для слов от 5 символов); «дед» → префикс;
«оплат срок» — только ключ, где совпали ОБА слова (AND); «ё» и «е» эквивалентны; пустой
запрос — все элементы в исходном порядке; чужое слово — пусто, а короткая опечатка — пусто с
непустой подсказкой; `highlightMatch` — ровно один `mark` на совпадение.

Без node тест пропускается с явной причиной — статические сторожа
`tests/test_miniapp_frontend.py::test_form_js_search_exports_and_no_synonym_literals`
остаются гейтом в любом случае.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORM_JS = ROOT / "miniapp" / "static" / "js" / "form.js"

# Элементы в форме ответа `settings/all` (план 22-04): подпись/подсказка/синонимы/значение.
ITEMS = [
    {"key": "payment_deadline", "label": "Дедлайн оплаты", "help": "Дата, до которой нужно оплатить",
     "search_terms": ["срок", "дата оплаты"], "display": "15.09.2026"},
    {"key": "start_text", "label": "Приветствие", "help": "Первое сообщение бота",
     "search_terms": ["привет", "первое сообщение", "старт"], "display": "Привет, {name}!"},
    {"key": "payment_details", "label": "Реквизиты", "help": "Куда переводить",
     "search_terms": ["карта", "куда платить", "оплата"], "display": "Сбер 1234"},
    {"key": "main_sheet_tab", "label": "Вкладка таблицы", "help": "Лист Google Sheets",
     "search_terms": ["таблица", "лист"], "display": "Delegates"},
    {"key": "event_date", "label": "Дата форума", "help": "Когда проходит",
     "search_terms": ["число", "когда"], "display": "01.10.2026"},
    {"key": "consent_toggle", "label": "Согласия включены", "help": "",
     "search_terms": ["галочка", "пдн"], "display": "Включено"},
    {"key": "green_tree", "label": "Ёлка", "help": "Новогодняя", "search_terms": [], "display": ""},
]
ALL_KEYS = [it["key"] for it in ITEMS]

# Запросы, для которых нужен список ключей (в исходном порядке).
KEY_QUERIES = {
    "exact": "дедлайн",
    "synonym": "карта",          # нет ни в подписи, ни в подсказке — только в search_terms
    "typo": "дедлаин",           # Левенштейн 1, слово ≥ 5
    "prefix": "дед",
    "and": "оплат срок",         # у payment_details есть «оплата», но нет «срок»
    "yo_lower": "елка",
    "yo_upper": "ЁЛКА",
    "empty": "",
    "blank": "   ",
    "alien": "xyzzy",
    "short_typo": "дате",        # 4 символа — опечатка не допускается, результат пуст
    "value": "deleg",            # совпадение по текущему значению
}

NODE_SCRIPT = """
const m = await import(%(url)s);
const items = %(items)s;
const queries = %(queries)s;
const h = (tag, attrs) => ({ tag, text: attrs && attrs.text });
const out = { keys: {}, ranges: {}, suggest: {}, highlight: {} };
for (const [name, q] of Object.entries(queries)) {
  out.keys[name] = m.searchFilter(items, q).map((r) => r.item.key);
}
out.ranges.prefix = m.searchFilter(items, queries.prefix)[0].ranges;
out.ranges.help_only = m.searchFilter(items, "первое")[0].ranges;
out.ranges.synonym = m.searchFilter(items, queries.synonym)[0].ranges;
out.suggest.short_typo = m.suggestTerms(items, queries.short_typo, 3);
out.suggest.alien = m.suggestTerms(items, queries.alien, 3);
out.suggest.and = m.suggestTerms(items, queries.and, 3);
out.suggest.empty = m.suggestTerms(items, "", 3);
const one = m.searchFilter(items, queries.prefix)[0];
out.highlight.single = m.highlightMatch(h, one.item.label, one.ranges.label);
out.highlight.overlap = m.highlightMatch(h, "Дата оплаты", [[0, 4], [5, 8], [2, 6]]);
out.highlight.none = m.highlightMatch(h, "Реквизиты", []);
out.exports = Object.keys(m).sort();
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест поиска form.js пропущен")
    script = NODE_SCRIPT % {
        "url": json.dumps(FORM_JS.resolve().as_uri()),
        "items": json.dumps(ITEMS, ensure_ascii=False),
        "queries": json.dumps(KEY_QUERIES, ensure_ascii=False),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_exports_search_and_registry_helpers(result):
    for name in ("field", "settingSpec", "searchFilter", "highlightMatch", "suggestTerms", "listChips", "groupCollapse"):
        assert name in result["exports"], name


@pytest.mark.parametrize("case, expected", [
    ("exact", ["payment_deadline"]),
    ("synonym", ["payment_details"]),
    ("typo", ["payment_deadline"]),
    ("prefix", ["payment_deadline"]),
    ("and", ["payment_deadline"]),
    ("yo_lower", ["green_tree"]),
    ("yo_upper", ["green_tree"]),
    ("empty", ALL_KEYS),
    ("blank", ALL_KEYS),
    ("alien", []),
    ("short_typo", []),
    ("value", ["main_sheet_tab"]),
])
def test_search_filter_matches_contract(result, case, expected):
    assert result["keys"][case] == expected, f"{case}: {KEY_QUERIES[case]!r}"


def test_ranges_point_at_label_or_help_only(result):
    # «дед» — префикс в подписи «Дедлайн оплаты»: отрезок [0, 3) в label, help пуст.
    assert result["ranges"]["prefix"] == {"label": [[0, 3]], "help": []}
    # «первое» есть только в подсказке — отрезок в help, label пуст.
    assert result["ranges"]["help_only"] == {"label": [], "help": [[0, 6]]}
    # Синоним совпал вне подписи/подсказки — подсвечивать нечего.
    assert result["ranges"]["synonym"] == {"label": [], "help": []}


def test_suggest_terms_on_zero_results(result):
    # Короткая опечатка «дате» → ближайшее слово реестра «дата» (D-15в).
    assert result["suggest"]["short_typo"], "при нуле результатов подсказка обязана быть"
    assert "дата" in result["suggest"]["short_typo"]
    assert len(result["suggest"]["short_typo"]) <= 3
    # AND без общего ключа: «оплат» → подсказать «оплата»/«оплаты», «срок» уже точное слово.
    assert result["suggest"]["and"] and result["suggest"]["and"][0].startswith("оплат")
    assert "срок" not in result["suggest"]["and"]
    # Заведомо чужое слово далеко от всех — подсказки нет, функция не падает.
    assert result["suggest"]["alien"] == []
    assert result["suggest"]["empty"] == []


def _marks(children: list) -> list[dict]:
    return [c for c in children if isinstance(c, dict) and c.get("tag") == "mark"]


def _plain(children: list) -> str:
    return "".join(c["text"] if isinstance(c, dict) else c for c in children)


def test_highlight_match_wraps_each_hit_in_one_mark(result):
    single = result["highlight"]["single"]
    assert len(_marks(single)) == 1
    assert _marks(single)[0]["text"] == "Дед"
    assert _plain(single) == "Дедлайн оплаты"  # текст не теряется и не дублируется
    # Пересекающиеся отрезки склеиваются в один mark, разрывы остаются строками.
    overlap = result["highlight"]["overlap"]
    assert len(_marks(overlap)) == 1
    assert _marks(overlap)[0]["text"] == "Дата опл"
    assert _plain(overlap) == "Дата оплаты"
    # Без отрезков — одна строка без mark.
    assert result["highlight"]["none"] == ["Реквизиты"]


# ── settingSpec: enum on/off -> тумблер (D-17 Task 1, владелец 03.09) ────────────────────

SETTING_SPEC_SCRIPT = """
const m = await import(%(url)s);
const out = {};
out.on_off = m.settingSpec({ key: "party_enabled", type: "enum", options: ["on", "off"], label: "Party" }).type;
out.off_on = m.settingSpec({ key: "r", type: "enum", options: ["off", "on"], label: "R" }).type;
out.three = m.settingSpec({ key: "registration_mode", type: "enum", options: ["short", "full"], label: "Форма" }).type;
out.many = m.settingSpec({ key: "x", type: "enum", options: ["a", "b", "c", "d", "e"], label: "X" }).type;
out.real_toggle = m.settingSpec({ key: "reg_q_age", type: "toggle", label: "Возраст" }).type;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def setting_spec_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест settingSpec form.js пропущен")
    script = SETTING_SPEC_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_setting_spec_enum_on_off_renders_as_toggle(setting_spec_result):
    # D-17 Task 1: options ровно {"on","off"} (порядок неважен) -> тот же control, что
    # настоящий toggle (reg_q_*) — владелец жаловался на два разных визуала одного вопроса.
    assert setting_spec_result["on_off"] == "toggle"
    assert setting_spec_result["off_on"] == "toggle"
    assert setting_spec_result["real_toggle"] == "toggle"
    # Не-on/off enum (даже двухвариантный) остаётся chip-выбором — это НЕ вопрос «да/нет».
    assert setting_spec_result["three"] == "choice-chips"
    assert setting_spec_result["many"] == "select"
