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
        if reg_engine.label_key_for(step) in reg_labels.REG_LABELS
    ]
    assert list(mc.CARD_STEPS.keys()) == expected_keys
    assert "full_name" not in mc.CARD_STEPS  # ФИО спрашивается вне REG_FLOW, не «вопрос»


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

    user3 = {"work_status": None}
    answers3 = dict(mc.card_answers(user3, ["work_status"], 300))
    assert mc.CARD_STEPS["work_status"] not in answers3  # None — строки нет


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
