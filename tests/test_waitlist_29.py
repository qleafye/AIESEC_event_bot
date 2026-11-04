"""Лист ожидания заявок: статус waitlist рядом с pending/approved/rejected.

Дефолт тумблера — выключен. Пока он выключен, экраны не меняются. Статус waitlist
не пускает делегата как одобренного и не шлёт текст отказа.
"""
from __future__ import annotations

import asyncio

import services.applications.application_effects as application_effects
from config import config
from database import db
from domain.regform.labels import STATUS_LABELS
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.user_actions import _gate_decision as bot_gate
from miniapp.deps import _gate_decision as web_gate
from tests._dbtpl import fast_init_db

_FORBIDDEN = ("отбор", "не прошёл", "не прошел", "отказ")


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "waitlist_29.db")


def _run(coro):
    return asyncio.run(coro)


def _seed(tid, status="pending"):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": "2026-11-03 12:00:00",
    }))
    _run(db.set_user_status(tid, status))


def _status(tid) -> str | None:
    user = _run(db.get_user(tid))
    return None if user is None else user.get("status")


def _claim_waitlist(tid):
    from services.applications.applications import claim_waitlist
    return _run(claim_waitlist(tid))


def test_labels_keep_three_old_and_add_waitlist():
    assert STATUS_LABELS["pending"] == "Новая"
    assert STATUS_LABELS["approved"] == "Одобрена"
    assert STATUS_LABELS["rejected"] == "Отклонена"
    assert STATUS_LABELS["waitlist"] == "Лист ожидания"


def test_waitlist_defaults_have_no_forbidden_words():
    for key in ("waitlist_delegate_text", "waitlist_heading_text"):
        default = SETTINGS_SCHEMA[key]["default"].lower()
        for word in _FORBIDDEN:
            assert word not in default, key
        assert SETTINGS_SCHEMA[key]["group"] == "reg"
        assert SETTINGS_SCHEMA[key]["type"] == "text"


def test_toggle_default_off():
    spec = SETTINGS_SCHEMA["apps_waitlist_enabled"]
    assert spec["default"] == "off"
    assert spec["group"] == "apps"
    assert spec["type"] == "enum"
    assert spec["options"] == ["on", "off"]
    assert "📋 Лист ожидания" in spec["label"]


