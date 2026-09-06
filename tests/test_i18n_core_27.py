"""Phase 27 (27-02, LANG-01/02) — сторож ядра перевода: тождественный возврат `tr()` при
`lang == "ru"` (golden-снимки/`is`-тождество Mini App это заметили бы), победа яруса A над
машинным переводом, устойчивость `src_hash`, лестница `resolve_lang`, покрытие яруса A и
дефолт-off реестровых тумблеров.

pytest-asyncio в этом окружении нет (см. tests/test_db_phase5.py) — асинхронные проверки идут
через asyncio.run().
"""
import asyncio

import pytest

import reg_engine
from i18n_ui_en import UI_EN
from services.i18n import context, delegate_lang, load_map, resolve_lang, src_hash, tr
from settings_schema import SETTINGS_SCHEMA


# ── tr() ─────────────────────────────────────────────────────────────────────────────────

def test_tr_returns_same_object_for_russian():
    text = "Уникальный объект строки"
    assert tr(text, "ru", {}) is text
    # Тот же объект даже если карта непустая и в ней (гипотетически) есть перевод.
    assert tr(text, "ru", {src_hash(text): "Should not matter"}) is text


def test_tr_fail_soft_on_empty_text():
    assert tr(None, "en", {"x": "y"}) is None
    assert tr("", "en", {"x": "y"}) == ""


def test_tr_no_translation_in_map_returns_russian_as_is():
    text = "Текст без перевода в карте"
    assert tr(text, "en", {}) == text
    assert tr(text, "en", {src_hash("другой текст"): "irrelevant"}) == text


def test_tr_layer_a_wins_over_machine_map():
    ru = "Отмена"
    assert ru in UI_EN
    machine_map = {src_hash(ru): "MACHINE TRANSLATION (stale or just different)"}
    assert tr(ru, "en", machine_map) == UI_EN[ru]


def test_tr_uses_machine_map_when_no_layer_a_entry():
    ru = "Уникальный текст вопроса анкеты, которого нет в UI_EN"
    assert ru not in UI_EN
    machine_map = {src_hash(ru): "Machine translated text"}
    assert tr(ru, "en", machine_map) == "Machine translated text"


# ── src_hash ─────────────────────────────────────────────────────────────────────────────

def test_src_hash_ignores_surrounding_whitespace():
    assert src_hash("Привет, делегат!") == src_hash("  Привет, делегат!  ")
    assert src_hash("Привет, делегат!") == src_hash("Привет, делегат!\n")


def test_src_hash_is_a_stable_contract():
    # Контракт хранилища (database/db.py::translations, план 27-02) — этот хеш зашит в БД
    # переводов, менять алгоритм src_hash без миграции существующих строк нельзя.
    assert src_hash("Привет, делегат!") == "2da8e294d232894a8e6c6b29d1fdc671"


def test_src_hash_changes_when_source_changes():
    assert src_hash("Привет, делегат!") != src_hash("Привет, участник!")


# ── Покрытие яруса A ─────────────────────────────────────────────────────────────────────
# Тексты, зашитые внутрь тела `_validate_answer_core` и недоступные интроспекцией снаружи —
# сверено с reg_engine.py на дату плана (2026-09-06). Список — сторож против расхождения
# (несовпадение хотя бы на символ = молчаливый пропуск перевода), а не дубликат кода.
_NON_INTROSPECTABLE_VALIDATION_ERRORS = [
    "Укажи ФИО полностью (минимум фамилию и имя).",
    "Укажи корректный возраст числом от 10 до 120.",
    "Укажи корректный email (например, name@example.com).",
    "Укажи номер телефона или нажми «Пропустить».",
    "Укажи корректный номер телефона или нажми «Пропустить».",
    "Укажи ник в ВК в формате @username (начинается с @, без пробелов).",
    "Напиши резюме текстом или прикрепи файл (PDF или DOCX).",
    "Выбери один из вариантов.",  # education_status; тот же текст, что и у informal_day
    "Выбери курс.",
    "Выбери «Да!» или «Пока нет».",
    "Выбери хотя бы один вариант.",  # multi
    "Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз.",  # date parse
    # validate_date_range — тоже зашиты в тело функции, не в словарь.
    "Дата рождения не может быть в будущем. Проверь и введи ещё раз.",
    "Проверь дату рождения (год выглядит неправдоподобно) и введи ещё раз.",
    "Дата приезда не может быть в прошлом. Введи корректную дату.",
    "Проверь дату приезда (слишком далеко в будущем) и введи ещё раз.",
]


def test_layer_a_covers_choice_empty_and_other_prompt():
    assert reg_engine._CHOICE_EMPTY_ERROR in UI_EN
    assert reg_engine._CHOICE_OTHER_PROMPT in UI_EN


def test_layer_a_covers_bespoke_choice():
    for step_key, (empty_err, other_prompt) in reg_engine._BESPOKE_CHOICE.items():
        assert empty_err in UI_EN, f"{step_key}: {empty_err!r} missing from UI_EN"
        assert other_prompt in UI_EN, f"{step_key}: {other_prompt!r} missing from UI_EN"


def test_layer_a_covers_skip_text_errors():
    for step_key, err in reg_engine._SKIP_TEXT_ERRORS.items():
        assert err in UI_EN, f"{step_key}: {err!r} missing from UI_EN"


