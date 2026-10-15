"""Сводка блока «Делегации»: одобрено в боте + ЦА, которые есть только в форме."""
import asyncio

from dashboard import db as dash_db
from dashboard.queries import Scope, delegations_block

from tests.test_dashboard_queries import _seed, _use_tmp_db
from tests.test_delegations_dashboard import _seed_delegations


def _summary(path, scope=None):
    with dash_db.read_conn(path) as conn:
        return delegations_block(conn, scope or Scope())["summary"]


def test_unique_by_nick_normalized_and_best_status(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[{"telegram_id": 1, "status": "approved"}])
    asyncio.run(_seed_delegations([
        {"username_needle": "Ivan"},
        {"username_needle": "@ivan "},
        {"username_needle": "petr", "ta_status": "check"},
        {"username_needle": "petr", "ta_status": "no"},
        {"username_needle": "ivan", "ta_status": "no"},
        {"username_needle": "olga", "ta_status": "no"},
    ]))
    s = _summary(path)
    assert (s["only_ok"], s["only_check"], s["only_no"]) == (1, 1, 1)
    assert s["approved"] == 1
    assert s["total"] == 2 and s["total_with_check"] == 3


def test_duplicate_and_linked_not_counted(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[{"telegram_id": 1, "status": "approved", "delegation": "МГУ"}])
    asyncio.run(_seed_delegations([
        {"username_needle": "a", "link_how": "duplicate"},
        {"username_needle": "b", "linked_telegram_id": 1, "link_how": "username"},
        {"username_needle": "c"},
    ]))
    s = _summary(path)
    assert s["only_ok"] == 1
    assert s["approved"] == 1 and s["from_delegations"] == 1  # без двойного счёта
    assert s["total"] == 2


def test_nick_already_in_users_not_counted(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[{"telegram_id": 1, "status": "pending", "username": "Known"}])
    asyncio.run(_seed_delegations([
        {"username_needle": "@known"}, {"username_needle": "fresh"},
    ]))
    s = _summary(path)
    assert s["only_ok"] == 1 and s["approved"] == 0 and s["total"] == 1


def test_from_delegations_via_link_only(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "status": "approved"},
        {"telegram_id": 2, "status": "approved"},
        {"telegram_id": 3, "status": "pending", "delegation": "ВШЭ"},
    ])
    asyncio.run(_seed_delegations([{"username_needle": "x", "linked_telegram_id": 1}]))
    s = _summary(path)
    assert s["approved"] == 2 and s["from_delegations"] == 1


def test_city_scope_narrows_bot_not_form(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "status": "approved", "event_city": "msk"},
            {"telegram_id": 2, "status": "approved", "event_city": "spb"},
        ],
    )
    asyncio.run(_seed_delegations([{"username_needle": "z"}]))
    s = _summary(path, Scope(city="msk"))
    assert s["approved"] == 1 and s["only_ok"] == 1 and s["total"] == 2


def test_season_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"event_season": "YL26"},
        users=[
            {"telegram_id": 1, "season": "YL26", "status": "approved"},
            {"telegram_id": 2, "season": "RusCo25", "status": "approved", "username": "old"},
        ],
    )
    asyncio.run(_seed_delegations([{"username_needle": "old"}]))
    cur = _summary(path)
    assert cur["approved"] == 1 and cur["only_ok"] == 1  # ник из прошлого сезона не гасит ответ
    past = _summary(path, Scope(season="RusCo25"))
    assert past["approved"] == 1 and past["only_ok"] == 0
