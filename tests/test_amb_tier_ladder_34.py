"""Лестница ступеней в админке: снять ступень, отдать место из листа ожидания, экран и права.

pytest-asyncio нет — async через `asyncio.run()`; хендлеры зовутся напрямую с фейковыми
Message/CallbackQuery (приём `tests/test_amb_tiers_admin_su5.py`).
"""
from __future__ import annotations

import asyncio
import json
import logging

from config import config
from database import amb_tiers_db as tdb
from database import db
from services.amb import amb_tiers
from tests._dbtpl import fast_init_db
from tests.test_amb_tiers_admin_su5 import (
    ADMIN_ID, FakeCallback, FakeMessage, _make_ambassador, _new_state, _sql,
)

SEASON = "SU26"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_tier_ladder_34.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))


def _quota(n, quota, on="on"):
    from shared.amb_tier_keys import tier_key
    _run(db.set_setting(tier_key(n, "quota_on"), on))
    _run(db.set_setting(tier_key(n, "quota"), str(quota)))


def _events():
    rows = _sql("SELECT kind, payload FROM miniapp_outbox WHERE kind = ?", (amb_tiers.TIER_EVENT_KIND,))
    return [json.loads(p) for _k, p in rows]


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _datas(kb):
    return [b.callback_data for b in _buttons(kb)]


# ── сервис: снять ступень ─────────────────────────────────────────────────────────────────

def test_revoke_tier_deletes_row_frees_quota_and_logs(tmp_path, caplog):
    _ready(tmp_path)
    _quota(2, 1)
    _make_ambassador(100)
    _make_ambassador(110)
    _run(tdb.claim_new_tiers(100, [1, 2], "2026-10-01 10:00:00", {2: 1}))
    _run(tdb.claim_new_tiers(110, [2], "2026-10-02 10:00:00", {2: 1}))
    assert _run(tdb.tiers_summary())[2] == {"reached": 2, "granted": 1, "waitlist": 1}

    with caplog.at_level(logging.INFO):
        assert _run(amb_tiers.revoke_tier(100, 2, by=ADMIN_ID)) is True
    assert f"admin={ADMIN_ID} amb_tier_revoke tid=100 tier=2" in caplog.text
    assert [r["tier"] for r in _run(tdb.list_tiers(100))] == [1]
    assert _run(tdb.tiers_summary())[2] == {"reached": 1, "granted": 0, "waitlist": 1}
    assert _events() == []
    assert _run(amb_tiers.revoke_tier(100, 2, by=ADMIN_ID)) is False


# ── сервис: отдать место ──────────────────────────────────────────────────────────────────

def test_promote_waitlist_gives_slot_to_earliest_and_enqueues(tmp_path, caplog):
    _ready(tmp_path)
    _quota(2, 1)
    for tid in (100, 110, 120):
        _make_ambassador(tid)
    _run(tdb.claim_new_tiers(100, [2], "2026-10-01 10:00:00", {2: 1}))
    _run(tdb.claim_new_tiers(120, [2], "2026-10-03 10:00:00", {2: 1}))
    _run(tdb.claim_new_tiers(110, [2], "2026-10-02 10:00:00", {2: 1}))
    _sql("UPDATE ambassador_tiers SET notified_at = '2026-10-01 10:00:00'")

    assert _run(amb_tiers.promote_waitlist(2, by=ADMIN_ID)) == "no_slot"
    assert _run(amb_tiers.revoke_tier(100, 2, by=ADMIN_ID))
    with caplog.at_level(logging.INFO):
        assert _run(amb_tiers.promote_waitlist(2, by=ADMIN_ID)) == "promoted:110"
    assert f"admin={ADMIN_ID} amb_tier_promote tid=110 tier=2" in caplog.text
    rows = {r["telegram_id"]: r for r in _run(tdb.list_tiers())}
    assert rows[110]["o2o_status"] == "granted" and rows[110]["notified_at"] is None
    assert rows[120]["o2o_status"] == "waitlist"
    events = _events()
    assert len(events) == 1 and events[0]["telegram_id"] == 110 and events[0]["tier"] == 2
    # место снова занято
    assert _run(amb_tiers.promote_waitlist(2, by=ADMIN_ID)) == "no_slot"


