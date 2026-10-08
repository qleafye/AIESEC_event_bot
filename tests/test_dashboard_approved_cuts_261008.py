"""Запрос DXP 08.10: число отобранных + разбивка среди отобранных по направлению, курсу и
возрасту. Переключатель «Все заявки / Только одобренные» в разделе «Кто подаёт» и разрез
«Возраст» корзинами."""
from datetime import date

from dashboard import db as dash_db
from dashboard.queries import Scope, _parse_age, age_breakdown, approved_count, breakdown

from tests.test_dashboard_queries import _seed, _use_tmp_db
from tests import test_dashboard_render as render


def _users():
    return [
        {"telegram_id": 1, "status": "approved", "study_field": "IT", "course": "2", "age": "19"},
        {"telegram_id": 2, "status": "approved", "study_field": "IT", "course": "3", "age": "22 года"},
        {"telegram_id": 3, "status": "pending", "study_field": "Экономика", "course": "2", "age": "17"},
        {"telegram_id": 4, "status": "rejected", "study_field": "Экономика", "course": "1", "age": "30"},
        {"telegram_id": 5, "status": "approved", "study_field": "Право", "course": "4",
         "age": None, "birth_date": "01.01.2000"},
        {"telegram_id": 6, "status": "approved", "study_field": None, "course": None, "age": "не скажу"},
    ]


def test_breakdown_approved_only_filters_by_status(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=_users())
    with dash_db.read_conn(path) as conn:
        everyone = dict(breakdown(conn, "study_field", scope=Scope()))
        approved = dict(breakdown(conn, "study_field", scope=Scope(), approved_only=True))
        assert approved_count(conn, Scope()) == 4
    assert everyone == {"IT": 2, "Экономика": 2, "Право": 1}
    assert approved == {"IT": 2, "Право": 1}


def test_age_breakdown_buckets_in_age_order_and_skips_unparsable(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=_users())
    with dash_db.read_conn(path) as conn:
        everyone = age_breakdown(conn, scope=Scope())
        approved = age_breakdown(conn, scope=Scope(), approved_only=True)
    labels = [label for label, _ in everyone]
    assert labels == ["до 18", "18–20", "21–23", "24–26", "27+"]
    assert dict(everyone) == {"до 18": 1, "18–20": 1, "21–23": 1, "24–26": 1, "27+": 1}
    # «не скажу» не попал никуда; одобренный с датой рождения 2000 г. — 24–26 или 27+.
    assert sum(count for _, count in approved) == 3
    assert dict(approved)["18–20"] == 1 and dict(approved)["21–23"] == 1


def test_parse_age_sources():
    today = date(2026, 10, 8)
    assert _parse_age("19", None, today) == 19
    assert _parse_age("19 лет", None, today) == 19
    assert _parse_age("", "09.10.2006", today) == 19  # день рождения завтра
    assert _parse_age(None, "08.10.2006", today) == 20
    assert _parse_age("абв", "кривая дата", today) is None
    assert _parse_age("5", None, today) is None
    assert _parse_age("2004", None, today) is None  # год вместо возраста — неправдоподобно


def test_dashboard_switcher_and_age_cut_render(tmp_path):
    db_path = render._use_tmp_db(tmp_path)
    render._seed(users=_users())
    client = render._stats_manager_client(db_path)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Кто подаёт</h2>" in resp.text
    assert "Возраст" in resp.text
    assert "Только одобренные" in resp.text
    assert "Экономика" in resp.text

    resp = client.get("/", params={"who": "approved"})
    assert resp.status_code == 200
    assert "Кто отобран</h2>" in resp.text
    assert "Экономика" not in resp.text  # ни одного одобренного экономиста

    # Чужое значение `who` — обычный режим, не ошибка.
    resp = client.get("/", params={"who": "'; DROP TABLE users"})
    assert resp.status_code == 200
    assert "Кто подаёт</h2>" in resp.text
