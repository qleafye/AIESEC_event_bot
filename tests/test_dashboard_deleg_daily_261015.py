"""Регистрации формы делегаций по дням рядом с реальными из бота."""
import asyncio
from datetime import timedelta

from dashboard import db as dash_db
from dashboard.queries import Scope, delegation_daily
from dashboard.timeutil import msk_now

from tests.test_dashboard_queries import _seed, _use_tmp_db
from tests.test_delegations_dashboard import _seed_delegations


def _day(back=0):
    return (msk_now() - timedelta(days=back)).strftime("%Y-%m-%d")


def _daily(path, scope=None):
    with dash_db.read_conn(path) as conn:
        return delegation_daily(conn, scope or Scope())


def _by_day(res):
    return {r["day"]: r for r in res["rows"]}


def test_none_without_answers(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[{"telegram_id": 1, "registration_date": _day() + " 10:00:00"}])
    assert _daily(path) is None


def test_per_day_today_week_total(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "registration_date": _day() + " 10:00:00"},
        {"telegram_id": 2, "registration_date": _day(1) + " 10:00:00"},
        {"telegram_id": 3, "registration_date": _day(2) + " 10:00:00", "auto_reject_rule_ids": "[1]"},
    ])
    asyncio.run(_seed_delegations([
        {"username_needle": "a", "answered_at": _day() + " 09:00:00"},
        {"username_needle": "b", "answered_at": _day() + " 09:30:00"},
        {"username_needle": "c", "answered_at": _day(3) + " 09:00:00"},
        {"username_needle": "old", "answered_at": _day(30) + " 09:00:00"},
    ]))
    res = _daily(path)
    days = _by_day(res)
    assert days[_day()] == {"day": _day(), "real": 1, "form": 2, "total": 3}
    assert days[_day(1)]["real"] == 1 and days[_day(1)]["form"] == 0
    assert days[_day(2)]["real"] == 0  # автоотказ не считается реальным
    assert days[_day(3)]["form"] == 1 and days[_day(3)]["total"] == 1
    assert (res["form_today"], res["form_week"], res["form_total"]) == (2, 3, 4)
    assert res["total_today"] == 3
    assert res["total_all"] == 2 + 4  # реальные 2 + форма 4


def test_duplicate_not_counted(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[])
    asyncio.run(_seed_delegations([
        {"username_needle": "a", "answered_at": _day() + " 09:00:00"},
        {"username_needle": "@A", "answered_at": _day() + " 10:00:00"},
        {"username_needle": "b", "answered_at": _day() + " 11:00:00", "link_how": "duplicate"},
        {"username_needle": "c", "answered_at": _day() + " 12:00:00", "ta_status": "no"},
    ]))
    res = _daily(path)
    assert res["form_today"] == 1 and res["form_total"] == 1


def test_linked_and_known_nick_not_doubled_in_total(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "registration_date": _day() + " 10:00:00"},
        {"telegram_id": 2, "registration_date": _day() + " 11:00:00", "username": "Known"},
    ])
    asyncio.run(_seed_delegations([
        {"username_needle": "x", "linked_telegram_id": 1, "link_how": "username",
         "answered_at": _day() + " 09:00:00"},
        {"username_needle": "@known", "answered_at": _day() + " 09:10:00"},
        {"username_needle": "fresh", "answered_at": _day() + " 09:20:00"},
    ]))
    res = _daily(path)
    assert res["form_today"] == 3  # все три — регистрации формы
    assert res["total_today"] == 2 + 1  # реальные 2 + только «fresh»
    assert res["total_all"] == 3
