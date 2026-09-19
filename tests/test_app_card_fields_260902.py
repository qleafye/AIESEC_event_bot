"""Quick 260902-tzh: карточка заявки печатает ответы анкеты по единой схеме
(`reg_engine.STEP_TO_COLUMN`/`label_for`), набор вопросов и лимит длины ответа — реестром,
переполнение карточки отдаётся по кнопке «📄 Полная анкета».

RED (Task 1): сервис `moderation_card.py` ещё не существует — набор обязан упасть на
ImportError. Тесты реестра/экрана «🧾 Поля карточки заявки» (Task 3) дописаны в этот же файл
ниже отдельным блоком, после сервисных тестов.
"""
from __future__ import annotations

import asyncio

import reg_engine
import reg_labels
import moderation_card as mc
from handlers import admin_moderation as am


def _run(coro):
    return asyncio.run(coro)


# ── сервис: CARD_STEPS / DEFAULT_CARD_STEPS — выведены из единой схемы анкеты ──────────────

def test_card_steps_derived_from_engine_not_a_second_schema():
    assert isinstance(mc.CARD_STEPS, dict)
    for step_key, label in mc.CARD_STEPS.items():
        assert label == reg_engine.label_for(step_key)
    expected_keys = [
        step for step in reg_engine.STEP_TO_COLUMN
        if step != reg_engine.FULL_NAME_STEP
        and reg_engine.label_key_for(step) in reg_labels.REG_LABELS
    ]
    assert list(mc.CARD_STEPS.keys()) == expected_keys
    # ФИО с 16.09 ИМЕЕТ подпись (reg_q_full_name — нужна мастеру приложения), но карточка
    # заявки печатает имя в заголовке, не строкой ответа — исключение явное, не по отсутствию
    # подписи (см. комментарий у CARD_STEPS в moderation_card.py).
    assert "full_name" not in mc.CARD_STEPS


def test_default_selection_is_the_selection_set():
    assert len(mc.DEFAULT_CARD_STEPS) == 20
    assert len(set(mc.DEFAULT_CARD_STEPS)) == 20
    assert all(k in mc.CARD_STEPS for k in mc.DEFAULT_CARD_STEPS)
    expected = {
        "age", "city", "education_status", "university", "course", "local_committee",
        "position", "alumni_status", "aiesec_role", "source", "work_sphere",
        "english_level", "attendance_format", "goal", "expectations", "exp_organizers",
        "exp_content", "missing_skills", "volunteer", "resume",
    }
    assert set(mc.DEFAULT_CARD_STEPS) == expected


def test_enabled_steps_filters_garbage_and_honours_sentinel():
    assert mc.enabled_steps(None) == list(mc.DEFAULT_CARD_STEPS)
    assert mc.enabled_steps(["city", "нет-такого-шага"]) == ["city"]
    assert mc.enabled_steps([mc.EMPTY_SENTINEL]) == []
    # Порядок результата — порядок CARD_STEPS (age раньше goal в REG_FLOW), не порядок
    # пришедшего списка ("goal" передан первым).
    assert mc.enabled_steps(["goal", "age"]) == ["age", "goal"]


# ── сервис: card_answers — составные/булевы колонки, обрезка длинных ответов ───────────────

def test_card_answers_composite_and_bool_columns():
    user1 = {
        "expectations": "весело", "expectations_ar": "ممتع",
        "work_status": 1, "is_ambassador_candidate": 1, "city": "",
    }
    answers1 = dict(mc.card_answers(user1, ["expectations", "work_status", "ambassador", "city"], 300))
    assert answers1[mc.CARD_STEPS["expectations"]] == "весело / ممتع"
    assert answers1[mc.CARD_STEPS["work_status"]] == "Да"
    assert answers1[mc.CARD_STEPS["ambassador"]] == "Да"
    assert mc.CARD_STEPS["city"] not in answers1  # пустая строка — строки нет

    user2 = {"work_status": 0, "is_ambassador_candidate": 0}
    answers2 = dict(mc.card_answers(user2, ["work_status", "ambassador"], 300))
    assert answers2[mc.CARD_STEPS["work_status"]] == "Нет"
    assert mc.CARD_STEPS["ambassador"] not in answers2  # 0 → пропуск строки (второе значение None)