def test_promote_waitlist_empty_and_no_quota(tmp_path):
    _ready(tmp_path)
    assert _run(amb_tiers.promote_waitlist(2, by=ADMIN_ID)) == "no_quota"
    _quota(2, 5)
    assert _run(amb_tiers.promote_waitlist(2, by=ADMIN_ID)) == "empty"
    assert _run(amb_tiers.promote_waitlist(4, by=ADMIN_ID)) == "no_quota"


def test_promote_race_single_promotion(tmp_path):
    _ready(tmp_path)
    _quota(2, 1)
    for tid in (100, 110, 120):
        _make_ambassador(tid)
    _run(tdb.claim_new_tiers(100, [2], "2026-10-01 10:00:00", {2: 1}))
    _run(tdb.claim_new_tiers(110, [2], "2026-10-02 10:00:00", {2: 1}))
    _run(tdb.claim_new_tiers(120, [2], "2026-10-03 10:00:00", {2: 1}))
    _run(amb_tiers.revoke_tier(100, 2, by=ADMIN_ID))

    async def both():
        return await asyncio.gather(
            amb_tiers.promote_waitlist(2, by=1), amb_tiers.promote_waitlist(2, by=2),
        )

    results = _run(both())
    assert sorted(r.split(":")[0] for r in results) == ["no_slot", "promoted"]
    assert _run(tdb.tiers_summary())[2]["granted"] == 1


# ── экран лестницы ────────────────────────────────────────────────────────────────────────

def _open_ladder(user_id=ADMIN_ID):
    from handlers.amb import admin_amb_tier_ladder as h
    cb = FakeCallback("ambl:main", user_id)
    _run(h.show_ladder(cb, _new_state()))
    return cb.message.edits[-1]


def test_ladder_screen_rows_buttons_and_quota_wording(tmp_path):
    _ready(tmp_path)
    _quota(2, 15)
    _make_ambassador(100)
    _run(tdb.claim_new_tiers(100, [1, 2], "2026-10-01 10:00:00", {2: 15}))
    text, kb = _open_ladder()
    assert "🎓 2: за 3 прошедших" in text
    assert "наград на ступени" in text and "выдано 1" in text
    assert "Ступени только амбассадору с одобренной заявкой" in text
    assert "amb_" not in text
    datas = _datas(kb)
    for expected in ("settings_edit:amb_tier1_threshold", "settings_edit:amb_tier2_granted_text",
                     "settings_edit:amb_o2o_quota", "settings_edit:amb_tier2_waitlist_text",
                     "ambl_quota:2", "ambl_quota:1", "ambl_add", "ambl_del", "ambl_req", "ambl_rev",
                     "admin_amb_tiers"):
        assert expected in datas, expected
    assert "settings_edit:amb_tier1_quota" not in datas  # квота ступени 1 выключена
    labels = " ".join(b.text for b in _buttons(kb))
    assert "amb_" not in labels


