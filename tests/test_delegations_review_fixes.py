"""Делегации вузов — регрессии по ревью фазы: оплата, маркер делегата, сезон/город, привязка,
дубли, гейты геймы, карточки менеджера, зеркало листа. Фикстуры — из `test_delegations_core`."""
from __future__ import annotations

import sqlite3

from config import config
from database import db
from database import delegations_db as ddb
from services import delegations as dlg
from settings_audit import set_setting_by_admin
from tests.test_delegations_core import (  # noqa: F401
    NEEDLE, SEASON, UNIVERSITY, _answer_from_fixture, _available, _delegation_form, _drow,
    _env, _reg_started, _row, _run, _user,
)


# ---------- CR-01: оплата делегату вуза не предлагается ----------

def test_delegate_never_offered_payment(tmp_path):
    from handlers.payment import should_offer_receipt_upload
    _env(tmp_path)
    _run(set_setting_by_admin(None, "payment_enabled", "on"))
    _run(set_setting_by_admin(None, "payment_requisites", "Карта 0000"))
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(601)
    _user(602, "approved", username="plain")
    _available(fid, "a1")
    assert _row(601)["delegation_answer_id"] == "a1"
    assert _run(should_offer_receipt_upload(601)) is False
    assert _run(should_offer_receipt_upload(602)) is True  # обычный не оплативший — как раньше


def test_payment_filter_excludes_delegates(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(601)
    _user(602, "approved", username="plain")
    _available(fid, "a1")
    where, params = db._build_filter_clause([{"field": "payment_status", "value": "not_paid"}])
    conn = sqlite3.connect(config.DB_PATH)
    ids = {r[0] for r in conn.execute(f"SELECT telegram_id FROM users{where}", params)}
    conn.close()
    assert ids == {602}
