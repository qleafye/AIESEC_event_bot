"""Страница конструктора «Источники по дням» (`/sources`): периметр как у `/`, фильтры в
URL, таблица с липкой первой колонкой, график и выгрузка CSV без отдельного маршрута."""
from __future__ import annotations

import html
import json
import re
import urllib.parse

import tests.test_dashboard_render as tdr

_CITIES = [("msk", "Москва", 1, 0), ("spb", "Санкт-Петербург", 1, 1)]


def _users():
    return [
        {"telegram_id": 5001, "full_name": "Иванов Иван Иванович", "registration_date": "2026-09-21 10:00:00",
         "source": "Узнал от амбассадора", "event_city": "spb", "status": "approved", "referrer_id": 1},
        {"telegram_id": 5002, "full_name": "Петров Пётр", "registration_date": "2026-09-21 11:00:00",
         "source": "Соцсети АЙСЕК", "event_city": "spb", "status": "pending"},
        {"telegram_id": 5003, "full_name": "Сидоров Сидор", "registration_date": "2026-09-22 11:00:00",
         "source": "vk_post", "event_city": "msk", "status": "approved"},
    ]


def _setup(tmp_path, *, city=None):
    db_path = tdr._use_tmp_db(tmp_path, "sources_page.db")
    tdr._seed(cities=_CITIES, settings={"event_city_enabled": "on"}, users=_users())
    return tdr._stats_manager_client(db_path, city=city), db_path


def _csv_text(page: str) -> str:
    m = re.search(r'href="(data:text/csv;charset=utf-8,[^"]+)"', page)
    assert m, "нет ссылки «Скачать CSV»"
    href = html.unescape(m.group(1))
    return urllib.parse.unquote_to_bytes(href.split(",", 1)[1]).decode("utf-8-sig")


def test_page_renders_block_table_chart_and_caption(tmp_path):
    client, _ = _setup(tmp_path)
    resp = client.get("/sources")
    assert resp.status_code == 200
    page = resp.text
    assert "Источники по дням" in page
    assert "Всего за день" in page
    assert "По ссылке амбассадора" in page
    assert "Узнал от амбассадора" in page  # подпись ответа в анкете
    assert "пересекаются" in page  # пояснение: факт ссылки vs ответ в анкете
    assert 'class="table-wrap sources-table-wrap"' in page
    assert "Скачать CSV" in page
    m = re.search(r"data-chart='([^']+)'", page)
    assert m
    chart = json.loads(html.unescape(m.group(1)))
    assert chart["labels"] and chart["datasets"]
    assert "Иванов" not in page  # только агрегаты


def test_filters_in_url_and_csv_follow_same_slice(tmp_path):
    client, _ = _setup(tmp_path)
    page = client.get("/sources", params={"city": "spb", "amb": "1"}).text
    text = _csv_text(page)
    assert "только по ссылке амбассадора" in text
    assert "город: Санкт-Петербург" in text
    day = next(line for line in text.splitlines() if line.startswith("21.09.2026"))
    assert day.endswith(";1;1")
    assert "Иванов" not in text
    # чип фильтра — ссылка, сохраняющая город
    assert 'href="?city=spb&amp;status=approved&amp;amb=1"' in page


def test_tag_view_and_week_step(tmp_path):
    client, _ = _setup(tmp_path)
    page = client.get("/sources", params={"by": "tag", "step": "week"}).text
    assert "vk_post" in page
    assert "Всего за неделю" in page


def test_bound_manager_cannot_see_other_city(tmp_path):
    client, _ = _setup(tmp_path, city="spb")
    page = client.get("/sources", params={"city": "msk", "by": "tag"}).text
    assert "vk_post" not in page  # московская заявка
    assert "Ваш город:" in page


def test_without_stats_403_and_without_login_redirect(tmp_path):
    db_path = tdr._use_tmp_db(tmp_path, "sources_page_denied.db")
    tdr._seed(users=_users())
    anon = tdr._client(tdr._cfg(db_path))
    resp = anon.get("/sources", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/login"
    client = tdr._client(tdr._cfg(db_path))
    tdr._login_as(client, 900500)
    denied = client.get("/sources")
    assert denied.status_code == 403
    assert "data:text/csv" not in denied.text


def test_empty_db_shows_human_note(tmp_path):
    db_path = tdr._use_tmp_db(tmp_path, "sources_page_empty.db")
    client = tdr._stats_manager_client(db_path)
    page = client.get("/sources").text
    assert "Заявок пока нет" in page
    assert "data:text/csv" not in page


def test_garbage_params_do_not_break_page(tmp_path):
    client, _ = _setup(tmp_path)
    resp = client.get("/sources", params={"by": "x", "step": "y", "from": "zzz", "status": "hacked", "period": "9"})
    assert resp.status_code == 200


def test_main_dashboard_links_to_sources_page(tmp_path):
    client, _ = _setup(tmp_path)
    assert 'href="/sources' in client.get("/").text


def test_sources_template_mobile_rules_present():
    css = tdr.APP_CSS.read_text(encoding="utf-8")
    assert ".sources-table-wrap" in css
    assert "position: sticky" in css.split(".sources-table-wrap", 1)[1]
