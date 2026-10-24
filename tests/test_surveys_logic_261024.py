"""Чистая логика анкеты из нескольких вопросов (services/comms/surveys.py): валидаторы,
рендер вопроса кнопками, лимит «до N», очередь уточнений, превью для менеджера. Без БД и Telegram."""
from __future__ import annotations

from services.comms import surveys as sv

QID = 99999999


def _q(qtype, **kw):
    base = {
        "id": 7, "poll_id": 1, "position": 1, "qtype": qtype, "text": "Как дела?",
        "required": 1, "max_choices": None, "options": [], "rows": [],
        "scale_lo": None, "scale_hi": None, "na_label": None,
    }
    if qtype in ("single", "multi") and "options" not in kw:
        base["options"] = [{"t": "Да", "more": None}, {"t": "Нет", "more": None}, {"t": "Не знаю", "more": None}]
    base.update(kw)
    return base


def _cbs(markup):
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_qtype_labels():
    assert sv.QTYPE_LABELS["single"] == "Один вариант"
    assert sv.QTYPE_LABELS["multi"] == "Несколько вариантов"
    assert sv.QTYPE_LABELS["scale"] == "Шкала 1–5"
    assert sv.QTYPE_LABELS["text"] == "Свободный ответ"
    assert sv.QTYPE_LABELS["table"] == "Таблица"


def test_single_short_options_on_buttons():
    text, kb = sv.render_question(_q("single"), number=1, total=3)
    assert "Вопрос 1 из 3" in text
    assert _labels(kb)[:3] == ["Да", "Нет", "Не знаю"]
    assert _cbs(kb)[:3] == ["sv:o:7:0", "sv:o:7:1", "sv:o:7:2"]


def test_single_long_options_numbered():
    opts = [{"t": "а" * 90, "more": None}, {"t": "б" * 40, "more": None}]
    text, kb = sv.render_question(_q("single", options=opts), number=1, total=1)
    assert "1. " + "а" * 90 in text
    assert "2. " + "б" * 40 in text
    assert _labels(kb)[:2] == ["1", "2"]
    assert _cbs(kb)[:2] == ["sv:o:7:0", "sv:o:7:1"]


def test_options_need_numbers():
    assert sv.options_need_numbers(["a" * 29]) is True
    assert sv.options_need_numbers(["a" * 28, "b"]) is False


def test_multi_checkmarks_done_and_limit_hint():
    text, kb = sv.render_question(_q("multi", max_choices=2), number=1, total=1, draft={"sel": [0, 2]})
    labels = _labels(kb)
    assert labels[0].startswith("✓") and labels[2].startswith("✓")
    assert not labels[1].startswith("✓")
    assert "sv:d:7" in _cbs(kb)
    assert "до 2" in text


def test_multi_no_limit_hint_without_max():
    text, _ = sv.render_question(_q("multi"), number=1, total=1, draft={"sel": []})
    assert "Можно выбрать до" not in text


def test_toggle_multi():
    sel, err = sv.toggle_multi([0, 1], 2, 2)
    assert sel == [0, 1] and err and "не больше 2" in err
    assert sv.toggle_multi([0, 1], 1, 2) == ([0], None)
    assert sv.toggle_multi([0, 1], 2, None) == ([0, 1, 2], None)


def test_scale_buttons_and_edges():
    text, kb = sv.render_question(_q("scale", scale_lo="Плохо", scale_hi="Отлично"), number=2, total=4)
    assert _cbs(kb)[:5] == [f"sv:n:7:{i}" for i in range(1, 6)]
    assert "Плохо" in text and "Отлично" in text


def test_table_row_and_na():
    q = _q("table", rows=["Еда", "Сцена", "Звук"])
    text, kb = sv.render_question(q, number=1, total=1, draft={"row": 1})
    assert "Сцена" in text and "2 из 3" in text
    cbs = _cbs(kb)
    assert [f"sv:t:7:{i}" for i in range(1, 6)] == cbs[:5]
    assert "sv:t:7:0" in cbs
    assert sv.DEFAULT_NA_LABEL in _labels(kb)
    q["na_label"] = "Не был"
    _, kb2 = sv.render_question(q, number=1, total=1, draft={"row": 0})
    assert "Не был" in _labels(kb2)


def test_text_skip_depends_on_required():
    text, kb = sv.render_question(_q("text", required=0), number=1, total=1)
    assert "sv:k:7" in _cbs(kb)
    assert "одним сообщением" in text
    _, kb2 = sv.render_question(_q("text", required=1), number=1, total=1)
    assert kb2 is None


