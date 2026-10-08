"""Админка теста компетенций кнопками: экран, вопросы, варианты, баллы, уровни, статистика."""
from __future__ import annotations

from database import db, quiz_db as qdb, session_enroll_db as edb
from handlers import admin_enroll_list as el
from handlers import admin_forum_functions as aff
from handlers import admin_quiz as aq
from handlers.admin_caps import role_caps_key
from handlers.states import QuizEdit
from tests._enroll38 import ADMIN_ID, CITY, ready, run
from tests.test_admin_enroll_38 import FakeCallback, FakeMessage, cbs, new_state, texts

SPB_MANAGER = 900038003


def _quiz():
    return run(qdb.get_or_create_quiz(CITY))


def _seed_question(n_options=3):
    quiz = _quiz()
    qid = run(qdb.create_question(quiz["id"], "Как вы ведёте команду?"))
    oids = [run(qdb.create_option(qid, f"Вариант {i}")) for i in range(n_options)]
    return quiz, qid, oids


def test_quiz_screen_empty(tmp_path):
    ready(tmp_path)
    cb = FakeCallback("prog_qz:msk")
    run(aq.prog_qz(cb, new_state()))
    text = cb.message.text_edited
    assert "Тест компетенций" in text and "Москва" in text
    assert "Вопросов: 0" in text and "Тест выключен" in text
    assert "Добавьте вопросы или загрузите их из таблицы" in text
    assert "prog_qzsw:msk" in cbs(cb.message.edit_markup)


def test_toggles(tmp_path):
    ready(tmp_path)
    run(aq.prog_qzsw(FakeCallback("prog_qzsw:msk")))
    assert _quiz()["enabled"] == 1
    run(aq.prog_qzrt(FakeCallback("prog_qzrt:msk")))
    assert _quiz()["allow_retake"] == 1
    cb = FakeCallback("prog_qzmode:msk")
    run(aq.prog_qzmode(cb))
    assert _quiz()["score_mode"] == "points"
    assert "Считать: в баллах" in texts(cb.message.edit_markup)
    run(aq.prog_qzmode(FakeCallback("prog_qzmode:msk")))
    assert _quiz()["score_mode"] == "percent"


def test_title_intro_input(tmp_path):
    ready(tmp_path)
    state = new_state()
    cb = FakeCallback("prog_qzf:msk:title")
    run(aq.prog_qzf(cb, state))
    assert run(state.get_state()) == QuizEdit.value.state
    assert "Например" in cb.message.answers_sent[0]
    bad = FakeMessage("   ")
    run(aq.prog_qz_value(bad, state))
    assert "Например" in bad.answers_sent[0]
    assert run(state.get_state()) == QuizEdit.value.state
    run(aq.prog_qz_value(FakeMessage("Тест компетенций Юлида"), state))
    assert _quiz()["title"] == "Тест компетенций Юлида"
    assert run(state.get_state()) is None
    run(aq.prog_qzf(FakeCallback("prog_qzf:msk:intro"), state))
    run(aq.prog_qz_value(FakeMessage("Ответьте на вопросы."), state))
    assert _quiz()["intro"] == "Ответьте на вопросы."


def test_question_crud(tmp_path):
    ready(tmp_path)
    state = new_state()
    run(aq.prog_qzqn(FakeCallback("prog_qzqn:msk"), state))
    msg = FakeMessage("Первый вопрос")
    run(aq.prog_qz_value(msg, state))
    quiz = _quiz()
    qs = run(qdb.list_questions(quiz["id"]))
    assert [q["text"] for q in qs] == ["Первый вопрос"]
    assert "Вопрос добавлен" in msg.answers_sent[0]
    qid = qs[0]["id"]
    run(aq.prog_qzqe(FakeCallback(f"prog_qzqe:{qid}"), state))
    run(aq.prog_qz_value(FakeMessage("Правка"), state))
    assert run(qdb.get_question(qid))["text"] == "Правка"
    q2 = run(qdb.create_question(quiz["id"], "Второй"))
    run(aq.prog_qzqm(FakeCallback(f"prog_qzqm:{q2}:-1"), ))
    assert [q["id"] for q in run(qdb.list_questions(quiz["id"]))] == [q2, qid]
    lst = FakeCallback("prog_qzql:msk:0")
    run(aq.prog_qzql(lst))
    assert f"prog_qzq:{qid}" in cbs(lst.message.edit_markup)


