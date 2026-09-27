"""Конструктор «Источники по дням» (запрос менеджера DXP СПб 27.09): сколько заявок пришло
по каждому источнику в конкретный день/неделю — фильтры, разбивка, шаг, CSV.

Фикстура — та же схема, что у бота (`fast_init_db`), сидинг через `database.db._connect()`,
чтение — через read-only `dashboard.db.read_conn`, как в tests/test_dashboard_queries.py.
"""
from __future__ import annotations

import asyncio
import csv
import io
import urllib.parse
from datetime import date

import pytest
from starlette.datastructures import QueryParams

from config import config
from database import db as bot_db
from dashboard import db as dash_db
from dashboard import sources_daily as sd
from dashboard.queries import Scope
from tests._dbtpl import fast_init_db

TODAY = date(2026, 9, 27)  # воскресенье


def _use_tmp_db(tmp_path, name="sources_daily.db") -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    fast_init_db()
    return path


async def _seed_async(cities=None, settings=None, users=None):
    async with bot_db._connect() as conn:
        for code, label, enabled, sort_order in cities or []:
            await conn.execute(
                "INSERT INTO cities (code, label, enabled, sort_order, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, label, enabled, sort_order, "2026-01-01 00:00:00"),
            )
        for key, value in (settings or {}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        for row in users or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


_NEXT_ID = [1000]


def _u(day: str, source=None, *, city="spb", status="approved", referrer=None,
       track="full", from_tag=None, time="12:00:00", **extra) -> dict:
    _NEXT_ID[0] += 1
    row = {
        "telegram_id": _NEXT_ID[0],
        "full_name": f"Делегат Секретный {_NEXT_ID[0]}",
        "registration_date": f"{day} {time}",
        "source": source,
        "event_city": city,
        "status": status,
        "referrer_id": referrer,
        "participant_type": track,
    }
    if from_tag is not None:
        row["source_from_tag"] = from_tag
    row.update(extra)
    return row


_CITIES = [("msk", "Москва", 1, 0), ("spb", "Санкт-Петербург", 1, 1)]
_CITY_ON = {"event_city_enabled": "on"}


def _build(db_path, scope=Scope(), query: str = "", today=TODAY):
    q = sd.SourcesQuery.from_params(QueryParams(query))
    with dash_db.read_conn(db_path) as conn:
        return sd.build(conn, scope, q, today=today)


def _row(result, key):
    for row in result["rows"]:
        if row["key"] == key:
            return row
    raise AssertionError(f"нет строки {key}: {[r['key'] for r in result['rows']]}")


def _col(result, label):
    labels = [c["label"] for c in result["columns"]]
    assert label in labels, f"нет колонки {label!r}: {labels}"
    return labels.index(label)


# ── счёт по дням и источникам ────────────────────────────────────────────────────────────

def test_per_day_counts_by_answer_with_ambassador_column_and_totals(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(cities=_CITIES, settings=_CITY_ON, users=[
        _u("2026-09-21", "Узнал от амбассадора", referrer=1),
        _u("2026-09-21", "Узнал от амбассадора"),
        _u("2026-09-21", "Соцсети АЙСЕК", referrer=2),
        _u("2026-09-22", "Соцсети АЙСЕК"),
    ])
    res = _build(db_path)
    assert res["has_data"] is True
    day21 = _row(res, "2026-09-21")
    assert day21["counts"][_col(res, "Узнал от амбассадора")] == 2
    assert day21["counts"][_col(res, "Соцсети АЙСЕК")] == 1
    assert day21["ambassador"] == 2  # по ссылке амбассадора — факт, независимо от ответа
    assert day21["total"] == 3
    assert _row(res, "2026-09-22")["total"] == 1
    assert res["totals"]["total"] == 4
    assert res["totals"]["ambassador"] == 2
    assert sum(res["totals"]["counts"]) == 4


def test_dense_calendar_newest_first_until_today(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[_u("2026-09-20", "ВК"), _u("2026-09-23", "ВК")])
    res = _build(db_path)
    keys = [r["key"] for r in res["rows"]]
    assert keys[0] == "2026-09-27"
    assert keys[-1] == "2026-09-20"
    assert len(keys) == 8  # 20..27 без дыр
    assert _row(res, "2026-09-21")["total"] == 0
    assert _row(res, "2026-09-21")["label"] == "21.09, пн"


def test_rest_bucket_collects_tail_beyond_top_seven(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    users = []
    # 7 крупных источников по 3 заявки + два мелких по одной
    for i in range(7):
        users += [_u("2026-09-21", f"Канал {i}") for _ in range(3)]
    users += [_u("2026-09-21", "Редкий А"), _u("2026-09-22", "Редкий Б")]
    _seed(users=users)
    res = _build(db_path)
    labels = [c["label"] for c in res["columns"]]
    assert labels[-1] == "Остальные"
    assert len(labels) == 8
    assert "Редкий А" not in labels
    assert res["columns"][-1]["color"] == "other"
    rest = _col(res, "Остальные")
    assert _row(res, "2026-09-21")["counts"][rest] == 1
    assert res["totals"]["counts"][rest] == 2
    # цвета — 1..7 по месту в рейтинге, не повторяются
    assert [c["color"] for c in res["columns"][:7]] == [str(i) for i in range(1, 8)]


def test_empty_answer_and_tag_source_get_human_labels(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[
        _u("2026-09-21", None),
        _u("2026-09-21", "  "),
        _u("2026-09-21", "-"),
        _u("2026-09-21", "vk_post"),  # метка из ссылки: вопрос «Откуда узнал» не задавался
    ])
    res = _build(db_path)
    day = _row(res, "2026-09-21")
    assert day["counts"][_col(res, "Не указано")] == 3
    assert day["counts"][_col(res, "По метке ссылки")] == 1


def test_tag_view_uses_campaign_tags_and_ambassador_link(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[
        _u("2026-09-21", "vk_post"),
        _u("2026-09-21", "vk_post"),
        _u("2026-09-21", "ВК", from_tag=1),  # флаг метки перекрывает эвристику
        _u("2026-09-21", "Соцсети АЙСЕК", referrer=5),
        _u("2026-09-21", "Соцсети АЙСЕК"),
    ])
    res = _build(db_path, query="by=tag")
    day = _row(res, "2026-09-21")
    assert day["counts"][_col(res, "vk_post")] == 2
    assert day["counts"][_col(res, "ВК")] == 1
    assert day["counts"][_col(res, "Личная ссылка амбассадора")] == 1
    assert day["counts"][_col(res, "Без метки")] == 1
    assert day["ambassador"] == 1


# ── скоуп и фильтры ──────────────────────────────────────────────────────────────────────

def test_city_scope_limits_rows(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(cities=_CITIES, settings=_CITY_ON, users=[
        _u("2026-09-21", "ВК", city="spb"),
        _u("2026-09-21", "ВК", city="msk"),
        _u("2026-09-21", "ВК", city=None),  # пустой город = город по умолчанию (Москва)
    ])
    assert _build(db_path, Scope(city="spb"))["totals"]["total"] == 1
    assert _build(db_path, Scope(city="msk"))["totals"]["total"] == 2
    assert _build(db_path, Scope())["totals"]["total"] == 3


def test_spb_ambassador_link_on_21st_exact_count(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(cities=_CITIES, settings=_CITY_ON, users=[
        _u("2026-09-21", "Узнал от амбассадора", city="spb", referrer=1),
        _u("2026-09-21", "Соцсети АЙСЕК", city="spb", referrer=1),
        _u("2026-09-21", "Узнал от амбассадора", city="spb"),  # сказал, но без ссылки
        _u("2026-09-21", "Узнал от амбассадора", city="msk", referrer=1),
        _u("2026-09-22", "Узнал от амбассадора", city="spb", referrer=1),
    ])
    res = _build(db_path, Scope(city="spb"), "amb=1")
    day = _row(res, "2026-09-21")
    assert day["total"] == 2
    assert day["ambassador"] == 2
    assert _row(res, "2026-09-22")["total"] == 1


def test_or_within_source_group_and_between_groups(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[
        _u("2026-09-21", "ВК", status="approved"),
        _u("2026-09-21", "ВК", status="pending"),
        _u("2026-09-21", "Телеграм", status="approved"),
        _u("2026-09-21", "Друг", status="approved"),
    ])
    both = _build(db_path, query="src=ВК&src=Телеграм")
    assert both["totals"]["total"] == 3
    approved_only = _build(db_path, query="src=ВК&src=Телеграм&status=approved")
    assert approved_only["totals"]["total"] == 2


def test_rest_chip_filters_tail(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    users = []
    for i in range(7):
        users += [_u("2026-09-21", f"Канал {i}") for _ in range(2)]
    users += [_u("2026-09-21", "Редкий")]
    _seed(users=users)
    res = _build(db_path, query="src=_rest")
    assert res["totals"]["total"] == 1
    chip_values = [o["value"] for o in res["options"]["answer"]]
    assert "_rest" in chip_values


def test_status_breakdown_and_track_filter(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[
        _u("2026-09-21", "ВК", status="approved"),
        _u("2026-09-21", "ВК", status="pending"),
        _u("2026-09-21", "ВК", status="rejected", track="party_overnight"),
    ])
    res = _build(db_path, query="by=status")
    labels = [c["label"] for c in res["columns"]]
    assert labels == ["Одобрена", "Ждёт решения", "Отказ"]
    tracks = {o["value"]: o["label"] for o in res["options"]["track"]}
    assert tracks["party_overnight"] == "\U0001f389 Гости с ночёвкой"
    assert _build(db_path, query="track=party_overnight")["totals"]["total"] == 1


def test_city_breakdown_collapses_unknown_into_default(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(cities=_CITIES, settings=_CITY_ON, users=[
        _u("2026-09-21", "ВК", city="spb"),
        _u("2026-09-21", "ВК", city=None),
        _u("2026-09-21", "ВК", city="msk"),
    ])
    res = _build(db_path, query="by=city")
    day = _row(res, "2026-09-21")
    assert day["counts"][_col(res, "Москва")] == 2
    assert day["counts"][_col(res, "Санкт-Петербург")] == 1


def test_week_step_buckets_monday_to_sunday(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[
        _u("2026-09-20", "ВК", time="23:59:00"),  # воскресенье — прошлая неделя
        _u("2026-09-21", "ВК", time="00:00:01"),  # понедельник
        _u("2026-09-27", "ВК", time="23:30:00"),  # воскресенье той же недели
    ])
    res = _build(db_path, query="step=week")
    keys = [r["key"] for r in res["rows"]]
    assert keys == ["2026-09-21", "2026-09-14"]
    assert _row(res, "2026-09-21")["total"] == 2
    assert _row(res, "2026-09-21")["label"] == "21.09 – 27.09"
    assert _row(res, "2026-09-14")["total"] == 1


def test_period_presets_and_custom_range(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[_u("2026-09-01", "ВК"), _u("2026-09-25", "ВК"), _u("2026-09-10", "ВК")])
    week = _build(db_path, query="period=7")
    assert [r["key"] for r in week["rows"]][-1] == "2026-09-21"
    assert week["totals"]["total"] == 1
    custom = _build(db_path, query="from=2026-09-05&to=2026-09-12")
    assert custom["totals"]["total"] == 1
    assert [r["key"] for r in custom["rows"]] [0] == "2026-09-12"
    assert len(custom["rows"]) == 8
    # мусорные даты молча игнорируются — весь сезон
    assert _build(db_path, query="from=вчера&to=2026-13-40")["totals"]["total"] == 3


def test_empty_db(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    res = _build(db_path)
    assert res["has_data"] is False
    assert res["rows"] == []
    assert res["totals"]["total"] == 0


def test_season_scope_respected(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(settings={"event_season": "YL 26/2"}, users=[
        _u("2026-09-21", "ВК", season="YL 26/2"),
        _u("2026-09-21", "ВК", season="YL 26/1"),
    ])
    assert _build(db_path)["totals"]["total"] == 1
    assert _build(db_path, Scope(season="YL 26/1"))["totals"]["total"] == 1


# ── состояние в URL ──────────────────────────────────────────────────────────────────────

def test_params_whitelist_and_toggle_href():
    q = sd.SourcesQuery.from_params(QueryParams(
        "by=drop_table&step=year&status=approved&status=evil&amb=1&src=ВК"
    ))
    assert q.by == "answer"
    assert q.step == "day"
    assert q.statuses == ("approved",)
    assert q.ambassador_only is True
    href = q.href(city="spb", season=None, toggle=("src", "Друг"))
    parsed = urllib.parse.parse_qs(href.lstrip("?"))
    assert parsed["src"] == ["ВК", "Друг"]
    assert parsed["city"] == ["spb"]
    assert "by" not in parsed  # значения по умолчанию в ссылку не пишутся
    off = urllib.parse.parse_qs(q.href(city=None, season=None, toggle=("src", "ВК")).lstrip("?"))
    assert "src" not in off


def test_track_labels_match_bot_registry():
    import reg_options

    assert sd.TRACK_LABELS == dict(reg_options.PARTY_TRACK_OPTIONS)


# ── CSV ──────────────────────────────────────────────────────────────────────────────────

def test_csv_bom_header_rows_and_no_pii(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[
        _u("2026-09-21", "=HYPERLINK(1)", referrer=1),
        _u("2026-09-21", "ВК"),
    ])
    res = _build(db_path)
    data = sd.csv_bytes(res, "Разбивка: ответ в анкете")
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    header = next(r for r in rows if r and r[0] == "День")
    assert header[-2:] == ["По ссылке амбассадора", "Всего за день"]
    assert "'=HYPERLINK(1)" in header  # формула не выполнится в Excel
    day = next(r for r in rows if r and r[0] == "21.09.2026")
    assert day[-1] == "2" and day[-2] == "1"
    assert rows[-1][0] == "Итого"
    assert "Секретный" not in text  # только агрегаты, никаких ПД


def test_csv_data_href_roundtrip(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[_u("2026-09-21", "ВК #1")])
    res = _build(db_path)
    href = sd.csv_data_href(res, "Фильтры: нет")
    assert href.startswith("data:text/csv;charset=utf-8,")
    raw = urllib.parse.unquote_to_bytes(href.split(",", 1)[1])
    assert raw.startswith(b"\xef\xbb\xbf")
    assert "ВК #1" in raw.decode("utf-8-sig")


@pytest.mark.parametrize("by", sd.BREAKDOWNS)
@pytest.mark.parametrize("step", sd.STEPS)
def test_every_breakdown_and_step_consistent(tmp_path, by, step):
    db_path = _use_tmp_db(tmp_path, f"combo_{by}_{step}.db")
    _seed(cities=_CITIES, settings=_CITY_ON, users=[
        _u("2026-09-15", "ВК", city="msk", status="pending"),
        _u("2026-09-21", "vk_post", city="spb", referrer=3),
        _u("2026-09-26", None, city="spb", status="rejected"),
    ])
    res = _build(db_path, query=f"by={by}&step={step}")
    assert res["totals"]["total"] == 3
    assert sum(r["total"] for r in res["rows"]) == 3
    for r in res["rows"]:
        assert sum(r["counts"]) == r["total"]
    chart = res["chart"]
    assert len(chart["labels"]) == len(res["rows"])
    assert [d["label"] for d in chart["datasets"]] == [c["label"] for c in res["columns"]]


# ── регистрация на месте (D-41) ──────────────────────────────────────────────────────────

def test_onsite_rows_get_own_tag_bucket(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(users=[
        _u("2026-09-21", "На месте", onsite_kind="walkin"),
        _u("2026-09-21", "ВК", referrer=5, onsite_kind="door"),  # на месте важнее ссылки
        _u("2026-09-21", "vk_post"),
        _u("2026-09-21", "Соцсети АЙСЕК", referrer=5),
        _u("2026-09-21", "Соцсети АЙСЕК"),
    ])
    res = _build(db_path, query="by=tag")
    day = _row(res, "2026-09-21")
    assert sd.TAG_ONSITE == "📍 На месте"
    assert day["counts"][_col(res, "📍 На месте")] == 2
    assert day["counts"][_col(res, "vk_post")] == 1
    assert day["counts"][_col(res, "Личная ссылка амбассадора")] == 1
    assert day["counts"][_col(res, "Без метки")] == 1
    # разбивка по ответу в анкете не тронута
    res_answer = _build(db_path, query="by=answer")
    assert _row(res_answer, "2026-09-21")["counts"][_col(res_answer, "На месте")] == 1


def test_old_db_without_onsite_column_still_builds(tmp_path):
    import sqlite3

    db_path = _use_tmp_db(tmp_path)
    _seed(users=[_u("2026-09-21", "ВК")])
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE users DROP COLUMN onsite_kind")
    conn.commit()
    conn.close()
    res = _build(db_path, query="by=tag")
    assert _row(res, "2026-09-21")["counts"][_col(res, "Без метки")] == 1
