"""Phase 26.1 Plan 01 (SD-02/SD-03/SD-04): сторожа мульти-чтения — изоляция баз, ось «день
N», недоступная база, кэш.

Каждый тест строит N НЕЗАВИСИМЫХ файлов БД (тот же приём, что
`tests/test_dashboard_render.py::_seed_full_fixture`, параметризованный на N файлов вместо
одного): `bot_config.DB_PATH` переставляется перед каждым `init_db()`/сидингом, дашборд
читает уже ПОСЛЕ того как aiosqlite-подключение с записью закрыто.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from config import config as bot_config
from database import db as bot_db

from dashboard import compare
from dashboard.config import DashboardConfig
from dashboard.registry import EventSource

COMPARE_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "compare.py"


def _use_tmp_db(tmp_path, name: str) -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(*, cities=None, settings=None, users=None, reg_events=None):
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
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO reg_events ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


def _make_event_db(tmp_path, name, **seed_kwargs) -> str:
    path = _use_tmp_db(tmp_path, name)
    if seed_kwargs:
        _seed(**seed_kwargs)
    return path


def _cfg(events) -> DashboardConfig:
    return DashboardConfig(
        db_path="data/forum.db",
        public_url="https://stats.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token="123:abc",
        admin_ids=(1,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
        events=events,
    )


@pytest.fixture(autouse=True)
def _reset_compare_cache():
    compare.reset_cache()
    yield
    compare.reset_cache()


def _users_row(tid, *, registration_date, status="approved", **overrides):
    row = {"telegram_id": tid, "registration_date": registration_date, "status": status}
    row.update(overrides)
    return row


# ── греп-сторож: ни одного ATTACH ────────────────────────────────────────────────────────

def test_no_attach_database_in_compare_module():
    text = COMPARE_FILE.read_text(encoding="utf-8")
    assert "ATTACH" not in text


# ── изоляция баз ──────────────────────────────────────────────────────────────────────────

def test_isolation_two_events_do_not_mix_numbers(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_name": "Юлид"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00")],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={"event_name": "РилТолк"},
        users=[
            _users_row(1, registration_date="2026-08-01 10:00:00"),
            _users_row(2, registration_date="2026-08-02 10:00:00"),
        ],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    ctx = compare.build_compare_context(cfg)
    by_code = {e["code"]: e for e in ctx["events"]}
    assert by_code["a"]["kpi"]["total"] == 1
    assert by_code["b"]["kpi"]["total"] == 2
    assert by_code["a"]["name"] == "Юлид"
    assert by_code["b"]["name"] == "РилТолк"


# ── недоступная база ─────────────────────────────────────────────────────────────────────

def test_unavailable_event_does_not_break_page_context(tmp_path):
    path_ok = _make_event_db(
        tmp_path, "ok.db",
        users=[_users_row(1, registration_date="2026-08-01 10:00:00")],
    )
    bad_path = str(tmp_path / "does_not_exist" / "forum.db")
    cfg = _cfg((
        EventSource(code="ok", db_path=path_ok),
        EventSource(code="bad", db_path=bad_path),
    ))
    ctx = compare.build_compare_context(cfg)
    by_code = {e["code"]: e for e in ctx["events"]}
    assert by_code["ok"]["available"] is True
    assert by_code["ok"]["kpi"]["total"] == 1
    assert by_code["bad"]["available"] is False
    assert by_code["bad"]["error"]
    assert {"code": "bad", "name": "bad", "error": by_code["bad"]["error"]} in ctx["unavailable"]
    assert ctx["dynamics"]["labels"] is not None  # страница-контекст собрался целиком


# ── ось «день N»: разные календарные даты старта совпадают по дню 1 ─────────────────────

def test_day_n_axis_aligns_peaks_across_different_calendar_starts(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        reg_events=[
            {"telegram_id": 1, "event": "start", "ts": "2026-08-01 09:00:00"},
        ],
        users=[
            _users_row(1, registration_date="2026-08-01 10:00:00"),
            _users_row(2, registration_date="2026-08-02 10:00:00"),
            _users_row(3, registration_date="2026-08-02 11:00:00"),
            _users_row(4, registration_date="2026-08-02 12:00:00"),
        ],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        reg_events=[
            {"telegram_id": 1, "event": "start", "ts": "2026-09-01 09:00:00"},
        ],
        users=[
            _users_row(1, registration_date="2026-09-01 10:00:00"),
            _users_row(2, registration_date="2026-09-02 10:00:00"),
            _users_row(3, registration_date="2026-09-02 11:00:00"),
            _users_row(4, registration_date="2026-09-02 12:00:00"),
        ],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    now = datetime(2026, 9, 10)
    ctx = compare.build_compare_context(cfg, axis="day_n", now=now)
    dynamics = ctx["dynamics"]
    assert dynamics["axis"] == "day_n"
    assert dynamics["labels"][0] == 0
    series_by_code = {s["code"]: s for s in dynamics["series"]}
    # День 0 -- один заход (первый день), день 1 -- пик по три захода у ОБОИХ событий,
    # несмотря на разные календарные даты.
    assert series_by_code["a"]["values"][0] == 1
    assert series_by_code["a"]["values"][1] == 3
    assert series_by_code["b"]["values"][0] == 1
    assert series_by_code["b"]["values"][1] == 3


def test_calendar_axis_gives_calendar_date_labels(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        users=[_users_row(1, registration_date="2026-08-01 10:00:00")],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        users=[_users_row(1, registration_date="2026-08-03 10:00:00")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    ctx = compare.build_compare_context(cfg, axis="calendar")
    dynamics = ctx["dynamics"]
    assert dynamics["axis"] == "calendar"
    # `daily_registrations` уплотняет календарь до СЕГОДНЯ (реальные часы, не `now`
    # супердашборда) — конец диапазона не фиксирован, проверяем начало и обе даты по индексу.
    assert dynamics["labels"][0] == "2026-08-01"
    idx_01 = dynamics["labels"].index("2026-08-01")
    idx_02 = dynamics["labels"].index("2026-08-02")
    idx_03 = dynamics["labels"].index("2026-08-03")
    series_by_code = {s["code"]: s for s in dynamics["series"]}
    assert series_by_code["a"]["values"][idx_01] == 1
    assert series_by_code["a"]["values"][idx_02] == 0
    assert series_by_code["a"]["values"][idx_03] == 0
    assert series_by_code["b"]["values"][idx_01] == 0
    assert series_by_code["b"]["values"][idx_03] == 1


# ── тумблер оплаты: «Оплатили» None у одного события, число у другого ───────────────────

def test_funnel_table_payment_toggle_none_for_disabled_event(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"payment_enabled": "off"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", payment_status="paid")],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={"payment_enabled": "on"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", payment_status="paid")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    ctx = compare.build_compare_context(cfg)
    assert "Оплатили" in ctx["funnel_labels"]
    row = next(r for r in ctx["funnel_table"] if r["label"] == "Оплатили")
    assert row["cells"]["a"] is None
    assert row["reason"]
    assert row["cells"]["b"]["count"] == 1


# ── разрез, погашенный тумблером у одного события ────────────────────────────────────────

def test_cuts_city_disabled_for_one_event_none_plus_reason(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        cities=[("msk", "Москва", 1, 0)],
        settings={"event_city_enabled": "off"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", event_city="msk")],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={"event_city_enabled": "on"},
        users=[
            _users_row(1, registration_date="2026-08-01 10:00:00", event_city="msk"),
            _users_row(2, registration_date="2026-08-01 11:00:00", event_city="spb"),
        ],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    ctx = compare.build_compare_context(cfg)
    city_cut = next(c for c in ctx["cuts"] if c["column"] == "event_city")
    assert city_cut["cells"]["a"] is None
    assert city_cut["reasons"]["a"]
    assert city_cut["cells"]["b"]["msk"]["count"] == 1
    assert city_cut["cells"]["b"]["spb"]["count"] == 1
    # Union значений — оба города видны в общем списке.
    assert set(city_cut["values"]) == {"msk", "spb"}


def test_cuts_disabled_for_all_events_is_omitted(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_city_enabled": "off"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00")],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={"event_city_enabled": "off"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    ctx = compare.build_compare_context(cfg)
    assert all(c["column"] != "event_city" for c in ctx["cuts"])


def test_cuts_source_union_of_values(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", source="ВК")],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", source="Инстаграм")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    ctx = compare.build_compare_context(cfg)
    source_cut = next(c for c in ctx["cuts"] if c["column"] == "source")
    assert set(source_cut["values"]) == {"ВК", "Инстаграм"}
    assert source_cut["cells"]["a"]["ВК"]["count"] == 1
    assert "Инстаграм" not in source_cut["cells"]["a"]
    assert source_cut["cells"]["b"]["Инстаграм"]["count"] == 1


# ── seasons: переключатель внутри события, мусорный сезон откатывается ──────────────────

def test_seasons_switch_affects_only_that_event(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_season": "YL26"},
        users=[
            _users_row(1, registration_date="2026-08-01 10:00:00", season="YL26"),
            _users_row(2, registration_date="2025-08-01 10:00:00", season="YL25"),
        ],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={"event_season": "RT26"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", season="RT26")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))

    ctx_current = compare.build_compare_context(cfg)
    by_code = {e["code"]: e for e in ctx_current["events"]}
    assert by_code["a"]["kpi"]["total"] == 1  # текущий сезон YL26
    assert by_code["b"]["kpi"]["total"] == 1

    compare.reset_cache()
    ctx_switched = compare.build_compare_context(cfg, seasons={"a": "YL25"})
    by_code_switched = {e["code"]: e for e in ctx_switched["events"]}
    assert by_code_switched["a"]["kpi"]["total"] == 1  # сместилось на прошлый сезон события a
    assert by_code_switched["a"]["season"] == "YL25"
    assert by_code_switched["b"]["kpi"]["total"] == 1  # b не задет переключателем a
    assert by_code_switched["b"]["season"] == "RT26"


def test_seasons_garbage_value_falls_back_to_current(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_season": "YL26"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", season="YL26")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_a)))
    ctx = compare.build_compare_context(cfg, seasons={"a": "НетТакогоСезона"})
    by_code = {e["code"]: e for e in ctx["events"]}
    assert by_code["a"]["season"] == "YL26"


# ── акцент серии = акцент пресета своего события ────────────────────────────────────────

def test_series_accent_matches_own_event_preset(tmp_path):
    path_a = _make_event_db(tmp_path, "a.db")  # без пресета -> дефолт bluebook
    path_b = _make_event_db(tmp_path, "b.db", settings={"miniapp_theme_preset": "realtalk"})
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    ctx = compare.build_compare_context(cfg)
    by_code = {e["code"]: e for e in ctx["events"]}
    assert by_code["a"]["accent"] == "#037EF3"
    assert by_code["b"]["accent"] == "#7552CC"
    series_by_code = {s["code"]: s for s in ctx["dynamics"]["series"]}
    assert series_by_code["a"]["accent"] == "#037EF3"
    assert series_by_code["b"]["accent"] == "#7552CC"


# ── TTL-кэш ───────────────────────────────────────────────────────────────────────────────

def test_cache_hit_within_ttl_does_not_reopen_connections(tmp_path, monkeypatch):
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))

    now = datetime(2026, 9, 6, 12, 0, 0)
    first = compare.build_compare_context(cfg, now=now)

    calls = []
    real_read_conn = compare.read_conn

    def _spy(path):
        calls.append(path)
        return real_read_conn(path)

    monkeypatch.setattr(compare, "read_conn", _spy)

    second = compare.build_compare_context(cfg, now=now)
    assert second is first  # тот же объект из кэша
    assert calls == []  # ни одного нового подключения


def test_cache_reset_forces_reread(tmp_path, monkeypatch):
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_a)))
    now = datetime(2026, 9, 6, 12, 0, 0)
    compare.build_compare_context(cfg, now=now)

    calls = []
    real_read_conn = compare.read_conn

    def _spy(path):
        calls.append(path)
        return real_read_conn(path)

    monkeypatch.setattr(compare, "read_conn", _spy)
    compare.reset_cache()
    compare.build_compare_context(cfg, now=now)
    assert len(calls) == 2  # оба события перечитаны заново


def test_cache_expires_after_ttl(tmp_path, monkeypatch):
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_a)))
    now = datetime(2026, 9, 6, 12, 0, 0)
    compare.build_compare_context(cfg, now=now)

    calls = []
    real_read_conn = compare.read_conn

    def _spy(path):
        calls.append(path)
        return real_read_conn(path)

    monkeypatch.setattr(compare, "read_conn", _spy)
    later = datetime(2026, 9, 6, 12, 1, 1)  # +61 с
    compare.build_compare_context(cfg, now=later)
    assert len(calls) == 2


# ── orchestrator-ревью плана 26.1-01, находка 1: кэш ограничен по размеру ────────────────

def test_cache_key_from_unvalidated_garbage_seasons_does_not_grow_without_bound(tmp_path):
    """`?seasons=<код>:<мусор>` — значение сезона не валидируется до попадания в ключ кэша
    (валидация — внутри `build_compare_context`, ключ строится раньше). Без потолка каждое
    новое мусорное значение растило бы `_CACHE` навсегда."""
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    now = datetime(2026, 9, 6, 12, 0, 0)

    for i in range(compare._CACHE_MAX_ENTRIES + 20):
        compare.build_compare_context(cfg, seasons={"a": f"garbage-season-{i}"}, now=now)

    assert len(compare._CACHE) <= compare._CACHE_MAX_ENTRIES
