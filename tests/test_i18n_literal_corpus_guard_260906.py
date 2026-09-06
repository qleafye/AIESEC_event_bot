"""Quick 260906, UAT run 2 (второй проход после 2773791/44c77df/222340a) — сторож регрессии
«русский литерал ушёл в перевод, но забыт в корпусе». Находка 1 первого прогона
(`handlers/reg_resume.py::offer_resume` звал `reg_i18n.say(message, "У тебя есть
незаконченная анкета — что дальше?", ...)`, но сам литерал не значился ни в
`services/i18n_sources.py::code_literals()` (ярус B), ни в `i18n_ui_en.UI_EN` (ярус A)) —
класс бага, который `tests/test_registration_send_guard_260906.py` не ловит: та проверка
смотрит на ШОВ (прошёл ли аргумент через `reg_i18n.*`), не на КОРПУС (есть ли у переведённого
текста вообще шанс найти перевод в БД).

Метод — тот же AST-разбор без построчного grep (не спотыкается о перенос строк в
многострочных вызовах `reg_i18n.say(...)`): собираем каждый вызов `reg_i18n.say`/
`reg_i18n.tr_for`/`reg_i18n.tr_text`/`reg_i18n.tr_fmt` в `SCANNED_FILES`, берём аргумент,
несущий текст (для `say`/`tr_for` — второй позиционный, получатель первым; для `tr_text`/
`tr_fmt` — первый), и если это ГОЛЫЙ строковый литерал (не переменная/f-строка — статически
такие уже покрыты `code_literals()`/registry по построению, см. докстринг
`services/i18n_sources.py`) с кириллицей — литерал обязан быть либо в `code_literals()`
(ярус B, машинный перевод), либо в `i18n_ui_en.UI_EN` (ярус A, рукописный). Новый литерал,
который не завели ни туда, ни туда, — тихая дыра ровно того класса, что нашёл стендовый UAT."""
import ast
from pathlib import Path

from i18n_ui_en import UI_EN
from services import i18n_sources

ROOT = Path(__file__).resolve().parent.parent

SCANNED_FILES = [
    ROOT / "handlers" / "registration.py",
    ROOT / "handlers" / "reg_flow.py",
    ROOT / "handlers" / "reg_consent.py",
    ROOT / "handlers" / "reg_steps.py",
    ROOT / "handlers" / "reg_resume.py",
    ROOT / "handlers" / "reg_handoff.py",
]

# Функция -> индекс позиционного аргумента, несущего видимый делегату текст.
_TEXT_ARG_INDEX = {"say": 1, "tr_for": 1, "tr_text": 0, "tr_fmt": 0}


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


def _find_literal_calls(path: Path):
    """Возвращает список (func_name, lineno, literal_text) для вызовов `reg_i18n.{say,tr_for,
    tr_text,tr_fmt}`, чей текстовый аргумент — голый строковый литерал с кириллицей."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    found = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "reg_i18n" or func.attr not in _TEXT_ARG_INDEX:
            continue
        idx = _TEXT_ARG_INDEX[func.attr]
        arg = node.args[idx] if len(node.args) > idx else None
        if arg is None:
            continue
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue  # переменная/f-строка — вне статической проверки (см. докстринг)
        text = arg.value
        if not _has_cyrillic(text):
            continue
        found.append((func.attr, node.lineno, text))

    return found


def test_every_translated_literal_is_in_corpus_or_tier_a():
    tier_b_texts = {text.strip() for _origin, text in i18n_sources.code_literals()}
    tier_a_texts = set(UI_EN.keys())
    allowed = tier_b_texts | tier_a_texts

    violations = []
    for path in SCANNED_FILES:
        rel = path.relative_to(ROOT).as_posix()
        for func_name, lineno, text in _find_literal_calls(path):
            if text.strip() in allowed:
                continue
            violations.append(f"{rel}:{lineno} reg_i18n.{func_name}({text!r})")

    assert not violations, (
        "Литералы переведены на отправке (reg_i18n.say/tr_text/tr_for/tr_fmt), но отсутствуют "
        "и в services/i18n_sources.py::code_literals() (ярус B), и в i18n_ui_en.UI_EN (ярус A) "
        "— делегат с lang != ru увидит русский текст (fail-soft тихо промолчит, тест — нет):\n  "
        + "\n  ".join(violations)
    )
