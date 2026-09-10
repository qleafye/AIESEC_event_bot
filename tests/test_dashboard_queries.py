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
    _QUESTION_STATUS_CASE,
    _SETTING_DEFAULTS,
    _task_title,
    breakdown,
    city_comparison,
    city_options,
    daily_registrations,
    dashboard_flags,
    dropout_steps,
    format_processing_time,
    funnel,
    funnel_tracking_since,
    game_block,
    kpi_row,
    monthly_table,
    questions_block,
    registration_start,
    season_options,
    status_totals,
    utm_table,
)
from services.questions import question_status

DASHBOARD_QUERIES_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "queries.py"


def _use_tmp_db(tmp_path, name="dashboard_queries.db") -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(
    cities=None, settings=None, users=None, reg_events=None, reg_started=None,
    game_tasks=None, game_submissions=None, application_decisions=None, coins=None,
    delegate_questions=None,
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
        for row in application_decisions or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO application_decisions ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        for row in coins or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO coins ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        for row in delegate_questions or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO delegate_questions ({cols}) VALUES ({placeholders})",
                tuple(row.values()),
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
        "processing_avg_minutes": None, "processing_avg_label": "—",
        "game_review_avg_minutes": None, "game_review_avg_label": "—",
        "question_answer_avg_minutes": None, "question_answer_avg_label": "—",
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


# ── format_processing_time / _avg_processing_minutes (квик 260908-dbo) ──────────────────

def test_format_processing_time_boundaries():
    cases = [
        (None, "—"),
        (0, "меньше минуты"),
        (0.4, "меньше минуты"),
        (45, "45 мин"),
        (59.6, "60 мин"),  # округление до целых минут ДО выбора формы (мин, не «1 ч»)
        (120, "2 ч"),
        (135, "2 ч 15 мин"),
        (1440, "1 д"),
        (1680, "1 д 4 ч"),
        (-5, "—"),  # битые данные не показываем как «минус два часа»
    ]
    for minutes, expected in cases:
        assert format_processing_time(minutes) == expected, minutes


def _decision(telegram_id, decided_at, effects_due_at, decision="approve", undone_at=None, decided_by=999):
    return {
        "telegram_id": telegram_id, "decision": decision, "decided_by": decided_by,
        "decided_at": decided_at, "effects_due_at": effects_due_at, "undone_at": undone_at,
    }


def test_kpi_row_processing_avg_ignores_delegate_without_decision(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "registration_date": "2026-08-01 10:00:00"},  # +120 мин
            {"telegram_id": 2, "registration_date": "2026-08-01 10:00:00"},  # +30 мин
            {"telegram_id": 3, "registration_date": "2026-08-01 10:00:00"},  # без решения
        ],
        application_decisions=[
            _decision(1, "2026-08-01 12:00:00", "2026-08-01 12:05:00"),
            _decision(2, "2026-08-01 10:30:00", "2026-08-01 10:35:00"),
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["processing_avg_minutes"] == 75.0
    assert row["processing_avg_label"] == "1 ч 15 мин"


def test_kpi_row_processing_avg_undone_later_decision_falls_back_to_earlier(tmp_path):
    """У делегата есть более позднее решение с `undone_at` не NULL -- в среднее идёт
    более раннее не-отменённое, а не отменённое позднее."""
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[{"telegram_id": 1, "registration_date": "2026-08-01 10:00:00"}],
        application_decisions=[
            _decision(1, "2026-08-01 10:30:00", "2026-08-01 10:35:00"),  # раннее, не отменено
            _decision(1, "2026-08-01 14:00:00", "2026-08-01 14:05:00",
                      decision="reject", undone_at="2026-08-01 15:00:00"),  # позднее, отменено
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["processing_avg_minutes"] == 30.0  # ровно раннее решение, не позднее (240 мин)


def test_kpi_row_processing_avg_delegate_with_only_undone_decision_excluded(tmp_path):
    """У делегата ЕДИНСТВЕННОЕ решение отменено -- он выпадает из среднего целиком."""
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "registration_date": "2026-08-01 10:00:00"},
            {"telegram_id": 2, "registration_date": "2026-08-01 10:00:00"},
        ],
        application_decisions=[
            _decision(1, "2026-08-01 10:30:00", "2026-08-01 10:35:00",
                      undone_at="2026-08-01 11:00:00"),
            _decision(2, "2026-08-01 11:00:00", "2026-08-01 11:05:00"),  # +60 мин
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["processing_avg_minutes"] == 60.0  # только делегат 2


def test_kpi_row_processing_avg_scoped_by_season(tmp_path):
    """Решение делегата другого сезона не влияет на среднее текущего сезона."""
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"event_season": "YL26"},
        users=[
            {"telegram_id": 1, "season": "YL26", "registration_date": "2026-08-01 10:00:00"},
            {"telegram_id": 2, "season": "RusCo25", "registration_date": "2026-08-01 10:00:00"},
        ],
        application_decisions=[
            _decision(1, "2026-08-01 10:30:00", "2026-08-01 10:35:00"),  # +30 мин, YL26
            _decision(2, "2026-08-01 20:00:00", "2026-08-01 20:05:00"),  # +600 мин, RusCo25
        ],
    )
    with dash_db.read_conn(path) as conn:
        current_row = kpi_row(conn, Scope())  # season=None -> текущий (YL26)
        past_row = kpi_row(conn, Scope(season="RusCo25"))
    assert current_row["processing_avg_minutes"] == 30.0
    assert past_row["processing_avg_minutes"] == 600.0