def test_question_delete_confirm(tmp_path):
    ready(tmp_path)
    quiz, qid, _ = _seed_question(3)
    run(qdb.create_attempt(555, quiz["id"], quiz["content_version"]))
    cb = FakeCallback(f"prog_qzqx:{qid}")
    run(aq.prog_qzqx(cb))
    assert "удалятся варианты: 3, незавершённые попытки начнутся заново: 1" in cb.message.text_edited.replace("Удалятся", "удалятся")
    assert run(qdb.get_question(qid)) is not None
    run(aq.prog_qzqxgo(FakeCallback(f"prog_qzqxgo:{qid}")))
    assert run(qdb.get_question(qid)) is None


def test_option_points_flow(tmp_path):
    ready(tmp_path)
    cid = run(edb.create_competency(CITY, "Лидерство"))
    _quiz_row, _qid, oids = _seed_question(1)
    oid = oids[0]
    cb = FakeCallback(f"prog_qzo:{oid}")
    run(aq.prog_qzo(cb))
    assert f"prog_qzoc:{oid}:{cid}" in cbs(cb.message.edit_markup)
    cb = FakeCallback(f"prog_qzoc:{oid}:{cid}")
    run(aq.prog_qzoc(cb))
    row = cbs(cb.message.edit_markup)
    assert [f"prog_qzop:{oid}:{cid}:{i}" for i in range(6)] == row[:6]
    run(aq.prog_qzop(FakeCallback(f"prog_qzop:{oid}:{cid}:3")))
    assert run(qdb.get_option(oid))["points"] == {cid: 3}
    cb = FakeCallback(f"prog_qzo:{oid}")
    run(aq.prog_qzo(cb))
    assert "Лидерство: 3" in texts(cb.message.edit_markup)
    run(aq.prog_qzop(FakeCallback(f"prog_qzop:{oid}:{cid}:0")))
    assert run(qdb.get_option(oid))["points"] == {}
    over = FakeCallback(f"prog_qzop:{oid}:{cid}:9")
    run(aq.prog_qzop(over))
    assert over.answers[0][1] is True and run(qdb.get_option(oid))["points"] == {}


def test_option_without_competencies(tmp_path):
    ready(tmp_path)
    _q, _qid, oids = _seed_question(1)
    cb = FakeCallback(f"prog_qzo:{oids[0]}")
    run(aq.prog_qzo(cb))
    assert "Сначала заведите компетенции в «🎯 Компетенции»" in cb.message.text_edited
    assert "prog_cmpl:msk" in cbs(cb.message.edit_markup)


def test_option_add_edit_delete(tmp_path):
    ready(tmp_path)
    _q, qid, _ = _seed_question(0)
    state = new_state()
    run(aq.prog_qzon(FakeCallback(f"prog_qzon:{qid}"), state))
    run(aq.prog_qz_value(FakeMessage("Беру ответственность"), state))
    opts = run(qdb.list_options(qid))
    assert [o["text"] for o in opts] == ["Беру ответственность"]
    run(aq.prog_qzoe(FakeCallback(f"prog_qzoe:{opts[0]['id']}"), state))
    run(aq.prog_qz_value(FakeMessage("Иначе"), state))
    assert run(qdb.get_option(opts[0]["id"]))["text"] == "Иначе"
    cb = FakeCallback(f"prog_qzox:{opts[0]['id']}")
    run(aq.prog_qzox(cb))
    assert "Да, удалить вариант" in texts(cb.message.edit_markup)
    run(aq.prog_qzoxgo(FakeCallback(f"prog_qzoxgo:{opts[0]['id']}")))
    assert run(qdb.list_options(qid)) == []


