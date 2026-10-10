"""Исправления по ревью фазы: фильтры рассылки, вопросы без вариантов и пр."""
from __future__ import annotations

import domain.cities as cities
from database import db, quiz_db as qz, session_enroll_db as se_db
from tests._enroll38 import CITY, add_user, ready, run, seed_delegates, seed_msk_program


def test_null_city_delegate_in_enroll_and_quiz_filters(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_msk_program()
        d = await seed_delegates()
        await add_user(301, city=None)
        scope = cities.city_scope(CITY)
        exclude = list(scope[1]) if scope else []
        for spec in (
            [{"field": "session_enroll", "value": db.SESSION_ENROLL_NONE, "city": CITY,
              "exclude": exclude}],
            [{"field": "quiz", "value": db.QUIZ_NOT_PASSED, "city": CITY, "exclude": exclude}],
        ):
            got = await db.count_and_list_filtered(spec)
            assert 301 in got and d["cur1"] in got

    run(go())


def test_refresh_city_filter_spec_recomputes_exclude_for_enroll_and_quiz():
    spec = [{"field": "quiz", "value": "x", "city": CITY, "exclude": ["old"]},
            {"field": "session_enroll", "value": "none", "city": "zz-unknown", "exclude": []}]
    assert cities.refresh_city_filter_spec(spec) is None
    out = cities.refresh_city_filter_spec(spec[:1])
    assert out[0]["exclude"] == list(cities.city_scope(CITY)[1])


# ── Вопрос без вариантов ─────────────────────────────────────────────────────────────────────

def test_question_without_options_blocks_enable_and_activity(tmp_path):
    from handlers.forum import admin_quiz as aq
    from tests.test_admin_enroll_38 import FakeCallback

    ready(tmp_path)

    async def go():
        quiz = await qz.get_or_create_quiz(CITY)
        qid = await qz.create_question(quiz["id"], "Пустой вопрос")
        cb = FakeCallback("prog_qzsw:msk")
        await aq.prog_qzsw(cb)
        assert (await qz.get_quiz(quiz["id"]))["enabled"] == 0
        assert "Пустой вопрос" in str(cb.answers if hasattr(cb, "answers") else vars(cb))
        await qz.update_quiz(quiz["id"], enabled=1)
        assert await qz.active_quiz_for_city(CITY) is None
        await qz.create_option(qid, "Да")
        assert await qz.active_quiz_for_city(CITY) is not None

    run(go())


def test_parse_csv_rejects_question_without_options():
    from services.forum.quiz_import import parse_csv

    parsed = parse_csv("Вопрос;Вариант;Компетенция;Баллы\nЕсть вопрос;;;\n".encode("utf-8"), {}, 5)
    assert parsed.errors


def test_current_question_skips_question_without_options(tmp_path):
    from services.forum import quiz as svc

    ready(tmp_path)

    async def go():
        quiz = await qz.get_or_create_quiz(CITY)
        await qz.create_question(quiz["id"], "Пустой")
        q2 = await qz.create_question(quiz["id"], "Нормальный")
        await qz.create_option(q2, "Да")
        attempt_id = await qz.create_attempt(1, quiz["id"], quiz["content_version"])
        cur = await svc.current_question(await qz.get_attempt(attempt_id))
        assert cur[0]["id"] == q2

    run(go())


def test_staff_enroll_validates_delegate(tmp_path):
    from domain.cities import per_city_key
    from services.forum import session_enroll as svc

    ready(tmp_path)

    async def go():
        p = await seed_msk_program()
        d = await seed_delegates()
        await add_user(201, city="spb")
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        assert (await svc.enroll_by_staff(999, p["A"], by_staff_id=1)).status == "denied"
        assert (await svc.enroll_by_staff(d["pending"], p["A"], by_staff_id=1)).status == "denied"
        assert (await svc.enroll_by_staff(201, p["A"], by_staff_id=1)).status == "wrong_city"
        assert (await svc.enroll_by_staff(d["cur1"], p["A"], by_staff_id=1)).status == "ok"
        assert await se_db.user_enrollment_ids(999) == set()

    run(go())


def test_limit_below_enrolled_and_time_change_ask_confirmation(tmp_path):
    from handlers.forum import admin_enroll, admin_enroll_guard as g, admin_program
    from handlers.states import ProgramEnrollLimit, ProgramSessionField
    from tests.test_admin_enroll_38 import FakeCallback, FakeMessage, new_state

    ready(tmp_path)

    async def go():
        p = await seed_msk_program()
        d = await seed_delegates()
        await se_db.enroll_tx(d["cur1"], p["A"])
        await se_db.enroll_tx(d["cur2"], p["A"])
        # лимит 1 при двух записанных — вопрос, значение пока не пишется
        state = new_state()
        await state.set_state(ProgramEnrollLimit.value)
        await state.update_data(enr_sid=p["A"])
        msg = FakeMessage("1")
        await admin_enroll.prog_enrlim_step(msg, state)
        assert "Записано 2 человека, а лимит 1" in msg.answers_sent[0]
        assert (await db.get_program_session(p["A"]))["enroll_limit"] is None
        cb = FakeCallback(f"prog_lmok:{p['A']}:1")
        await g.prog_lmok(cb)
        assert (await db.get_program_session(p["A"]))["enroll_limit"] == 1
        # лимит не ниже числа записанных — применяется сразу
        state2 = new_state()
        await state2.set_state(ProgramEnrollLimit.value)
        await state2.update_data(enr_sid=p["A"])
        await admin_enroll.prog_enrlim_step(FakeMessage("5"), state2)
        assert (await db.get_program_session(p["A"]))["enroll_limit"] == 5
        # «²» — не число
        state3 = new_state()
        await state3.set_state(ProgramEnrollLimit.value)
        await state3.update_data(enr_sid=p["A"])
        bad = FakeMessage("²")
        await admin_enroll.prog_enrlim_step(bad, state3)
        assert "Нужно целое число" in bad.answers_sent[0]
        # время
        state4 = new_state()
        await state4.set_state(ProgramSessionField.time)
        await state4.update_data(pmode="edit", pf_session_id=p["A"])
        tm = FakeMessage("12:00-13:00")
        await admin_program.prog_time_step(tm, state4)
        assert "Записано 2 человека" in tm.answers_sent[0] and "время изменится" in tm.answers_sent[0]
        assert (await db.get_program_session(p["A"]))["start_time"] == "10:00"
        await g.prog_tmok(FakeCallback(f"prog_tmok:{p['A']}"), state4)
        assert (await db.get_program_session(p["A"]))["start_time"] == "12:00"

    run(go())


def test_parallel_start_gives_single_open_attempt(tmp_path):
    ready(tmp_path)

    async def go():
        quiz = await qz.get_or_create_quiz(CITY)
        a = await qz.create_attempt(7, quiz["id"], quiz["content_version"])
        b = await qz.create_attempt(7, quiz["id"], quiz["content_version"])
        assert a == b and await qz.count_open_attempts(quiz["id"]) == 1
        await qz.finish_attempt(a, {})
        c = await qz.create_attempt(7, quiz["id"], quiz["content_version"])
        assert c != a

    run(go())


def test_already_enrolled_on_full_session_is_not_full(tmp_path):
    from domain.cities import per_city_key
    from services.forum import session_enroll as svc

    ready(tmp_path)

    async def go():
        p = await seed_msk_program()
        d = await seed_delegates()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        await db.update_program_session(p["A"], enroll_limit=1)
        assert (await svc.enroll(d["cur1"], p["A"])).status == "ok"
        assert (await svc.enroll(d["cur1"], p["A"])).status == "already"
        assert (await svc.enroll(d["cur2"], p["A"])).status == "full"

    run(go())


def test_unavailable_session_has_own_text(tmp_path):
    from services.forum import session_enroll as svc

    ready(tmp_path)
    for status in ("wrong_city", "not_enrollable", "no_session"):
        assert svc.EnrollOutcome(status).text_key == "session_enroll_unavailable_text"
    assert svc.EnrollOutcome("ok").text_key is None
    assert "session_enroll_scan_error_text" in svc.ENROLL_TEXT_KEYS


def test_hint_not_duplicated_when_prompt_lists_placeholders():
    import domain.settings.placeholders as sp
    from domain.settings.schema import SETTINGS_SCHEMA

    prompt = SETTINGS_SCHEMA["session_enroll_slot_text"]["prompt"]
    assert "Подстановки:" in prompt and sp.hint("session_enroll_slot_text", prompt) == ""
    assert sp.hint("session_enroll_slot_text") != ""


def test_menu_label_cannot_equal_other_button():
    from domain.settings.validation import validate_setting_value

    value, error = validate_setting_value("quiz_menu_label", "📞 Контакты")
    assert value is None and "уже у кнопки «📞 Контакты»" in error
    assert validate_setting_value("quiz_menu_label", "🧭 Тест")[1] is None  # собственная подпись
    assert validate_setting_value("quiz_menu_label", "🧭 Проверка навыков")[1] is None
