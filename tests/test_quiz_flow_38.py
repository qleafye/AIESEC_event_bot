"""Делегатский поток теста компетенций: вход, гейт, вопросы, продолжение, результат, пересдача."""
from cities import per_city_key
from database import db, quiz_db as qz, session_enroll_db as se
from handlers import forum_deeplinks, quiz as h
from services import quiz as svc
from keyboards.menu_dynamic import DynamicMenuText
from tests._enroll38 import CITY, add_user, ready, run
from tests._enroll38_chat import FakeCallback, FakeMessage, buttons, make_state, setup_world

U = 101


async def seed_quiz(*, allow_retake=0, base_level=True, enabled=1):
    await setup_world()
    comp = await se.create_competency(CITY, "Лидерство")
    q = await qz.get_or_create_quiz(CITY)
    await qz.update_quiz(q["id"], enabled=enabled, allow_retake=allow_retake,
                         title="Тест компетенций", intro="Ответь на три вопроса")
    oids = {}
    for n in range(1, 4):
        qid = await qz.create_question(q["id"], f"Вопрос про {n}")
        hi = await qz.create_option(qid, "Высокий")
        lo = await qz.create_option(qid, "Низкий")
        await qz.set_option_points(hi, comp, 2)
        await qz.set_option_points(lo, comp, 0)
        oids[n] = (qid, hi, lo)
    if base_level:
        await qz.create_level(q["id"], "Базовый", 0, "Начало пути")
    await qz.create_level(q["id"], "Продвинутый", 60, "Уверенно")
    return await qz.get_quiz(q["id"]), oids


async def tap(handler, data, uid=U, message=None):
    cb = FakeCallback(data, uid, message)
    await handler(cb)
    return cb


async def answer_all(oids, *, pick=1, uid=U, message=None):
    attempt_id = None
    cb = None
    for n in range(1, 4):
        if attempt_id is None:
            cb = await tap(h.quiz_go, "qz:go", uid, message)
            attempt_id = (await qz.get_open_attempt(uid, await _qid(uid)))["id"]
        qid, hi, lo = oids[n]
        cb = await tap(h.quiz_answer, f"qz:a:{attempt_id}:{qid}:{hi if pick else lo}", uid, message)
    return cb


async def _qid(uid):
    return (await qz.get_quiz_for_city(CITY))["id"]


