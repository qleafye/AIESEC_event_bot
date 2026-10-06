"""«❔ Проверить курс», «⏳ Не зашли» и «🔗 Привязать вручную» (handlers/admin_delegations_review.py):
карточки неразобранных курсов с решением кнопками (в т.ч. отклонённого в боте — с пометкой),
список ЦА без аккаунта по вузам, ручная привязка через пересылку / @ник / id с подтверждением.

Окружение — из tests/test_delegations_core.py (форма с ключами, ответ из фикстуры, FakeBot
в модуле делегаций), фейки экрана — из tests/test_delegations_admin.py.
pytest-asyncio нет — `asyncio.run()`.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from config import config
from database import delegations_db as ddb
from database import ext_forms_db as ef
from handlers import admin_delegations_review as mod
from handlers.admin_caps import required_capability
from handlers.states import DelegationLink
from services import delegations as dlg
from services.checkin import checkin_denial
from settings_audit import set_setting_by_admin
from tests.test_delegations_admin import (
    _FakeCallback, _button_texts, _callbacks, _last_edit, _run, _state,
)
from tests.test_delegations_core import (
    MANAGER, UNIVERSITY, USERNAME, _answer_from_fixture, _delegation_form, _fixture_items,
    _journal, _reg_started, _row, _user,
)
from tests.test_delegations_core import _env as _core_env

NAME = next(i["value"] for i in _fixture_items() if i["q"] == "q1")


class _FakeMessage:
    def __init__(self, text=None, *, user_id=MANAGER, forward_from_id=None):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.forward_origin = (SimpleNamespace(sender_user=SimpleNamespace(id=forward_from_id))
                               if forward_from_id else None)

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


def _env(tmp_path):
    bot, storage = _core_env(tmp_path, name="dlg_review.db")
    fid = _delegation_form()
    return bot, fid


def _check_row(fid: int, aid: str, *, username: str | None = "@" + USERNAME,
               university: str = UNIVERSITY, course: str = "2", note: str | None = None,
               sheet_state: str | None = None) -> int:
    """Ответ формы с оценкой «проверить» (парсер не разобрал курс) и, если нужно, пометкой
    «в боте отказ»."""
    _answer_from_fixture(fid, aid, username=username, course=course, university=university,
                         sheet_state=sheet_state)
    needle = username.lstrip("@") if username else None
    row_id = _run(ddb.upsert_eval(fid, aid, ta_status="check", university=university,
                                  course_raw=course, course_canonical=None,
                                  username_needle=needle, answered_at="2026-10-01 12:00:00"))
    if note:
        _run(ddb.set_decision(row_id, "check", None, note=note))
    return row_id


def _ok_row(fid: int, aid: str, *, username: str = "@nobody", university: str = UNIVERSITY,
            sheet_state: str | None = None) -> int:
    _answer_from_fixture(fid, aid, username=username, university=university,
                         sheet_state=sheet_state)
    return _run(ddb.upsert_eval(fid, aid, ta_status="ok", university=university,
                                course_raw="1", course_canonical="1",
                                username_needle=username.lstrip("@"),
                                answered_at="2026-10-01 12:00:00"))


def _drow(row_id: int) -> dict:
    return _run(ddb.get_by_id(row_id))


def _sheet_state(fid: int, aid: str) -> str:
    return _run(ef.get_answer(fid, aid))["sheet_state"]


def _bind_user(tid: int, aid: str) -> None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET delegation_answer_id = ? WHERE telegram_id = ?", (aid, tid))
    conn.commit()
    conn.close()


# ── «❔ Проверить курс» ────────────────────────────────────────────────────────────────────

def test_review_list_paginates_eight_per_page(tmp_path):
    _, fid = _env(tmp_path)
    ids = [_check_row(fid, f"a{i:02d}", username=f"@u{i}", university=f"Вуз {i:02d}")
           for i in range(10)]
    cb = _FakeCallback("dlg_review:0")
    _run(mod.dlg_review(cb))
    text, mode, kb = _last_edit(cb)
    assert mode == "HTML" and "❔ <b>Проверить курс</b> (10)" in text
    cards = [c for c in _callbacks(kb) if c.startswith("dlg_card:")]
    assert len(cards) == 8 and cards[0] == f"dlg_card:{ids[0]}"
    assert _button_texts(kb)[0] == f"{NAME} — Вуз 00"
    assert "dlg_review:8" in _callbacks(kb) and "admin_delegations" in _callbacks(kb)
    cb = _FakeCallback("dlg_review:8")
    _run(mod.dlg_review(cb))
    _, _, kb = _last_edit(cb)
    assert len([c for c in _callbacks(kb) if c.startswith("dlg_card:")]) == 2
    assert "dlg_review:0" in _callbacks(kb) and "dlg_review:16" not in _callbacks(kb)
    cb = _FakeCallback("dlg_review:800")  # устаревшее смещение — последняя страница
    _run(mod.dlg_review(cb))
    assert len([c for c in _callbacks(_last_edit(cb)[2]) if c.startswith("dlg_card:")]) == 2


def test_review_empty_and_no_form(tmp_path):
    _, fid = _env(tmp_path)
    cb = _FakeCallback("dlg_review:0")
    _run(mod.dlg_review(cb))
    assert "Все ответы разобраны — проверять нечего." in _last_edit(cb)[0]
    _run(set_setting_by_admin(None, "delegation_form_id", ""))
    cb = _FakeCallback("dlg_review:0")
    _run(mod.dlg_review(cb))
    assert "Форма делегаций не выбрана" in _last_edit(cb)[0]


def test_card_shows_course_as_written_and_rejected_mark(tmp_path):
    _, fid = _env(tmp_path)
    plain = _check_row(fid, "a1", username="@plain", course="2", university="МГУ <x>")
    rejected = _check_row(fid, "a2", course="2 к.", note=dlg.NOTE_REJECTED_IN_BOT)
    cb = _FakeCallback(f"dlg_card:{plain}")
    _run(mod.dlg_card(cb))
    text, _, kb = _last_edit(cb)
    assert f"❔ <b>{NAME}</b>" in text
    assert "Вуз: МГУ &lt;x&gt;" in text
    assert "Курс как в форме: «2»" in text
    assert "Ответ: 2026-10-01 12:00:00" in text
    assert "Ник: @plain" in text
    assert "В боте у этого человека отказ" not in text
    assert _callbacks(kb)[:2] == [f"dlg_ta:{plain}:ok", f"dlg_ta:{plain}:no"]
    assert "dlg_review:0" in _callbacks(kb)
    cb = _FakeCallback(f"dlg_card:{rejected}")
    _run(mod.dlg_card(cb))
    text = _last_edit(cb)[0]
    assert "Курс как в форме: «2 к.»" in text
    assert "⚠️ В боте у этого человека отказ — одобряйте только если уверены." in text
    cb = _FakeCallback("dlg_card:4242")
    _run(mod.dlg_card(cb))
    assert cb.answer_calls == [(mod._ROW_GONE, True)]


def test_card_of_linked_person_has_no_decision_buttons(tmp_path):
    _, fid = _env(tmp_path)
    row_id = _check_row(fid, "a1")
    _user(501, "approved")
    _run(ddb.link(row_id, 501, "username"))
    cb = _FakeCallback(f"dlg_card:{row_id}")
    _run(mod.dlg_card(cb))
    text, _, kb = _last_edit(cb)
    assert "✅ Уже делегат в боте" in text
    assert not any(c.startswith("dlg_ta:") for c in _callbacks(kb))


def test_ta_ok_converts_person_rejected_in_bot(tmp_path):
    """D-26: отклонённого в боте автоматика отправила в «проверить» — «✅ ЦА» менеджера
    проводит его в approved, журнал называет менеджера, QR доступен."""
    bot, fid = _env(tmp_path)
    _user(501, "rejected")  # анкета в users со статусом rejected, ник как в форме
    row_id = _check_row(fid, "a1", note=dlg.NOTE_REJECTED_IN_BOT, sheet_state="synced")
    cb = _FakeCallback(f"dlg_ta:{row_id}:ok")
    _run(mod.dlg_ta(cb))
    u = _row(501)
    assert u["status"] == "approved" and u["approved_at"]
    assert u["delegation_answer_id"] == "a1"
    assert _run(checkin_denial(u)) is None
    assert _journal(501) == [("approved", MANAGER)]
    row = _drow(row_id)
    assert row["ta_status"] == "ok" and row["decided_by"] == MANAGER
    assert row["linked_telegram_id"] == 501 and row["link_how"] == "username"
    assert cb.answer_calls == [("Отмечено: ЦА — заявка одобрена", False)]
    assert len(bot.sent) == 1 and bot.sent[0][0] == 501
    assert _sheet_state(fid, "a1") == "update"
    assert "Все ответы разобраны" in _last_edit(cb)[0]  # вернулись к списку


def test_ta_ok_converts_reg_started_person_and_waits_without_person(tmp_path):
    bot, fid = _env(tmp_path)
    _reg_started(502, "second")
    row_id = _check_row(fid, "a1", username="@second")
    nobody = _check_row(fid, "a2", username="@nobody")
    cb = _FakeCallback(f"dlg_ta:{row_id}:ok")
    _run(mod.dlg_ta(cb))
    assert _row(502)["status"] == "approved"
    assert _journal(502) == [("approved", MANAGER)]
    assert cb.answer_calls == [("Отмечено: ЦА — заявка одобрена", False)]
    cb = _FakeCallback(f"dlg_ta:{nobody}:ok")
    _run(mod.dlg_ta(cb))
    assert cb.answer_calls == [
        ("Отмечено: ЦА — человек ещё не заходил в бота, появится в «⏳ Не зашли»", False)]
    assert _drow(nobody)["ta_status"] == "ok" and _drow(nobody)["linked_telegram_id"] is None
    assert len(bot.sent) == 1


def test_ta_no_marks_and_sends_nothing(tmp_path):
    bot, fid = _env(tmp_path)
    _reg_started(503, USERNAME)
    row_id = _check_row(fid, "a1")
    cb = _FakeCallback(f"dlg_ta:{row_id}:no")
    _run(mod.dlg_ta(cb))
    row = _drow(row_id)
    assert row["ta_status"] == "no" and row["decided_by"] == MANAGER
    assert row["linked_telegram_id"] is None
    assert _row(503) is None and bot.sent == []
    assert cb.answer_calls == [("Отмечено: не ЦА", False)]
    cb = _FakeCallback(f"dlg_ta:{row_id}:maybe")
    _run(mod.dlg_ta(cb))
    assert cb.answer_calls == [(mod._ROW_GONE, True)]


# ── «⏳ Не зашли» ──────────────────────────────────────────────────────────────────────────

def test_absent_list_groups_by_university(tmp_path):
    _, fid = _env(tmp_path)
    b = _ok_row(fid, "a1", username="@one", university="Б-вуз")
    a1 = _ok_row(fid, "a2", username="@two", university="А-вуз")
    a2 = _ok_row(fid, "a3", username="@three", university="А-вуз")
    _ok_row(fid, "a4", username="@linked", university="А-вуз")
    _user(504, "approved", username="linked")
    _run(ddb.link(_run(ddb.get_by_answer(fid, "a4"))["id"], 504, "username"))
    cb = _FakeCallback("dlg_absent:0")
    _run(mod.dlg_absent(cb))
    text, _, kb = _last_edit(cb)
    assert "⏳ <b>Не зашли в бота</b> (3)" in text
    assert text.count("🏫 А-вуз") == 1 and text.count("🏫 Б-вуз") == 1
    assert text.index("🏫 А-вуз") < text.index("🏫 Б-вуз")
    assert f"• {NAME} (@two)" in text
    assert "@linked" not in text
    assert [c for c in _callbacks(kb) if c.startswith("dlg_link:")] == [
        f"dlg_link:{a1}", f"dlg_link:{a2}", f"dlg_link:{b}"]
    assert f"🔗 {NAME} (@two)" in _button_texts(kb)
    assert "admin_delegations" in _callbacks(kb)


def test_absent_empty(tmp_path):
    _, fid = _env(tmp_path)
    cb = _FakeCallback("dlg_absent:0")
    _run(mod.dlg_absent(cb))
    assert "Все делегаты ЦА уже в боте." in _last_edit(cb)[0]


# ── «🔗 Привязать вручную» ────────────────────────────────────────────────────────────────

def _start_link(row_id: int):
    st = _state()
    cb = _FakeCallback(f"dlg_link:{row_id}")
    _run(mod.dlg_link(cb, st))
    return cb, st


def test_link_by_forward_confirms_then_converts_manually(tmp_path):
    """Пересланное сообщение → экран подтверждения → «✅ Привязать»: approved, link_how manual,
    decided_by менеджер, строка листа synced → update (колонка «В боте»)."""
    bot, fid = _env(tmp_path)
    row_id = _ok_row(fid, "a1", username="@typo_name", university="РУДН", sheet_state="synced")
    _reg_started(601, "real_name")
    cb, st = _start_link(row_id)
    text, _, kb = _last_edit(cb)
    assert f"🔗 <b>Привязать {NAME} (РУДН)</b>" in text
    assert "Кого это?" in text and _callbacks(kb) == ["dlg_cancel"]
    assert _run(st.get_state()) == DelegationLink.waiting_person.state
    msg = _FakeMessage(forward_from_id=601)
    _run(mod.dlg_link_person(msg, st))
    text, mode, kb = msg.answers[-1]
    assert mode == "HTML"
    assert f"Привязать <b>{NAME}</b> (РУДН) к <b>@real_name (id 601)</b>?" in text
    assert "одобрена без анкеты" in text and "QR" in text
    assert _callbacks(kb) == ["dlg_link_yes", "dlg_cancel"]
    assert _run(st.get_state()) == DelegationLink.waiting_confirm.state
    assert _row(601) is None  # до подтверждения ничего не произошло
    cb = _FakeCallback("dlg_link_yes")
    _run(mod.dlg_link_yes(cb, st))
    u = _row(601)
    assert u["status"] == "approved" and u["delegation_answer_id"] == "a1"
    assert u["delegation"] == "РУДН"
    assert _run(checkin_denial(u)) is None
    row = _drow(row_id)
    assert row["linked_telegram_id"] == 601 and row["link_how"] == "manual"
    assert row["decided_by"] == MANAGER and row["ta_status"] == "ok"
    assert _journal(601) == [("approved", MANAGER)]
    assert _sheet_state(fid, "a1") == "update"
    assert cb.answer_calls == [("Привязано — заявка одобрена", False)]
    assert _run(st.get_state()) is None
    assert len(bot.sent) == 1 and bot.sent[0][0] == 601
    assert "Все делегаты ЦА уже в боте." in _last_edit(cb)[0]


def test_link_rejected_in_bot_person_is_authorised_by_manual_path(tmp_path):
    bot, fid = _env(tmp_path)
    row_id = _ok_row(fid, "a1", username="@typo", university="МГУ")
    _user(602, "rejected", username="other2")
    cb, st = _start_link(row_id)
    msg = _FakeMessage("602")  # числовой id
    _run(mod.dlg_link_person(msg, st))
    text = msg.answers[-1][0]
    assert "Старое Имя (@other2) (id 602)" in text
    assert "⚠️ В боте ему отказали — ручная привязка это решение отменит" in text
    cb = _FakeCallback("dlg_link_yes")
    _run(mod.dlg_link_yes(cb, st))
    u = _row(602)
    assert u["status"] == "approved" and u["approved_at"]
    assert _drow(row_id)["link_how"] == "manual"
    assert _journal(602) == [("approved", MANAGER)]
    assert cb.answer_calls == [("Привязано — заявка одобрена", False)]


def test_link_by_username_via_search_people(tmp_path):
    _, fid = _env(tmp_path)
    row_id = _ok_row(fid, "a1", username="@typo")
    _reg_started(603, "Real_Nick")
    _, st = _start_link(row_id)
    msg = _FakeMessage("@real_nick")
    _run(mod.dlg_link_person(msg, st))
    assert "к <b>@Real_Nick (id 603)</b>?" in msg.answers[-1][0]
    assert _run(st.get_data())["dlg_link_tid"] == 603


def test_link_unknown_username_and_bad_input(tmp_path):
    _, fid = _env(tmp_path)
    row_id = _ok_row(fid, "a1", username="@typo")
    _, st = _start_link(row_id)
    msg = _FakeMessage("@unknown")
    _run(mod.dlg_link_person(msg, st))
    assert msg.answers[-1][0] == mod._NOT_FOUND
    assert _run(st.get_state()) == DelegationLink.waiting_person.state  # ждём ещё раз
    msg = _FakeMessage("Иван Петров")  # по имени не ищем
    _run(mod.dlg_link_person(msg, st))
    assert msg.answers[-1][0].startswith("Не понял, кого добавить")
    msg = _FakeMessage("99999")  # id, которого бот не знает
    _run(mod.dlg_link_person(msg, st))
    assert msg.answers[-1][0] == mod._NOT_FOUND
    msg = _FakeMessage("отмена")
    _run(mod.dlg_link_person(msg, st))
    assert _run(st.get_state()) is None
    assert msg.answers[0][0] == "Отменено."


def test_link_refuses_person_bound_to_another_row(tmp_path):
    _, fid = _env(tmp_path)
    row_id = _ok_row(fid, "a1", username="@typo")
    _user(604, "approved", username="bound")
    _bind_user(604, "zz-other")
    _, st = _start_link(row_id)
    msg = _FakeMessage("604")
    _run(mod.dlg_link_person(msg, st))
    assert msg.answers[-1][0] == mod._OTHER_ROW
    assert _run(st.get_state()) == DelegationLink.waiting_person.state
    assert _drow(row_id)["linked_telegram_id"] is None


def test_link_yes_without_state_is_stale(tmp_path):
    _, fid = _env(tmp_path)
    row_id = _ok_row(fid, "a1", username="@typo")
    cb = _FakeCallback("dlg_link_yes")
    _run(mod.dlg_link_yes(cb, _state()))
    assert cb.answer_calls == [(mod._STALE_LINK, True)]
    assert _drow(row_id)["linked_telegram_id"] is None
    # состояние «ждём человека», но «✅ Привязать» пришло раньше подтверждения — тоже устарело
    _, st = _start_link(row_id)
    cb = _FakeCallback("dlg_link_yes")
    _run(mod.dlg_link_yes(cb, st))
    assert cb.answer_calls == [(mod._STALE_LINK, True)]
    cb = _FakeCallback("dlg_pick:605")
    _run(mod.dlg_pick(cb, _state()))
    assert cb.answer_calls == [(mod._STALE_LINK, True)]


def test_link_already_linked_row_alerts(tmp_path):
    _, fid = _env(tmp_path)
    row_id = _ok_row(fid, "a1", username="@typo")
    _user(606, "approved", username="typo")
    _run(ddb.link(row_id, 606, "username"))
    cb = _FakeCallback(f"dlg_link:{row_id}")
    _run(mod.dlg_link(cb, _state()))
    assert cb.answer_calls == [(mod._ALREADY_LINKED_ALERT, True)]


def test_capabilities_are_moderate_reg():
    for data in ("dlg_review:0", "dlg_card:1", "dlg_ta:1:ok", "dlg_absent:0", "dlg_link:1",
                 "dlg_pick:5", "dlg_link_yes"):
        assert required_capability(callback_data=data) == "moderate_reg", data
    assert required_capability(raw_state="DelegationLink:waiting_person") == "moderate_reg"
    assert required_capability(raw_state="DelegationLink:waiting_confirm") == "moderate_reg"