def test_card_answers_truncates_to_limit():
    long_val = "x" * 500
    answers = dict(mc.card_answers({"goal": long_val}, ["goal"], 300))
    val = answers[mc.CARD_STEPS["goal"]]
    assert len(val) == 301
    assert val == "x" * 300 + "…"

    short_val = "y" * 50
    answers2 = dict(mc.card_answers({"goal": short_val}, ["goal"], 300))
    assert answers2[mc.CARD_STEPS["goal"]] == short_val


# ── рендер карточки: fields/show_resume, эскейп, обратная совместимость ────────────────────

def test_card_shows_enabled_labels_and_hides_disabled():
    user = {"full_name": "Иван", "city": "Москва", "goal": "Нетворкинг", "university": "ВШЭ"}
    out = am._render_application_card(user, 1, 1, fields=mc.card_answers(user, ["city", "goal"], 300))
    assert "🏙 Город: Москва" in out
    assert "🎯 Цель участия: " in out
    assert "🏫 ВУЗ" not in out  # значение в user есть, но шаг не включён в fields


def test_card_escapes_answers():
    user = {"full_name": "Иван", "city": "<b>x</b>"}
    out = am._render_application_card(user, 1, 1, fields=mc.card_answers(user, ["city"], 300))
    assert "<b>x</b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_card_without_fields_is_byte_identical_to_service_only_card():
    out = am._render_application_card({"full_name": "Иван"}, 1, 1)
    assert out == "📋 <b>Заявка 1/1</b>\n\n👤 Иван\n📎 Резюме: нет"


def test_resume_block_hidden_when_resume_step_off():
    user = {"full_name": "Иван"}
    out_off = am._render_application_card(user, 1, 1, show_resume=False)
    assert "Резюме" not in out_off
    out_on = am._render_application_card(user, 1, 1)
    assert "📎 Резюме: нет" in out_on


# ── резюме: ссылка/мини-профиль/маркер потери данных (приёмка 19.09, review-260919) ─────────

def test_resume_block_shows_link_kind():
    """Развилка R2b — ссылка вместо файла; раньше карточка бота эту ветку не печатала вовсе."""
    user = {"full_name": "Иван", "resume_link": "https://hh.ru/resume/123"}
    out = am._render_application_card(user, 1, 1)
    assert "📎 Резюме (ссылка): https://hh.ru/resume/123" in out


def test_resume_block_shows_mini_profile_fields():
    """Находки №2/№3: ветка «мини-профиль» — карточка показывает подполя с подписями, а не
    «резюме не приложено»."""
    user = {
        "full_name": "Иван", "resume_type": "mini",
        "mini_projects": "Бот для АЙСЕК",
        "mini_direction": "Бэкенд",
    }
    out = am._render_application_card(user, 1, 1)
    assert "📎 Резюме: мини-профиль" in out
    assert f"{mc.CARD_STEPS['mini_projects']}: Бот для АЙСЕК" in out
    assert f"{mc.CARD_STEPS['mini_direction']}: Бэкенд" in out
    assert "mini_projects" not in out  # кодовое значение делегату/менеджеру не показываем


def test_resume_block_warns_when_type_set_but_data_lost():
    """`resume_type` задан, но ни один карман не заполнен — сигнал потери данных, не тихое
    «нет резюме»."""
    user = {"full_name": "Иван", "resume_type": "mini"}
    out = am._render_application_card(user, 1, 1)
    assert "📎 Резюме: ⚠️ резюме не сохранилось" in out


# ── сентинел «-» и реципрокная пара age/birth_date (приёмка 19.09, review-260919 «Модерация») ─

def test_card_answers_skips_sentinel_dash_for_disabled_questions():
    """Находка №1: вопрос выключен на анкете, но столбец хранит `reg_engine`'ов сентинел «-» —
    строка не печатается (раньше карточка печатала «Сфера работы: -»)."""
    user = {"full_name": "Иван", "work_sphere": "-"}
    fields = mc.card_answers(user, ["work_sphere"], 300)
    assert fields == []
    out = am._render_application_card(user, 1, 1, fields=fields)
    assert mc.CARD_STEPS["work_sphere"] not in out