def test_claim_waitlist_from_pending_only(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()
    _seed(2901, "pending")
    _seed(2902, "approved")
    _seed(2903, "rejected")
    _seed(2908, "pending")
    _run(_mark_walkin(2908))

    assert _claim_waitlist(2901) is True
    assert _status(2901) == "waitlist"
    assert _claim_waitlist(2901) is False
    assert _status(2901) == "waitlist"

    assert _claim_waitlist(2902) is False
    assert _status(2902) == "approved"
    assert _claim_waitlist(2903) is False
    assert _status(2903) == "rejected"
    assert _claim_waitlist(2908) is False
    assert _status(2908) == "pending"


def test_approve_and_reject_from_waitlist(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()
    _seed(2911, "pending")
    _seed(2912, "waitlist")
    _seed(2913, "waitlist")
    _seed(2914, "approved")

    assert _run(db.approve_user_atomic(2911)) is True
    assert _status(2911) == "approved"
    assert _run(db.approve_user_atomic(2911)) is False

    assert _run(db.approve_user_atomic(2912)) is True
    assert _status(2912) == "approved"

    assert _run(db.reject_user(2913)) is True
    assert _status(2913) == "rejected"
    assert _run(db.approve_user_atomic(2914)) is False
    assert _status(2914) == "approved"


def test_mass_approve_skips_waitlist(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()
    _seed(2921, "pending")
    _seed(2922, "waitlist")

    flipped = _run(db.approve_all_pending())
    assert 2921 in flipped
    assert 2922 not in flipped
    assert _status(2921) == "approved"
    assert _status(2922) == "waitlist"


def test_gate_waitlist_closed_even_if_toggle_off():
    assert SETTINGS_SCHEMA["apps_waitlist_enabled"]["default"] == "off"
    for gate in (bot_gate, web_gate):
        assert gate("waitlist") == (False, "waitlist")
        assert gate(None) == (True, None)
        assert gate("approved") == (True, None)


def test_effects_waitlist_does_not_send_reject_text(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()
    calls = {"reject": 0, "sheet": []}

    async def reject_message_text(*_args, **_kwargs):
        calls["reject"] += 1
        return "К сожалению, твоя заявка отклонена."

    async def update_status_in_sheet(tid, label):
        calls["sheet"].append((tid, label))

    monkeypatch.setattr(application_effects, "reject_message_text", reject_message_text)
    monkeypatch.setattr(application_effects, "update_status_in_sheet", update_status_in_sheet)

    class _Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))

    bot = _Bot()
    _run(application_effects.apply_decision_effects(bot, 2931, "waitlist"))

    assert calls["reject"] == 0
    assert calls["sheet"] == [(2931, "Лист ожидания")]
    assert len(bot.sent) == 1
    text = bot.sent[0][1].lower()
    for word in _FORBIDDEN:
        assert word not in text
    assert "лист" in text and "ожидан" in text


def test_web_claim_waitlist_from_pending_only(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()
    _seed(2941, "pending")
    _seed(2942, "waitlist")
    _seed(2943, "pending")
    _run(_mark_walkin(2943))

    decision_id = _run(db.claim_decision_with_undo_row(
        2941, "waitlist", None, 1, "2026-11-03 12:00:00", "2026-11-03 12:00:05",
    ))
    assert decision_id
    user = _run(db.get_user(2941))
    assert user["status"] == "waitlist"
    assert not user.get("approved_at")
    assert not user.get("rejected_at")

    missed = _run(db.claim_decision_with_undo_row(
        2943, "waitlist", None, 1, "2026-11-03 12:00:00", "2026-11-03 12:00:05",
    ))
    assert missed is None
    assert _status(2943) == "pending"

    approved_id = _run(db.claim_decision_with_undo_row(
        2942, "approved", None, 1, "2026-11-03 12:01:00", "2026-11-03 12:01:05",
    ))
    assert approved_id
    assert _status(2942) == "approved"

    try:
        _run(db.claim_decision_with_undo_row(
            2941, "nope", None, 1, "2026-11-03 12:02:00", "2026-11-03 12:02:05",
        ))
    except ValueError:
        return
    raise AssertionError("unknown decision must raise ValueError")


def _kb_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _kb_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_button_hidden_when_toggle_off(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()
    from handlers.applications.admin_moderation import _appr_card_kb

    off = _run(_appr_card_kb(2950, False, 1))
    assert not any("waitlist" in (data or "") for data in _kb_data(off))
    assert not any("Лист ожидания" in text for text in _kb_texts(off))

    _run(db.set_setting("apps_waitlist_enabled", "on"))
    on = _run(_appr_card_kb(2950, False, 1))
    assert "appr_waitlist:2950" in _kb_data(on)
    assert "📋 В лист ожидания" in _kb_texts(on)


def test_button_calls_claim_and_effects(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    fast_init_db()
    _seed(2951, "pending")
    _seed(2952, "approved")
    _run(db.set_setting("apps_waitlist_enabled", "on"))

    effects = []

    async def fake_effects(bot, tid, status, reason=None):
        effects.append((tid, status))

    async def fake_show(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "services.applications.application_effects.apply_decision_effects", fake_effects,
    )
    monkeypatch.setattr(
        "handlers.applications.admin_moderation._show_current_card", fake_show,
    )

    from handlers.applications.admin_waitlist import appr_waitlist

    class _User:
        id = 1

    class _Message:
        async def delete(self):
            return None

    class _Callback:
        def __init__(self, data):
            self.data = data
            self.from_user = _User()
            self.message = _Message()
            self.bot = None
            self.answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append((text, show_alert))

    pending = _Callback("appr_waitlist:2951")
    _run(appr_waitlist(pending, object()))
    assert _status(2951) == "waitlist"
    assert effects == [(2951, "waitlist")]
    assert pending.answers[-1][0] == "Заявка в листе ожидания. Делегат увидит: Лист ожидания."

    approved = _Callback("appr_waitlist:2952")
    _run(appr_waitlist(approved, object()))
    assert _status(2952) == "approved"
    assert approved.answers[-1][0] == "Заявка уже решена"
    assert effects == [(2951, "waitlist")]


def test_app_list_chip_only_when_on(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()
    _seed(2961, "waitlist")
    from handlers.applications.admin_app_list import render_app_list_screen

    text, kb = _run(render_app_list_screen(1))
    assert "apl:waitlist:0" not in _kb_data(kb)
    assert not any("Лист ожидания" in item for item in _kb_texts(kb))
    assert "waitlist" not in _run(db.count_applications())

    _run(db.set_setting("apps_waitlist_enabled", "on"))
    text, kb = _run(render_app_list_screen(1))
    assert "apl:waitlist:0" in _kb_data(kb)
    assert any("📋 Лист ожидания" in item for item in _kb_texts(kb))
    assert "📋 1" in text
    counts = _run(db.count_applications())
    assert counts["waitlist"] == 1
    assert counts["pending"] == 0


def test_sheet_dropdown_unchanged_when_off():
    from services.sheets import sheets as sh

    assert sh.status_dropdown_values(False) == ["Новая", "Одобрена", "Отклонена"]
    assert sh.status_dropdown_values(False) == list(sh.STATUS_DROPDOWN)
    on = sh.status_dropdown_values(True)
    assert on[:3] == ["Новая", "Одобрена", "Отклонена"]
    assert on[3] == "Лист ожидания"

    captured = []

    class _Spreadsheet:
        def fetch_sheet_metadata(self):
            return {"sheets": [{"properties": {"sheetId": 7}, "conditionalFormats": []}]}

        def batch_update(self, body):
            captured.append(body)

    class _Sheet:
        id = 7
        spreadsheet = _Spreadsheet()

        def row_values(self, _n):
            return ["Статус"]

    def _values(body):
        for req in body["requests"]:
            rule = req.get("setDataValidation")
            if rule:
                return [item["userEnteredValue"] for item in rule["rule"]["condition"]["values"]]
        return []

    def _painted(body):
        painted = []
        for req in body["requests"]:
            added = req.get("addConditionalFormatRule")
            if not added:
                continue
            painted.append(
                added["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            )
        return painted

    sh._apply_status_formatting_sync(_Sheet(), 1)
    assert _values(captured[-1]) == ["Новая", "Одобрена", "Отклонена"]
    assert _painted(captured[-1]) == ["Новая", "Одобрена", "Отклонена"]

    sh._apply_status_formatting_sync(_Sheet(), 1, dropdown=on)
    assert _values(captured[-1]) == on
    assert _painted(captured[-1])[:3] == ["Новая", "Одобрена", "Отклонена"]
    assert _painted(captured[-1])[3] == "Лист ожидания"


def test_status_totals_three_keys_when_off_and_zero(tmp_path):
    from dashboard import db as dash_db
    from dashboard.queries import Scope, status_totals

    _use_tmp_db(tmp_path)
    fast_init_db()
    with dash_db.read_conn(config.DB_PATH) as conn:
        assert status_totals(conn, Scope()) == {"pending": 0, "approved": 0, "rejected": 0}

    _seed(2962, "waitlist")
    with dash_db.read_conn(config.DB_PATH) as conn:
        totals = status_totals(conn, Scope())
    assert totals["waitlist"] == 1
    assert totals["pending"] == 0
    assert totals["approved"] == 0
    assert totals["rejected"] == 0

    _run(db.set_setting("apps_waitlist_enabled", "on"))
    with dash_db.read_conn(config.DB_PATH) as conn:
        turned_on = status_totals(conn, Scope())
    assert set(turned_on) == {"pending", "approved", "rejected", "waitlist"}
    assert turned_on["waitlist"] == 1


def test_sources_daily_waitlist_label(tmp_path):
    from datetime import date

    from starlette.datastructures import QueryParams

    from dashboard import db as dash_db
    from dashboard.queries import Scope
    from dashboard.sources_daily import (
        STATUS_LABELS as labels,
        SourcesQuery,
        _STATUS_COLORS,
        build,
    )

    assert labels["waitlist"] == "Лист ожидания"
    assert labels["rejected"] == "Отказ"
    assert labels["waitlist"] not in ("Отказ", "Другой статус")
    assert _STATUS_COLORS["waitlist"] != _STATUS_COLORS["rejected"]

    _use_tmp_db(tmp_path)
    fast_init_db()
    _seed(2971, "waitlist")
    _seed(2972, "rejected")
    query = SourcesQuery.from_params(QueryParams("by=status"))
    with dash_db.read_conn(config.DB_PATH) as conn:
        result = build(conn, Scope(), query, today=date(2026, 11, 3))
    column_labels = [column["label"] for column in result["columns"]]
    assert "Лист ожидания" in column_labels
    assert "Отказ" in column_labels
    assert "Другой статус" not in column_labels


async def _mark_walkin(tid: int) -> None:
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET onsite_kind = 'walkin' WHERE telegram_id = ?",
            (tid,),
        )
        await conn.commit()
