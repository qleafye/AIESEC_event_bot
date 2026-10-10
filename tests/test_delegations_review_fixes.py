"""Делегации вузов — регрессии по ревью фазы: оплата, маркер делегата, сезон/город, привязка,
дубли, гейты геймы, карточки менеджера, зеркало листа. Фикстуры — из `test_delegations_core`."""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db
from database import delegations_db as ddb
from services.delegations import delegations as dlg
from services.settings.audit import set_setting_by_admin
from tests.test_delegations_core import (  # noqa: F401
    NEEDLE, SEASON, UNIVERSITY, _answer_from_fixture, _available, _delegation_form, _drow,
    _env, _reg_started, _row, _run, _user,
)


USERNAME_X = NEEDLE
MANAGER_ID = 7


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
    import domain.cities as cities_mod
    from services.forum.checkin import checkin_denial
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


# ---------- WR-01: человека ищем по нику, а не по сохранённому совпадению ----------

def test_stale_matched_telegram_id_is_ignored(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат", tid=661)  # совпадение по телефону
    _reg_started(661, "not_the_form_nick")
    res = _available(fid, "a1")
    assert res.get("waiting") == "no_person"
    assert _row(661) is None and bot.sent == []


# ---------- WR-02: ник у нескольких людей — не одобряем сами ----------

def test_ambiguous_nick_goes_to_check_and_stays_there(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(671, "approved", username=USERNAME_X)      # прежний владелец ника, ещё в базе
    _reg_started(672, USERNAME_X)                    # нынешний владелец только нажал /start
    res = _available(fid, "a1")
    assert res["waiting"] == "ambiguous_nick" and res["ta"] == "check"
    row = _drow(fid, "a1")
    assert row["ta_status"] == "check" and row["note"] == dlg.NOTE_AMBIGUOUS_NICK
    assert row["linked_telegram_id"] is None and bot.sent == []
    assert _row(671)["delegation_answer_id"] is None
    _available(fid, "a1", reason="sweep")  # переоценка не возвращает в «ЦА»
    assert _drow(fid, "a1")["ta_status"] == "check"


def test_ambiguous_nick_manager_ok_does_not_autoconvert(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(671, "approved", username=USERNAME_X)
    _reg_started(672, USERNAME_X)
    _available(fid, "a1")
    _run(ddb.set_decision(_drow(fid, "a1")["id"], "ok", MANAGER_ID))
    res = _available(fid, "a1", reason="manual")
    assert res["waiting"] == "ambiguous_nick"
    row = _drow(fid, "a1")
    assert row["ta_status"] == "ok" and row["linked_telegram_id"] is None and bot.sent == []
    # привязать нужного менеджер может вручную
    assert _convert(fid, "a1", 672, how="manual", by=MANAGER_ID).get("converted")


def test_unique_nick_still_converts(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(673, USERNAME_X)
    assert _available(fid, "a1")["converted"]["converted"] is True


# ---------- CR-04: отказ в боте виден менеджеру и не снимается одним нажатием ----------

def test_card_and_ok_confirm_for_rejected_person_without_note(tmp_path):
    from handlers.delegations import admin_delegations_review as mod
    from tests.test_delegations_admin import _FakeCallback, _callbacks, _last_edit
    from tests.test_delegations_admin_review import _check_row
    _env(tmp_path)
    fid = _delegation_form()
    _user(681, "rejected")  # отказ не по курсу: пометки в ответе нет
    row_id = _check_row(fid, "a1")  # «проверить» по курсу, note пуст
    cb = _FakeCallback(f"dlg_card:{row_id}")
    _run(mod.dlg_card(cb))
    assert "В боте у этого человека отказ" in _last_edit(cb)[0]
    cb = _FakeCallback(f"dlg_ta:{row_id}:ok")
    _run(mod.dlg_ta(cb))
    text, _, kb = _last_edit(cb)
    assert "отказ будет снят" in text and f"dlg_ta:{row_id}:okc" in _callbacks(kb)
    assert _row(681)["status"] == "rejected" and _drow(fid, "a1")["decided_by"] is None
    cb = _FakeCallback(f"dlg_ta:{row_id}:okc")
    _run(mod.dlg_ta(cb))
    assert _row(681)["status"] == "approved"


# ---------- WR-10: устаревшая карточка не меняет статус уже привязанного ответа ----------

def test_stale_card_cannot_mark_linked_answer_not_ta(tmp_path):
    from handlers.delegations import admin_delegations_review as mod
    from tests.test_delegations_admin import _FakeCallback
    from tests.test_delegations_admin_review import _check_row
    _env(tmp_path)
    fid = _delegation_form()
    row_id = _check_row(fid, "a1")
    _user(691, "approved")
    _run(ddb.link(row_id, 691, "username"))
    cb = _FakeCallback(f"dlg_ta:{row_id}:no")
    _run(mod.dlg_ta(cb))
    assert cb.answer_calls == [(mod._ALREADY_LINKED_ALERT, True)]
    row = _run(ddb.get_by_id(row_id))
    assert row["ta_status"] == "check" and row["decided_by"] is None


# ---------- WR-08: настройки не одобряют людей молча ----------

def _setup_would_become_ta(tmp_path):
    """Ответ «1 бакалавриат» после отсечки = не ЦА; человек в боте есть. Сдвиг отсечки
    на дату после ответа сделает его ЦА."""
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _run(set_setting_by_admin(None, "delegation_ta_cutoff", "23.09.2026"))
    _run(set_setting_by_admin(None, "delegation_not_ta_courses", "1\n2"))
    _answer_from_fixture(fid, "a1", course="1 бакалавриат", answered_at="2026-10-01 12:00:00")
    _reg_started(701)
    _available(fid, "a1")
    assert _drow(fid, "a1")["ta_status"] == "no" and _row(701) is None
    return bot, fid


def test_cutoff_change_asks_confirmation_with_count(tmp_path, monkeypatch):
    from handlers.delegations import admin_delegations as mod
    from tests.test_delegations_admin import (
        _FakeCallback, _FakeMessage, _callbacks, _spy_sweep, _state,
    )
    _setup_would_become_ta(tmp_path)
    calls = _spy_sweep(monkeypatch)
    _run(set_setting_by_admin(None, "delegation_ta_cutoff", "23.09.2026"))

    async def go():
        state = _state()
        await mod.dlg_cutoff(_FakeCallback("dlg_cutoff"), state)
        msg = _FakeMessage("20.10.2026")
        await mod.dlg_cutoff_input(msg, state)
        return msg
    msg = _run(go())
    text, _, kb = msg.answers[-1]
    assert "станут ЦА: 1" in text and "будут одобрены и получат сообщение" in text
    assert "dlg_apply" in _callbacks(kb)
    assert calls == []  # пока не подтвердили — пересчёта нет
    assert _row(701) is None


def test_apply_runs_sweep_and_preview_counts_zero_when_nobody(tmp_path, monkeypatch):
    from handlers.delegations import admin_delegations as mod
    from tests.test_delegations_admin import _FakeCallback, _spy_sweep
    _setup_would_become_ta(tmp_path)
    assert _run(dlg.preview_reevaluate()) == 0  # настройки прежние — никто не изменится
    _run(set_setting_by_admin(None, "delegation_ta_cutoff", "20.10.2026"))
    assert _run(dlg.preview_reevaluate()) == 1
    calls = _spy_sweep(monkeypatch)

    async def go():
        cb = _FakeCallback("dlg_apply")
        await mod.dlg_apply(cb)
        await asyncio.sleep(0)
    _run(go())
    assert calls == [True]


# ---------- IN-02: счётчики и списки считают одно и то же, вуз группируется без учёта регистра ----------

def test_summary_groups_university_case_insensitively_and_counts_match_lists(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", username="@u1", course="3 бакалавриат", university="МГУ")
    _answer_from_fixture(fid, "a2", username="@u2", course="3 бакалавриат", university=" мгу ")
    _available(fid, "a1")
    _available(fid, "a2")
    rows = _run(ddb.summary_by_university(fid))
    assert len(rows) == 1 and rows[0]["total"] == 2 and rows[0]["ta"] == 2
    # ответ без строки в таблице ответов формы не попадает ни в счётчик, ни в список
    _run(ddb.upsert_eval(fid, "ghost", ta_status="ok", university="Призрак", course_raw="3",
                         course_canonical="3", username_needle=None, answered_at=None))
    assert _run(ddb.count_by_status(fid, "ok", linked=None)) == len(
        _run(ddb.list_by_status(fid, "ok", linked=None, offset=0, limit=50)))
