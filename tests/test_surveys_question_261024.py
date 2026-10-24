"""Мастер добавления вопроса анкеты: пять типов, лимиты, уточнения, эталон дня 1."""
import asyncio

from database import survey_db as sdb
from tests.test_roles_phase8 import _flat_callback_data
from tests.test_polls_260822 import ADMIN_ID, _ready, _state, _cb, _msg, FakeBot
from tests.test_surveys_builder_261024 import _new_draft, _q


def _run(c):
    return asyncio.run(c)


class Wiz:
    def __init__(self, tmp_path):
        _ready(tmp_path)
        self.state, self.bot = _state(ADMIN_ID), FakeBot()
        self.pid = _new_draft(self.state, self.bot)

    def cb(self, data):
        return _cb(data, self.state, self.bot)[1]

    def msg(self, text):
        return _msg(text, self.state, self.bot)[1]

    def st(self):
        return _run(self.state.get_state())

    def start(self, qtype, text):
        self.cb(f"psv_qadd:{self.pid}")
        self.cb(f"psv_qt:{qtype}")
        assert self.st() == "SurveyBuild:q_text"
        self.msg(text)

    def questions(self):
        return _run(sdb.list_survey_questions(self.pid))


def test_qadd_shows_five_types(tmp_path):
    w = Wiz(tmp_path)
    ev = w.cb(f"psv_qadd:{w.pid}")
    data = _flat_callback_data(ev.message.markup)
    assert {f"psv_qt:{t}" for t in ("single", "multi", "scale", "text", "table")} <= set(data)
    assert "Шкала 1–5" in ev.message.text and "Таблица" in ev.message.text


def test_qadd_at_limit_alerts(tmp_path):
    w = Wiz(tmp_path)
    for k in range(15):
        _q(w.pid, f"В{k}")
    ev = w.cb(f"psv_qadd:{w.pid}")
    assert ev.answers[-1][1] is True and "15 вопросов" in ev.answers[-1][0]
    assert w.st() is None


def test_unknown_type_rejected(tmp_path):
    w = Wiz(tmp_path)
    w.cb(f"psv_qadd:{w.pid}")
    ev = w.cb("psv_qt:bogus")
    assert ev.answers[-1][1] is True and w.st() is None


def test_expired_session_alert(tmp_path):
    w = Wiz(tmp_path)
    ev = w.cb("psv_qsave")
    assert "Сессия истекла" in ev.answers[-1][0]


def test_text_too_long_keeps_state(tmp_path):
    w = Wiz(tmp_path)
    w.cb(f"psv_qadd:{w.pid}")
    w.cb("psv_qt:single")
    ev = w.msg("x" * 301)
    assert "301" in ev.answers[-1][0] and w.st() == "SurveyBuild:q_text"


def test_options_flow_and_errors(tmp_path):
    w = Wiz(tmp_path)
    w.start("single", "Вопрос?")
    assert w.st() == "SurveyBuild:q_options"
    ev = w.cb("psv_qopts")
    assert "хотя бы 2" in ev.answers[-1][0] and ev.answers[-1][1] is True
    ev = w.msg("А; Б")
    assert "Варианты: 2 (нужно 2–10)" in ev.answers[-1][0]
    ev = w.msg("В" * 91)
    assert "91" in ev.answers[-1][0]
    ev = w.msg("В")
    assert "Варианты: 3" in ev.answers[-1][0]
    ev = w.msg(";".join(str(i) for i in range(8)))
    assert "11 вариантов" in ev.answers[-1][0]
    q = _run(w.state.get_data())["q"]
    assert [o["t"] for o in q["options"]] == ["А", "Б", "В"]
    ev = w.msg("Г" * 90)
    assert "Варианты: 4" in ev.answers[-1][0]