def test_cancel_word_clears_state(tmp_path):
    ready(tmp_path)
    state = new_state()
    run(aq.prog_qzqn(FakeCallback("prog_qzqn:msk"), state))
    run(aq.prog_qz_cancel_word(FakeMessage("Отмена"), state))
    assert run(state.get_state()) is None


def test_hub_and_enroll_entry(tmp_path):
    ready(tmp_path)
    text, kb = run(aff._render_hub(ADMIN_ID, "msk"))
    assert "🧭 Тест компетенций: ❌ Выкл" in text
    assert "prog_qz:msk" in cbs(kb)
    run(aq.prog_qzsw(FakeCallback("prog_qzsw:msk")))
    text, _ = run(aff._render_hub(ADMIN_ID, "msk"))
    assert "🧭 Тест компетенций: ✅ Вкл" in text
    cb = FakeCallback("prog_enrset:msk")
    run(el.prog_enrset(cb))
    assert "🧭 Тест компетенций" in texts(cb.message.edit_markup)
    assert "prog_qz:msk" in cbs(cb.message.edit_markup)


def test_foreign_city_denied(tmp_path):
    ready(tmp_path)

    async def setup():
        await db.set_setting(role_caps_key("reg_manager"), "settings")
        await db.add_staff(SPB_MANAGER, "reg_manager", ADMIN_ID)
        await db.set_staff_city(SPB_MANAGER, "spb")

    run(setup())
    quiz, qid, oids = _seed_question(1)
    for handler, data in (
        (aq.prog_qzsw, "prog_qzsw:msk"), (aq.prog_qzql, "prog_qzql:msk:0"),
        (aq.prog_qzq, f"prog_qzq:{qid}"), (aq.prog_qzo, f"prog_qzo:{oids[0]}"),
        (aq.prog_qzqx, f"prog_qzqx:{qid}"),
    ):
        cb = FakeCallback(data, user_id=SPB_MANAGER)
        run(handler(cb))
        assert cb.answers[0] == (aq._CITY_FORBIDDEN_ALERT, True), data
        assert cb.message.text_edited is None
    assert _quiz()["enabled"] == 0


# ── Уровни, статистика, ссылка, тексты ───────────────────────────────────────────────────────

from handlers import admin_quiz_levels as lv  # noqa: E402
from handlers.states import EditSetting  # noqa: E402
from services.quiz import QUIZ_TEXT_KEYS  # noqa: E402


def test_levels_crud(tmp_path):
    ready(tmp_path)
    state = new_state()
    run(lv.prog_qzln(FakeCallback("prog_qzln:msk"), state))
    run(aq.prog_qz_value(FakeMessage("Продвинутый"), state))
    bad = FakeMessage("abc")
    run(aq.prog_qz_value(bad, state))
    assert "Нужно число от 0 до 100, например 50" in bad.answers_sent[0]
    bad = FakeMessage("150")
    run(aq.prog_qz_value(bad, state))
    assert "от 0 до 100" in bad.answers_sent[0]
    run(aq.prog_qz_value(FakeMessage("60"), state))
    done = FakeMessage("Уверенно ведёте команду")
    run(aq.prog_qz_value(done, state))
    assert run(state.get_state()) is None
    run(qdb.create_level(_quiz()["id"], "Базовый", 0))
    levels = run(qdb.list_levels(_quiz()["id"]))
    assert [(x["name"], x["threshold"]) for x in levels] == [("Базовый", 0), ("Продвинутый", 60)]
    assert levels[1]["description"] == "Уверенно ведёте команду"
    cb = FakeCallback("prog_qzl:msk")
    run(lv.prog_qzl(cb))
    assert texts(cb.message.edit_markup)[0] == "Базовый — от 0%"
    lid = levels[1]["id"]
    run(lv.prog_qzle(FakeCallback(f"prog_qzle:{lid}:threshold"), state))
    run(aq.prog_qz_value(FakeMessage("70%"), state))
    assert run(qdb.get_level(lid))["threshold"] == 70
    run(lv.prog_qzle(FakeCallback(f"prog_qzle:{lid}:description"), state))
    run(aq.prog_qz_value(FakeMessage("-"), state))
    assert run(qdb.get_level(lid))["description"] == ""