def test_skip_only_for_optional_choice():
    _, kb = sv.render_question(_q("single", required=0), number=1, total=1)
    assert "sv:k:7" in _cbs(kb)
    _, kb2 = sv.render_question(_q("single", required=1), number=1, total=1)
    assert "sv:k:7" not in _cbs(kb2)


def test_more_queue_and_prompt():
    opts = [{"t": "a", "more": None}, {"t": "b", "more": "Что именно?"},
            {"t": "c", "more": None}, {"t": "d", "more": "Почему?"}]
    q = _q("multi", options=opts)
    assert sv.more_queue(q, [1, 3]) == [3] or sv.more_queue(q, [1, 3]) == [1, 3]
    assert sv.more_queue(q, [3, 1]) == [1, 3]
    assert sv.more_queue(q, [0, 2]) == []
    text, kb = sv.render_more_prompt(q, 3)
    assert "Почему?" in text
    assert "sv:k:7" in _cbs(kb)


def test_next_question_id_and_number():
    qs = [_q("text", id=5, position=1), _q("text", id=9, position=2), _q("text", id=12, position=3)]
    assert sv.next_question_id(qs, 5) == 9
    assert sv.next_question_id(qs, 12) is None
    assert sv.question_number(qs, 9) == 2


def test_validators():
    assert sv.validate_option("x" * 90) is None
    err = sv.validate_option("x" * 91)
    assert err and "90" in err
    assert sv.validate_answer_text("") is not None
    assert "Пришли ответ текстом" in sv.validate_answer_text("")
    assert sv.validate_answer_text("ок") is None
    assert sv.validate_answer_text("x" * 1001) is not None
    assert sv.validate_question_text("Ок?") is None
    assert "300" in sv.validate_question_text("x" * 301)
    assert sv.validate_options(["a"]) is not None
    assert sv.validate_options(["a"] * 11) is not None
    assert sv.validate_options(["a", "b"]) is None
    assert sv.validate_rows(["r"] * 21) is not None
    assert sv.validate_rows(["r"] * 20) is None
    assert sv.validate_label("x" * 61) is not None
    assert sv.validate_label(None) is None


def test_callback_data_within_64_bytes():
    qs = [
        _q("single", id=QID), _q("multi", id=QID, max_choices=3),
        _q("scale", id=QID), _q("text", id=QID, required=0),
        _q("table", id=QID, rows=["a", "b"], required=0),
        _q("single", id=QID, options=[{"t": "я" * 90, "more": "x"}] * 10),
    ]
    for q in qs:
        _, kb = sv.render_question(q, number=1, total=1, draft={"sel": [0], "row": 0})
        for cb in _cbs(kb):
            assert cb.startswith("sv:") and len(cb.encode()) <= 64
    for started in (False, True):
        for cb in _cbs(sv.invite_kb(QID, started=started)):
            assert cb.startswith("sv:") and len(cb.encode()) <= 64


def test_invite_kb():
    assert _cbs(sv.invite_kb(4)) == ["sv:s:4"]
    assert _cbs(sv.invite_kb(4, started=True)) == ["sv:c:4"]


def test_html_escaped():
    q = _q("single", text="<b>жирно</b>", options=[{"t": "<i>x</i>", "more": None}, {"t": "y", "more": None}])
    text, kb = sv.render_question(q, number=1, total=1)
    assert "<b>жирно" not in text and "&lt;b&gt;" in text
    chunks = sv.render_survey_preview({"id": 1, "title": "<u>Тема</u>", "intro": "<b>в</b>"}, [q])
    joined = "".join(chunks)
    assert "<u>Тема" not in joined and "&lt;u&gt;" in joined
    assert "&lt;b&gt;жирно" in joined


def test_preview_chunks_within_limit():
    qs = [_q("single", id=i, position=i, text="В" * 300,
             options=[{"t": "о" * 90, "more": "уточни"}] * 8) for i in range(1, 14)]
    chunks = sv.render_survey_preview({"id": 1, "title": "Моё название"}, qs)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "Моё название" in chunks[0]


def test_preview_small_single_chunk():
    qs = [_q("scale", scale_lo="1", scale_hi="5", required=0), _q("table", id=8, position=2, rows=["A", "B"])]
    chunks = sv.render_survey_preview({"id": 1, "title": "Т"}, qs)
    assert len(chunks) == 1
    assert "необязательный" in chunks[0]


def test_no_native_polls_in_module():
    import inspect
    src = inspect.getsource(sv)
    assert "send_poll" not in src