def test_kpi_row_processing_avg_excludes_decision_from_before_re_registration(tmp_path):
    """Делегат переоформился на новый сезон -- `users.registration_date`/`season`
    переписаны UPSERT'ом НА МЕСТЕ (тот же telegram_id, database/db.py), а строка старого
    решения в `application_decisions` осталась как была. Пока новое решение не принято,
    старое решение (раньше ТЕКУЩЕЙ регистрации) в среднее не идёт -- иначе разница дат
    уходит в минус и молча тянет метрику вниз."""
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            # Единственный делегат: решение прошлого сезона (10:30) раньше НОВОЙ регистрации
            # (2026-09-01) -- в среднем по этому единственному делегату нет данных.
            {"telegram_id": 1, "registration_date": "2026-09-01 09:00:00"},
        ],
        application_decisions=[
            _decision(1, "2026-08-01 10:30:00", "2026-08-01 10:35:00"),  # решение прошлого сезона
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["processing_avg_minutes"] is None
    assert row["processing_avg_label"] == "—"


def test_kpi_row_processing_avg_stale_decision_does_not_affect_other_delegates(tmp_path):
    """Тот же сценарий переоформления, но рядом есть второй делегат с валидным решением --
    среднее считается ТОЛЬКО по нему, переоформившийся делегат просто исключается из
    выборки, не искажая число."""
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "registration_date": "2026-09-01 09:00:00"},  # переоформился
            {"telegram_id": 2, "registration_date": "2026-08-01 10:00:00"},  # без переоформления
        ],
        application_decisions=[
            _decision(1, "2026-08-01 10:30:00", "2026-08-01 10:35:00"),  # решение прошлого сезона
            _decision(2, "2026-08-01 11:00:00", "2026-08-01 11:05:00"),  # +60 мин, валидное
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["processing_avg_minutes"] == 60.0  # только делегат 2, делегат 1 исключён


# ── kpi_row: game_review_avg_minutes/_label (квик 260910-qgn) ────────────────────────────

def _game_task(**overrides):
    task = {
        "id": 1, "text": "t", "category": "photo", "coins": 10, "proof_type": "photo",
        "deadline_at": "2026-09-01 00:00:00", "created_at": "2026-08-01 00:00:00",
    }
    task.update(overrides)
    return task


def test_kpi_row_game_review_avg_ignores_submission_without_decision(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "registration_date": "2026-08-01 00:00:00"},
            {"telegram_id": 2, "registration_date": "2026-08-01 00:00:00"},
            {"telegram_id": 3, "registration_date": "2026-08-01 00:00:00"},
        ],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 10:00:00", "status": "approved",
             "reviewed_at": "2026-08-02 12:00:00"},  # +120 мин
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "b",
             "submitted_at": "2026-08-02 10:00:00", "status": "approved",
             "reviewed_at": "2026-08-02 10:30:00"},  # +30 мин
            {"task_id": 1, "user_id": 3, "content_type": "photo", "content": "c",
             "submitted_at": "2026-08-02 10:00:00", "status": "pending"},  # без решения
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["game_review_avg_minutes"] == 75.0
    assert row["game_review_avg_label"] == "1 ч 15 мин"


