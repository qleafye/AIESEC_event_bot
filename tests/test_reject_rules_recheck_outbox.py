"""Веб-правка анкеты ставит одно событие пересчёта правил, бот шлёт одно сообщение о паузе.

Повторная доставка того же события второго сообщения не шлёт: пауза уже записана.
"""
from __future__ import annotations

import asyncio
import json

from config import config
from database import db
from miniapp.outbox import OUTBOX_KINDS
from services.applications import reject_rules_affects as affects
from services.applications import reject_rules_notify as notify
from tests._dbtpl import fast_init_db
from tests.test_miniapp_labels_drift import _loaded_aiogram
from tests.test_miniapp_settings_batch import _batch, _run as _batch_run, _setup

SUPERADMIN_ID = 370200001


def _ready(tmp_path, name="test_reject_rules_recheck.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _recheck_rows():
    rows = _batch_run(db.list_unprocessed_miniapp_outbox(limit=50))
    return [row for row in rows if row["kind"] == "reject_rules_recheck"]


def _no_tab_probe_stays_out():
    from miniapp.routers import settings as settings_router

    assert not hasattr(settings_router, "tab_row_count")


def test_affects_module_does_not_load_aiogram():
    assert _loaded_aiogram("import services.applications.reject_rules_affects") == []


def test_notify_reexports_predicate():
    assert notify.affects_reject_rules is affects.affects_reject_rules


def test_outbox_kind_registered():
    assert "reject_rules_recheck" in OUTBOX_KINDS


def test_recheck_event_end_to_end(tmp_path):
    """Выключенный вопрос -> одно событие -> одно сообщение держателю права. Второй раз — тишина."""
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    _run(db.create_reject_rule(
        name="Курс", city=None, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1", "2"]}]]),
        action="reject", reject_text="текст отказа", enabled=1, created_by=None,
    ))
    _run(db.set_setting("reg_q_course", "off"))

    from miniapp.outbox import enqueue
    from services import scheduler
    from services.infra.miniapp_outbox import drain

    assert _run(enqueue("reject_rules_recheck", {"keys": ["reg_q_course"]})) is not None
    bot = _FakeBot()
    # Уведомление читает бота процесса. Джоба очереди передаёт в drain тот же объект.
    previous = scheduler._bot
    scheduler._bot = bot
    try:
        _run(drain(bot))
        assert len(bot.sent) == 1
        assert bot.sent[0][0] == SUPERADMIN_ID
        assert "паузу" in bot.sent[0][1]
        assert _run(enqueue("reject_rules_recheck", {"keys": ["reg_q_course"]})) is not None
        _run(drain(bot))
        assert len(bot.sent) == 1
    finally:
        scheduler._bot = previous


def test_batch_question_off_enqueues_recheck(tmp_path):
    _no_tab_probe_stays_out()
    client = _setup(tmp_path, "rr_recheck_course.db")
    resp = _batch(client, [("reg_q_course", "off")])
    assert resp.status_code == 200, resp.text
    rows = _recheck_rows()
    assert len(rows) == 1
    assert rows[0]["payload"]["keys"] == ["reg_q_course"]


def test_batch_unrelated_key_no_event(tmp_path):
    client = _setup(tmp_path, "rr_recheck_name.db")
    resp = _batch(client, [("event_name", "форума RusCo")])
    assert resp.status_code == 200, resp.text
    assert _recheck_rows() == []


def test_batch_two_question_keys_one_event(tmp_path):
    client = _setup(tmp_path, "rr_recheck_two.db")
    resp = _batch(client, [("reg_q_course", "off"), ("reg_q_age", "off")])
    assert resp.status_code == 200, resp.text
    rows = _recheck_rows()
    assert len(rows) == 1
    assert rows[0]["payload"]["keys"] == ["reg_q_course", "reg_q_age"]


def test_batch_errors_no_event(tmp_path):
    client = _setup(tmp_path, "rr_recheck_bad.db")
    resp = _batch(client, [("reg_q_course", "off"), ("reg_resume_ttl_hours", "сорок восемь")])
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] == []
    assert _recheck_rows() == []