def test_ladder_hides_add_at_five_and_del_at_one(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_tiers_count", "5"))
    _, kb = _open_ladder()
    assert "ambl_add" not in _datas(kb) and "ambl_del" in _datas(kb)
    _run(db.set_setting("amb_tiers_count", "1"))
    _, kb = _open_ladder()
    assert "ambl_add" in _datas(kb) and "ambl_del" not in _datas(kb)


def test_ladder_without_settings_right_hides_edit_buttons(tmp_path):
    _ready(tmp_path)
    _sql("INSERT INTO admin_roles (telegram_id, role) VALUES (?, ?)", (777001, "moderator")) \
        if False else None
    from handlers.amb import admin_amb_tier_ladder as h

    async def no_settings(*_a, **_k):
        return False

    orig = h.has_capability
    h.has_capability = no_settings
    try:
        text, kb = _open_ladder(777001)
    finally:
        h.has_capability = orig
    assert not any(d.startswith("settings_edit:") for d in _datas(kb))
    assert "ambl_add" in _datas(kb)


def test_add_tier_and_threshold_alert(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    _run(db.set_setting("amb_tier3_threshold", "20"))
    cb = FakeCallback("ambl_add")
    _run(h.ladder_add(cb))
    assert _run(db.get_setting("amb_tiers_count")) == "4"
    text, show_alert = cb.answers[-1]
    assert show_alert and "Задайте порог ступени 4 больше, чем у ступени 3 (20)" in text
    assert len(text) <= 200
    assert "settings_edit:amb_tier4_threshold" in _datas(cb.message.edits[-1][1])

    _run(db.set_setting("amb_tiers_count", "5"))
    cb2 = FakeCallback("ambl_add")
    _run(h.ladder_add(cb2))
    assert _run(db.get_setting("amb_tiers_count")) == "5"
    assert cb2.answers[-1][1] is True


def test_delete_last_needs_confirmation_and_says_who_keeps_it(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    _run(db.set_setting("amb_tiers_count", "4"))
    for tid in (100, 110, 120):
        _make_ambassador(tid)
        _run(tdb.claim_new_tiers(tid, [4], "2026-10-01 10:00:00", {}))
    cb = FakeCallback("ambl_del")
    _run(h.ladder_del(cb))
    text, kb = cb.message.edits[-1]
    assert "Убрать ступень 4?" in text and "3 человека" in text and "у них она останется" in text
    assert _run(db.get_setting("amb_tiers_count")) == "4"
    assert "ambl_del_go" in _datas(kb) and "ambl:main" in _datas(kb)

    _run(h.ladder_del_go(FakeCallback("ambl_del_go")))
    assert _run(db.get_setting("amb_tiers_count")) == "3"
    assert len(_run(tdb.list_tiers(100))) == 1  # строки ступени не тронуты


def test_require_approved_toggle_alerts(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    cb = FakeCallback("ambl_req")
    _run(h.ladder_require_toggle(cb))
    assert _run(amb_tiers.require_approved_on()) is False
    assert "даже без заявки" in cb.answers[-1][0] and cb.answers[-1][1] is True
    cb2 = FakeCallback("ambl_req")
    _run(h.ladder_require_toggle(cb2))
    assert _run(amb_tiers.require_approved_on()) is True
    assert "с одобренной заявкой" in cb2.answers[-1][0]


def test_quota_toggle_by_button(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    cb = FakeCallback("ambl_quota:3")
    _run(h.ladder_quota_toggle(cb))
    assert _run(db.get_setting("amb_tier3_quota_on")) == "on"
    assert cb.answers[-1][1] is True and len(cb.answers[-1][0]) <= 200
    _run(h.ladder_quota_toggle(FakeCallback("ambl_quota:3")))
    assert _run(db.get_setting("amb_tier3_quota_on")) == "off"
    bad = FakeCallback("ambl_quota:9")
    _run(h.ladder_quota_toggle(bad))
    assert "устарела" in bad.answers[-1][0]


# ── снять ступень: FSM ────────────────────────────────────────────────────────────────────

def test_revoke_flow_full(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    _quota(2, 5)
    _make_ambassador(100, username="anna")
    _run(tdb.claim_new_tiers(100, [1, 2], "2026-10-01 10:00:00", {2: 5}))
    state = _new_state()
    _run(h.revoke_start(FakeCallback("ambl_rev"), state))
    assert _run(state.get_state()) == "AmbTierRevoke:waiting_for_person"

    bad = FakeMessage("какой-то текст")
    _run(h.revoke_person_step(bad, state))
    assert "@username" in bad.answers[-1][0]
    nobody = FakeMessage("@nobody_here")
    _run(h.revoke_person_step(nobody, state))
    assert "Не нашёл" in nobody.answers[-1][0]

    msg = FakeMessage("@anna")
    _run(h.revoke_person_step(msg, state))
    text, kb = msg.answers[-1]
    assert _datas(kb)[:2] == ["ambl_rev_pick:1", "ambl_rev_pick:2"]

    cb = FakeCallback("ambl_rev_pick:2")
    _run(h.revoke_pick(cb, state))
    text, kb = cb.message.edits[-1]
    assert "Снять ступень 2 у Амбассадор 100" in text
    assert "Ей ничего не придёт" in text or "ничего не придёт" in text
    assert "место в квоте освободится" in text
    assert _run(tdb.list_tiers(100))[1]["tier"] == 2  # ещё не снято

    go = FakeCallback("ambl_rev_go:2")
    _run(h.revoke_go(go, state))
    assert [r["tier"] for r in _run(tdb.list_tiers(100))] == [1]
    assert _run(state.get_state()) is None
    assert _events() == []


def test_revoke_person_without_tiers_keeps_waiting_and_cancel(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    _make_ambassador(100)
    state = _new_state()
    _run(h.revoke_start(FakeCallback("ambl_rev"), state))
    msg = FakeMessage("100")
    _run(h.revoke_person_step(msg, state))
    assert "нет ступеней" in msg.answers[-1][0]
    assert _run(state.get_state()) == "AmbTierRevoke:waiting_for_person"
    stop = FakeMessage("/admin")
    _run(h.revoke_person_step(stop, state))
    assert _run(state.get_state()) is None


def test_revoke_go_without_state_is_noop(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    _make_ambassador(100)
    _run(tdb.claim_new_tiers(100, [1], "2026-10-01 10:00:00", {}))
    cb = FakeCallback("ambl_rev_go:1")
    _run(h.revoke_go(cb, _new_state()))
    assert len(_run(tdb.list_tiers(100))) == 1
    assert "устарела" in cb.answers[-1][0]


# ── отдать место ──────────────────────────────────────────────────────────────────────────

def test_promote_button_visible_only_with_slot_and_waitlist(tmp_path):
    _ready(tmp_path)
    _quota(2, 1)
    for tid in (100, 110):
        _make_ambassador(tid)
    _run(tdb.claim_new_tiers(100, [2], "2026-10-01 10:00:00", {2: 1}))
    _run(tdb.claim_new_tiers(110, [2], "2026-10-02 10:00:00", {2: 1}))
    _, kb = _open_ladder()
    assert "ambl_prom:2" not in _datas(kb)
    _run(amb_tiers.revoke_tier(100, 2, by=ADMIN_ID))
    text, kb = _open_ladder()
    assert "ambl_prom:2" in _datas(kb) and "ждут 1" in text


def test_promote_flow_confirm_then_go(tmp_path):
    from handlers.amb import admin_amb_tier_ladder as h
    _ready(tmp_path)
    _quota(2, 1)
    _make_ambassador(100)
    _make_ambassador(110, username="ivan")
    _run(tdb.claim_new_tiers(100, [2], "2026-10-01 10:00:00", {2: 1}))
    _run(tdb.claim_new_tiers(110, [2], "2026-10-12 10:00:00", {2: 1}))
    _run(amb_tiers.revoke_tier(100, 2, by=ADMIN_ID))

    cb = FakeCallback("ambl_prom:2")
    _run(h.promote_confirm(cb))
    text, kb = cb.message.edits[-1]
    assert "Отдать освободившееся место на ступени 2" in text
    assert "дошёл 12.10" in text and "Ему придёт текст ступени" in text
    assert "ambl_prom_go:2" in _datas(kb)
    assert _events() == []

    go = FakeCallback("ambl_prom_go:2")
    _run(h.promote_go(go))
    assert len(_events()) == 1
    assert _run(tdb.tiers_summary())[2]["granted"] == 1

    again = FakeCallback("ambl_prom_go:2")
    _run(h.promote_go(again))
    assert len(_events()) == 1
    assert "листе ожидания" in again.answers[-1][0]


# ── права, регистрация, экран ступеней ────────────────────────────────────────────────────

def test_caps_resolve_for_every_ladder_callback():
    from handlers.access.admin_caps import required_capability
    for data in ("ambl:main", "ambl_add", "ambl_del", "ambl_del_go", "ambl_quota:2", "ambl_req",
                 "ambl_rev", "ambl_rev_cancel", "ambl_rev_pick:2", "ambl_rev_go:2",
                 "ambl_prom:2", "ambl_prom_go:2"):
        assert required_capability(callback_data=data) == "moderate_game", data
    assert required_capability(raw_state="AmbTierRevoke:waiting_for_person") == "moderate_game"


def test_tiers_screen_links_to_ladder(tmp_path):
    from handlers.amb import admin_amb_tiers as h
    _ready(tmp_path)
    cb = FakeCallback("admin_amb_tiers")
    _run(h.show_amb_tiers(cb, _new_state()))
    text, kb = cb.message.edits[-1]
    assert "ambl:main" in _datas(kb) and "settings_group:game" not in _datas(kb)
    assert "🪜 Лестница ступеней" in " ".join(b.text for b in _buttons(kb))
    assert "Ступень 3" in text
