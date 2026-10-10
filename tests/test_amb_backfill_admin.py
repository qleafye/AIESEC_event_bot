"""«🔁 Начислить за прошлых приглашённых»: предпросмотр ничего не пишет, начисление совпадает с
показанным, повтор безопасен, кнопка на экране «Баллы и приватность», права moderate_game."""
from __future__ import annotations

import asyncio

from database import db
from handlers.amb import admin_amb_backfill as h
from handlers.amb.admin_amb_points import render_points_screen
from handlers.access.admin_caps import ADMIN_CAPS, required_capability
from tests.test_amb_tiers_admin_su5 import FakeCallback
from tests.test_referral_credit_32 import _make_ambassador, _ready, _run, _seed_user, db_rows


def _credits():
    return db_rows("SELECT invitee_id, referrer_id, coins, wave_id, source FROM referral_credits ORDER BY invitee_id")


def _setup(tmp_path, coins="25"):
    _ready(tmp_path, "amb_backfill_admin.db")
    _run(db.set_setting("ambassador_referral_coins", coins))
    _make_ambassador(9201, full_name="Амбассадор Первый")
    _seed_user(9301, referrer_id=9201, status="approved")
    _seed_user(9302, referrer_id=9201, status="approved")
    _seed_user(9303, referrer_id=9201, status="pending")


def test_preview_writes_nothing_and_shows_who_and_how_much(tmp_path):
    _setup(tmp_path)
    cb = FakeCallback("ambpt_fill")
    _run(h.amb_backfill_preview(cb))
    text, kb = cb.message.edits[0]
    assert "Начислится: 50 баллов. Амбассадоров: 1, приглашённых: 2." in text
    assert "Амбассадор Первый: приглашённых 2, баллов 50" in text
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == ["ambpt_fill_go:50:2", "admin_amb_points"]
    assert _credits() == []


def test_go_credits_exactly_previewed_and_repeat_is_safe(tmp_path):
    _setup(tmp_path)
    cb = FakeCallback("ambpt_fill_go:50:2")
    _run(h.amb_backfill_go(cb))
    assert cb.message.answers[0][0] == "Готово. Начислено баллов: 50. Амбассадоров: 1, приглашённых: 2."
    assert "Баллы и приватность" in cb.message.answers[1][0]
    assert _credits() == [(9301, 9201, 25, None, "backfill"), (9302, 9201, 25, None, "backfill")]

    again = FakeCallback("ambpt_fill_go:0:0")
    _run(h.amb_backfill_go(again))
    assert "Новых начислений нет" in again.message.answers[0][0]
    assert len(_credits()) == 2

    pv = FakeCallback("ambpt_fill")
    _run(h.amb_backfill_preview(pv))
    assert "Начислять нечего" in pv.message.edits[0][0]


def test_coins_zero_is_explained_not_applied(tmp_path):
    _setup(tmp_path, coins="0")
    cb = FakeCallback("ambpt_fill")
    _run(h.amb_backfill_preview(cb))
    assert "выключены (0)" in cb.message.edits[0][0]
    assert _credits() == []


def test_points_screen_has_button_and_caps(tmp_path):
    _setup(tmp_path)
    _, kb = _run(render_points_screen())
    assert ("🔁 Начислить за прошлых приглашённых", "ambpt_fill") in [
        (b.text, b.callback_data) for r in kb.inline_keyboard for b in r
    ]
    for key in ("ambpt_fill", "ambpt_fill_go*"):
        assert ADMIN_CAPS[key] == "moderate_game"
    assert required_capability(callback_data="ambpt_fill_go:50:2") == "moderate_game"


def test_go_with_stale_numbers_recounts_instead_of_crediting(tmp_path):
    _setup(tmp_path)
    cb = FakeCallback("ambpt_fill_go:10:1")
    _run(h.amb_backfill_go(cb))
    assert _credits() == []
    assert "Начислится: 50 баллов" in cb.message.edits[0][0]


def test_city_scoped_manager_cannot_credit_all_cities(tmp_path, monkeypatch):
    _setup(tmp_path)

    async def scoped(admin_id):
        return ("moscow", ("moscow",)), "Москва"

    monkeypatch.setattr("handlers.admin_core._admin_city_view", scoped)
    pv = FakeCallback("ambpt_fill")
    _run(h.amb_backfill_preview(pv))
    assert pv.answers[0][1] is True and "Все города" in pv.answers[0][0] and pv.message.edits == []
    go = FakeCallback("ambpt_fill_go:50:2")
    _run(h.amb_backfill_go(go))
    assert go.answers[0][1] is True and _credits() == []


def test_apply_crash_is_reported_with_next_step(tmp_path, monkeypatch):
    _setup(tmp_path)
    real = h.referrals.backfill_approved

    async def boom(dry_run=False):
        if dry_run:
            return await real(dry_run=True)
        raise RuntimeError("db locked")

    monkeypatch.setattr(h.referrals, "backfill_approved", boom)
    cb = FakeCallback("ambpt_fill_go:50:2")
    _run(h.amb_backfill_go(cb))
    text = cb.message.answers[0][0]
    assert "Не получилось начислить" in text and "«✅ Начислить»" in text
    assert cb.answers
