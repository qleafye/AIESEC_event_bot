"""Quick 260824 (расширено 260903 — правило владельца: «АЙСЕК»/«Юлид» кириллицей везде,
кроме хэштегов/юзернеймов/ссылок/кода): «ЮЛид и АЙСЕК пишем только на русском».

Сторож против регресса: проверяет не файлы целиком (в путях к SVG латиница законна),
а именно значения человеко-видимых словарей подписей — подписи пресетов оформления,
подписи/подсказки/дефолты реестра настроек (включая зеркало в reg_labels), варианты
ответов анкеты (reg_options.py) и строковые литералы во фронтенде Mini App
(miniapp/static/js/**).

Коды (id пресетов `bluebook`/`youlead`, ключи реестра `reg_q_aiesec_role`,
localStorage-ключи вида `aiesec_miniapp_*_v1` и т.п.) — не человеко-видимые строки,
их не трогаем и в этом сторожe не проверяем. Так же не проверяем: хэштеги (#YouLead26),
юзернеймы/упоминания (@YouLead2026_bot), ссылки и email, токены с суффиксом `_bot`,
путь к паттерн-ассету (`pattern/youlead.svg`) и код-комментарии/докстроки (не видны
человеку, только разработчику).
"""
from __future__ import annotations

import re
from pathlib import Path

from handlers.admin_miniapp_theme import _PRESET_BLURBS, _PRESET_LABELS
import reg_labels
import reg_options
import web_theme
from settings_schema import SETTINGS_SCHEMA

LATIN_BRAND_SUBSTRINGS = ("aiesec", "youlead", "bluebook", "realtalk")

# Только для широкого сторожа реестра/анкеты/фронтенда (задача 260903, расширено 260904-183) —
# узкий список из правила владельца, без «bluebook» (кодовое имя пресета, отдельно покрыто выше).
LATIN_OWNER_BRAND_SUBSTRINGS = ("aiesec", "youlead", "realtalk")

# Примеры-URL (t.me/aiesec_ru и т.п.) — не человеко-видимая надпись бренда, а
# служебная ссылка; латиница там обязана остаться рабочей ссылкой.
_URL_RE = re.compile(r"https?://\S+")

# Общие исключения для широкого сторожа: ссылки/email, хэштеги, @упоминания/юзернеймы,
# токены с суффиксом `_bot` (username бота) и путь к паттерн-ассету.
_EXEMPT_RE = re.compile(
    r"https?://\S+"
    r"|[\w.+-]+@[\w.-]+\.\w+"
    r"|@[\w]+"
    r"|#\w+"
    r"|\b[\w-]*_bot\b"
    r"|pattern/[\w.-]+"
)

# snake_case-токены (localStorage-ключи, коды настроек и т.п.) — код-идентификаторы,
# не человеко-видимый текст (правило исключений из объектива задачи).
_SNAKE_IDENTIFIER_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# JS-комментарии (// ...) — разработческий текст, не видимый пользователю.
_JS_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.MULTILINE)

# Инлайновый код в обратных кавычках и цель markdown-ссылки `](...)` — код/URL, не
# человеко-видимая проза. Заборы ```...``` вырезаются построчно (см. _iter_cleaned_lines),
# т.к. re.sub с DOTALL по всему тексту сдвинул бы номера строк в отчёте о падении.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")


def _contains_latin_brand(text: str) -> bool:
    lowered = _URL_RE.sub("", text.lower())
    return any(sub in lowered for sub in LATIN_BRAND_SUBSTRINGS)


def _contains_owner_latin_brand(text: str) -> bool:
    """Широкая проверка (задача 260903): AIESEC|YouLead вне хэштегов/юзернеймов/ссылок/
    email/`_bot`-токенов/паттерн-ассета/код-идентификаторов."""
    if not isinstance(text, str):
        return False
    cleaned = _EXEMPT_RE.sub(" ", text.lower())
    cleaned = _SNAKE_IDENTIFIER_RE.sub(" ", cleaned)
    return any(sub in cleaned for sub in LATIN_OWNER_BRAND_SUBSTRINGS)


def test_preset_labels_are_cyrillic():
    # Сверка с источником (quick 260904-183) — новый пресет без человеческой подписи роняет
    # тест, а не тихо исчезает из клавиатуры (`build_miniapp_theme_keyboard` пропускает
    # пресеты без записи в _PRESET_LABELS).
    assert set(_PRESET_LABELS) == set(_PRESET_BLURBS) == set(web_theme.PRESETS)

    offenders = [
        key for key, value in {**_PRESET_LABELS, **_PRESET_BLURBS}.items()
        if _contains_latin_brand(value)
    ]
    assert not offenders, f"латиница бренда в подписях/описаниях пресетов: {offenders}"


def test_preset_label_has_no_nested_parens():
    offenders = [key for key, value in _PRESET_LABELS.items() if "(" in value]
    assert not offenders, f"скобки в подписи пресета дадут вложенные скобки: {offenders}"