def test_card_answers_still_shows_real_dash_free_value():
    """Сентинел фильтруется ТОЛЬКО как литерал «-» — обычный непустой ответ печатается как
    раньше."""
    user = {"full_name": "Иван", "work_sphere": "IT"}
    fields = mc.card_answers(user, ["work_sphere"], 300)
    assert fields == [(mc.CARD_STEPS["work_sphere"], "IT")]


def test_card_shows_age_computed_from_birth_date_when_age_missing():
    """Находка №1: `age` выключен, у делегата только `birth_date` (новая схема, 17-19.09) — при
    выбранном шаге `age` карточка вычисляет возраст по МСК, а не молчит."""
    user = {"full_name": "Иван", "birth_date": "01.09.2007"}
    fields = mc.card_answers(user, ["age"], 300)
    assert fields  # непустое — возраст вычислен
    label, value = fields[0]
    assert label == mc.CARD_STEPS["age"]
    assert value.isdigit()


def test_card_shows_raw_age_when_birth_date_field_selected_but_empty():
    """Обратный случай: менеджер выбрал «Дата рождения», у делегата только `age` (старая схема,
    до 16.09) — карточка показывает «Возраст: N», а не пустоту."""
    user = {"full_name": "Иван", "age": "19"}
    fields = mc.card_answers(user, ["birth_date"], 300)
    assert fields == [(mc.CARD_STEPS["age"], "19")]


def test_card_shows_both_when_both_selected_and_both_have_real_data():
    """И `age`, и `birth_date` включены разом, у делегата есть оба реальных значения — обе
    строки печатаются (не дублируются, не теряются)."""
    user = {"full_name": "Иван", "age": "19", "birth_date": "01.09.2007"}
    fields = mc.card_answers(user, ["age", "birth_date"], 300)
    assert fields == [
        (mc.CARD_STEPS["age"], "19"),
        (mc.CARD_STEPS["birth_date"], "01.09.2007"),
    ]


def test_card_dedups_when_both_selected_but_only_one_has_data():
    """И `age`, и `birth_date` включены разом, но данные есть только у ОДНОГО — вторая строка
    не дублирует ту же информацию под своим ярлыком."""
    user = {"full_name": "Иван", "age": "19"}
    fields = mc.card_answers(user, ["age", "birth_date"], 300)
    assert fields == [(mc.CARD_STEPS["age"], "19")]


# ── переполнение карточки: fit_card / split_for_telegram / кнопка «📄 Полная анкета» ────────

def test_fit_card_flags_overflow_and_cuts_on_line_boundary():
    short_text = "привет"
    assert mc.fit_card(short_text) == (short_text, False)

    lines = [f"строка {i} " + "x" * 40 for i in range(200)]
    long_text = "\n".join(lines)
    assert len(long_text) > mc.CARD_TEXT_LIMIT
    result, overflow = mc.fit_card(long_text)
    assert overflow is True
    assert len(result) <= mc.CARD_TEXT_LIMIT
    assert result.endswith(mc.OVERFLOW_HINT)
    body = result[: -len(mc.OVERFLOW_HINT)].rstrip("\n")
    assert long_text.startswith(body)  # обрыв ровно по границе строки, не посередине


def test_full_button_only_on_overflow():
    kb = _run(am._appr_card_kb(1, False, 1))
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "appr_full:1" not in flat

    kb2 = _run(am._appr_card_kb(1, False, 1, has_full=True))
    rows2 = kb2.inline_keyboard
    flat2 = [b.callback_data for row in rows2 for b in row]
    assert "appr_full:1" in flat2
    full_row = next(row for row in rows2 if any(b.callback_data == "appr_full:1" for b in row))
    row_cbs = [b.callback_data for b in full_row]
    assert "appr_skip:1" in row_cbs


