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


# ---------- CR-02: делегат без вуза в ответе всё равно помечен ----------

def test_delegate_without_university_is_still_marked(tmp_path):
    from miniapp.deps import game_denial  # noqa: F401  (гейт читает ту же колонку)
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат", university="")
    _reg_started(611)
    _available(fid, "a1")
    u = _row(611)
    assert u["status"] == "approved" and u["delegation_answer_id"] == "a1"
    assert u["delegation"] == dlg.UNIVERSITY_UNKNOWN
    where, params = db._build_filter_clause([{"field": "delegation_any", "value": db.DELEGATION_YES}])
    conn = sqlite3.connect(config.DB_PATH)
    assert [r[0] for r in conn.execute(f"SELECT telegram_id FROM users{where}", params)] == [611]
    conn.close()


# ---------- CR-06: ответ заявляется до одобрения ----------

def _convert(fid, aid, tid, how="username", by=None):
    form = _run(dlg.ef.get_form(fid))
    row, fields, _ = _run(dlg.evaluate(_run(dlg.ef.get_answer(fid, aid)), form,
                                       _run(dlg.field_keys())))
    return _run(dlg.convert_to_delegate(tid, row, fields, how=how, by=by))


def test_link_conflict_means_no_approval_no_welcome(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(621)
    _reg_started(622, "other_one")
    assert _convert(fid, "a1", 621).get("converted")
    sent = len(bot.sent)
    res = _convert(fid, "a1", 622)
    assert res == {"conflict": True}
    assert _row(622) is None  # анкеты не было и одобрения не будет
    assert len(bot.sent) == sent
    assert _drow(fid, "a1")["linked_telegram_id"] == 621


def test_failed_approval_releases_claim(tmp_path, monkeypatch):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(623)

    async def boom(tid):
        raise RuntimeError("db down")
    monkeypatch.setattr(dlg, "approve_user_atomic", boom)
    try:
        _convert(fid, "a1", 623)
    except RuntimeError:
        pass
    else:
        raise AssertionError("ожидали исключение")
    assert _drow(fid, "a1")["linked_telegram_id"] is None


# ---------- WR-07: второй ответ того же человека ----------

def test_second_answer_of_same_person_is_quiet(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат", university="Первый вуз")
    _answer_from_fixture(fid, "a2", course="3 бакалавриат", university="Второй вуз")
    _reg_started(631)
    _available(fid, "a1")
    sent = len(bot.sent)
    res = _available(fid, "a2")
    assert res["converted"] == {"duplicate": True}
    assert len(bot.sent) == sent
    u = _row(631)
    assert u["delegation"] == "Первый вуз" and u["delegation_answer_id"] == "a1"
    assert _drow(fid, "a2")["linked_telegram_id"] == 631
    assert _drow(fid, "a2")["link_how"] == dlg.LINK_HOW_DUPLICATE


def test_summary_counts_one_person_once_per_university(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _answer_from_fixture(fid, "a2", course="3 бакалавриат")
    _reg_started(632)
    _available(fid, "a1")
    _available(fid, "a2")
    rows = _run(ddb.summary_by_university(fid))
    assert len(rows) == 1 and rows[0]["total"] == 2 and rows[0]["in_bot"] == 1


# ---------- CR-05: существующая анкета переезжает в текущий сезон и город делегации ----------

def test_existing_user_moves_to_current_season_and_city(tmp_path):
    import cities as cities_mod
    from services.checkin import checkin_denial
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(641, "approved", season="YL 26/1", city="spb")
    _available(fid, "a1")
    u = _row(641)
    assert u["season"] == SEASON and u["prev_season"] == "YL 26/1"
    assert u["event_city"] == cities_mod.default_city_code()
    assert _run(checkin_denial(u)) is None


def test_existing_user_same_season_keeps_prev_season_empty(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(642, "pending", season=SEASON, city="spb")
    _available(fid, "a1")
    u = _row(642)
    assert u["season"] == SEASON and not u.get("prev_season")


# ---------- WR-03: ник в users — настоящий, не из текста формы ----------

def test_manual_link_does_not_write_form_nick_into_username(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", username="@someone_else", course="3 бакалавриат")
    _run(db.mark_reg_started(651, None))  # у человека в Telegram нет ника
    assert _convert(fid, "a1", 651, how="manual", by=7).get("converted")
    assert _row(651)["username"] == "-"


def test_manual_link_keeps_real_nick_from_start(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", username="@typo_nick", course="3 бакалавриат")
    _run(db.mark_reg_started(652, "RealNick"))
    assert _convert(fid, "a1", 652, how="manual", by=7).get("converted")
    assert _row(652)["username"] == "@RealNick"
