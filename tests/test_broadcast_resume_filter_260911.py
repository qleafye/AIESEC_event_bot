"""Квик 260911-0fh (RESUME-FILTER-01..06): «Резюме: есть/нет» как поле фильтра рассылки.

Прод-инцидент: с 05.09 по 10.09 у 203 делегатов молча потерялось резюме, приложенное файлом
(баг починен, квик 260910-wb6, коммит `ef315f9`). Их нужно попросить прислать резюме заново,
но фильтров рассылки хватает только на «зарегистрированы после даты» — это 523 человека, из
которых у 370 резюме на месте: без второго фильтра просьба уходит и тем, кто уже прислал.

Два раздела, по образцу `tests/test_broadcast_season_filter_260910.py`:

1. «SQL» (`database.db`) — `resume` в `_FILTER_COLUMNS` + `_FILTER_VIRTUAL_FIELDS` (поле
   ВИРТУАЛЬНОЕ, колонки `users.resume` не существует), собственная ветка
   `_build_filter_clause`, сентинелы `RESUME_HAS`/`RESUME_MISSING`, единый набор колонок
   резюме (`RESUME_COLUMNS`/`RESUME_RECALL_COLUMNS`), `get_resume_filter_options`.
2. «Экран» (`handlers.admin_broadcasts`) — кнопка «Резюме» в меню фильтров (только когда в
   базе есть и делегаты с резюме, и без), пикер двумя кнопками «есть»/«нет», сводка условий.

Правило двойной регистрации поля (прецедент Фазы 5, D-19, закреплено тестом
`tests/test_city_broadcast_phase72.py:411`): поле фильтра обязано быть в ОБОИХ местах —
`db._FILTER_COLUMNS` и `handlers.admin_broadcasts._PICKER_FIELDS`. Если поле есть только в
одном из двух, оно либо не доходит до SQL (виден на экране, молча не фильтрует), либо не
появляется на экране вовсе.

`-` (прочерк) в колонке резюме считается «не заполнено» — та же конвенция, что в
`reg_engine.has_prior_resume`/`prior_answers_for` (иначе делегат с прочерком в
`resume_file_id` попал бы в «есть» и просьбы прислать резюме заново не получил бы).

pytest-asyncio в этом окружении не установлен — каждый async-хелпер гоняется через
`asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`, как в
`tests/test_broadcast_season_filter_260910.py`.
"""
import asyncio
import json
import sqlite3

from config import config
from database import db
from database.db import _build_filter_clause


# ── Задача 1: «SQL» — whitelist, виртуальное поле, ветка _build_filter_clause ───────────────

def test_resume_is_whitelisted_and_virtual():
    assert "resume" in db._FILTER_COLUMNS
    assert "resume" in db._FILTER_VIRTUAL_FIELDS


def test_filter_columns_whitelist_guard(tmp_path):
    """Каждое поле `_FILTER_COLUMNS` — либо реальная колонка `users`, либо объявлено
    виртуальным в `_FILTER_VIRTUAL_FIELDS`. Ловит и опечатку в имени колонки, и виртуальное
    поле, забытое в наборе."""
    config.DB_PATH = str(tmp_path / "test_resume_whitelist_guard.db")
    asyncio.run(db.init_db())
    con = sqlite3.connect(config.DB_PATH)
    try:
        real_cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    finally:
        con.close()
    for field in db._FILTER_COLUMNS:
        assert field in real_cols or field in db._FILTER_VIRTUAL_FIELDS, (
            f"'{field}' is neither a real users column nor declared virtual"
        )


def test_get_distinct_filter_values_resume_returns_empty_not_crash(tmp_path):
    """Сегодня та же строка кода собрала бы `SELECT DISTINCT resume` — `OperationalError`."""
    config.DB_PATH = str(tmp_path / "test_resume_distinct_no_crash.db")
    asyncio.run(db.init_db())
    assert asyncio.run(db.get_distinct_filter_values("resume")) == []


def test_resume_columns_constants():
    assert db.RESUME_COLUMNS == ("resume_file_id", "resume_text", "resume_url", "resume_link")
    assert db.RESUME_RECALL_COLUMNS == ("resume_file_id", "resume_text", "resume_url")


def test_resume_recall_columns_is_proper_subset_of_resume_columns():
    assert set(db.RESUME_RECALL_COLUMNS) < set(db.RESUME_COLUMNS)


