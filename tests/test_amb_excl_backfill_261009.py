"""Ступени амбассадоров: кнопка «Исключить» в списке исключённых и «🔁 Пересчитать ступени».

pytest-asyncio нет — async через `asyncio.run()`, хендлеры зовутся напрямую с фейками
(приём `tests/test_amb_tiers_admin_su5.py`).
"""
from __future__ import annotations

from database import amb_tiers_db as tdb
from database import db
from handlers import admin_amb_tiers as h
from handlers.admin_caps import ADMIN_CAPS
from tests.test_amb_tiers_admin_su5 import ADMIN_ID, FakeCallback, FakeMessage, _new_state, _run
from tests.test_amb_tiers_dashboard_backfill_su5 import (
    _outbox_events,
    _three_approved_before_program,
)


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _screen_texts(cb):
    return [t for t, _ in cb.message.edits] + [t for t, _ in cb.message.answers]


# ── кнопка «Исключить» в списке ──────────────────────────────────────────────────────────

def test_exclusions_list_has_exclude_button(tmp_path):
    _three_approved_before_program(tmp_path)
    cb = FakeCallback("ambt_excl_list:0")
    _run(h.amb_exclusions_list(cb))
    text, kb = cb.message.edits[0]
    assert ("🚫 Исключить приглашённого из зачёта", "ambt_excl_l") in _buttons(kb)


def test_exclude_from_list_cancel_returns_to_list(tmp_path):
    _three_approved_before_program(tmp_path)
    state = _new_state()
    cb = FakeCallback("ambt_excl_l")
    _run(h.amb_exclude_start_from_list(cb, state))
    assert "Кого исключить из зачёта амбассадора?" in cb.message.answers[0][0]
    assert _run(state.get_data()) == {"return_to": "list"}

    cancel = FakeCallback("ambt_excl_cancel")
    _run(h.amb_exclude_cancel(cancel, state))
    texts = [t for t, _ in cancel.message.answers]
    assert texts[0] == "Отменено, никого не исключили."
    assert "Исключённые из зачёта" in texts[1]
    assert _run(state.get_data()) == {}


def test_exclude_from_list_full_flow_returns_to_list(tmp_path):
    _three_approved_before_program(tmp_path)
    state = _new_state()
    _run(h.amb_exclude_start_from_list(FakeCallback("ambt_excl_l"), state))
    _run(h.amb_exclude_person_step(FakeMessage("201"), state))
    _run(h.amb_exclude_reason_step(FakeMessage("накрутка"), state))
    go = FakeCallback("ambt_excl_go")
    _run(h.amb_exclude_go(go, state))
    texts = [t for t, _ in go.message.answers]
    assert texts[0].startswith("Готово:")
    assert "Исключённые из зачёта" in texts[-1] and "накрутка" in texts[-1]
    assert _run(tdb.get_exclusion(201))


def test_exclude_from_main_screen_still_returns_to_tiers(tmp_path):
    _three_approved_before_program(tmp_path)
    state = _new_state()
    _run(h.amb_exclude_start(FakeCallback("ambt_excl"), state))
    assert _run(state.get_data()) == {}
    cancel = FakeCallback("ambt_excl_cancel")
    _run(h.amb_exclude_cancel(cancel, state))
    assert "Ступени амбассадоров" in cancel.message.answers[1][0]


def test_text_cancel_from_list_returns_to_list(tmp_path):
    _three_approved_before_program(tmp_path)
    state = _new_state()
    _run(h.amb_exclude_start_from_list(FakeCallback("ambt_excl_l"), state))
    msg = FakeMessage("отмена")
    _run(h.amb_exclude_person_step(msg, state))
    assert msg.answers[0][0] == "Отменено, никого не исключили."
    assert "Исключённые из зачёта" in msg.answers[1][0]


# ── пересчёт ступеней ────────────────────────────────────────────────────────────────────

def test_main_screen_has_fill_button_and_no_developer_text(tmp_path):
    _three_approved_before_program(tmp_path)
    cb = FakeCallback("admin_amb_tiers")
    _run(h.show_amb_tiers(cb, _new_state()))
    _, kb = cb.message.edits[0]
    assert ("🔁 Пересчитать ступени", "ambt_fill") in _buttons(kb)

    toggle = FakeCallback("ambt_toggle:program")
    _run(h.amb_tiers_toggle(toggle))
    alert = toggle.answers[0][0]
    assert "разработчик" not in alert
    assert len(alert) <= 200