def test_single_with_more_and_save(tmp_path):
    w = Wiz(tmp_path)
    w.start("single", "Как программа?")
    w.msg("Супер; Нормально; Плохо")
    ev = w.cb("psv_qopts")
    data = _flat_callback_data(ev.message.markup)
    assert "psv_qmore:2" in data and "psv_qmore_done" in data
    w.cb("psv_qmore:2")
    assert w.st() == "SurveyBuild:q_more_text"
    ev = w.msg("Что не понравилось?")
    assert w.st() is None
    q = _run(w.state.get_data())["q"]
    assert q["options"][2]["more"] == "Что не понравилось?"
    assert _flat_buttons_text(ev.answers[-1][2])[2].startswith("💬")
    w.cb("psv_qmore:2")
    assert _run(w.state.get_data())["q"]["options"][2]["more"] is None
    w.cb("psv_qmore:2")
    w.msg("Что не понравилось?")
    ev = w.cb("psv_qmore_done")
    assert "Обязательный: ✅" in str(_flat_buttons_text(ev.message.markup))
    ev = w.cb("psv_qreq")
    assert "Обязательный: ⬜" in str(_flat_buttons_text(ev.message.markup))
    w.cb("psv_qreq")
    ev = w.cb("psv_qsave")
    qs = w.questions()
    assert len(qs) == 1 and qs[0]["qtype"] == "single" and qs[0]["required"]
    assert qs[0]["options"][2] == {"t": "Плохо", "more": "Что не понравилось?"}
    assert qs[0]["options"][0]["more"] in (None, "")
    assert w.st() is None
    assert "Вопросов: 1 из 15" in ev.message.text


def _flat_buttons_text(markup):
    return [b.text for r in markup.inline_keyboard for b in r]


def test_more_marker_and_bad_index(tmp_path):
    w = Wiz(tmp_path)
    w.start("single", "В?")
    w.msg("А; Б")
    w.cb("psv_qopts")
    ev = w.cb("psv_qmore:9")
    assert ev.answers[-1][1] is True
    w.cb("psv_qmore:0")
    ev = w.msg("Что именно?")
    assert _flat_buttons_text(ev.answers[-1][2])[0].startswith("💬")
    assert _run(w.state.get_data())["q"]["options"][0]["more"] == "Что именно?"


def test_multi_limit_buttons(tmp_path):
    w = Wiz(tmp_path)
    w.start("multi", "Что понравилось?")
    w.msg("А; Б; В; Г")
    ev = w.cb("psv_qopts")
    data = _flat_callback_data(ev.message.markup)
    assert {"psv_qmax:1", "psv_qmax:2", "psv_qmax:3", "psv_qmax:0"} <= set(data)
    assert "psv_qmax:4" not in data
    ev = w.cb("psv_qmax:4")
    assert ev.answers[-1][1] is True
    w.cb("psv_qmax:3")
    w.cb("psv_qmore_done")
    w.cb("psv_qsave")
    q = w.questions()[0]
    assert q["qtype"] == "multi" and q["max_choices"] == 3


def test_multi_no_limit(tmp_path):
    w = Wiz(tmp_path)
    w.start("multi", "Что?")
    w.msg("А; Б")
    w.cb("psv_qopts")
    w.cb("psv_qmax:0")
    w.cb("psv_qmore_done")
    w.cb("psv_qsave")
    assert w.questions()[0]["max_choices"] is None


def test_scale_flow(tmp_path):
    w = Wiz(tmp_path)
    w.start("scale", "Насколько полезно?")
    assert w.st() == "SurveyBuild:q_scale_lo"
    ev = w.msg("   ")
    assert "Подпись пустая" in ev.answers[-1][0]
    w.msg("1 — не моё")
    assert w.st() == "SurveyBuild:q_scale_hi"
    w.msg("5 — классно")
    w.cb("psv_qsave")
    q = w.questions()[0]
    assert q["qtype"] == "scale" and q["scale_lo"] == "не моё" and q["scale_hi"] == "классно"


def test_text_flow_optional(tmp_path):
    w = Wiz(tmp_path)
    w.start("text", "Что улучшить?")
    w.cb("psv_qreq")
    w.cb("psv_qsave")
    q = w.questions()[0]
    assert q["qtype"] == "text" and not q["required"]


def test_table_flow_keep_label(tmp_path):
    w = Wiz(tmp_path)
    w.start("table", "Оцени сессии")
    ev = w.cb("psv_qrows")
    assert ev.answers[-1][1] is True
    w.msg("Открытие; Сессия Карьера")
    w.msg("Ужин")
    ev = w.cb("psv_qrows")
    assert "psv_qna:keep" in _flat_callback_data(ev.message.markup)
    w.cb("psv_qna:keep")
    w.cb("psv_qsave")
    q = w.questions()[0]
    assert q["qtype"] == "table" and q["rows"] == ["Открытие", "Сессия Карьера", "Ужин"]
    assert not q.get("na_label")