def test_has_prior_resume_matches_recall_columns_oracle():
    """`reg_engine.has_prior_resume` даёт True/False ровно на тех же строках, что кортеж
    `RESUME_RECALL_COLUMNS` — по строке-словарю на каждую колонку + строка с «-» + пустая."""
    import reg_engine

    for col in db.RESUME_RECALL_COLUMNS:
        assert reg_engine.has_prior_resume({col: "some-value"}) is True
        assert reg_engine.has_prior_resume({col: "-"}) is False
        assert reg_engine.has_prior_resume({col: ""}) is False
    # resume_link НЕ входит в RESUME_RECALL_COLUMNS — recall его не видит.
    assert reg_engine.has_prior_resume({"resume_link": "https://vk.com/profile"}) is False
    assert reg_engine.has_prior_resume({}) is False
    assert reg_engine.has_prior_resume(None) is False


def test_build_filter_clause_resume_missing():
    where, params = _build_filter_clause([{"field": "resume", "value": db.RESUME_MISSING}])
    assert params == []
    assert " AND " in where
    for col in db.RESUME_COLUMNS:
        assert col in where


def test_build_filter_clause_resume_has():
    where, params = _build_filter_clause([{"field": "resume", "value": db.RESUME_HAS}])
    assert params == []
    assert " OR " in where
    for col in db.RESUME_COLUMNS:
        assert col in where


def test_resume_empty_value_is_fail_closed():
    """WR-01 (тот же довод, что у event_city/season): пустое значение НЕ снимает условие."""
    assert _build_filter_clause([{"field": "resume", "value": ""}]) == (" WHERE 0", [])


def test_resume_garbage_value_is_fail_closed():
    assert _build_filter_clause([{"field": "resume", "value": "garbage"}]) == (" WHERE 0", [])


def test_resume_empty_value_with_other_filter_does_not_fan_out():
    where, params = _build_filter_clause([
        {"field": "status", "value": "approved"},
        {"field": "resume", "value": ""},
    ])
    assert where == " WHERE status = ? AND 0"
    assert params == ["approved"]


def test_resume_combined_with_registration_date_keeps_and_and_single_bind():
    where, params = _build_filter_clause([
        {"field": "registration_date", "op": "after", "value": "2026-09-05"},
        {"field": "resume", "value": db.RESUME_MISSING},
    ])
    assert where.startswith(" WHERE registration_date >= ? AND (")
    assert params == ["2026-09-05"]


def test_resume_filter_survives_json_round_trip():
    """Отложенная рассылка хранит спеку фильтра как JSON — round-trip обязан давать то же
    самое условие (доказательство того, что отложенная рассылка бьёт по той же аудитории,
    что и мгновенная)."""
    filters = [{"field": "resume", "value": db.RESUME_MISSING}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before


# ── Задача 1: сквозной тест на засеянной базе ───────────────────────────────────────────

def _seed_resume_kinds(tmp_path):
    """По строке на каждый вид резюме + прочерк + вообще без резюме."""
    config.DB_PATH = str(tmp_path / "test_resume_kinds.db")

    async def go():
        await db.init_db()
        rows = [
            (1, {"resume_file_id": "file123"}),
            (2, {"resume_text": "мой опыт..."}),
            (3, {"resume_url": "https://nextcloud.example/s/abc"}),
            (4, {"resume_link": "https://vk.com/id1"}),
            (5, {"resume_file_id": "-"}),
            (6, {}),
        ]
        for tid, extra in rows:
            data = {
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
            }
            data.update(extra)
            await db.add_user(data)

    asyncio.run(go())


def test_count_and_list_filtered_resume_missing_is_dash_and_empty(tmp_path):
    _seed_resume_kinds(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "resume", "value": db.RESUME_MISSING}]))
    assert set(ids) == {5, 6}


def test_count_and_list_filtered_resume_has_is_the_rest(tmp_path):
    _seed_resume_kinds(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "resume", "value": db.RESUME_HAS}]))
    assert set(ids) == {1, 2, 3, 4}


def test_resume_has_and_missing_partition_the_base(tmp_path):
    _seed_resume_kinds(tmp_path)
    has_ids = set(asyncio.run(db.count_and_list_filtered([{"field": "resume", "value": db.RESUME_HAS}])))
    missing_ids = set(asyncio.run(db.count_and_list_filtered([{"field": "resume", "value": db.RESUME_MISSING}])))
    assert has_ids | missing_ids == {1, 2, 3, 4, 5, 6}
    assert has_ids & missing_ids == set()