def test_level_threshold_points_mode(tmp_path):
    ready(tmp_path)
    run(qdb.update_quiz(_quiz()["id"], score_mode="points"))
    state = new_state()
    run(lv.prog_qzln(FakeCallback("prog_qzln:msk"), state))
    run(aq.prog_qz_value(FakeMessage("Старт"), state))
    bad = FakeMessage("-3")
    run(aq.prog_qz_value(bad, state))
    assert "целое число баллов" in bad.answers_sent[0]
    run(aq.prog_qz_value(FakeMessage("150"), state))  # в баллах 150 допустимо
    assert run(state.get_data())["threshold"] == 150


def test_level_delete_confirm(tmp_path):
    ready(tmp_path)
    lid = run(qdb.create_level(_quiz()["id"], "Базовый", 0))
    cb = FakeCallback(f"prog_qzlx:{lid}")
    run(lv.prog_qzlx(cb))
    assert "Да, удалить уровень" in texts(cb.message.edit_markup)
    assert run(qdb.get_level(lid)) is not None
    run(lv.prog_qzlxgo(FakeCallback(f"prog_qzlxgo:{lid}")))
    assert run(qdb.get_level(lid)) is None


def test_stats_screen(tmp_path):
    ready(tmp_path)
    cid = run(edb.create_competency(CITY, "Лидерство"))
    quiz = _quiz()
    base = run(qdb.create_level(quiz["id"], "Базовый", 0))
    pro = run(qdb.create_level(quiz["id"], "Продвинутый", 60))
    for tid, level in ((1, base), (2, pro)):
        att = run(qdb.create_attempt(tid, quiz["id"], quiz["content_version"]))
        run(qdb.finish_attempt(att, {cid: {"points": 3, "max": 5, "level_id": level}}))
    run(qdb.create_attempt(3, quiz["id"], quiz["content_version"]))
    cb = FakeCallback("prog_qzst:msk")
    run(lv.prog_qzst(cb))
    text = cb.message.text_edited
    assert "Начали: 3, закончили: 2" in text
    assert "Лидерство: Базовый — 1, Продвинутый — 1" in text


def test_link(tmp_path):
    ready(tmp_path)

    class Bot:
        async def get_me(self):
            return types_ns(username="yl_bot")

    cb = FakeCallback("prog_qzlink:msk")
    run(lv.prog_qzlink(cb, Bot()))
    assert "https://t.me/yl_bot?start=quiz" in cb.message.answers_sent[0]
    assert "рассылки" in cb.message.answers_sent[0]


def types_ns(**kw):
    from types import SimpleNamespace
    return SimpleNamespace(**kw)


def test_texts(tmp_path):
    ready(tmp_path)
    cb = FakeCallback("prog_qztx:msk:0")
    run(lv.prog_qztx(cb))
    labels = texts(cb.message.edit_markup)
    assert "кнопка меню" in labels[0] and not any("quiz_" in t for t in labels)
    assert "prog_qztx:msk:1" in cbs(cb.message.edit_markup)
    idx = QUIZ_TEXT_KEYS.index("quiz_question_header")
    state = new_state()
    cb = FakeCallback(f"prog_qzte:msk:{idx}")
    run(lv.prog_qzte(cb, state))
    assert run(state.get_state()) == EditSetting.waiting_for_value.state
    assert run(state.get_data())["setting_key"] == "quiz_question_header"
    assert "{n}" in cb.message.text_edited
    bad = FakeCallback("prog_qzte:msk:999")
    run(lv.prog_qzte(bad, new_state()))
    assert bad.answers[0][1] is True
