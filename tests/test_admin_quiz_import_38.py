"""Импорт теста компетенций из CSV в админке: шаблон, файл, предпросмотр, применение."""
from __future__ import annotations

import io
from types import SimpleNamespace

from database import quiz_db as qdb, session_enroll_db as edb
from handlers import admin_quiz_import as qi
from handlers.states import QuizImport
from tests._enroll38 import CITY, ready, run
from tests.test_admin_enroll_38 import FakeCallback, FakeMessage, cbs, new_state, texts

CSV = (
    "вопрос;вариант;компетенция;баллы\n"
    "Q1;A1;Лидерство;5\n;A2;Лидерство;1\n;A3;Лидерство;0\n"
    "Q2;B1;Лидерство;4\n;B2;Лидерство;2\n;B3;Лидерство;1\n"
).encode("utf-8-sig")


class DocMessage(FakeMessage):
    def __init__(self, size, text=None):
        super().__init__(text)
        self.document = SimpleNamespace(file_id="f1", file_size=size)


class FakeBot:
    def __init__(self, raw=b""):
        self.raw = raw
        self.downloads = 0

    async def download(self, file_id):
        self.downloads += 1
        return io.BytesIO(self.raw)


def _quiz():
    return run(qdb.get_or_create_quiz(CITY))


def _upload(state, raw=CSV, size=None):
    run(qi.prog_qzimp(FakeCallback("prog_qzimp:msk"), state))
    msg = DocMessage(len(raw) if size is None else size)
    bot = FakeBot(raw)
    run(qi.prog_qzimp_file(msg, state, bot))
    return msg, bot


def test_template_sent(tmp_path):
    ready(tmp_path)
    cb = FakeCallback("prog_qztpl:msk")
    run(qi.prog_qztpl(cb))
    doc, caption = cb.message.documents[0]
    assert doc.filename == "quiz_template.csv" and doc.data.startswith(b"\xef\xbb\xbf")
    assert caption == "Заполните и пришлите файлом."


def test_upload_preview_and_apply(tmp_path):
    ready(tmp_path)
    run(edb.create_competency(CITY, "Лидерство"))
    quiz = _quiz()
    old = run(qdb.create_question(quiz["id"], "Старый"))
    run(qdb.create_option(old, "x"))
    state = new_state()
    cb = FakeCallback("prog_qzimp:msk")
    run(qi.prog_qzimp(cb, state))
    assert run(state.get_state()) == QuizImport.waiting_file.state
    assert "prog_qztpl:msk" in cbs(cb.message.edit_markup)
    msg, _ = _upload(state)
    text = msg.answers_sent[0]
    assert "2 вопроса, 6 вариантов" in text and "Будет заменено: 1 вопрос, 1 вариант" in text
    assert "✅ Применить" in texts(msg.answer_markups[0])
    version = _quiz()["content_version"]
    cb = FakeCallback("prog_qzapply:msk")
    run(qi.prog_qzapply(cb, state))
    assert "✅ Тест обновлён: 2 вопроса" in cb.message.text_edited
    assert [q["text"] for q in run(qdb.list_questions(quiz["id"]))] == ["Q1", "Q2"]
    assert _quiz()["content_version"] > version
    assert run(state.get_state()) is None


def test_upload_too_big(tmp_path):
    ready(tmp_path)
    state = new_state()
    msg, bot = _upload(state, size=3 * 1024 * 1024)
    assert "Файл больше 2 МБ" in msg.answers_sent[0]
    assert bot.downloads == 0
    assert run(state.get_state()) == QuizImport.waiting_file.state


def test_upload_errors_no_apply(tmp_path):
    ready(tmp_path)
    run(edb.create_competency(CITY, "Лидерство"))
    state = new_state()
    msg, _ = _upload(state, raw="вопрос;вариант;компетенция;баллы\n;A;Лидерство;1\n".encode("utf-8"))
    assert "Строка 2" in msg.answers_sent[0]
    assert "✅ Применить" not in texts(msg.answer_markups[0])
    assert run(state.get_state()) == QuizImport.waiting_file.state
    stale = FakeCallback("prog_qzapply:msk")
    run(qi.prog_qzapply(stale, state))
    assert stale.answers[0][1] is True and run(qdb.list_questions(_quiz()["id"])) == []


def test_unknown_competency_in_preview(tmp_path):
    ready(tmp_path)
    state = new_state()
    msg, _ = _upload(state)
    assert "Неизвестные компетенции: Лидерство" in msg.answers_sent[0]


def test_decline_changes_nothing(tmp_path):
    ready(tmp_path)
    run(edb.create_competency(CITY, "Лидерство"))
    state = new_state()
    _upload(state)
    cb = FakeCallback("prog_qzimpno:msk")
    run(qi.prog_qzimpno(cb, state))
    assert run(state.get_state()) is None and run(qdb.list_questions(_quiz()["id"])) == []


def test_text_instead_of_file_and_cancel(tmp_path):
    ready(tmp_path)
    state = new_state()
    run(qi.prog_qzimp(FakeCallback("prog_qzimp:msk"), state))
    msg = FakeMessage("привет")
    run(qi.prog_qzimp_not_file(msg))
    assert "Пришлите файл CSV — тот, что получили по кнопке «📥 Шаблон»" in msg.answers_sent[0]
    run(qi.prog_qzimp_cancel_word(FakeMessage("Отмена"), state))
    assert run(state.get_state()) is None


def test_unknown_size_is_checked_after_download(tmp_path):
    ready(tmp_path)
    state = new_state()
    big = b"x" * (2 * 1024 * 1024 + 10)
    run(qi.prog_qzimp(FakeCallback("prog_qzimp:msk"), state))
    msg = DocMessage(None)
    run(qi.prog_qzimp_file(msg, state, FakeBot(big)))
    assert "Файл больше 2 МБ" in msg.answers_sent[0]