def test_split_for_telegram_chunks_fit_and_keep_lines():
    lines = [f"строка {i} " + "x" * 30 for i in range(150)]
    text = "\n".join(lines)
    assert len(text) > mc.TELEGRAM_LIMIT
    chunks = mc.split_for_telegram(text)
    assert all(len(c) <= mc.TELEGRAM_LIMIT for c in chunks)
    assert "\n".join(chunks) == text
    for chunk in chunks:
        for line in chunk.split("\n"):
            assert line in lines


def test_split_for_telegram_hard_cuts_single_line_longer_than_limit():
    """Полная анкета: вопрос+ответ+HTML-экранирование дали одну строку за 4096 символов —
    режется жёстко на куски по `limit`, конкатенация БЕЗ разделителя равна исходнику."""
    long_line = "z" * 5000
    chunks = mc.split_for_telegram(long_line)
    assert all(len(c) <= mc.TELEGRAM_LIMIT for c in chunks)
    assert "".join(chunks) == long_line
    assert len(chunks) == 2


def test_split_for_telegram_hard_cuts_long_line_among_normal_lines():
    head = ["строка 0", "строка 1"]
    tail = ["строка 2", "строка 3", "строка 4"]
    long_line = "y" * 4500
    text = "\n".join(head + [long_line] + tail)
    chunks = mc.split_for_telegram(text)
    assert all(len(c) <= mc.TELEGRAM_LIMIT for c in chunks)
    expected = [
        "\n".join(head),
        long_line[: mc.TELEGRAM_LIMIT],
        long_line[mc.TELEGRAM_LIMIT:],
        "\n".join(tail),
    ]
    assert chunks == expected


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 3 — экран «🧾 Поля карточки заявки»: реестр, IA, сторожа
# ═══════════════════════════════════════════════════════════════════════════════════════════

from pathlib import Path

from settings_schema import SETTINGS_SCHEMA, _parse_setting
from settings_synonyms import SETTINGS_SYNONYMS
from handlers import admin_sections as sec
from handlers import admin_settings
from handlers import admin_modcard
from handlers.admin_caps import ADMIN_CAPS


def _db_ready(tmp_path):
    from config import config
    from database import db

    config.DB_PATH = str(tmp_path / "test_app_card_fields_260902.db")
    _run(db.init_db())


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class _FakeCallback:
    def __init__(self, data, user_id=900001):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_registry_keys_live_in_apps_group():
    for key in ("modcard_fields", "modcard_answer_limit"):
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "apps"
        _parse_setting(key, entry["default"])  # не падает
    # Quick 260906-6xe: modcard_fields переведён на закрытый тип `multi` — веб рисует
    # чекбоксы с подписями (см. tests/test_settings_multi_260906.py), чтение бота (эта
    # ветка _parse_setting) не изменилось ни на йоту.
    modcard_entry = SETTINGS_SCHEMA["modcard_fields"]
    assert modcard_entry["type"] == "multi"
    assert modcard_entry["options_ref"] == "moderation_card:CARD_STEPS"
    assert modcard_entry["empty_value"] == mc.EMPTY_SENTINEL
    assert SETTINGS_SCHEMA["modcard_answer_limit"]["type"] == "int"


def test_registry_default_matches_service_default():
    assert SETTINGS_SCHEMA["modcard_fields"]["default"] == list(mc.DEFAULT_CARD_STEPS)


def test_registry_keys_not_added_to_bot_group_screen():
    field_keys = {k for k, _, _ in admin_settings.SETTINGS_FIELDS}
    assert "modcard_fields" not in field_keys
    assert "modcard_answer_limit" not in field_keys


def test_synonyms_cover_new_keys():
    for key in ("modcard_fields", "modcard_answer_limit"):
        assert key in SETTINGS_SYNONYMS
        assert len(SETTINGS_SYNONYMS[key]) >= 2


def test_screen_declared_in_apps_section_and_capability_guarded():
    apps_rows = next(rows for token, _label, rows in sec.SECTIONS if token == "apps")
    assert ("screen", "modcard_open", "🧾 Поля карточки заявки") in apps_rows
    assert ADMIN_CAPS["modcard_open"] == "settings"
    assert ADMIN_CAPS["appr_full:*"] == "moderate_reg"
    assert sec.back_button("modcard_open").callback_data == "admin_sec:apps"


