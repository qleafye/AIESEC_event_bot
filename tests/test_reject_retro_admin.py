"""«🕘 Применить к уже поданным»: предпросмотр ничего не пишет, применение меняет ровно показанное,
повтор безопасен, кнопки и права. Движок правил подменён (`_auto_reject_patch`) — проверяем
обвязку, а не сами условия правил.
"""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db
from handlers.applications import admin_reject_retro as h
from handlers.access.admin_caps import ADMIN_CAPS, required_capability
from services.registration import reg_finalize
from services.applications import reject_retro
from tests._dbtpl import fast_init_db
from tests.test_amb_tiers_admin_su5 import ADMIN_ID, FakeCallback

SEASON = "SU26"
RULE_TEXT = "Нужен курс из списка"


def _run(coro):
    return asyncio.run(coro)


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, status, date="2099-01-01 10:00:00", username=None, bad=True):
    _run(db.add_user({
        "telegram_id": tid, "full_name": f"Делегат {tid}", "registration_date": date,
        "season": SEASON, "event_city": "moscow" if bad else "kazan",
    }))
    _run(db.set_user_status(tid, status))
    if username:
        _sql("UPDATE users SET username = ? WHERE telegram_id = ?", (username, tid))


def _setup(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "retro.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))
    _seed(1, "pending", username="p_one")
    _seed(2, "pending")
    _seed(3, "rejected")
    _seed(4, "approved")
    _seed(5, "pending", bad=False)
    _seed(6, "pending", date="2000-01-01 10:00:00")

    async def fake_patch(tid, answers):
        if answers.get("event_city") != "moscow":
            return {}
        return {
            "auto_reject_rule_ids": "[7]", "auto_rejected_at": "2099-01-02 03:04:05",
            "flagged_rule_ids": None, "auto_rule_note": "🤖 Автоотказ", "rejected_at": "2099-01-02 03:04:05",
            "status_override": "rejected", "reject_rule_ids": [7], "reject_texts": [RULE_TEXT],
        }

    sent = []

    async def fake_effects(bot, tid, status, reason, **kw):
        sent.append((tid, status, reason))

    monkeypatch.setattr(reg_finalize, "_auto_reject_patch", fake_patch)
    monkeypatch.setattr("services.applications.application_effects.apply_decision_effects", fake_effects)
    return sent


def _state():
    return {t: (s, a) for t, s, a in _sql("SELECT telegram_id, status, auto_reject_rule_ids FROM users")}


def _digest(since="2026-01-01"):
    return _run(reject_retro.preview(since))["digest"]


class FakeBotCallback(FakeCallback):
    bot = object()


def test_preview_writes_nothing_and_counts(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    before = _state()
    report = _run(reject_retro.preview("2026-01-01"))
    assert (report["pending"], report["rejected"], report["approved"]) == (2, 1, 1)
    assert "Делегат 1 (@p_one)" in report["examples"]
    assert _state() == before


def test_screen_preview_shows_people_words_and_apply_button(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(h, "_since", lambda p: "2026-01-01")
    cb = FakeBotCallback("rjretro_p:7")
    _run(h.reject_retro_preview(cb))
    text, kb = cb.message.edits[0]
    assert "делегаты получат письмо с причиной: 2" in text
    assert "писем не будет: 1" in text
    assert "@p_one" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"rjretro_go:7:{_digest()}" in datas and "admin_reject_rules" in datas
    assert _state()[1][0] == "pending"


def test_apply_changes_exactly_previewed_and_is_idempotent(tmp_path, monkeypatch):
    sent = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(h, "_since", lambda p: "2026-01-01")
    cb = FakeBotCallback(f"rjretro_go:7:{_digest()}")
    _run(h.reject_retro_go(cb))
    st = _state()
    assert st[1][0] == st[2][0] == st[3][0] == "rejected"
    assert st[4] == ("approved", None)
    assert st[5] == ("pending", None)
    assert st[6] == ("pending", None)
    assert st[1][1] and st[3][1]
    assert sorted(t for t, _, _ in sent) == [1, 2]
    assert sent[0][2] == RULE_TEXT
    assert cb.message.answers[0][0] == "Готово. Отклонено правилами: 2, пометка у отклонённых вручную: 1."

    sent.clear()
    again = FakeBotCallback(f"rjretro_go:7:{_digest()}")
    _run(h.reject_retro_go(again))
    assert again.message.answers[0][0] == "Готово. Отклонено правилами: 0, пометка у отклонённых вручную: 0."
    assert sent == []
    preview = FakeBotCallback("rjretro_p:7")
    _run(h.reject_retro_preview(preview))
    assert "применять нечего" in preview.message.edits[0][0]


def test_menu_has_period_buttons_and_denied_for_city_manager(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    cb = FakeBotCallback("rjretro")
    _run(h.reject_retro_menu(cb))
    datas = [b.callback_data for row in cb.message.edits[0][1].inline_keyboard for b in row]
    assert datas[:5] == [f"rjretro_p:{p}" for p in ("3", "7", "14", "30", "all")]

    _sql("INSERT INTO staff (telegram_id, role, city, added_at) VALUES (777, 'moderator', 'moscow', '2026-01-01 00:00:00')")
    config.ADMIN_IDS = []
    denied = FakeBotCallback("rjretro", user_id=777)
    _run(h.reject_retro_menu(denied))
    assert denied.answers and denied.answers[0][1] is True


def test_bad_period_is_explained(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    cb = FakeBotCallback("rjretro_go:zzz:abcd")
    _run(h.reject_retro_go(cb))
    assert "Период не распознан" in cb.answers[0][0]


def test_button_on_rules_screen_and_caps(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from handlers.applications.admin_reject_rules import render_rules_screen

    _, kb = _run(render_rules_screen(ADMIN_ID))
    assert ("🕘 Применить к уже поданным", "rjretro") in [
        (b.text, b.callback_data) for row in kb.inline_keyboard for b in row
    ]
    for key in ("rjretro", "rjretro_p:*", "rjretro_go:*"):
        assert ADMIN_CAPS[key] == "settings"
    assert required_capability(callback_data="rjretro_go:7:abcd1234") == "settings"


def test_failure_after_effects_does_not_lose_row(tmp_path, monkeypatch):
    """Сбой посередине обработки: метка автоотказа не должна «закрыть» строку, иначе повтор её
    пропустит навсегда."""
    sent = _setup(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def flaky(bot, tid, status, reason, **kw):
        if tid == 1 and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("telegram упал")
        sent.append((tid, status, reason))

    monkeypatch.setattr("services.applications.application_effects.apply_decision_effects", flaky)
    first = _run(reject_retro.apply(object(), "2026-01-01", pause=0))
    assert first["failed"] == 1
    assert _state()[1][1] is None  # метка не поставлена — строка видна повтору
    second = _run(reject_retro.apply(object(), "2026-01-01", pause=0))
    assert second["failed"] == 0
    assert _state()[1][1]  # дообработана
    assert [t for t, _, _ in sent].count(1) == 1  # письмо не потеряно: статус уже rejected, но дошлём
    assert _sql("SELECT attempt_count FROM auto_reject_log WHERE telegram_id = 1") == [(1,)]  # журнал не задвоен
    assert _sql("SELECT decided_by FROM application_decisions WHERE telegram_id = 1") == [(-1,)]


def test_manual_reject_does_not_get_a_letter(tmp_path, monkeypatch):
    """Отклонённый менеджером вручную (решение человека в журнале) получает только пометку, даже
    если в журнале автоотказа у него осталась живая строка с прошлых подач."""
    sent = _setup(tmp_path, monkeypatch)
    from services.applications.reject_journal import record_auto_reject
    _run(record_auto_reject(3, [7], [RULE_TEXT]))
    stamp = "2099-01-01 00:00:00"
    _run(db.record_application_decision(3, "rejected", "вручную", 777, stamp, stamp, effects_sent_at=stamp))
    _run(reject_retro.apply(object(), "2026-01-01", pause=0))
    assert 3 not in [t for t, _, _ in sent]
    assert _state()[3][1]  # пометка поставлена
    assert _sql("SELECT COUNT(*) FROM application_decisions WHERE telegram_id = 3") == [(1,)]


def test_sheet_failure_is_reported(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(h, "_since", lambda p: "2026-01-01")
    monkeypatch.setattr("services.sheets.sheet_target.sheets_enabled", lambda: True)

    async def bulk(mapping):
        return -1

    monkeypatch.setattr("services.sheets.sheets.bulk_update_status_in_sheet", bulk)
    cb = FakeBotCallback(f"rjretro_go:7:{_digest()}")
    _run(h.reject_retro_go(cb))
    assert "Сверить таблицу с базой" in cb.message.answers[0][0]


def test_manager_approval_during_run_is_not_overwritten(tmp_path, monkeypatch):
    sent = _setup(tmp_path, monkeypatch)
    real = reg_finalize._auto_reject_patch
    seen = {"n": 0}

    async def approving(tid, answers):
        patch = await real(tid, answers)
        seen["n"] += tid == 2
        if tid == 2 and seen["n"] >= 2:  # менеджер одобрил, пока шёл многоминутный цикл
            _sql("UPDATE users SET status = 'approved' WHERE telegram_id = 2")
        return patch

    monkeypatch.setattr(reg_finalize, "_auto_reject_patch", approving)
    pairs = _run(reject_retro.collect("2026-01-01"))
    ids = {int(u["telegram_id"]) for u, _ in pairs}
    done = _run(reject_retro.apply(object(), "2026-01-01", pause=0, ids=ids))
    assert _state()[2][0] == "approved"
    assert 2 not in [t for t, _, _ in sent]
    assert done["skipped"] >= 1


def test_apply_only_previewed_ids(tmp_path, monkeypatch):
    sent = _setup(tmp_path, monkeypatch)
    _run(reject_retro.apply(object(), "2026-01-01", pause=0, ids={1}))
    assert _state()[1][0] == "rejected" and _state()[2][0] == "pending"
    assert [t for t, _, _ in sent] == [1]


def test_stale_digest_recounts_instead_of_applying(tmp_path, monkeypatch):
    sent = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(h, "_since", lambda p: "2026-01-01")
    stale = _digest()
    _seed(8, "pending")  # появился ещё один подходящий после предпросмотра
    cb = FakeBotCallback(f"rjretro_go:7:{stale}")
    _run(h.reject_retro_go(cb))
    assert sent == [] and _state()[1][0] == "pending"
    assert "делегаты получат письмо с причиной: 3" in cb.message.edits[0][0]


def test_sheet_updated_with_one_bulk_call(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    calls = []

    async def bulk(mapping):
        calls.append(mapping)

    monkeypatch.setattr("services.sheets.sheets.bulk_update_status_in_sheet", bulk)
    _run(reject_retro.apply(object(), "2026-01-01", pause=0))
    assert len(calls) == 1 and set(calls[0]) == {"1", "2"}


def test_apply_error_is_reported_and_summary_before_screen(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(h, "_since", lambda p: "2026-01-01")

    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(reject_retro, "apply", boom)
    cb = FakeBotCallback(f"rjretro_go:7:{_digest()}")
    _run(h.reject_retro_go(cb))
    assert "оборвалось" in cb.message.answers[0][0]