def test_menu_starts_quiz(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz()
        assert await DynamicMenuText("menu_quiz")(FakeMessage(U, "🧭 Тест"))
        m = FakeMessage(U, "🧭 Тест")
        await h.quiz_menu(m)
        text, kb = m.answers[-1]
        assert "Тест компетенций" in text and "Ответь на три вопроса" in text
        assert buttons(kb) == [("▶️ Начать тест", "qz:go")]
    run(go())


def test_gates(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _ = await seed_quiz()
        m = FakeMessage(104, "x")
        assert not await h.open_quiz(m, 104)
        assert m.answers[-1][0] == "Тест откроется, когда твою заявку одобрят."
        m = FakeMessage(105, "x")
        assert not await h.open_quiz(m, 105)
        assert m.answers[-1][0] != "Тест откроется, когда твою заявку одобрят."
        await qz.update_quiz(quiz_row["id"], enabled=0)
        m = FakeMessage(U, "x")
        assert not await h.open_quiz(m, U)
        assert m.answers[-1][0] == "Тест сейчас недоступен."
        cb = await tap(h.quiz_go, "qz:go")
        assert cb.alerts[-1] == ("Тест сейчас недоступен.", True)
    run(go())


def test_question_and_next(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        cb = await tap(h.quiz_go, "qz:go")
        text, kb = cb.message.last
        assert text.startswith("Вопрос 1 из 3\n\nВопрос про 1")
        attempt = await qz.get_open_attempt(U, quiz_row["id"])
        qid, hi, lo = oids[1]
        assert buttons(kb) == [("Высокий", f"qz:a:{attempt['id']}:{qid}:{hi}"),
                               ("Низкий", f"qz:a:{attempt['id']}:{qid}:{lo}")]
        cb = await tap(h.quiz_answer, f"qz:a:{attempt['id']}:{qid}:{hi}", message=cb.message)
        assert cb.message.edits[-1][0].startswith("Вопрос 2 из 3")
    run(go())


def test_resume_after_restart(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        await tap(h.quiz_go, "qz:go")
        attempt = await qz.get_open_attempt(U, quiz_row["id"])
        qid, hi, _ = oids[1]
        await tap(h.quiz_answer, f"qz:a:{attempt['id']}:{qid}:{hi}")
        # «рестарт»: FSM/память не участвуют, всё из БД
        m = FakeMessage(U, "🧭 Тест")
        await h.quiz_menu(m)
        assert buttons(m.answers[-1][1])[0][1] == "qz:go"
        cb = await tap(h.quiz_go, "qz:go")
        text = cb.message.last[0]
        assert text.startswith("Продолжаем с вопроса 2 из 3.") and "Вопрос 2 из 3" in text
    run(go())


def test_restarted_text(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        await tap(h.quiz_go, "qz:go")
        attempt = await qz.get_open_attempt(U, quiz_row["id"])
        await tap(h.quiz_answer, f"qz:a:{attempt['id']}:{oids[1][0]}:{oids[1][1]}")
        await qz.bump_content_version(quiz_row["id"])
        cb = await tap(h.quiz_go, "qz:go")
        text = cb.message.last[0]
        assert text.startswith("Мы обновили тест — начнём сначала.") and "Вопрос 1 из 3" in text
    run(go())


def test_stale_and_foreign_callbacks(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        await tap(h.quiz_go, "qz:go")
        attempt = await qz.get_open_attempt(U, quiz_row["id"])
        qid, hi, _ = oids[1]
        # чужой делегат жмёт кнопку с чужой попыткой
        cb = await tap(h.quiz_answer, f"qz:a:{attempt['id']}:{qid}:{hi}", uid=102)
        assert not cb.message.edits and not cb.message.answers
        assert (await qz.get_attempt(attempt["id"]))["answers"] == {}
        # мусор в callback
        cb = await tap(h.quiz_answer, "qz:a:x:y:z")
        assert not cb.message.edits
        # повторный ответ на уже отвеченный вопрос -> перерисовка текущего
        await tap(h.quiz_answer, f"qz:a:{attempt['id']}:{qid}:{hi}")
        cb = await tap(h.quiz_answer, f"qz:a:{attempt['id']}:{qid}:{hi}")
        assert cb.message.edits[-1][0].startswith("Вопрос 2 из 3")
    run(go())


def test_deeplink_quiz(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz()
        assert forum_deeplinks.DEEPLINKS["quiz"] == "handlers.quiz:open_from_deeplink"
        m = FakeMessage(U, "/start quiz")
        assert await forum_deeplinks.try_forum_deeplink(m, make_state(U), "quiz")
        assert buttons(m.answers[-1][1])[0][1] == "qz:go"
        m = FakeMessage(999, "/start quiz")
        assert not await forum_deeplinks.try_forum_deeplink(m, make_state(999), "quiz")
        assert not m.answers
    run(go())


def test_result_after_last_answer(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        cb = await answer_all(oids, pick=1)
        text, kb = cb.message.last
        assert text.startswith("Твой результат по компетенциям:")
        assert "Лидерство — Продвинутый" in text and "Уверенно" in text
        assert "Сессия" not in text  # рекомендаций сессий нет
    run(go())


def test_no_level(tmp_path):
    ready(tmp_path)

    async def go():
        _, oids = await seed_quiz(base_level=False)
        cb = await answer_all(oids, pick=0)
        assert "Лидерство — пока без уровня" in cb.message.last[0]
    run(go())


def test_choose_sessions_button(tmp_path):
    ready(tmp_path)

    async def go():
        _, oids = await seed_quiz()
        cb = await answer_all(oids)
        assert ("📅 Выбрать сессии", "se:open") in buttons(cb.message.last[1])
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "off")
        m = FakeMessage(U, "🧭 Тест")
        await h.quiz_menu(m)
        assert "se:open" not in [d for _, d in buttons(m.answers[-1][1])]
    run(go())


def test_my_result_no_retake_and_no_result(tmp_path):
    ready(tmp_path)

    async def go():
        _, oids = await seed_quiz(allow_retake=0)
        cb = await tap(h.quiz_result, "qz:res", uid=102)
        assert cb.message.last[0] == "Ты ещё не проходил тест."
        await answer_all(oids)
        m = FakeMessage(U, "🧭 Тест")
        await h.quiz_menu(m)
        text, kb = m.answers[-1]
        assert text.startswith("Твой результат по компетенциям:")
        assert "qz:re" not in [d for _, d in buttons(kb)]
        cb = await tap(h.quiz_result, "qz:res")
        assert cb.message.last[0].startswith("Твой результат по компетенциям:")
        cb = await tap(h.quiz_go, "qz:go")  # старая кнопка «Начать» не даёт пройти заново
        assert cb.message.last[0].startswith("Твой результат по компетенциям:")
    run(go())


def test_retake(tmp_path):
    ready(tmp_path)

    async def go():
        _, oids = await seed_quiz(allow_retake=1)
        cb = await answer_all(oids)
        assert ("🔁 Пройти заново", "qz:re") in buttons(cb.message.last[1])
        cb = await tap(h.quiz_retake, "qz:re")
        assert cb.message.last[0].startswith("Вопрос 1 из 3")
    run(go())


def test_html_in_quiz_content_is_escaped(tmp_path):
    ready(tmp_path)

    async def go():
        quiz, oids = await seed_quiz()
        await qz.update_question_text(oids[1][0], "Что лучше: A<B & C?")
        cb = await tap(h.quiz_go, "qz:go")
        text = cb.message.last[0]
        assert "A&lt;B &amp; C?" in text
    run(go())


def test_answer_after_content_change_restarts_attempt(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        cb = await tap(h.quiz_go, "qz:go")
        attempt = await qz.get_open_attempt(U, quiz_row["id"])
        qid, hi, lo = oids[1]
        await qz.bump_content_version(quiz_row["id"])  # менеджер поправил тест
        assert await svc.answer(U, attempt["id"], qid, hi) == "restarted"
        assert (await qz.get_attempt(attempt["id"]))["answers"] == {}
        cb = await tap(h.quiz_answer, f"qz:a:{attempt['id']}:{qid}:{hi}", message=cb.message)
        text = cb.message.last[0]
        assert "Вопрос 1 из 3" in text
        fresh = await qz.get_open_attempt(U, quiz_row["id"])
        assert fresh["id"] != attempt["id"]
    run(go())


def test_finished_result_uses_current_thresholds(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        await answer_all(oids, pick=1)  # 6 из 6 = 100%
        levels = await qz.list_levels(quiz_row["id"])
        top = max(levels, key=lambda lv: lv["threshold"])
        low = min(levels, key=lambda lv: lv["threshold"])
        lines = await svc.result_lines(U, quiz_row)
        assert lines[0]["level_name"] == top["name"]
        await qz.update_level(top["id"], threshold=101)  # недостижимый порог
        lines = await svc.result_lines(U, quiz_row)
        assert lines[0]["level_name"] == low["name"]
        stats = await svc.stats(quiz_row)
        assert stats["by_competency"]["Лидерство"] == {low["name"]: 1}
    run(go())