def test_registry_labels_and_prompts_are_cyrillic():
    offenders = []

    for key, entry in SETTINGS_SCHEMA.items():
        label = entry.get("label")
        if label is not None and _contains_latin_brand(label):
            offenders.append(f"SETTINGS_SCHEMA[{key!r}].label")
        prompt = entry.get("prompt")
        if prompt is not None and _contains_latin_brand(prompt):
            offenders.append(f"SETTINGS_SCHEMA[{key!r}].prompt")

    for key, label in reg_labels.REG_LABELS.items():
        if label is not None and _contains_latin_brand(label):
            offenders.append(f"REG_LABELS[{key!r}]")

    assert not offenders, f"латиница бренда в реестре/подписях анкеты: {offenders}"


def test_registry_defaults_have_no_owner_latin_brand():
    """Задача 260903: `default` того же реестра — превью-значения и списки по умолчанию
    (напр. `modcard_fields`) не должны протаскивать AIESEC/YouLead человеку, даже если это
    не label/prompt. Код-значения (`bluebook`, ключи полей вроде `aiesec_role`) исключены
    через _contains_owner_latin_brand (snake_case-идентификаторы)."""
    offenders = []

    for key, entry in SETTINGS_SCHEMA.items():
        default = entry.get("default")
        values = default if isinstance(default, list) else [default]
        for value in values:
            if isinstance(value, str) and _contains_owner_latin_brand(value):
                offenders.append(f"SETTINGS_SCHEMA[{key!r}].default")

    assert not offenders, f"латиница бренда в дефолтах реестра: {offenders}"


def test_reg_options_labels_have_no_owner_latin_brand():
    """Задача 260903: варианты ответов анкеты (reg_options.py) — то, что делегат реально
    видит кнопками. Коды городов/департаментов (Moscow, OGV и т.п.) — не бренд, не задеты
    этой проверкой (в списке только aiesec/youlead)."""
    offenders = []

    for name in dir(reg_options):
        if not name.isupper():
            continue
        value = getattr(reg_options, name)
        if not isinstance(value, list):
            continue
        for item in value:
            candidates = item if isinstance(item, tuple) else (item,)
            for candidate in candidates:
                if isinstance(candidate, str) and _contains_owner_latin_brand(candidate):
                    offenders.append(f"reg_options.{name}: {candidate!r}")

    assert not offenders, f"латиница бренда в вариантах ответа анкеты: {offenders}"


def test_miniapp_js_display_strings_have_no_owner_latin_brand():
    """Задача 260903: строковые литералы фронтенда Mini App (miniapp/static/js/**). Ключи
    localStorage (snake_case, напр. `aiesec_miniapp_onboarding_seen_v1`) и код-комментарии
    (`// ...`) исключены — это не то, что видит человек."""
    js_root = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "js"
    js_files = sorted(js_root.rglob("*.js"))
    assert js_files, "не нашли JS Mini App — проверь путь miniapp/static/js"

    offenders = []
    for path in js_files:
        text = path.read_text(encoding="utf-8")
        no_comments = _JS_LINE_COMMENT_RE.sub("", text)
        if _contains_owner_latin_brand(no_comments):
            offenders.append(str(path.relative_to(js_root.parent.parent.parent)))

    assert not offenders, f"латиница бренда в JS Mini App: {offenders}"


def _iter_cleaned_doc_lines(text: str):
    """Отдаёт (номер_строки, очищенная_строка) — вырезает код-заборы целиком, инлайновый
    код и цели markdown-ссылок построчно, чтобы номер строки в отчёте о падении совпадал
    с номером строки в исходном файле."""
    in_fence = False
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if raw_line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _INLINE_CODE_RE.sub(" ", raw_line)
        line = _LINK_TARGET_RE.sub(" ", line)
        yield lineno, line


def test_human_docs_have_no_owner_latin_brand():
    """Задача 260903 (правило владельца, закон РФ): менеджер и делегат нигде не должны
    читать латинские «AIESEC»/«YouLead» вне ссылок/email/юзернеймов/хэштегов/кода — три
    дока, которые они реально читают: docs/BOT_GUIDE.md (делегат), docs/ADMIN_CHEATSHEET.md и
    docs/ADMIN_GUIDE.md (менеджер).

    README.md и CLAUDE.md сюда намеренно НЕ входят — это доки для разработчика, их не
    видит ни делегат, ни менеджер, правило владельца на них не распространяется.
    """
    docs_root = Path(__file__).resolve().parent.parent / "docs"
    doc_names = ("BOT_GUIDE.md", "ADMIN_CHEATSHEET.md", "ADMIN_GUIDE.md")

    offenders = []
    for name in doc_names:
        text = (docs_root / name).read_text(encoding="utf-8")
        for lineno, line in _iter_cleaned_doc_lines(text):
            if _contains_owner_latin_brand(line):
                offenders.append(f"{name}:{lineno}: {line.strip()}")

    assert not offenders, "латиница бренда в человеческих доках: " + "; ".join(offenders)


def test_aiesec_role_label_matches_registry():
    assert (
        reg_labels.REG_LABELS["reg_q_aiesec_role"]
        == SETTINGS_SCHEMA["reg_q_aiesec_role"]["label"]
    )