def test_toggle_writes_sentinel_when_nothing_selected(tmp_path):
    _db_ready(tmp_path)
    from database.db import get_setting, set_setting

    _run(set_setting("modcard_fields", "age"))
    callback = _FakeCallback("modcard_toggle:age")
    _run(admin_modcard.modcard_toggle(callback))
    raw = _run(get_setting("modcard_fields"))
    assert raw == mc.EMPTY_SENTINEL
    assert mc.enabled_steps([raw]) == []
    assert callback.message.edit_calls == 1


def test_full_card_handler_checks_city_scope():
    source_path = Path(__file__).resolve().parent.parent / "handlers" / "admin_moderation.py"
    text = source_path.read_text(encoding="utf-8")
    start = text.index("async def appr_full(")
    end = text.index("# Quick 260902-tzh: handlers/admin_modcard.py", start)
    body = text[start:end]
    assert "_card_out_of_scope" in body


def test_keyboard_never_shows_step_codes():
    kb = admin_modcard.build_modcard_keyboard(list(mc.DEFAULT_CARD_STEPS), 300)
    for row in kb.inline_keyboard:
        for button in row:
            assert "_" not in button.text
            assert button.text not in mc.CARD_STEPS


# ── Квик 260919-m9x: связь «вопрос анкеты ↔ поле карточки» ───────────────────────────────
# 16–17.09 на проде выключили «Возраст» и включили «Дату рождения» — карточка заявки об этом
# не узнала: у новых заявок не появилась дата, у старых пропал возраст, менеджер пришёл с
# «баг?». Сторож держит пометку ⚠️, кнопку синхронизации и то, что она ничего не снимает.

def _set_q(key: str, value: str) -> None:
    from database.db import set_setting

    _run(set_setting(key, value))


def test_asked_but_hidden_question_is_marked(tmp_path):
    _db_ready(tmp_path)
    from database.db import set_setting

    # Анкета спрашивает дату рождения, карточка показывает только возраст.
    _set_q("reg_q_birth_date", "on")
    _set_q("reg_q_age", "off")
    _run(set_setting("modcard_fields", "age"))

    asked = _run(admin_modcard.asked_steps())
    assert "birth_date" in asked and "age" not in asked
    missing = admin_modcard.missing_steps(["age"], asked)
    assert "birth_date" in missing and "age" not in missing

    text = _run(admin_modcard.render_modcard_text())
    assert "Спрашиваем в анкете, но не показываем" in text
    assert f"⚠️ {mc.CARD_STEPS['birth_date']}" in text
    assert f"✅ {mc.CARD_STEPS['age']}" in text

    kb = admin_modcard.build_modcard_keyboard(["age"], 300, asked)
    sync = [b for row in kb.inline_keyboard for b in row if b.callback_data == "modcard_sync"]
    assert len(sync) == 1


def test_sync_button_absent_when_nothing_is_hidden():
    kb = admin_modcard.build_modcard_keyboard(["age"], 300, {"age"})
    assert not [b for row in kb.inline_keyboard for b in row if b.callback_data == "modcard_sync"]


def test_sync_adds_asked_questions_and_keeps_old_ones(tmp_path):
    _db_ready(tmp_path)
    from database.db import get_setting, set_setting

    _set_q("reg_q_birth_date", "on")
    _set_q("reg_q_age", "off")
    _run(set_setting("modcard_fields", "age"))

    callback = _FakeCallback("modcard_sync")
    _run(admin_modcard.modcard_sync(callback))

    steps = mc.enabled_steps((_run(get_setting("modcard_fields")) or "").split("\n"))
    # Дата рождения добавлена, возраст НЕ снят: у заявок до переключения ответ на него есть.
    assert "birth_date" in steps and "age" in steps
    assert callback.message.edit_calls == 1

    # Повторное нажатие уже нечего добавлять — экран не перерисовывается, только тост.
    again = _FakeCallback("modcard_sync")
    _run(admin_modcard.modcard_sync(again))
    assert again.message.edit_calls == 0
    assert again.answers and "Уже показываем" in again.answers[0][0]
