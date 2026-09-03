"""Phase 23 План 02 (APP-TINDER-01) — юнит-покрытие `services/applications.py`: ядро отбора
заявок без aiogram (очередь, карточка, атомарные решения, журнал отмены).

pytest-asyncio не используется — асинхронщина через `asyncio.run()`, БД — временная
(`config.DB_PATH = tmp_path / "..."` + `database.db.init_db()`), как в `tests/test_applications_db.py`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import services.applications as applications
from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "applications_service.db")


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, **fields):
    row = {
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
    }
    row.update(fields)
    _run(db.add_user(row))
    status = fields.get("status", "pending")
    _run(db.set_user_status(tid, status))


# ── TRACK_FILTERS ────────────────────────────────────────────────────────────────────────

def test_track_filters_shape():
    assert applications.TRACK_FILTERS["full"] == ("full",)
    assert set(applications.TRACK_FILTERS["party"]) == {"party_overnight", "party_noovernight"}
    assert applications.TRACK_FILTERS["short"] == ("short",)


# ── queue_page: счётчик и выборка по ОДНОМУ набору фильтров ─────────────────────────────────

def test_queue_page_returns_row_and_matching_total(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1001, participant_type="full")
    _seed_user(1002, participant_type="short")

    row, total = _run(applications.queue_page(scope=None, offset=0))
    assert total == 2
    assert row["telegram_id"] == 1001

    row2, total2 = _run(applications.queue_page(scope=None, offset=1))
    assert total2 == 2
    assert row2["telegram_id"] == 1002


def test_queue_page_track_filter_narrows_both_count_and_row(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1101, participant_type="full")
    _seed_user(1102, participant_type="short")

    row, total = _run(applications.queue_page(scope=None, offset=0, track="short"))
    assert total == 1
    assert row["telegram_id"] == 1102


def test_queue_page_offset_past_total_is_empty(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1201, participant_type="full")

    row, total = _run(applications.queue_page(scope=None, offset=5))
    assert row is None
    assert total == 1


def test_queue_page_empty_queue(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())

    row, total = _run(applications.queue_page(scope=None, offset=0))
    assert row is None
    assert total == 0


# ── manager_scope / out_of_scope ─────────────────────────────────────────────────────────

def test_manager_scope_none_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    assert _run(applications.manager_scope("msk")) is None


def test_manager_scope_none_when_city_none(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("event_city_enabled", "on"))
    assert _run(applications.manager_scope(None)) is None


def test_out_of_scope_false_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1301, event_city="spb")
    assert _run(applications.out_of_scope("msk", 1301)) is False


def test_out_of_scope_true_for_mismatched_city(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("event_city_enabled", "on"))
    _seed_user(1401, event_city="spb")
    assert _run(applications.out_of_scope("msk", 1401)) is True


def test_out_of_scope_false_for_matching_city(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("event_city_enabled", "on"))
    _seed_user(1402, event_city="msk")
    assert _run(applications.out_of_scope("msk", 1402)) is False


# ── card_payload ──────────────────────────────────────────────────────────────────────────

def test_card_payload_main_fields_respect_answer_limit(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    long_goal = "о" * 500
    _seed_user(1501, goal=long_goal)
    _run(db.set_setting("modcard_fields", "goal"))
    _run(db.set_setting("modcard_answer_limit", "50"))

    payload = _run(applications.card_payload(_run(db.get_user(1501))))
    main = dict(payload["main_fields"])
    assert len(main) == 1
    value = next(iter(main.values()))
    assert len(value) <= 51  # 50 символов + «…»


def test_card_payload_extra_fields_not_truncated(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    long_goal = "о" * 500
    _seed_user(1502, goal=long_goal)
    _run(db.set_setting("modcard_fields", "age"))  # goal НЕ включён -> extra
    _run(db.set_setting("modcard_answer_limit", "10"))

    payload = _run(applications.card_payload(_run(db.get_user(1502))))
    extra = dict(payload["extra_fields"])
    values = list(extra.values())
    assert any(len(v) == 500 for v in values)


def test_card_payload_resume_kinds(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1601, resume_file_id="file-abc")
    _seed_user(1602, resume_text="текстовое резюме")
    _seed_user(1603)

    p1 = _run(applications.card_payload(_run(db.get_user(1601))))
    p2 = _run(applications.card_payload(_run(db.get_user(1602))))
    p3 = _run(applications.card_payload(_run(db.get_user(1603))))

    assert p1["resume"] == {"kind": "file", "file_id": "file-abc", "text": None}
    assert p2["resume"] == {"kind": "text", "file_id": None, "text": "текстовое резюме"}
    assert p3["resume"] == {"kind": "none", "file_id": None, "text": None}


def test_card_payload_show_resume_reflects_enabled_steps(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1701)
    _run(db.set_setting("modcard_fields", "age\nresume"))
    assert _run(applications.card_payload(_run(db.get_user(1701))))["show_resume"] is True

    _run(db.set_setting("modcard_fields", "age"))
    assert _run(applications.card_payload(_run(db.get_user(1701))))["show_resume"] is False


def test_card_payload_history_reads_answer_history(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1801)
    _run(db.mark_user_edited(1801, "bot"))

    payload = _run(applications.card_payload(_run(db.get_user(1801))))
    assert isinstance(payload["history"], list)


# ── claim_approve / claim_reject / claim_approve_all: выигрывает ровно один ────────────────

def test_claim_approve_wins_once(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1901, participant_type="full")

    first = _run(applications.claim_approve(1901))
    second = _run(applications.claim_approve(1901))
    assert first is True
    assert second is False
    assert _run(db.get_user(1901))["status"] == "approved"


def test_claim_reject_wins_once(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1902, participant_type="full")

    first = _run(applications.claim_reject(1902))
    second = _run(applications.claim_reject(1902))
    assert first is True
    assert second is False


def test_claim_approve_all_returns_flipped_ids(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(1903, participant_type="full")
    _seed_user(1904, participant_type="short")

    ids = _run(applications.claim_approve_all(None))
    assert set(ids) == {1903, 1904}


# ── Журнал отмены: record -> undo внутри окна -> flush после окна ──────────────────────────

def test_record_decision_then_undo_inside_window_reverts_and_nothing_flushes(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2001, participant_type="full", status="pending")
    won = _run(applications.claim_approve(2001))
    assert won is True

    now = datetime(2026, 1, 1, 12, 0, 0)
    decision_id = _run(applications.record_decision(2001, "approved", None, 999, now))
    assert decision_id > 0

    enqueued = []
    flushed_before = _run(applications.flush_due_decisions(now, lambda k, p: enqueued.append((k, p))))
    assert flushed_before == 0
    assert enqueued == []

    result = _run(applications.undo_decision(decision_id))
    assert result == {"ok": True, "telegram_id": 2001}
    assert _run(db.get_user(2001))["status"] == "pending"


def test_undo_decision_after_flush_returns_already(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2002, participant_type="full", status="pending")
    _run(applications.claim_approve(2002))

    now = datetime(2026, 1, 1, 12, 0, 0)
    decision_id = _run(applications.record_decision(2002, "approved", None, 999, now))

    later = now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1)
    enqueued = []
    flushed = _run(applications.flush_due_decisions(later, lambda k, p: enqueued.append((k, p))))
    assert flushed == 1
    assert len(enqueued) == 1
    kind, payload = enqueued[0]
    assert kind == "approved"
    assert payload["telegram_id"] == 2002

    result = _run(applications.undo_decision(decision_id))
    assert result == {"ok": False, "reason": "already"}
    assert _run(db.get_user(2002))["status"] == "approved"  # эффекты состоялись, отката нет


def test_undo_decision_unknown_id_returns_already(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    result = _run(applications.undo_decision(999999))
    assert result == {"ok": False, "reason": "already"}


def test_flush_due_decisions_skips_not_yet_due(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(2003, participant_type="full", status="pending")
    _run(applications.claim_approve(2003))

    now = datetime(2026, 1, 1, 12, 0, 0)
    _run(applications.record_decision(2003, "approved", None, 999, now))

    enqueued = []
    flushed = _run(applications.flush_due_decisions(now, lambda k, p: enqueued.append((k, p))))
    assert flushed == 0
    assert enqueued == []


# ── reject_message_text / reject_reason_templates ───────────────────────────────────────────

def test_reject_message_text_default_prefix_and_reason(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    text = _run(applications.reject_message_text("плохое качество"))
    assert text == "К сожалению, твоя заявка отклонена.\n\nплохое качество"


def test_reject_message_text_escapes_html(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _run(db.set_setting("reject_text", "<b>Отказ</b>"))
    text = _run(applications.reject_message_text("<script>"))
    assert "<b>" not in text
    assert "&lt;b&gt;Отказ&lt;/b&gt;" in text
    assert "&lt;script&gt;" in text


def test_reject_message_text_no_reason_no_trailing_blank(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    text = _run(applications.reject_message_text(None))
    assert text == "К сожалению, твоя заявка отклонена."


def test_reject_reason_templates_default_has_four_items(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    templates = _run(applications.reject_reason_templates())
    assert len(templates) == 4
    assert all(isinstance(t, str) and t for t in templates)


# ── UNDO_WINDOW_SECONDS ──────────────────────────────────────────────────────────────────

def test_undo_window_seconds_is_five():
    assert applications.UNDO_WINDOW_SECONDS == 5
