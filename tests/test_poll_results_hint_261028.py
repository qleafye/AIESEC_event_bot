"""Строка «Итоги: в боте → 📊 Опросы и на дашборде» (ключ `poll_results_admin_hint_text`):
финальный экран мастера опроса и анкеты, отчёт об отправке, подтверждение «запланирован(а)»,
карточки опроса и анкеты. Пустой ключ — строки нет нигде; значение экранируется."""
import asyncio
from datetime import timedelta

from database import db
from database import survey_db as sdb
from services import scheduler as sched
from services.comms import polls as polls_svc
from handlers.comms import admin_poll_wizard as wiz
from handlers.comms import admin_polls
from handlers.comms import admin_survey_builder as sb
from handlers.comms import admin_survey_results  # noqa: F401
from tests.test_polls_260822 import ADMIN_ID, FakeBot, _add_user, _cb, _msg, _ready, _state, _mk_poll

HINT = "Итоги: в боте → 📊 Опросы и на дашборде"


def _run(c):
    return asyncio.run(c)


def _set_hint(value):
    _run(db.set_setting("poll_results_admin_hint_text", value))


# ── хелпер ─────────────────────────────────────────────────────────────────────────────────

def test_hint_line_default_empty_and_escaped(tmp_path):
    _ready(tmp_path)
    assert HINT in _run(polls_svc.results_hint_line())
    _set_hint("   ")
    assert _run(polls_svc.results_hint_line()) == ""
    _set_hint("Итоги <тут>")
    assert _run(polls_svc.results_hint_line()) == "Итоги &lt;тут&gt;"


# ── мастер опроса ──────────────────────────────────────────────────────────────────────────

def _poll_to_confirm(state, bot):
    _cb("poll_new_single", state, bot)
    _msg("Во сколько начинаем?", state, bot)
    _msg("10:00; 11:00", state, bot)
    _cb("poll_opts_done", state, bot)
    _cb("poll_settings_next", state, bot)
    _, ev = _cb("poll_aud:all", state, bot)
    return ev.message.answers[-1][0]


def test_poll_wizard_final_screen_has_hint_before_question(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    text = _poll_to_confirm(_state(ADMIN_ID), FakeBot())
    assert HINT in text
    assert text.index(HINT) < text.index("Отправить сейчас или запланировать?")


def test_poll_wizard_final_screen_without_hint_when_empty(tmp_path):
    _ready(tmp_path)
    _set_hint("")
    text = _poll_to_confirm(_state(ADMIN_ID), FakeBot())
    assert "Итоги" not in text
    assert "Отправить сейчас или запланировать?" in text
    assert "\n\n\n" not in text


def test_deliver_report_has_hint_instead_of_old_literal(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def fake_deliver(bot, poll_id):
        return {"sent": 2, "failed": 0}

    monkeypatch.setattr(wiz, "deliver_poll", fake_deliver)
    bot = FakeBot()
    _run(wiz._deliver_and_report(bot, 7, ADMIN_ID))
    text = bot.sent_messages[-1][1]
    assert "Доставлено: 2" in text and HINT in text
    assert "Итоги — в «📊 Опросы»." not in text

    _set_hint("")
    bot = FakeBot()
    _run(wiz._deliver_and_report(bot, 7, ADMIN_ID))
    text = bot.sent_messages[-1][1]
    assert "Доставлено: 2" in text and "Итоги" not in text
    assert not text.endswith("\n")


def test_poll_schedule_confirmation_has_hint(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(wiz, "schedule_poll_job", lambda pid, when: None)
    state, bot = _state(ADMIN_ID), FakeBot()
    _poll_to_confirm(state, bot)
    _cb("poll_schedule", state, bot)
    when = sched._now_moscow_naive() + timedelta(days=1)
    _, ev = _msg(when.strftime("%d.%m.%Y %H:%M"), state, bot)
    text = ev.answers[-1][0]
    assert "запланирован" in text and HINT in text


# ── анкета ─────────────────────────────────────────────────────────────────────────────────

def _survey_to_confirm(state, bot):
    _cb("psv_new", state, bot)
    _msg("Итоги дня", state, bot)
    pid = _run(db.list_polls(statuses=("draft",)))[0]["id"]
    _run(sdb.add_survey_question(pid, "text", "Как вам?"))
    _run(sdb.update_survey_texts(pid, intro="Привет"))
    _cb(f"psv_go:{pid}", state, bot)
    _, ev = _cb("poll_aud:all", state, bot)
    return pid, ev.message.answers[-1][0]


def test_survey_final_screen_has_hint(tmp_path):
    _ready(tmp_path)
    _pid, text = _survey_to_confirm(_state(ADMIN_ID), FakeBot())
    assert HINT in text
    assert text.index(HINT) < text.index("Отправить сейчас или запланировать?")


def test_survey_final_screen_without_hint_when_empty(tmp_path):
    _ready(tmp_path)
    _set_hint("")
    _pid, text = _survey_to_confirm(_state(ADMIN_ID), FakeBot())
    assert "Итоги:" not in text and "Отправить сейчас или запланировать?" in text


def test_survey_schedule_confirmation_has_hint(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(sb, "schedule_poll_job", lambda pid, when: None)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid, _text = _survey_to_confirm(state, bot)
    _cb(f"psv_sched:{pid}", state, bot)
    when = sched._now_moscow_naive() + timedelta(days=1)
    _, ev = _msg(when.strftime("%d.%m.%Y %H:%M"), state, bot)
    text = ev.answers[-1][0]
    assert "запланирована" in text and HINT in text


# ── карточки ───────────────────────────────────────────────────────────────────────────────

def test_poll_card_has_hint_and_hides_when_empty(tmp_path):
    _ready(tmp_path)
    pid = _run(_mk_poll())
    _run(db.set_poll_status(pid, "open"))
    text, _kb = _run(admin_polls.render_poll_card(pid))
    assert text.rstrip().endswith(HINT + " (🎪 Статистика форума → «Опросы»)")
    _set_hint("")
    text, _kb = _run(admin_polls.render_poll_card(pid))
    assert "Итоги:" not in text and not text.endswith("\n")


def test_survey_card_has_hint_and_escapes(tmp_path):
    _ready(tmp_path)
    pid = _run(sdb.create_survey_draft("Анкета", ADMIN_ID))
    _run(sdb.add_survey_question(pid, "text", "Как вам?"))
    _run(db.set_poll_status(pid, "open"))
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"poll_card:{pid}", state, bot)
    assert HINT in ev.message.text
    assert "Начали: 0 · Закончили: 0" in ev.message.text
    _set_hint("Смотри <тут>")
    _, ev = _cb(f"poll_card:{pid}", state, bot)
    assert "Смотри &lt;тут&gt;" in ev.message.text
    _set_hint("")
    _, ev = _cb(f"poll_card:{pid}", state, bot)
    assert "Итоги:" not in ev.message.text and "Смотри" not in ev.message.text