# ── Задача 1: сценарий инцидента ─────────────────────────────────────────────────────────

def _seed_incident_base(tmp_path):
    """5 делегатов: до отсечки (05.09) — резюме есть; после отсечки — смесь есть/нет/прочерк."""
    config.DB_PATH = str(tmp_path / "test_resume_incident.db")

    async def go():
        await db.init_db()
        rows = [
            (1, "2026-09-01 09:00:00", {"resume_file_id": "f1"}),   # до отсечки, есть
            (2, "2026-09-02 09:00:00", {}),                          # до отсечки, нет (не задет)
            (3, "2026-09-06 09:00:00", {"resume_file_id": "f2"}),   # после, есть — НЕ пострадал
            (4, "2026-09-07 09:00:00", {}),                          # после, нет — пострадал
            (5, "2026-09-08 09:00:00", {"resume_text": "текст"}),   # после, есть — НЕ пострадал
            (6, "2026-09-09 09:00:00", {"resume_file_id": "-"}),    # после, прочерк — пострадал
        ]
        for tid, reg_date, extra in rows:
            data = {"telegram_id": tid, "full_name": f"User {tid}", "registration_date": reg_date}
            data.update(extra)
            await db.add_user(data)

    asyncio.run(go())


def test_incident_scenario_date_plus_resume_missing_is_exactly_affected(tmp_path):
    _seed_incident_base(tmp_path)
    affected = asyncio.run(db.count_and_list_filtered([
        {"field": "registration_date", "op": "after", "value": "2026-09-05"},
        {"field": "resume", "value": db.RESUME_MISSING},
    ]))
    assert set(affected) == {4, 6}


def test_incident_scenario_date_alone_is_a_superset(tmp_path):
    _seed_incident_base(tmp_path)
    after_only = asyncio.run(db.count_and_list_filtered([
        {"field": "registration_date", "op": "after", "value": "2026-09-05"},
    ]))
    assert set(after_only) == {3, 4, 5, 6}
    assert len(after_only) > 2


# ── Задача 1: get_resume_filter_options ─────────────────────────────────────────────────

def test_get_resume_filter_options_both_sides(tmp_path):
    config.DB_PATH = str(tmp_path / "test_resume_options_both.db")

    async def go():
        await db.init_db()
        await db.add_user({"telegram_id": 1, "full_name": "A", "registration_date": "2026-01-01 09:00:00",
                            "resume_file_id": "f1"})
        await db.add_user({"telegram_id": 2, "full_name": "B", "registration_date": "2026-01-01 09:01:00"})

    asyncio.run(go())
    assert asyncio.run(db.get_resume_filter_options()) == [db.RESUME_HAS, db.RESUME_MISSING]


def test_get_resume_filter_options_all_have_resume(tmp_path):
    config.DB_PATH = str(tmp_path / "test_resume_options_all_has.db")

    async def go():
        await db.init_db()
        await db.add_user({"telegram_id": 1, "full_name": "A", "registration_date": "2026-01-01 09:00:00",
                            "resume_file_id": "f1"})
        await db.add_user({"telegram_id": 2, "full_name": "B", "registration_date": "2026-01-01 09:01:00",
                            "resume_text": "text"})

    asyncio.run(go())
    assert asyncio.run(db.get_resume_filter_options()) == [db.RESUME_HAS]


def test_get_resume_filter_options_all_missing_resume(tmp_path):
    config.DB_PATH = str(tmp_path / "test_resume_options_all_missing.db")

    async def go():
        await db.init_db()
        await db.add_user({"telegram_id": 1, "full_name": "A", "registration_date": "2026-01-01 09:00:00"})
        await db.add_user({"telegram_id": 2, "full_name": "B", "registration_date": "2026-01-01 09:01:00",
                            "resume_file_id": "-"})

    asyncio.run(go())
    assert asyncio.run(db.get_resume_filter_options()) == [db.RESUME_MISSING]


def test_get_resume_filter_options_empty_base(tmp_path):
    config.DB_PATH = str(tmp_path / "test_resume_options_empty.db")
    asyncio.run(db.init_db())
    assert asyncio.run(db.get_resume_filter_options()) == []