def test_table_custom_label_and_row_limit(tmp_path):
    w = Wiz(tmp_path)
    w.start("table", "Оцени")
    w.msg(";".join(f"С{i}" for i in range(20)))
    ev = w.msg("Лишняя")
    assert "20" in ev.answers[-1][0] and "сохранены" in ev.answers[-1][0]
    assert len(_run(w.state.get_data())["q"]["rows"]) == 20
    w.cb("psv_qrows")
    w.cb("psv_qna:edit")
    assert w.st() == "SurveyBuild:q_na"
    ev = w.msg("н" * 61)
    assert "61" in ev.answers[-1][0] and w.st() == "SurveyBuild:q_na"
    w.msg("Не был(а)")
    w.cb("psv_qsave")
    q = w.questions()[0]
    assert q["na_label"] == "Не был(а)" and len(q["rows"]) == 20


def test_stale_button_rejected(tmp_path):
    w = Wiz(tmp_path)
    w.start("text", "Что?")
    ev = w.cb("psv_qna:keep")
    assert ev.answers[-1][1] is True and not w.questions()


def test_cancel_question_keeps_draft_and_adds_nothing(tmp_path):
    w = Wiz(tmp_path)
    w.start("text", "Что?")
    ev = w.cb(f"psv_draft:{w.pid}")
    assert w.st() is None and not w.questions()
    assert "черновик" in ev.message.text


def test_save_appends_last(tmp_path):
    w = Wiz(tmp_path)
    first = _q(w.pid, "Первый")
    w.start("text", "Второй")
    w.cb("psv_qsave")
    qs = w.questions()
    assert qs[0]["id"] == first and qs[1]["text"] == "Второй"


def test_etalon_day1_ten_questions(tmp_path):
    """Эталон дня 1: 10 вопросов разных типов собираются кнопками и сообщениями."""
    w = Wiz(tmp_path)
    long_opt = "Очень длинный вариант ответа про программу первого дня " + "я" * 30
    assert len(long_opt) <= 90

    def single(text, opts, more=()):
        w.start("single", text)
        w.msg(";".join(opts))
        w.cb("psv_qopts")
        for i in more:
            w.cb(f"psv_qmore:{i}")
            w.msg("Что именно?")
        w.cb("psv_qmore_done")
        w.cb("psv_qsave")

    def multi(text, opts, limit, more=()):
        w.start("multi", text)
        w.msg(";".join(opts))
        w.cb("psv_qopts")
        w.cb(f"psv_qmax:{limit}")
        for i in more:
            w.cb(f"psv_qmore:{i}")
            w.msg("Какая сессия или спикер?")
        w.cb("psv_qmore_done")
        w.cb("psv_qsave")

    def scale(text):
        w.start("scale", text)
        w.msg("1 — не моё")
        w.msg("5 — классно")
        w.cb("psv_qsave")

    def free(text):
        w.start("text", text)
        w.cb("psv_qsave")

    single("Как оцениваешь день?", ["Отлично", "Хорошо", long_opt], more=(2,))
    multi("Что понравилось?", ["Открытие", "Кейс", "Ужин", "Нетворкинг"], 3, more=(2, 3))
    scale("Организация")
    scale("Контент")
    free("Что улучшить?")
    w.start("table", "Оцени сессии")
    w.msg("Открытие; Сессия Карьера; Ужин")
    w.cb("psv_qrows")
    w.cb("psv_qna:keep")
    w.cb("psv_qsave")
    single("Приедешь снова?", ["Да", "Нет"])
    multi("Что обсудить?", ["А", "Б", "В"], 0)
    scale("Общее впечатление")
    free("Пожелания")

    qs = w.questions()
    assert [q["qtype"] for q in qs] == [
        "single", "multi", "scale", "scale", "text", "table", "single", "multi", "scale", "text"]
    assert qs[0]["options"][2]["t"] == long_opt and qs[0]["options"][2]["more"] == "Что именно?"
    assert qs[1]["max_choices"] == 3
    assert [bool(o["more"]) for o in qs[1]["options"]] == [False, False, True, True]
    assert qs[5]["rows"] == ["Открытие", "Сессия Карьера", "Ужин"]
    assert qs[7]["max_choices"] is None
    assert [q["position"] for q in qs] == list(range(1, 11))
