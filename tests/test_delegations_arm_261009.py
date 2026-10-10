"""Делегации вузов: модуль одобряет и пишет людям только после «✅ Включить делегации».

Гейт `is_armed()` в services/delegations.py, экраны включения/выключения в
handlers/delegations/admin_delegations.py, ручные решения и привязка в handlers/delegations/admin_delegations_review.py,
миграция совместимости в init_db. Окружение — из tests/test_delegations_core.py.
pytest-asyncio нет — `asyncio.run()`.
"""
from __future__ import annotations

import sqlite3

from config import config
from database import delegations_db as ddb
from database import ext_forms_db as ef
from handlers.delegations import admin_delegations as mod
from handlers.delegations import admin_delegations_review as review
from services import delegations as dlg
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import get_setting_typed
from tests.test_delegations_admin import (
    _FakeCallback, _callbacks, _last_edit, _run, _state,
)
from tests.test_delegations_core import (
    MANAGER, _answer_from_fixture, _delegation_form, _env, _reg_started,
)

ARM = "delegation_armed_form_id"


def _ans(fid, aid, **kw):
    kw.setdefault("answered_at", "2026-09-01 12:00:00")  # до отсечки ЦА — «ok»
    return _answer_from_fixture(fid, aid, **kw)


def _drow(fid, aid):
    return _run(ddb.get_by_answer(fid, aid))


def _set(key, value):
    _run(set_setting_by_admin(None, key, str(value)))