def test_kpi_row_game_review_avg_excludes_other_season(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"event_season": "YL26"},
        users=[
            {"telegram_id": 1, "registration_date": "2026-08-01 00:00:00", "season": "YL26"},
            {"telegram_id": 2, "registration_date": "2025-08-01 00:00:00", "season": "YL25"},
        ],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 10:00:00", "status": "approved",
             "reviewed_at": "2026-08-02 10:30:00"},  # +30 мин, текущий сезон
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "b",
             "submitted_at": "2026-08-02 10:00:00", "status": "approved",
             "reviewed_at": "2026-08-02 14:00:00"},  # +240 мин, прошлый сезон
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["game_review_avg_minutes"] == 30.0


def test_kpi_row_game_review_avg_excludes_other_city(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "registration_date": "2026-08-01 00:00:00", "event_city": "msk"},
            {"telegram_id": 2, "registration_date": "2026-08-01 00:00:00", "event_city": "spb"},
        ],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 10:00:00", "status": "approved",
             "reviewed_at": "2026-08-02 10:30:00"},  # +30 мин, msk
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "b",
             "submitted_at": "2026-08-02 10:00:00", "status": "approved",
             "reviewed_at": "2026-08-02 14:00:00"},  # +240 мин, spb
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope(city="msk"))
    assert row["game_review_avg_minutes"] == 30.0


def test_kpi_row_game_review_avg_excludes_reviewed_before_submitted(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[{"telegram_id": 1, "registration_date": "2026-08-01 00:00:00"}],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 10:00:00", "status": "approved",
             "reviewed_at": "2026-08-02 09:00:00"},  # раньше submitted_at -- битые данные
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["game_review_avg_minutes"] is None
    assert row["game_review_avg_label"] == "—"


# ── kpi_row/questions_block: время ответа на вопрос делегата (квик 260910-tt5) ───────────
#
# D-1: метрика считается по `asked_at -> delivered_at`, НЕ `answered_at` (штамп захвата
# вопроса менеджером, а не доставки ответа делегату) -- см. докстринг
# `_avg_question_answer_minutes`.

def _question(**overrides):
    question = {
        "user_id": 1,
        "question_text": "Когда открывается регистрация?",
        "asked_at": "2026-08-02 10:00:00",
    }
    question.update(overrides)
    return question


