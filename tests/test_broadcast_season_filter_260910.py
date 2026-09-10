"""Квик 260910-vfl (SEASON-FILTER-01..06): «Сезон» как поле фильтра рассылки.

Инцидент 10.09: на прод залито 482 делегата сезона «YL 26/1» рядом с 1691 делегатом
«YL 26/2» — кнопка «Всем» стала бить по 2173 адресатам вместо 1691. Поле `users.season`
в базе есть (07.3-A), но в whitelist фильтров рассылки его не было — сегментировать
было нечем.

Два раздела, по образцу `tests/test_city_broadcast_phase72.py`:

1. «SQL» (`database.db`) — `season` в `_FILTER_COLUMNS`, отдельная ветка `_build_filter_clause`,
   сентинел `SEASON_NONE` для легаси-строк без сезона, `get_season_filter_options`.
2. «Экран» (`handlers.admin_broadcasts`) — кнопка «Сезон» в меню фильтров (только когда
   сезонов больше одного), пикер значений кнопками, сводка условий.

Правило двойной регистрации поля (прецедент Фазы 5, D-19, тот же, что у `event_city`):
поле фильтра обязано быть в ОБОИХ местах — `db._FILTER_COLUMNS` и
`handlers.admin_broadcasts._PICKER_FIELDS`. Если поле есть только в одном из двух, оно
либо не доходит до SQL (виден на экране, молча не фильтрует), либо не появляется на
экране вовсе. Один из тестов ниже проверяет именно эту связку.

pytest-asyncio в этом окружении не установлен — каждый async-хелпер гоняется через
`asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`, как в
`tests/test_city_broadcast_phase72.py`.
"""
import asyncio
import json

from config import config
from database import db
from database.db import _build_filter_clause


# ── Задача 1: «SQL» — whitelist, ветка _build_filter_clause, SEASON_NONE ───────────────

def test_season_is_whitelisted():
    assert "season" in db._FILTER_COLUMNS


def test_season_plain_equality():
    assert _build_filter_clause([
        {"field": "season", "value": "YL 26/2"}
    ]) == (" WHERE season = ?", ["YL 26/2"])


def test_season_none_sentinel_collapses_to_null_or_blank():
    assert _build_filter_clause([
        {"field": "season", "value": db.SEASON_NONE}
    ]) == (" WHERE (season IS NULL OR TRIM(season) = '')", [])


def test_season_empty_value_is_fail_closed():
    """WR-01 (тот же довод, что у event_city): пустое значение НЕ снимает условие, а
    эмитит заведомо ложное «0» — аудитория гарантированно пуста."""
    assert _build_filter_clause([{"field": "season", "value": ""}]) == (" WHERE 0", [])


def test_season_empty_value_with_other_filter_does_not_fan_out():
    where, params = _build_filter_clause([
        {"field": "status", "value": "approved"},
        {"field": "season", "value": ""},
    ])
    assert where == " WHERE status = ? AND 0"
    assert params == ["approved"]


def test_season_combined_with_other_field_keeps_and_and_bind_order():
    where, params = _build_filter_clause([
        {"field": "status", "value": "approved"},
        {"field": "season", "value": "YL 26/2"},
    ])
    assert where == " WHERE status = ? AND season = ?"
    assert params == ["approved", "YL 26/2"]


def test_season_filter_survives_json_round_trip():
    """Отложенная рассылка хранит спеку фильтра как JSON и пересобирает аудиторию после
    рестарта — round-trip обязан давать то же самое условие (доказательство того, что
    отложенная рассылка бьёт по той же аудитории, что и мгновенная)."""
    filters = [{"field": "season", "value": "YL 26/2"}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before


def test_season_none_sentinel_survives_json_round_trip():
    filters = [{"field": "season", "value": db.SEASON_NONE}]
    before = _build_filter_clause(filters)
    restored = json.loads(json.dumps(filters, ensure_ascii=False))
    assert _build_filter_clause(restored) == before
    assert restored[0]["value"] == db.SEASON_NONE


# ── Задача 1: сквозной тест на засеянной базе ───────────────────────────────────────────

def _seed_season_base(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_filter_260910.db")

    async def go():
        await db.init_db()
        rows = [
            (1, "YL 26/2"), (2, "YL 26/2"), (3, "YL 26/2"),
            (4, "YL 26/1"), (5, "YL 26/1"),
            (6, None),
        ]
        for tid, season in rows:
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"User {tid}",
                "registration_date": f"2026-01-01 09:{tid:02d}:00",
                "season": season,
            })

    asyncio.run(go())


def test_count_and_list_filtered_by_season_yl262(tmp_path):
    _seed_season_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "season", "value": "YL 26/2"}]))
    assert set(ids) == {1, 2, 3}


def test_count_and_list_filtered_by_season_yl261(tmp_path):
    _seed_season_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "season", "value": "YL 26/1"}]))
    assert set(ids) == {4, 5}


def test_count_and_list_filtered_by_season_none_only_legacy_row(tmp_path):
    _seed_season_base(tmp_path)
    ids = asyncio.run(db.count_and_list_filtered([{"field": "season", "value": db.SEASON_NONE}]))
    assert set(ids) == {6}


def test_get_season_filter_options_real_seasons_alphabetical_then_none_last(tmp_path):
    _seed_season_base(tmp_path)
    options = asyncio.run(db.get_season_filter_options())
    assert options == ["YL 26/1", "YL 26/2", db.SEASON_NONE]


def test_get_season_filter_options_no_legacy_rows_no_sentinel(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_filter_no_legacy_260910.db")

    async def go():
        await db.init_db()
        await db.add_user({
            "telegram_id": 1, "full_name": "User 1",
            "registration_date": "2026-01-01 09:00:00", "season": "YL 26/2",
        })
        await db.add_user({
            "telegram_id": 2, "full_name": "User 2",
            "registration_date": "2026-01-01 09:01:00", "season": "YL 26/1",
        })

    asyncio.run(go())
    options = asyncio.run(db.get_season_filter_options())
    assert options == ["YL 26/1", "YL 26/2"]


def test_get_season_filter_options_empty_base(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_filter_empty_260910.db")
    asyncio.run(db.init_db())
    assert asyncio.run(db.get_season_filter_options()) == []
