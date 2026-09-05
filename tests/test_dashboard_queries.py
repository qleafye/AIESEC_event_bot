"""Phase 15 Plan 03 (STAT-01/STAT-04): агрегаты дашборда на фикстурной БД.

Фикстура создаётся тем же `database.db.init_db()`, что и бот (та же схема), затем
наполняется прямыми INSERT через `database.db._connect()` (aiosqlite) — дашборд сам читает
через `dashboard.db.read_conn` (синхронный `sqlite3`, `mode=ro`) уже ПОСЛЕ того как
aiosqlite-подключение с записью закрыто (иначе `mode=ro` не увидит незакоммиченные строки
другого подключения).

pytest-asyncio недоступен в этом окружении — сидинг идёт через `asyncio.run()`, как и во
всех остальных тестах, трогающих `database.db` (см. tests/test_reg_events_log.py).
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from config import config
from database import db as bot_db
from settings_schema import SETTINGS_SCHEMA

from dashboard import db as dash_db
from dashboard.queries import (
    ALLOWED_BREAKDOWNS,
    Scope,
    _SETTING_DEFAULTS,
    breakdown,
    city_comparison,
    city_options,
    daily_registrations,
    dashboard_flags,
    dropout_steps,
    funnel,
    funnel_tracking_since,
    game_block,
    kpi_row,
    monthly_table,
    registration_start,
    season_options,
    status_totals,
    utm_table,
)

DASHBOARD_QUERIES_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "queries.py"


def _use_tmp_db(tmp_path, name="dashboard_queries.db") -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(
    cities=None, settings=None, users=None, reg_events=None, reg_started=None,
    game_tasks=None, game_submissions=None,
):
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
        for row in reg_events or []:
            if isinstance(row, dict):
                cols = ", ".join(row.keys())
                placeholders = ", ".join("?" for _ in row)
                await conn.execute(
                    f"INSERT INTO reg_events ({cols}) VALUES ({placeholders})", tuple(row.values())
                )
            else:
                await conn.execute(
                    "INSERT INTO reg_events (telegram_id, event, event_city, season, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    row,
                )
        for row in reg_started or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO reg_started ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        for row in game_tasks or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO game_tasks ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        for row in game_submissions or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO game_submissions ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


# ── kpi_row ───────────────────────────────────────────────────────────────────────────────

def test_kpi_row_on_empty_db_returns_zeros_and_none(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row == {
        "total": 0, "today": 0, "week": 0, "week_delta": 0,
        "conversion": None, "tracking_since": None,
    }


def test_kpi_week_delta_against_previous_seven_days(tmp_path):
    path = _use_tmp_db(tmp_path)
    now = datetime.now()

    def _d(offset_days: int) -> str:
        return (now - timedelta(days=offset_days)).strftime("%Y-%m-%d 12:00:00")

    users = []
    tid = 1
    for offset in (0, 3):  # текущее окно (0..6 дней назад)
        users.append({"telegram_id": tid, "registration_date": _d(offset), "status": "approved"})
        tid += 1
    for offset in (7, 10, 13):  # предыдущее окно (7..13 дней назад)
        users.append({"telegram_id": tid, "registration_date": _d(offset), "status": "approved"})
        tid += 1
    _seed(users=users)

    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["week"] == 2
    assert row["week_delta"] == 2 - 3


def test_kpi_conversion_none_until_events_then_computed(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert kpi_row(conn, Scope())["conversion"] is None

    _seed(reg_events=[
        (1, "start", None, None, "2026-08-01 10:00:00"),
        (2, "start", None, None, "2026-08-01 10:05:00"),
        (1, "form_completed", None, None, "2026-08-01 10:10:00"),
    ])
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["conversion"] == 50.0
    assert row["tracking_since"] == "2026-08-01 10:00:00"


def test_city_scope_collects_null_and_unknown_into_default_city(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "event_city": None, "status": "approved"},
            {"telegram_id": 2, "event_city": "garbage", "status": "approved"},
            {"telegram_id": 3, "event_city": "spb", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        msk_row = kpi_row(conn, Scope(city="msk"))
        spb_row = kpi_row(conn, Scope(city="spb"))
    assert msk_row["total"] == 2  # NULL + мусорный код собраны в город по умолчанию
    assert spb_row["total"] == 1  # соседний город не задет


def test_season_scope_null_lands_in_current_season_other_season_excluded(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"event_season": "YL26"},
        users=[
            {"telegram_id": 1, "season": None, "status": "approved"},
            {"telegram_id": 2, "season": "YL26", "status": "approved"},
            {"telegram_id": 3, "season": "RusCo25", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        current_row = kpi_row(conn, Scope())  # season=None -> текущий сезон
        past_row = kpi_row(conn, Scope(season="RusCo25"))
    assert current_row["total"] == 2
    assert past_row["total"] == 1


# ── funnel ────────────────────────────────────────────────────────────────────────────────

def test_funnel_payment_stage_only_when_payment_enabled(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "status": "pending", "payment_status": "not_paid"},
        {"telegram_id": 2, "status": "approved", "payment_status": "paid"},
    ])
    with dash_db.read_conn(path) as conn:
        stages = dict(funnel(conn, Scope()))
    assert "Оплатили" not in stages
    assert stages["На модерации"] == 1
    assert stages["Одобрено"] == 1

    _seed(settings={"payment_enabled": "on"})
    with dash_db.read_conn(path) as conn:
        stages = dict(funnel(conn, Scope()))
    assert stages["Оплатили"] == 1


def test_funnel_counts_distinct_reg_events(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(reg_events=[
        (1, "start", None, None, "2026-08-01 10:00:00"),
        (1, "start", None, None, "2026-08-01 10:01:00"),  # повтор /start того же человека
        (2, "start", None, None, "2026-08-01 10:02:00"),
        (1, "form_started", None, None, "2026-08-01 10:03:00"),
    ])
    with dash_db.read_conn(path) as conn:
        stages = dict(funnel(conn, Scope()))
    assert stages["Зашли"] == 2  # DISTINCT telegram_id, повтор не удваивает
    assert stages["Начали анкету"] == 1


# ── funnel: отсечка статусных ступеней по началу трекинга (квик 260905-iyw) ──────────────

def test_funnel_status_stages_cut_by_tracking_since(tmp_path):
    """Заявка РАНЬШЕ единственного reg_events.ts не попадает в статусные ступени; более
    поздняя — попадает (иначе воронка смешивает событийный период с сезонным и даёт
    проценты за 100%, см. объективку квика)."""
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "registration_date": "2026-01-01 09:00:00", "status": "approved"},  # до трекинга
            {"telegram_id": 2, "registration_date": "2026-09-01 09:00:00", "status": "approved"},  # после трекинга
        ],
        reg_events=[(2, "start", None, None, "2026-08-30 23:25:00")],
    )
    with dash_db.read_conn(path) as conn:
        stages = dict(funnel(conn, Scope()))
    assert stages["Одобрено"] == 1  # только поздняя заявка


def test_funnel_status_stages_uncut_when_reg_events_empty(tmp_path):
    """Пустая reg_events -> отсечки нет вовсе, старые заявки по-прежнему считаются
    (страхует прежнее поведение до появления трекинга событий)."""
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "registration_date": "2020-01-01 09:00:00", "status": "approved"},
    ])
    with dash_db.read_conn(path) as conn:
        stages = dict(funnel(conn, Scope()))
    assert stages["Одобрено"] == 1


def test_funnel_tracking_since_not_narrowed_by_scope(tmp_path):
    """`funnel_tracking_since` — глобальная отсечка, без параметра scope вовсе: просмотр
    одного города должен резать статусные ступени по ОБЩЕМУ началу трекинга, а не по
    первому событию в этом самом городе (иначе город с поздним первым входом резал бы
    статусные ступени сильнее событийных и воронка снова врала бы, только в другую
    сторону)."""
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on"},
        reg_events=[
            (1, "start", "msk", None, "2026-08-01 10:00:00"),  # общее начало трекинга
            (2, "start", "spb", None, "2026-08-15 10:00:00"),  # первое событие СПб — позже
        ],
        users=[
            # Заявка СПб между общим началом трекинга и первым событием СПб: если бы
            # отсечка резалась по-городски, эта заявка выпала бы из "Одобрено" СПб.
            {"telegram_id": 3, "event_city": "spb", "registration_date": "2026-08-10 09:00:00", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        assert funnel_tracking_since(conn) == "2026-08-01 10:00:00"
        spb_stages = dict(funnel(conn, Scope(city="spb")))
    assert spb_stages["Одобрено"] == 1


def test_funnel_start_event_city_counts_only_for_matching_city_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on"},
        reg_events=[(1, "start", "spb", None, "2026-08-01 10:00:00")],
    )
    with dash_db.read_conn(path) as conn:
        spb_stages = dict(funnel(conn, Scope(city="spb")))
        msk_stages = dict(funnel(conn, Scope(city="msk")))
    assert spb_stages["Зашли"] == 1
    assert msk_stages["Зашли"] == 0


# ── registration_start (Phase 26.1 Plan 01, SD-03) ───────────────────────────────────────

def test_registration_start_scoped_by_season_differs_from_funnel_tracking_since(tmp_path):
    """База, прожившая два сезона: `funnel_tracking_since` держится за самый ранний ts
    вообще, `registration_start(scope)` — за старт КОНКРЕТНОГО сезона. На неравных датах
    подмена одной функции другой сдвинула бы всю ось «день N»."""
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"event_season": "YL26"},
        reg_events=[
            (1, "start", None, "RusCo25", "2025-09-01 10:00:00"),  # старый сезон — раньше
            (2, "start", None, "YL26", "2026-08-01 10:00:00"),      # текущий сезон
        ],
    )
    with dash_db.read_conn(path) as conn:
        global_since = funnel_tracking_since(conn)
        current_season_start = registration_start(conn, Scope())  # season=None -> текущий
        past_season_start = registration_start(conn, Scope(season="RusCo25"))
    assert global_since == "2025-09-01 10:00:00"
    assert current_season_start == "2026-08-01 10:00:00"
    assert past_season_start == "2025-09-01 10:00:00"
    assert current_season_start != global_since


def test_registration_start_empty_reg_events_returns_none(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert registration_start(conn, Scope()) is None


def test_registration_start_city_in_scope_does_not_narrow_result(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on"},
        reg_events=[(1, "start", "spb", None, "2026-08-01 10:00:00")],
    )
    with dash_db.read_conn(path) as conn:
        msk_scoped = registration_start(conn, Scope(city="msk"))
        spb_scoped = registration_start(conn, Scope(city="spb"))
    # Городская ось игнорируется -- оба скоупа видят одно и то же событие СПб.
    assert msk_scoped == spb_scoped == "2026-08-01 10:00:00"


# ── status_totals (Phase 26.1 Plan 01, SD-03) ────────────────────────────────────────────

def test_status_totals_always_has_three_keys_zero_on_empty_db(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        totals = status_totals(conn, Scope())
    assert totals == {"pending": 0, "approved": 0, "rejected": 0}


def test_status_totals_counts_by_status(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "status": "pending"},
        {"telegram_id": 2, "status": "pending"},
        {"telegram_id": 3, "status": "approved"},
        {"telegram_id": 4, "status": "rejected"},
    ])
    with dash_db.read_conn(path) as conn:
        totals = status_totals(conn, Scope())
    assert totals == {"pending": 2, "approved": 1, "rejected": 1}


def test_status_totals_scoped_by_city_and_season(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on", "event_season": "YL26"},
        users=[
            {"telegram_id": 1, "event_city": "msk", "season": "YL26", "status": "approved"},
            {"telegram_id": 2, "event_city": "spb", "season": "YL26", "status": "approved"},
            {"telegram_id": 3, "event_city": "msk", "season": "RusCo25", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        msk_current = status_totals(conn, Scope(city="msk"))
        spb_current = status_totals(conn, Scope(city="spb"))
        msk_past = status_totals(conn, Scope(city="msk", season="RusCo25"))
    assert msk_current["approved"] == 1
    assert spb_current["approved"] == 1
    assert msk_past["approved"] == 1


def test_status_totals_not_cut_by_funnel_tracking_since(tmp_path):
    """В отличие от `funnel()`'s «Одобрено», здесь НЕТ отсечки по началу трекинга событий —
    это итог по всей базе, а не окно воронки; числа МОГУТ законно разойтись с `funnel()`."""
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "registration_date": "2020-01-01 09:00:00", "status": "approved"},
        ],
        reg_events=[(2, "start", None, None, "2026-08-30 23:25:00")],
    )
    with dash_db.read_conn(path) as conn:
        totals = status_totals(conn, Scope())
        funnel_stages = dict(funnel(conn, Scope()))
    assert totals["approved"] == 1  # status_totals видит старую заявку
    assert funnel_stages["Одобрено"] == 0  # funnel() режет её по началу трекинга


# ── daily_registrations ─────────────────────────────────────────────────────────────────

def test_daily_registrations_groups_by_day_ascending(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "registration_date": "2026-08-01 10:00:00"},
        {"telegram_id": 2, "registration_date": "2026-08-01 12:00:00"},
        {"telegram_id": 3, "registration_date": "2026-08-02 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = daily_registrations(conn, Scope())
    assert rows[:2] == [("2026-08-01", 2), ("2026-08-02", 1)]
    # Хвост до сегодняшнего дня — нули (календарь плотный, см. ниже).
    assert all(cnt == 0 for _, cnt in rows[2:])
    assert rows[-1][0] == datetime.now().strftime("%Y-%m-%d")


def test_daily_registrations_fills_gaps_with_zero_days(tmp_path):
    """Дни без заявок между первым и последним — нулями, а не пропуском: иначе линия
    графика соединяет соседние «непустые» дни и скрывает провалы темпа."""
    path = _use_tmp_db(tmp_path)
    now = datetime.now()
    d0 = (now - timedelta(days=4)).strftime("%Y-%m-%d")
    d4 = now.strftime("%Y-%m-%d")
    _seed(users=[
        {"telegram_id": 1, "registration_date": f"{d0} 10:00:00"},
        {"telegram_id": 2, "registration_date": f"{d4} 09:00:00"},
        {"telegram_id": 3, "registration_date": f"{d4} 11:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = daily_registrations(conn, Scope())
    assert len(rows) == 5
    assert rows[0] == (d0, 1)
    assert rows[1:4] == [
        ((now - timedelta(days=k)).strftime("%Y-%m-%d"), 0) for k in (3, 2, 1)
    ]
    assert rows[4] == (d4, 2)
    # Пустая выборка — пустой список, без «календаря из нулей».
    with dash_db.read_conn(path) as conn:
        assert daily_registrations(conn, Scope(season="NoSuchSeason")) == []


# ── dropout_steps ────────────────────────────────────────────────────────────────────────

def test_dropout_steps_uses_human_labels_not_raw_codes(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(reg_started=[
        {"telegram_id": 1, "username": "a", "started_at": "2026-08-01 10:00:00", "last_step": "university"},
        {"telegram_id": 2, "username": "b", "started_at": "2026-08-01 10:00:00", "last_step": None},
        {"telegram_id": 3, "username": "c", "started_at": "2026-08-01 10:00:00", "last_step": "consent:v1"},
    ])
    with dash_db.read_conn(path) as conn:
        steps = dropout_steps(conn, Scope())
    labels = [label for label, _ in steps]
    assert "university" not in labels
    assert "consent:v1" not in labels
    assert "ВУЗ" in labels
    assert "до первого вопроса" in labels
    assert "Согласие" in labels


def test_dropout_steps_excludes_registered_users(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[{"telegram_id": 1, "status": "approved"}],
        reg_started=[
            {"telegram_id": 1, "username": "a", "started_at": "2026-08-01 10:00:00", "last_step": "age"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        steps = dropout_steps(conn, Scope())
    assert steps == []


# ── season_options / city_options / dashboard_flags ─────────────────────────────────────

def test_dashboard_flags_uses_defaults_when_bot_settings_empty(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        flags = dashboard_flags(conn)
    assert flags["dashboard_block_funnel"] == "on"
    assert flags["dashboard_block_game"] == "off"
    assert flags["payment_enabled"] == "off"
    assert flags["event_city_enabled"] == "off"


def test_dashboard_flags_defaults_match_settings_schema_no_drift():
    for key, default in _SETTING_DEFAULTS.items():
        assert SETTINGS_SCHEMA[key]["default"] == default, (
            f"{key}: dashboard default {default!r} != SETTINGS_SCHEMA default "
            f"{SETTINGS_SCHEMA[key]['default']!r}"
        )


def test_season_options_current_first_then_others(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"event_season": "YL26"},
        users=[
            {"telegram_id": 1, "season": "RusCo25"},
            {"telegram_id": 2, "season": "YL26"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        options = season_options(conn)
    assert options[0] == {"value": "YL26", "label": "YL26", "current": True}
    values = [o["value"] for o in options]
    assert "RusCo25" in values
    assert values.count("YL26") == 1  # текущий сезон не задублирован


def test_city_options_empty_when_module_off_then_lists_enabled(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert city_options(conn) == []

    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 0, 1)],
        settings={"event_city_enabled": "on"},
    )
    with dash_db.read_conn(path) as conn:
        options = city_options(conn)
    assert options == [{"code": "msk", "label": "Москва"}]  # spb выключен (enabled=0)


# ── breakdown (T-15-03-02) ───────────────────────────────────────────────────────────────

def test_breakdown_rejects_unknown_column():
    class _FakeConn:  # breakdown must raise before touching the connection at all
        def execute(self, *a, **k):
            raise AssertionError("must not query the DB for an unknown column")

    try:
        breakdown(_FakeConn(), "full_name", scope=Scope())
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a column outside ALLOWED_BREAKDOWNS")


def test_breakdown_counts_by_allowed_column_and_respects_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on"},
        users=[
            {"telegram_id": 1, "source": "vk", "event_city": "msk"},
            {"telegram_id": 2, "source": "vk", "event_city": "msk"},
            {"telegram_id": 3, "source": "instagram", "event_city": "spb"},
            {"telegram_id": 4, "source": None, "event_city": "spb"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        by_source = dict(breakdown(conn, "source", scope=Scope()))
        by_source_spb = dict(breakdown(conn, "source", scope=Scope(city="spb")))
    assert by_source == {"vk": 2, "instagram": 1}  # NULL отброшен, не превращён в 0
    assert by_source_spb == {"instagram": 1}


def test_breakdown_payment_option_gated_by_payment_enabled(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[{"telegram_id": 1, "payment_option": "full"}])
    with dash_db.read_conn(path) as conn:
        assert breakdown(conn, "payment_option", scope=Scope()) == []

    _seed(settings={"payment_enabled": "on"})
    with dash_db.read_conn(path) as conn:
        assert breakdown(conn, "payment_option", scope=Scope()) == [("full", 1)]


def test_breakdown_event_city_gated_by_event_city_enabled(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(cities=[("msk", "Москва", 1, 0)], users=[{"telegram_id": 1, "event_city": "msk"}])
    with dash_db.read_conn(path) as conn:
        assert breakdown(conn, "event_city", scope=Scope()) == []

    _seed(settings={"event_city_enabled": "on"})
    with dash_db.read_conn(path) as conn:
        assert breakdown(conn, "event_city", scope=Scope()) == [("msk", 1)]


def test_allowed_breakdowns_is_the_only_gate_for_column_names():
    for column in ALLOWED_BREAKDOWNS:
        assert isinstance(column, str) and column.isidentifier()


# ── city_comparison (D-10/D-15) ──────────────────────────────────────────────────────────

def test_city_comparison_one_row_per_city_null_folds_into_default(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "event_city": None, "status": "pending"},
            {"telegram_id": 2, "event_city": "spb", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        rows = {row["code"]: row for row in city_comparison(conn, Scope())}
    assert rows["msk"]["total"] == 1
    assert rows["msk"]["pending"] == 1
    assert rows["spb"]["total"] == 1
    assert rows["spb"]["approved"] == 1


# ── utm_table (квик 260905-qqg) ──────────────────────────────────────────────────────────

def _tag_event(telegram_id, event, ts, source_tag, event_city=None, season=None):
    return {
        "telegram_id": telegram_id, "event": event, "event_city": event_city,
        "season": season, "ts": ts, "source_tag": source_tag,
    }


def test_utm_table_counts_per_tag_with_conversion(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        reg_events=[
            _tag_event(1, "start", "2026-08-01 10:00:00", "vk_post_1"),
            _tag_event(1, "form_started", "2026-08-01 10:01:00", "vk_post_1"),
        ],
        users=[
            {"telegram_id": 1, "source": "vk_post_1", "status": "approved",
             "registration_date": "2026-08-01 10:02:00"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        rows = {row["tag"]: row for row in utm_table(conn, Scope())}
    row = rows["vk_post_1"]
    assert row["starts"] == 1
    assert row["form_started"] == 1
    assert row["completed"] == 1
    assert row["approved"] == 1
    assert row["conversion"] == 100.0


def test_utm_table_repeat_start_is_deduped_by_telegram_id(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(reg_events=[
        _tag_event(1, "start", "2026-08-01 10:00:00", "vk_post_1"),
        _tag_event(1, "start", "2026-08-01 10:05:00", "vk_post_1"),  # повторный /start
    ])
    with dash_db.read_conn(path) as conn:
        rows = {row["tag"]: row for row in utm_table(conn, Scope())}
    assert rows["vk_post_1"]["starts"] == 1


def test_utm_table_narrowed_by_city_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on"},
        reg_events=[
            _tag_event(1, "start", "2026-08-01 10:00:00", "vk_post_1", event_city="spb"),
            _tag_event(2, "start", "2026-08-01 10:00:00", "vk_post_1", event_city="msk"),
        ],
    )
    with dash_db.read_conn(path) as conn:
        spb_rows = {row["tag"]: row for row in utm_table(conn, Scope(city="spb"))}
        msk_rows = {row["tag"]: row for row in utm_table(conn, Scope(city="msk"))}
    assert spb_rows["vk_post_1"]["starts"] == 1
    assert msk_rows["vk_post_1"]["starts"] == 1


def test_utm_table_sorted_by_starts_desc_then_tag_asc_and_limited(tmp_path):
    path = _use_tmp_db(tmp_path)
    reg_events = [_tag_event(1, "start", "2026-08-01 10:00:00", "b_tag")]
    reg_events += [
        _tag_event(100 + i, "start", "2026-08-01 10:00:00", "a_tag") for i in range(2)
    ]
    reg_events += [_tag_event(200, "start", "2026-08-01 10:00:00", "c_tag")]
    _seed(reg_events=reg_events)
    with dash_db.read_conn(path) as conn:
        rows = utm_table(conn, Scope())
    tags = [row["tag"] for row in rows]
    assert tags == ["a_tag", "b_tag", "c_tag"]  # a_tag (2 starts) выше, дальше по алфавиту


def test_utm_table_conversion_none_when_starts_zero(tmp_path):
    path = _use_tmp_db(tmp_path)
    # source_tag есть только на form_started -- событие start с этой меткой отсутствует.
    _seed(reg_events=[_tag_event(1, "form_started", "2026-08-01 10:00:00", "orphan_tag")])
    with dash_db.read_conn(path) as conn:
        rows = {row["tag"]: row for row in utm_table(conn, Scope())}
    assert rows["orphan_tag"]["starts"] == 0
    assert rows["orphan_tag"]["conversion"] is None


def test_utm_table_completed_not_cut_by_tracking_since(tmp_path):
    """Квик 260906-dmq: отсечка по `funnel_tracking_since` снята — заявка старше начала
    трекинга событий (05.09 22:57 UTC на проде) больше не отсекается из `completed`. Раньше
    эта отсечка обнуляла низ воронки заодно с верхом: у меток старше начала трекинга не было
    ни одного события, а отсечка вдобавок съедала и заявки — строка оставалась пустой во ВСЕХ
    колонках, хотя `users.source` честно говорит, что заявки были."""
    path = _use_tmp_db(tmp_path)
    _seed(
        reg_events=[_tag_event(2, "start", "2026-08-30 23:25:00", "vk_post_1")],
        users=[
            {"telegram_id": 1, "source": "vk_post_1", "status": "approved",
             "registration_date": "2026-01-01 09:00:00"},  # до начала трекинга событий
            {"telegram_id": 2, "source": "vk_post_1", "status": "approved",
             "registration_date": "2026-09-01 09:00:00"},  # после начала трекинга событий
        ],
    )
    with dash_db.read_conn(path) as conn:
        rows = {row["tag"]: row for row in utm_table(conn, Scope())}
    assert rows["vk_post_1"]["completed"] == 2  # обе заявки, отсечки больше нет


def test_utm_table_ignores_empty_or_null_source_tag(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(reg_events=[
        _tag_event(1, "start", "2026-08-01 10:00:00", None),
        _tag_event(2, "start", "2026-08-01 10:00:00", ""),
    ])
    with dash_db.read_conn(path) as conn:
        rows = utm_table(conn, Scope())
    assert rows == []


def test_utm_table_tag_present_only_in_users_source(tmp_path):
    """Метка живёт только в `users.source` (событий с этой меткой нет вовсе, например —
    заявка старше начала трекинга событий) -- строка всё равно есть: `starts`/`form_started`
    нулевые, `completed` -- число заявок, `conversion` -- `None` (нет `starts`, делить не на
    что)."""
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "old_slug", "status": "approved",
         "registration_date": "2026-01-01 09:00:00"},
        {"telegram_id": 2, "source": "old_slug", "status": "pending",
         "registration_date": "2026-01-02 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = {row["tag"]: row for row in utm_table(conn, Scope())}
    row = rows["old_slug"]
    assert row["starts"] == 0
    assert row["form_started"] == 0
    assert row["completed"] == 2
    assert row["approved"] == 1
    assert row["conversion"] is None


def test_utm_table_cyrillic_manual_answer_is_not_a_tag(tmp_path):
    """Кириллический ручной ответ на вопрос «Источник» («ВК») при `source_from_tag = 0`
    (дефолт) меткой не считается -- в таблице такой строки нет вовсе."""
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "ВК", "status": "approved",
         "registration_date": "2026-08-01 09:00:00", "source_from_tag": 0},
    ])
    with dash_db.read_conn(path) as conn:
        rows = utm_table(conn, Scope())
    assert rows == []


def test_utm_table_source_from_tag_flag_overrides_cyrillic_heuristic(tmp_path):
    """`source_from_tag = 1` перекрывает эвристику латиницы -- метка считается, даже если
    текст в `users.source` кириллический."""
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "ВК", "status": "approved",
         "registration_date": "2026-08-01 09:00:00", "source_from_tag": 1},
    ])
    with dash_db.read_conn(path) as conn:
        rows = {row["tag"]: row for row in utm_table(conn, Scope())}
    assert rows["ВК"]["completed"] == 1


def test_utm_table_dash_and_blank_source_are_not_tags(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "-", "status": "approved",
         "registration_date": "2026-08-01 09:00:00"},
        {"telegram_id": 2, "source": "", "status": "approved",
         "registration_date": "2026-08-01 09:00:00"},
        {"telegram_id": 3, "source": None, "status": "approved",
         "registration_date": "2026-08-01 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = utm_table(conn, Scope())
    assert rows == []


def test_utm_table_sorted_by_completed_desc_first(tmp_path):
    """Новая сортировка: `completed` desc идёт ПЕРЕД `starts` desc -- метка с меньшим числом
    `starts`, но большим числом заявок, оказывается выше."""
    path = _use_tmp_db(tmp_path)
    _seed(
        reg_events=[
            _tag_event(1, "start", "2026-08-01 10:00:00", "many_starts"),
            _tag_event(2, "start", "2026-08-01 10:00:00", "many_starts"),
            _tag_event(3, "start", "2026-08-01 10:00:00", "few_starts"),
        ],
        users=[
            {"telegram_id": 10, "source": "few_starts", "status": "approved",
             "registration_date": "2026-08-01 09:00:00"},
            {"telegram_id": 11, "source": "few_starts", "status": "approved",
             "registration_date": "2026-08-01 09:00:00"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        rows = utm_table(conn, Scope())
    tags = [row["tag"] for row in rows]
    assert tags[0] == "few_starts"  # completed=2 против completed=0 у many_starts


# ── monthly_table (квик 260906-dmq, задача 2) ────────────────────────────────────────────

def test_monthly_table_empty_db_returns_empty_list(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert monthly_table(conn, Scope()) == []


def test_monthly_table_two_months_fresh_first(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "vk", "status": "approved",
         "registration_date": "2026-08-01 09:00:00"},
        {"telegram_id": 2, "source": "vk", "status": "pending",
         "registration_date": "2026-09-01 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = monthly_table(conn, Scope())
    keys = [row["month_key"] for row in rows]
    assert keys == ["2026-09", "2026-08"]  # свежий месяц первым


def test_monthly_table_human_month_label(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "vk", "status": "approved",
         "registration_date": "2026-09-05 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = monthly_table(conn, Scope())
    assert rows[0]["month"] == "Сентябрь 2026"


def test_monthly_table_total_and_approved(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "vk", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 2, "source": "vk", "status": "pending",
         "registration_date": "2026-09-02 09:00:00"},
        {"telegram_id": 3, "source": "vk", "status": "rejected",
         "registration_date": "2026-09-03 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = monthly_table(conn, Scope())
    assert rows[0]["total"] == 3
    assert rows[0]["approved"] == 1


def test_monthly_table_top_sources_and_top_tags_capped_at_three(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "a_tag", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 2, "source": "a_tag", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 3, "source": "b_tag", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 4, "source": "c_tag", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 5, "source": "d_tag", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = monthly_table(conn, Scope())
    row = rows[0]
    assert len(row["top_sources"]) == 3
    assert row["top_sources"][0] == ("a_tag", 2)
    assert len(row["top_tags"]) == 3
    assert row["top_tags"][0] == ("a_tag", 2)


def test_monthly_table_top_sources_excludes_dash_and_blank(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "-", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 2, "source": "", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 3, "source": None, "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
        {"telegram_id": 4, "source": "vk", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = monthly_table(conn, Scope())
    assert rows[0]["top_sources"] == [("vk", 1)]


def test_monthly_table_top_tags_excludes_cyrillic_manual_answer(tmp_path):
    """`top_tags` использует предикат метки из задачи 1 -- кириллический ручной ответ на
    вопрос «Источник» в `top_tags` не попадает, а в `top_sources` (без предиката метки) —
    попадает наравне со слагами."""
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "ВК", "status": "approved",
         "registration_date": "2026-09-01 09:00:00"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = monthly_table(conn, Scope())
    assert rows[0]["top_sources"] == [("ВК", 1)]
    assert rows[0]["top_tags"] == []


def test_monthly_table_narrowed_by_city_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on"},
        users=[
            {"telegram_id": 1, "source": "vk", "status": "approved",
             "registration_date": "2026-09-01 09:00:00", "event_city": "spb"},
            {"telegram_id": 2, "source": "vk", "status": "approved",
             "registration_date": "2026-09-01 09:00:00", "event_city": "msk"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        spb_rows = monthly_table(conn, Scope(city="spb"))
        msk_rows = monthly_table(conn, Scope(city="msk"))
    assert spb_rows[0]["total"] == 1
    assert msk_rows[0]["total"] == 1


def test_monthly_table_broken_registration_date_does_not_crash(tmp_path):
    """Непустой, но не парсящийся `registration_date` -- строка создаётся (фильтр режет
    только `NULL`/пустое), подпись месяца отдаётся как есть, страница не падает."""
    path = _use_tmp_db(tmp_path)
    _seed(users=[
        {"telegram_id": 1, "source": "vk", "status": "approved",
         "registration_date": "не дата"},
    ])
    with dash_db.read_conn(path) as conn:
        rows = monthly_table(conn, Scope())
    assert len(rows) == 1
    assert rows[0]["month_key"] == "не дата"[:7]
    assert rows[0]["month"] == rows[0]["month_key"]


# ── game_block (D-12) ────────────────────────────────────────────────────────────────────

def test_game_block_none_when_toggle_off(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(game_tasks=[{
        "id": 1, "text": "t", "category": "photo", "coins": 10, "proof_type": "photo",
        "deadline_at": "2026-09-01 00:00:00", "created_at": "2026-08-01 00:00:00",
    }], game_submissions=[{
        "task_id": 1, "user_id": 1, "content_type": "photo", "content": "file123",
        "submitted_at": "2026-08-02 00:00:00", "status": "approved",
    }])
    with dash_db.read_conn(path) as conn:
        assert game_block(conn, Scope()) is None  # dashboard_block_game default "off"


def test_game_block_none_when_toggle_on_but_no_submissions(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(settings={"dashboard_block_game": "on"})
    with dash_db.read_conn(path) as conn:
        assert game_block(conn, Scope()) is None


def test_game_block_matches_get_game_stats_on_same_fixture(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on"},
        game_tasks=[
            {"id": 1, "text": "t1", "category": "photo", "coins": 10, "proof_type": "photo",
             "deadline_at": "2026-09-01 00:00:00", "created_at": "2026-08-01 00:00:00"},
            {"id": 2, "text": "t2", "category": "video", "coins": 20, "proof_type": "video",
             "deadline_at": "2026-09-01 00:00:00", "created_at": "2026-08-01 00:00:00"},
        ],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
            {"task_id": 2, "user_id": 1, "content_type": "video", "content": "b",
             "submitted_at": "2026-08-02 00:00:00", "status": "pending"},
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "c",
             "submitted_at": "2026-08-02 00:00:00", "status": "rejected"},
        ],
    )
    reference = asyncio.run(bot_db.get_game_stats())
    with dash_db.read_conn(path) as conn:
        dashboard_stats = game_block(conn, Scope())
    assert dashboard_stats == reference


# ── T-15-03-03 (D-17): нет ПД в исходнике модуля ─────────────────────────────────────────

_PII_TOKENS = ("full_name", "phone", "email", "vk_username", "resume")


def test_queries_module_never_selects_pii_columns():
    """Сканирует ВЕСЬ файл, КРОМЕ `_STEP_LABELS`/`_step_label` — те намеренно оперируют
    step_key дропаута анкеты («email», «phone», «resume», «full_name» — это ключи словаря
    подписей и специальный step_key, а не колонки в SELECT), см. модульный докстринг
    `dashboard/queries.py`."""
    text = DASHBOARD_QUERIES_FILE.read_text(encoding="utf-8")
    start = text.index("_STEP_LABELS = {")
    end = text.index("\ndef dropout_steps", start)
    scanned = text[:start] + text[end:]
    offenders = [token for token in _PII_TOKENS if token in scanned]
    assert not offenders, f"dashboard/queries.py must never touch PII columns: {offenders}"


# ── T-15-03-02: значения только через ?-параметры ────────────────────────────────────────

def test_queries_module_never_string_formats_scope_values_into_sql():
    text = DASHBOARD_QUERIES_FILE.read_text(encoding="utf-8")
    assert "scope.city}" not in text
    assert "scope.season}" not in text
    assert ".format(" not in text
    assert '% (' not in text and "%s" not in text