def test_fill_preview_writes_nothing_and_explains(tmp_path):
    _three_approved_before_program(tmp_path)
    cb = FakeCallback("ambt_fill")
    _run(h.amb_fill_preview(cb))
    text, kb = cb.message.edits[0]
    assert "Амбассадоров: 1, новых ступеней: 2" in text
    assert "@amb_one: прошли отбор 3 → ступени 1, 2" in text
    assert "отправит сообщение каждому из 1 амбассадоров" in text
    assert _buttons(kb) == [
        ("✅ Пересчитать и уведомить", "ambt_fill_go:n"),
        ("🔕 Пересчитать без уведомлений", "ambt_fill_go:q"),
        ("❌ Отмена", "admin_amb_tiers"),
    ]
    assert _run(tdb.list_tiers()) == []


def test_fill_preview_program_off_offers_only_quiet(tmp_path):
    _three_approved_before_program(tmp_path, program="off")
    cb = FakeCallback("ambt_fill")
    _run(h.amb_fill_preview(cb))
    text, kb = cb.message.edits[0]
    assert "Программа сейчас выключена" in text
    assert [d for _, d in _buttons(kb)] == ["ambt_fill_go:q", "admin_amb_tiers"]


def test_fill_preview_nothing_to_do(tmp_path):
    _three_approved_before_program(tmp_path)
    _run(h.amb_fill_go(FakeCallback("ambt_fill_go:q")))
    cb = FakeCallback("ambt_fill")
    _run(h.amb_fill_preview(cb))
    assert "Новых ступеней к выдаче нет" in cb.message.edits[0][0]


def test_fill_go_quiet_writes_tiers_without_messages(tmp_path):
    _three_approved_before_program(tmp_path)
    cb = FakeCallback("ambt_fill_go:q")
    _run(h.amb_fill_go(cb))
    rows = _run(tdb.list_tiers(100))
    assert [r["tier"] for r in rows] == [1, 2]
    assert all(r["notified_at"] for r in rows)
    assert _outbox_events() == []
    assert cb.message.answers[0][0] == "Готово. Записано ступеней: 2. Сообщений амбассадорам не отправляли."
    assert "Ступени амбассадоров" in cb.message.answers[1][0]


def test_fill_go_notify_queues_one_event(tmp_path):
    _three_approved_before_program(tmp_path)
    cb = FakeCallback("ambt_fill_go:n")
    _run(h.amb_fill_go(cb))
    events = _outbox_events()
    assert len(events) == 1 and events[0]["telegram_id"] == 100
    assert cb.message.answers[0][0] == (
        "Готово. Записано ступеней: 2. Амбассадорам отправлены сообщения о ступенях."
    )


def test_fill_go_notify_refused_when_program_off(tmp_path):
    _three_approved_before_program(tmp_path, program="off")
    cb = FakeCallback("ambt_fill_go:n")
    _run(h.amb_fill_go(cb))
    assert cb.answers[0][1] is True
    assert _run(tdb.list_tiers()) == []
    # тихий пересчёт при выключенной программе работает
    _run(h.amb_fill_go(FakeCallback("ambt_fill_go:q")))
    assert [r["tier"] for r in _run(tdb.list_tiers(100))] == [1, 2]


def test_fill_go_is_idempotent(tmp_path):
    _three_approved_before_program(tmp_path)
    _run(h.amb_fill_go(FakeCallback("ambt_fill_go:q")))
    cb = FakeCallback("ambt_fill_go:q")
    _run(h.amb_fill_go(cb))
    assert len(_run(tdb.list_tiers(100))) == 2
    assert "Записано ступеней: 0." in cb.message.answers[0][0]


# ── права ────────────────────────────────────────────────────────────────────────────────

def test_new_callbacks_have_moderate_game_caps():
    for key in ("ambt_excl_l", "ambt_fill", "ambt_fill_go:*"):
        assert ADMIN_CAPS[key] == "moderate_game"


def test_cap_resolver_covers_new_callbacks():
    from handlers.admin_caps import required_capability

    assert required_capability(callback_data="ambt_excl_l") == "moderate_game"
    assert required_capability(callback_data="ambt_fill") == "moderate_game"
    assert required_capability(callback_data="ambt_fill_go:n") == "moderate_game"