def _disarmed_env(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()  # выбрана и включена фикстурой
    _set(ARM, "")
    return bot, fid


def _patch_spawn(monkeypatch):
    spawned = []
    monkeypatch.setattr(mod, "spawn", lambda coro: spawned.append(coro))
    return spawned


def _drain(spawned):
    for coro in spawned:
        _run(coro)
    spawned.clear()


def _available(fid, aid, reason="ingest"):
    return _run(dlg.on_answer_available(fid, aid, reason=reason))


# ── сервис ────────────────────────────────────────────────────────────────────────────────

def test_not_armed_evaluates_but_does_not_convert(tmp_path):
    bot, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1")
    _reg_started(501)
    res = _available(fid, "a1")
    assert res["waiting"] == "not_armed" and "converted" not in res
    row = _drow(fid, "a1")
    assert row["ta_status"] == "ok" and row["linked_telegram_id"] is None
    assert bot.sent == []


def test_armed_converts_as_before(tmp_path):
    bot, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1")
    _reg_started(501)
    _set(ARM, fid)
    res = _available(fid, "a1")
    assert res.get("converted")
    assert _drow(fid, "a1")["linked_telegram_id"] == 501
    assert len(bot.sent) == 1


def test_form_change_disarms(tmp_path):
    bot, fid = _disarmed_env(tmp_path)
    _set(ARM, fid)
    assert _run(dlg.is_armed()) is True
    other = _run(ef.create_form(platform="google", external_id="other", title="Другая"))
    _set("delegation_form_id", other)
    assert _run(dlg.is_armed()) is False


def test_try_delegate_start_not_armed_has_no_side_effects(tmp_path):
    bot, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1")
    _available(fid, "a1")
    before = _drow(fid, "a1")

    class _M:
        from_user = type("U", (), {"id": 501, "username": "IvanTest"})()

    assert _run(dlg.try_delegate_start(_M(), None, bot)) is False
    assert _drow(fid, "a1") == before and bot.sent == []


def test_sweep_not_armed_evaluates_without_converting(tmp_path):
    bot, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1")
    _reg_started(501)
    res = _run(dlg.sweep_pending(reevaluate=True))
    assert res["linked"] == 0 and bot.sent == []
    assert _run(ddb.count_by_status(fid, "ok", linked=None)) == 1
    assert _drow(fid, "a1")["linked_telegram_id"] is None


# ── экран ─────────────────────────────────────────────────────────────────────────────────

def test_screen_shows_state_and_button(tmp_path):
    _, fid = _disarmed_env(tmp_path)
    cb = _FakeCallback("admin_delegations")
    _run(mod.render_screen(cb))
    text, _, kb = _last_edit(cb)
    assert "⏸ Не включено" in text and "dlg_arm" in _callbacks(kb)
    assert "dlg_disarm" not in _callbacks(kb)
    _set(ARM, fid)
    cb = _FakeCallback("admin_delegations")
    _run(mod.render_screen(cb))
    text, _, kb = _last_edit(cb)
    assert "▶️ Включено" in text and "dlg_disarm" in _callbacks(kb)
    assert "dlg_arm" not in _callbacks(kb)


def test_dlg_form_disarms(tmp_path):
    _, fid = _disarmed_env(tmp_path)
    _set(ARM, fid)
    _ans(fid, "a1")  # колонки формы
    cb = _FakeCallback(f"dlg_form:{fid}")
    _run(mod.dlg_form(cb))
    assert _run(get_setting_typed(ARM)) in ("", None)
    assert _run(dlg.is_armed()) is False


def test_dlg_arm_confirm_with_count(tmp_path):
    _, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1")
    _reg_started(501)
    cb = _FakeCallback("dlg_arm")
    _run(mod.dlg_arm(cb))
    text, _, kb = _last_edit(cb)
    assert "Бот одобрит 1 человек" in text and "dlg_arm_yes" in _callbacks(kb)
    assert _run(dlg.is_armed()) is False


def test_dlg_arm_zero_text(tmp_path):
    _, fid = _disarmed_env(tmp_path)
    cb = _FakeCallback("dlg_arm")
    _run(mod.dlg_arm(cb))
    assert "Сейчас в боте нет никого из ЦА этой формы" in _last_edit(cb)[0]


def test_dlg_arm_without_keys_alerts(tmp_path):
    _, fid = _disarmed_env(tmp_path)
    _set("delegation_q_course", "")
    cb = _FakeCallback("dlg_arm")
    _run(mod.dlg_arm(cb))
    assert cb.answer_calls == [(mod._KEYS_MISSING, True)] and not cb.message.edits


def test_dlg_arm_yes_arms_and_sweeps(tmp_path, monkeypatch):
    bot, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1")
    _reg_started(501)
    spawned = _patch_spawn(monkeypatch)
    cb = _FakeCallback("dlg_arm_yes")
    _run(mod.dlg_arm_yes(cb))
    assert str(_run(get_setting_typed(ARM))) == str(fid)
    assert cb.answer_calls[0][0] == mod._ARM_TOAST
    assert len(spawned) == 1
    _drain(spawned)
    assert _drow(fid, "a1")["linked_telegram_id"] == 501 and len(bot.sent) == 1


def test_dlg_disarm(tmp_path):
    _, fid = _disarmed_env(tmp_path)
    _set(ARM, fid)
    cb = _FakeCallback("dlg_disarm")
    _run(mod.dlg_disarm(cb))
    assert _run(dlg.is_armed()) is False
    assert "остаются одобренными" in cb.answer_calls[0][0]


def test_offer_reevaluate_not_armed(tmp_path, monkeypatch):
    _, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1")
    _reg_started(501)
    spawned = _patch_spawn(monkeypatch)
    cb = _FakeCallback("x")
    assert _run(mod._offer_reevaluate(cb, "Сохранено.")) is False
    assert len(spawned) == 1 and not cb.message.edits
    spawned[0].close()


# ── ручные решения и привязка ─────────────────────────────────────────────────────────────

def test_manual_ta_before_arm_only_records(tmp_path, monkeypatch):
    bot, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1", course="2")
    _reg_started(501)
    _run(ddb.upsert_eval(fid, "a1", ta_status="check", university="Вуз", course_raw="2",
                         course_canonical=None, username_needle="IvanTest",
                         answered_at="2026-09-01 12:00:00"))
    r = _drow(fid, "a1")
    cb = _FakeCallback(f"dlg_ta:{r['id']}:ok", user_id=MANAGER)
    _run(review.dlg_ta(cb))
    assert "Включить делегации" in cb.answer_calls[0][0]
    r = _drow(fid, "a1")
    assert r["decided_by"] == MANAGER and r["linked_telegram_id"] is None
    assert bot.sent == []
    _set(ARM, fid)
    _run(dlg.sweep_pending(reevaluate=True))
    assert _drow(fid, "a1")["linked_telegram_id"] == 501


def test_link_blocked_before_arm(tmp_path):
    bot, fid = _disarmed_env(tmp_path)
    _ans(fid, "a1", username="@nobody")
    _run(dlg.sweep_pending(reevaluate=True))
    r = _drow(fid, "a1")
    st = _state()
    cb = _FakeCallback(f"dlg_link:{r['id']}", user_id=MANAGER)
    _run(review.dlg_link(cb, st))
    assert cb.answer_calls == [(review._NOT_ARMED_LINK, True)]
    cb = _FakeCallback("dlg_link_yes", user_id=MANAGER)
    _run(review.dlg_link_yes(cb, st))
    assert cb.answer_calls == [(review._NOT_ARMED_LINK, True)]
    assert bot.sent == [] and _drow(fid, "a1")["linked_telegram_id"] is None


# ── миграция ──────────────────────────────────────────────────────────────────────────────

def _armed_in_db():
    conn = sqlite3.connect(config.DB_PATH)
    r = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (ARM,)).fetchone()
    conn.close()
    return None if r is None else r[0]


def _restart():
    # шаблонная БД кешируется; миграция гоняется настоящим init_db
    from database import db
    _run(db.init_db())


def test_migration_keeps_selected_form_armed(tmp_path):
    _env(tmp_path)
    assert _armed_in_db() is None  # формы нет — выключено
    _restart()
    assert _armed_in_db() is None
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("INSERT INTO bot_settings (key, value) VALUES ('delegation_form_id', '5')")
    conn.commit()
    conn.close()
    _restart()
    assert _armed_in_db() == "5"


def test_migration_does_not_rearm_after_disarm(tmp_path):
    _env(tmp_path)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("INSERT INTO bot_settings (key, value) VALUES ('delegation_form_id', '5')")
    conn.execute(f"INSERT INTO bot_settings (key, value) VALUES ('{ARM}', '')")
    conn.commit()
    conn.close()
    _restart()
    assert _armed_in_db() == ""
