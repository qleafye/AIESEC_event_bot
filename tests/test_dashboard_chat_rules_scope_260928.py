"""Режим «По правилам города»: чек-ины и задания соцсетей — только текущего сезона и города чата.

Раньше `_social_dates` и `_checkin_days` брали всю историю: делегат, ходивший на прошлый форум
или сдававший задания в прошлом сезоне, получал коины в текущей таблице «Всё время».
Приглашённые друзья по-прежнему считаются из заявок любого города (так написано на странице).
"""
import pytest

from config import config
from tests._dbtpl import fast_init_db
from tests.test_dashboard_chat_rules_260927 import (
    STAFF,
    _checkin,
    _exec,
    _member,
    _rows,
    _rules,
    _setting,
    _submission,
    _user,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "rules_scope.db")
    config.DB_PATH = path
    fast_init_db()
    _exec(path, "INSERT INTO staff (telegram_id, role, added_by, added_at) "
                "VALUES (?, 'reg_manager', 1, '2026-01-01 00:00:00')", (STAFF,))
    _setting(path, "chat_rating_mode__city__spb", "rules")
    return path


def _season(db_path):
    _setting(db_path, "event_season", "YL 26/2")


def test_checkins_of_past_season_do_not_count(db_path):
    _season(db_path)
    _member(db_path, 11)
    _member(db_path, 12)
    _user(db_path, 11, event_city="spb")
    _user(db_path, 12, event_city="spb", season="YL 26/1")  # прошлый сезон
    for tid in (11, 12):
        _checkin(db_path, tid, "entry", "2026-10-30")
    rows = _rows(_rules(db_path))
    assert rows[11]["checkins"] == 1
    assert 12 not in rows


def test_social_of_other_city_does_not_count(db_path):
    _season(db_path)
    _setting(db_path, "chat_rules_social_tasks__city__spb", "7")
    _member(db_path, 11)
    _member(db_path, 12)
    _user(db_path, 11, event_city="spb")
    _user(db_path, 12, event_city="msk")  # делегат Москвы, заглянувший в чат СПб
    for tid in (11, 12):
        _submission(db_path, 7, tid)
    rows = _rows(_rules(db_path))
    assert rows[11]["social"] == 1
    assert 12 not in rows


def test_without_application_no_checkin_or_social(db_path):
    _season(db_path)
    _setting(db_path, "chat_rules_social_tasks__city__spb", "7")
    _member(db_path, 11)
    _checkin(db_path, 11, "entry", "2026-10-30")
    _submission(db_path, 7, 11)
    assert _rules(db_path)["rows"] == []


def test_page_season_filter_applies(db_path):
    _season(db_path)
    _member(db_path, 12)
    _user(db_path, 12, event_city="spb", season="YL 26/1")
    _checkin(db_path, 12, "entry", "2026-03-30")
    from dashboard import chat_rating
    from dashboard import db as dash_db
    from tests.test_dashboard_chat_rules_260927 import NOW, OWNER, SPB
    with dash_db.read_conn(db_path) as conn:
        result = chat_rating.rules_rating(conn, SPB, period="all", admin_ids={OWNER}, now=NOW,
                                          season="YL 26/1")
    assert _rows(result)[12]["checkins"] == 1