def test_layer_a_covers_membership_steps():
    for step_key, (allowed, err, _as_bool) in reg_engine._MEMBERSHIP_STEPS.items():
        for literal in allowed:
            assert literal in UI_EN, f"{step_key}: option {literal!r} missing from UI_EN"
        assert err in UI_EN, f"{step_key}: {err!r} missing from UI_EN"


def test_layer_a_covers_generic_fallback_label():
    for step_type, label in reg_engine._GENERIC_FALLBACK_LABEL.items():
        assert label in UI_EN, f"{step_type}: {label!r} missing from UI_EN"


def test_layer_a_covers_non_introspectable_validation_errors():
    for text in _NON_INTROSPECTABLE_VALIDATION_ERRORS:
        assert text in UI_EN, f"{text!r} missing from UI_EN"


def test_layer_a_covers_control_words():
    for word in ("Готово", "Отмена", "Всё верно", "Изменить", "Да, отменить",
                 "Да", "Нет", "Сохранено", "✅ Принято"):
        assert word in UI_EN, f"{word!r} missing from UI_EN"


# ── resolve_lang ─────────────────────────────────────────────────────────────────────────

def test_resolve_lang_module_off_always_ru():
    assert resolve_lang(False, None, "en") == "ru"
    assert resolve_lang(False, "en", "en") == "ru"  # даже если уже был сохранён "en"


def test_resolve_lang_stored_choice_wins():
    assert resolve_lang(True, "ru", "en") == "ru"
    assert resolve_lang(True, "en", "ru") == "en"


def test_resolve_lang_russian_client_defaults_to_ru():
    assert resolve_lang(True, None, "ru") == "ru"
    assert resolve_lang(True, None, "ru-RU") == "ru"


def test_resolve_lang_unknown_client_language_asks():
    assert resolve_lang(True, None, "de") == "ask"
    assert resolve_lang(True, None, None) == "ask"
    assert resolve_lang(True, None, "en") == "ask"


# ── load_map ─────────────────────────────────────────────────────────────────────────────

def test_load_map_russian_makes_zero_db_reads(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(lang):
        calls["n"] += 1
        return {}

    import database.db as db_mod
    monkeypatch.setattr(db_mod, "fetch_translations", fake_fetch)
    result = asyncio.run(load_map("ru"))
    assert result == {}
    assert calls["n"] == 0


def test_load_map_english_reads_once(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(lang):
        calls["n"] += 1
        return {"h": "v"}

    import database.db as db_mod
    monkeypatch.setattr(db_mod, "fetch_translations", fake_fetch)
    result = asyncio.run(load_map("en"))
    assert result == {"h": "v"}
    assert calls["n"] == 1


# ── delegate_lang / context: fail-soft ─────────────────────────────────────────────────

def test_delegate_lang_fails_soft_to_russian(monkeypatch):
    async def boom(key):
        raise RuntimeError("db is on fire")

    import settings_schema as ss
    monkeypatch.setattr(ss, "get_setting_typed", boom)
    # services.i18n imported get_setting_typed by reference — patch it there too.
    import services.i18n as i18n_mod
    monkeypatch.setattr(i18n_mod, "get_setting_typed", boom)
    result = asyncio.run(delegate_lang(12345))
    assert result == "ru"


# ── Реестр: дефолт off, тумблеры видны менеджеру (не «Прочие») ──────────────────────────

def test_registry_delegate_lang_defaults():
    assert SETTINGS_SCHEMA["delegate_lang_enabled"]["type"] == "enum"
    assert SETTINGS_SCHEMA["delegate_lang_enabled"]["options"] == ["on", "off"]
    assert SETTINGS_SCHEMA["delegate_lang_enabled"]["default"] == "off"
    assert SETTINGS_SCHEMA["delegate_lang_ask_on_start"]["default"] == "on"


def test_registry_delegate_lang_toggles_are_reachable_from_admin_ui():
    """Отклонение от буквы плана (группа `_REG_FIELD_ORDER`/«📝 Регистрация»): оба ключа —
    enum on/off, показ их через generic settings_edit заставил бы менеджера ВВОДИТЬ "on"/"off"
    текстом — прямое нарушение CLAUDE.md («кодовые значения человеку не показываем и ввести
    не просим»). Вместо этого — тот же паттерн, что у соседних модульных тумблеров
    (party_enabled/consent_enabled/quiet_hours_enabled): group="toggles",
    settings_toggle_rows()-строка и кнопка в разделе «📝 Анкета» (handlers/admin_sections.py),
    рядом с toggle_party_enabled/toggle_consent_enabled — тот же принцип, что уже применён к
    toggle_reg_edit_remoderation (group="reg", тоже НЕ в _REG_FIELD_ORDER)."""
    from handlers import admin_sections as sec
    from handlers.admin_settings import settings_toggle_rows

    assert SETTINGS_SCHEMA["delegate_lang_enabled"]["group"] == "toggles"
    assert SETTINGS_SCHEMA["delegate_lang_ask_on_start"]["group"] == "toggles"

    form_rows = next(rows for token, _label, rows in sec.SECTIONS if token == "form")
    form_callbacks = {cb for kind, cb, *_ in form_rows if kind == "toggle"}
    assert "toggle_delegate_lang_enabled" in form_callbacks
    assert "toggle_delegate_lang_ask_on_start" in form_callbacks

    rows = asyncio.run(settings_toggle_rows())
    assert "toggle_delegate_lang_enabled" in rows
    assert "toggle_delegate_lang_ask_on_start" in rows