def test_kpi_row_question_answer_avg_ignores_question_without_answer(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[{"telegram_id": 1}, {"telegram_id": 2}],
        delegate_questions=[
            _question(user_id=1, asked_at="2026-08-02 10:00:00",
                      delivered_at="2026-08-02 10:30:00"),  # +30 мин
            _question(user_id=2, asked_at="2026-08-02 10:00:00"),  # без ответа
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["question_answer_avg_minutes"] == 30.0
    assert row["question_answer_avg_label"] == "30 мин"


def test_kpi_row_question_answer_avg_parses_iso_t_format(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[{"telegram_id": 1}],
        delegate_questions=[
            _question(
                user_id=1,
                asked_at="2026-08-17T12:47:00.804496",
                delivered_at="2026-08-17T13:17:00.804496",  # +30 мин, «T» + микросекунды
            ),
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["question_answer_avg_minutes"] == 30.0
    assert row["question_answer_avg_label"] == "30 мин"


def test_kpi_row_question_answer_avg_excludes_delivered_before_asked(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[{"telegram_id": 1}],
        delegate_questions=[
            _question(user_id=1, asked_at="2026-08-02 10:00:00",
                      delivered_at="2026-08-02 09:00:00"),  # раньше asked_at -- битые данные
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["question_answer_avg_minutes"] is None
    assert row["question_answer_avg_label"] == "—"


def test_kpi_row_question_answer_avg_excludes_other_season(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"event_season": "YL26"},
        users=[
            {"telegram_id": 1, "season": "YL26"},
            {"telegram_id": 2, "season": "YL25"},
        ],
        delegate_questions=[
            _question(user_id=1, asked_at="2026-08-02 10:00:00",
                      delivered_at="2026-08-02 10:30:00"),  # +30 мин, текущий сезон
            _question(user_id=2, asked_at="2026-08-02 10:00:00",
                      delivered_at="2026-08-02 14:00:00"),  # +240 мин, прошлый сезон
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope())
    assert row["question_answer_avg_minutes"] == 30.0


def test_kpi_row_question_answer_avg_excludes_other_city(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "event_city": "msk"},
            {"telegram_id": 2, "event_city": "spb"},
        ],
        delegate_questions=[
            _question(user_id=1, asked_at="2026-08-02 10:00:00",
                      delivered_at="2026-08-02 10:30:00"),  # +30 мин, msk
            _question(user_id=2, asked_at="2026-08-02 10:00:00",
                      delivered_at="2026-08-02 14:00:00"),  # +240 мин, spb
        ],
    )
    with dash_db.read_conn(path) as conn:
        row = kpi_row(conn, Scope(city="msk"))
    assert row["question_answer_avg_minutes"] == 30.0


# ── questions_block (квик 260910-tt5) ────────────────────────────────────────────────────

def test_questions_block_none_when_no_questions_in_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert questions_block(conn, Scope()) is None

    path2 = _use_tmp_db(tmp_path, name="dashboard_queries_2.db")
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[{"telegram_id": 1, "event_city": "spb"}],
        delegate_questions=[_question(user_id=1)],
    )
    with dash_db.read_conn(path2) as conn:
        assert questions_block(conn, Scope(city="msk")) is None


def test_questions_block_counts_sum_to_total(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[{"telegram_id": i} for i in range(1, 5)],
        delegate_questions=[
            _question(user_id=1),  # new -- ни захвата, ни доставки
            _question(user_id=2, answered_by=100, answered_by_name="Аня",
                      answered_at="2026-08-02 10:05:00"),  # in_work
            _question(user_id=3, answered_by=100, answered_by_name="Аня",
                      answered_at="2026-08-02 10:05:00",
                      delivered_at="2026-08-02 10:10:00"),  # answered
            _question(user_id=4, delivered_at="2026-08-02 10:10:00"),  # легаси: доставлен
                                                                        # без захвата -> answered
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = questions_block(conn, Scope())
    assert stats["total"] == 4
    assert stats["new"] == 1
    assert stats["in_work"] == 1
    assert stats["answered"] == 2
    assert stats["new"] + stats["in_work"] + stats["answered"] == stats["total"]
    assert stats["waiting_now"] == 2
    assert stats["answered_share"] == 50.0


def test_questions_block_oldest_waiting_parses_both_stamp_formats(tmp_path):
    path = _use_tmp_db(tmp_path)
    now = datetime.utcnow()
    older_iso_t = (now - timedelta(minutes=150)).isoformat()
    newer_legacy = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    _seed(
        users=[{"telegram_id": 1}, {"telegram_id": 2}],
        delegate_questions=[
            _question(user_id=1, asked_at=older_iso_t),
            _question(user_id=2, asked_at=newer_legacy),
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = questions_block(conn, Scope())
    assert stats["oldest_waiting_minutes"] is not None
    assert abs(stats["oldest_waiting_minutes"] - 150.0) < 1.0


def test_questions_block_top_managers_limited_and_unnamed_merged(tmp_path):
    """Четыре именованных менеджера (Аня/Боря/Вика/Гоша) + две строки с пустым и NULL-именем.
    Счётчики нарочно РАЗНЫЕ (6/4/2-мёрж/1/1) — без завязки на устойчивость сортировки при
    равенстве (`ORDER BY answered DESC, name ASC`, где пустая строка «без имени» лексикографически
    меньше любого непустого имени — это отдельный, честно задокументированный сценарий, покрытый
    ниже `test_questions_block_unnamed_managers_collapse_into_one_row`)."""
    path = _use_tmp_db(tmp_path)
    delivered = "2026-08-02 10:10:00"
    questions: list[dict] = []
    uid = 1

    def _answered(manager_id, name, count):
        nonlocal uid
        for _ in range(count):
            questions.append(_question(
                user_id=uid, answered_by=manager_id, answered_by_name=name,
                answered_at="2026-08-02 09:00:00", delivered_at=delivered,
            ))
            uid += 1

    _answered(101, "Аня", 6)
    _answered(102, "Боря", 4)
    _answered(105, "", 1)      # пустое имя
    _answered(106, None, 1)    # NULL имя -- вместе с пустым мёржится в "без имени" (итого 2)
    _answered(103, "Вика", 1)
    _answered(104, "Гоша", 1)

    _seed(
        users=[{"telegram_id": i} for i in range(1, uid)],
        delegate_questions=questions,
    )
    with dash_db.read_conn(path) as conn:
        stats = questions_block(conn, Scope())
    top = stats["top_managers"]
    assert len(top) == 3
    assert top[0] == {"name": "Аня", "answered": 6}
    assert top[1] == {"name": "Боря", "answered": 4}
    assert top[2] == {"name": "без имени", "answered": 2}
    # Вика/Гоша (по 1 ответу) отрезаны лимитом-3.
    assert "Вика" not in [row["name"] for row in top]
    assert "Гоша" not in [row["name"] for row in top]


def test_questions_block_unnamed_managers_collapse_into_one_row(tmp_path):
    """`answered_by_name` пустая строка и NULL схлопываются в ОДНУ строку «без имени», а не в
    две разные -- проверяем без давления лимита топ-3 (всего один именованный конкурент)."""
    path = _use_tmp_db(tmp_path)
    delivered = "2026-08-02 10:10:00"
    _seed(
        users=[{"telegram_id": i} for i in range(1, 4)],
        delegate_questions=[
            _question(user_id=1, answered_by=105, answered_by_name="",
                      answered_at="2026-08-02 09:00:00", delivered_at=delivered),
            _question(user_id=2, answered_by=106, answered_by_name=None,
                      answered_at="2026-08-02 09:00:00", delivered_at=delivered),
            _question(user_id=3, answered_by=101, answered_by_name="Аня",
                      answered_at="2026-08-02 09:00:00", delivered_at=delivered),
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = questions_block(conn, Scope())
    top = stats["top_managers"]
    unnamed = [row for row in top if row["name"] == "без имени"]
    assert len(unnamed) == 1
    assert unnamed[0]["answered"] == 2


def test_question_status_case_matches_services_question_status(tmp_path):
    """Паритет `_QUESTION_STATUS_CASE` (копия в `dashboard/queries.py`, D-3) с оригиналом
    `services.questions.question_status` -- перебор всех четырёх сочетаний `answered_by`/
    `delivered_at`, бакет читается с РЕАЛЬНОЙ БД через то же SQL-выражение, что использует
    `questions_block`."""
    combos = [
        {"answered_by": None, "delivered_at": None},
        {"answered_by": None, "delivered_at": "2026-08-02 10:10:00"},
        {"answered_by": 100, "delivered_at": None},
        {"answered_by": 100, "delivered_at": "2026-08-02 10:10:00"},
    ]
    path = _use_tmp_db(tmp_path)
    questions = [
        _question(user_id=1, answered_by=combo["answered_by"], delivered_at=combo["delivered_at"])
        for combo in combos
    ]
    _seed(users=[{"telegram_id": 1}], delegate_questions=questions)
    with dash_db.read_conn(path) as conn:
        rows = conn.execute(
            f"SELECT answered_by, delivered_at, {_QUESTION_STATUS_CASE} AS status "
            "FROM delegate_questions q ORDER BY q.id ASC"
        ).fetchall()
    assert len(rows) == len(combos)
    for row, combo in zip(rows, combos):
        assert row["status"] == question_status(combo)


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


def _game_task(**overrides):
    task = {
        "id": 1, "text": "t", "category": "photo", "coins": 10, "proof_type": "photo",
        "deadline_at": "2026-09-01 00:00:00", "created_at": "2026-08-01 00:00:00",
    }
    task.update(overrides)
    return task


def test_game_block_matches_get_game_stats_on_same_fixture(tmp_path):
    """Раньше эта фикстура (БЕЗ строк `users`) сверяла блок с `bot_db.get_game_stats()`
    побайтно — блок считался по ВСЕЙ базе. С квик 260910-qgn блок сужен скоупом страницы
    (JOIN на `users`), и та же фикстура без единой строки `users` больше не даёт ни одного
    играющего в скоупе — `None`, а не совпадение с `get_game_stats()`. Сверка чисел с
    `get_game_stats()` теперь живёт в тесте ниже, на фикстуре С `users`."""
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
    with dash_db.read_conn(path) as conn:
        assert game_block(conn, Scope()) is None  # без users в скоупе играющих не найти


def test_game_block_status_counts_match_get_game_stats_when_users_in_scope(tmp_path):
    """Тот же сторож, что раньше — блок и `get_game_stats()` считают одни и те же числа по
    статусам сдач и категориям, — но теперь на фикстуре С `users` в текущем сезоне (иначе
    блок сузит всех в 0, см. тест выше). Сравнение по КЛЮЧАМ, не dict целиком: у блока теперь
    больше ключей, чем у `get_game_stats()`."""
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on"},
        users=[
            {"telegram_id": 1, "status": "approved"},
            {"telegram_id": 2, "status": "approved"},
        ],
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
    for key in ("participants", "pending", "approved", "rejected", "by_category"):
        assert dashboard_stats[key] == reference[key]


def test_game_block_excludes_submission_from_other_season_or_city(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on", "event_season": "YL26"},
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "registration_date": "2026-08-01 00:00:00", "season": "YL26",
             "event_city": "msk", "status": "approved"},
            {"telegram_id": 2, "registration_date": "2025-08-01 00:00:00", "season": "YL25",
             "event_city": "msk", "status": "approved"},  # другой сезон
            {"telegram_id": 3, "registration_date": "2026-08-01 00:00:00", "season": "YL26",
             "event_city": "spb", "status": "approved"},  # другой город
        ],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "b",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
            {"task_id": 1, "user_id": 3, "content_type": "photo", "content": "c",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope(city="msk"))
    assert stats["participants"] == 1
    assert stats["submissions_total"] == 1


def test_game_block_participants_and_submissions_shares(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on"},
        users=[
            {"telegram_id": 1, "status": "approved"},
            {"telegram_id": 2, "status": "approved"},
            {"telegram_id": 3, "status": "approved"},
            {"telegram_id": 4, "status": "approved"},
        ],
        game_tasks=[_game_task(), _game_task(id=2)],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
            # второе задание -- тот же делегат, другая задача (иначе конфликт с partial
            # unique index idx_game_submissions_active на (task_id, user_id) для не-rejected)
            {"task_id": 2, "user_id": 1, "content_type": "photo", "content": "b",
             "submitted_at": "2026-08-02 01:00:00", "status": "pending"},
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "c",
             "submitted_at": "2026-08-02 02:00:00", "status": "rejected"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope())
    assert stats["participants"] == 2
    assert stats["participants_share"] == 50.0
    assert stats["submissions_total"] == 3
    assert (stats["pending"], stats["approved"], stats["rejected"]) == (1, 1, 1)
    assert stats["approved_share"] == 33.3


def test_game_block_coins_totals_and_per_participant(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on"},
        users=[
            {"telegram_id": 1, "status": "approved"},
            {"telegram_id": 2, "status": "approved"},
        ],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "b",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
        ],
        coins=[
            {"user_id": 1, "delta": 10, "source": "task", "timestamp": "2026-08-02 00:00:00"},
            {"user_id": 1, "delta": 5, "source": "manual", "timestamp": "2026-08-02 00:00:00"},
            {"user_id": 1, "delta": -3, "source": "manual", "timestamp": "2026-08-02 00:00:00"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope())
    assert stats["coins_total"] == 15  # списание -3 в начисления не входит
    assert stats["coins_task"] == 10
    assert stats["coins_manual"] == 5
    assert stats["coins_per_participant"] == 7.5


def test_game_block_coins_exclude_delegate_out_of_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on"},
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "status": "approved", "event_city": "msk"},
            {"telegram_id": 2, "status": "approved", "event_city": "spb"},
        ],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
        ],
        coins=[
            {"user_id": 1, "delta": 10, "source": "task", "timestamp": "2026-08-02 00:00:00"},
            {"user_id": 2, "delta": 50, "source": "task", "timestamp": "2026-08-02 00:00:00"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope(city="msk"))
    assert stats["coins_total"] == 10


def test_game_block_top_tasks_ordered_limited_and_use_title_rule(tmp_path):
    tasks = []
    submissions = []
    users = []
    uid = 1
    # (task_id, title, text, число_сдач, число_одобренных) -- шестое задание отрезается
    # лимитом top-5.
    task_specs = [
        (1, "Заданный заголовок", "t1", 3, 3),
        (2, "", "первая строка второго задания", 2, 1),
        (3, "", "t3", 2, 0),
        (4, "", "t4", 1, 1),
        (5, "", "t5", 1, 0),
        (6, "", "t6", 1, 0),
    ]
    for task_id, title, text, submissions_cnt, approved_cnt in task_specs:
        tasks.append(_game_task(id=task_id, title=title, text=text))
        for i in range(submissions_cnt):
            status = "approved" if i < approved_cnt else "pending"
            submissions.append({
                "task_id": task_id, "user_id": uid, "content_type": "photo", "content": "x",
                "submitted_at": "2026-08-02 00:00:00", "status": status,
            })
            users.append({"telegram_id": uid, "status": "approved"})
            uid += 1
    path = _use_tmp_db(tmp_path)
    _seed(settings={"dashboard_block_game": "on"}, users=users, game_tasks=tasks,
          game_submissions=submissions)
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope())
    top = stats["top_tasks"]
    assert len(top) == 5
    assert [row["submissions"] for row in top] == [3, 2, 2, 1, 1]
    assert top[0] == {"title": "Заданный заголовок", "submissions": 3, "approved": 3}
    assert top[1]["title"] == "первая строка второго задания"
    assert top[1]["approved"] == 1


def test_game_block_pending_oldest_minutes_and_label(tmp_path):
    path = _use_tmp_db(tmp_path)
    now = datetime.now()
    older = (now - timedelta(minutes=150)).strftime("%Y-%m-%d %H:%M:%S")
    newer = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    _seed(
        settings={"dashboard_block_game": "on"},
        users=[
            {"telegram_id": 1, "status": "approved"},
            {"telegram_id": 2, "status": "approved"},
        ],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": older, "status": "pending"},
            {"task_id": 1, "user_id": 2, "content_type": "photo", "content": "b",
             "submitted_at": newer, "status": "pending"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope())
    assert abs(stats["pending_oldest_minutes"] - 150.0) < 1.0
    assert stats["pending_oldest_label"] == "2 ч 30 мин"


def test_game_block_pending_oldest_none_when_queue_empty(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on"},
        users=[{"telegram_id": 1, "status": "approved"}],
        game_tasks=[_game_task()],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope())
    assert stats["pending_oldest_minutes"] is None
    assert stats["pending_oldest_label"] == "—"


def test_game_block_by_category_scoped(tmp_path):
    """`by_category` (существующий ключ) сохраняется и тоже сужается скоупом."""
    path = _use_tmp_db(tmp_path)
    _seed(
        settings={"dashboard_block_game": "on"},
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        users=[
            {"telegram_id": 1, "status": "approved", "event_city": "msk"},
            {"telegram_id": 2, "status": "approved", "event_city": "spb"},
        ],
        game_tasks=[
            {"id": 1, "text": "t1", "category": "photo", "coins": 10, "proof_type": "photo",
             "deadline_at": "2026-09-01 00:00:00", "created_at": "2026-08-01 00:00:00"},
            {"id": 2, "text": "t2", "category": "video", "coins": 20, "proof_type": "video",
             "deadline_at": "2026-09-01 00:00:00", "created_at": "2026-08-01 00:00:00"},
        ],
        game_submissions=[
            {"task_id": 1, "user_id": 1, "content_type": "photo", "content": "a",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
            {"task_id": 2, "user_id": 2, "content_type": "video", "content": "b",
             "submitted_at": "2026-08-02 00:00:00", "status": "approved"},
        ],
    )
    with dash_db.read_conn(path) as conn:
        stats = game_block(conn, Scope(city="msk"))
    assert stats["by_category"] == {"photo": 1}


# ── _task_title / database.db.task_title (квик 260910-qgn): дрейф правила подписи задания ─

def test_task_title_matches_bot_db_task_title():
    cases = [
        {"title": "", "text": ""},
        {"title": "", "text": "короткий text"},
        {"title": "", "text": "длинная первая строка ровно сорок с лишним символов текста\nвторая строка"},
        {"title": "Задан заголовок", "text": "неважно"},
    ]
    for task in cases:
        assert _task_title(task.get("title"), task.get("text")) == bot_db.task_title(task)


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
