"""«🧹 Сбросить статусы»: предпросмотр ничего не пишет, сброс совпадает с показанным, повтор
безопасен, список изменился между предпросмотром и «Сбросить» -> не сбрасываем, кнопка на экране
кандидатов, права moderate_game."""
from __future__ import annotations

from database import amb_status_db as sdb
from handlers import admin_amb_reset as h
from handlers.admin_amb_bulk import bulk_buttons
from handlers.admin_caps import ADMIN_CAPS, required_capability
from tests.test_amb_status_reset_tool_34 import AT, PAST, _ready, _run, _seed, _status, _world
from tests.test_amb_tiers_admin_su5 import FakeCallback


def _buttons(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def test_menu_shows_counts(tmp_path):
    _world(tmp_path)
    cb = FakeCallback("ambrst")
    _run(h.amb_reset_menu(cb))
    text, kb = cb.message.edits[0]
    assert "Статусы прошлых сезонов: 2" in text and "Кандидаты этого сезона: 2" in text
    assert _buttons(kb)[:2] == ["ambrst_p:past", "ambrst_p:cand"]


def test_preview_writes_nothing_and_explains_what_disappears(tmp_path):
    _world(tmp_path)
    cb = FakeCallback("ambrst_p:past")
    _run(h.amb_reset_preview(cb))
    text, kb = cb.message.edits[0]
    assert "Будет сброшено людей: 2" in text
    assert f"«{PAST}»" in text
    assert "Что пропадёт" in text and "действующие амбассадоры прошлых сезонов — 1" in text
    assert _buttons(kb) == ["ambrst_go:past:2", "ambrst"]
    assert _status(3) == ("candidate", 0) and _status(4) == ("active", 1)


def test_go_resets_exactly_previewed_and_repeat_is_safe(tmp_path):
    _world(tmp_path)
    cb = FakeCallback("ambrst_go:cand:2")
    _run(h.amb_reset_go(cb))
    assert "Сброшено людей: 2" in cb.message.edits[0][0]
    assert _status(1) == (None, 0) and _status(6) == (None, 0)
    assert _status(2) == ("active", 1) and _status(3) == ("candidate", 0)

    again = FakeCallback("ambrst_go:cand:2")
    _run(h.amb_reset_go(again))
    text = again.message.edits[0][0]
    assert "Сбрасывать нечего" in text
    assert _status(2) == ("active", 1)


def test_list_changed_since_preview_does_not_reset(tmp_path):
    _world(tmp_path)
    assert _run(sdb.set_status(1, "active", at=AT))
    cb = FakeCallback("ambrst_go:cand:2")
    _run(h.amb_reset_go(cb))
    assert _status(6) == ("candidate", 0)
    assert "Будет сброшено людей: 1" in cb.message.edits[0][0]


def test_bad_callback_is_stale(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("ambrst_go:zzz:1")
    _run(h.amb_reset_go(cb))
    assert cb.message.edits == []


def test_button_on_candidates_screen_only_for_global_admin_and_caps(tmp_path):
    _world(tmp_path)
    rows = _run(bulk_buttons(None))
    assert ("🧹 Сбросить статусы", "ambrst") in [(b.text, b.callback_data) for r in rows for b in r]
    scoped = _run(bulk_buttons(("moscow", ("moscow",))))
    assert "ambrst" not in [b.callback_data for r in scoped for b in r]
    for key in ("ambrst", "ambrst_p:*", "ambrst_go:*"):
        assert ADMIN_CAPS[key] == "moderate_game"
    assert required_capability(callback_data="ambrst_go:past:3") == "moderate_game"
