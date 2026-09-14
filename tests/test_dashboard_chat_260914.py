"""Квик 260914-rgr (RGR-01..07), задача 4: страница «Чат» на веб-дашборде.

Фикстура — `database.db.init_db()` (та же схема, что у бота), сидинг через aiosqlite,
чтение — через `dashboard.db.read_conn` (sqlite3, mode=ro) ПОСЛЕ закрытия пишущего
подключения (тот же приём, что tests/test_dashboard_queries.py).
"""
import asyncio
import hashlib
import hmac
import time

from starlette.testclient import TestClient

from config import config
from database import db as bot_db

from dashboard import db as dash_db
from dashboard.config import DashboardConfig
from dashboard.main import create_app
from dashboard.queries import (
    Scope,
    chat_bindings,
    chat_joins_daily,
    chat_messages_daily,
    chat_not_joined,
    chat_overview,
)
from dashboard.registry import EventSource

BOT_TOKEN = "123456:ABCDEF-testtoken"
ADMIN_ID = 900001
STATS_MANAGER_ID = 900601
CHAT_ID = -1001111111111
SPB_CHAT_ID = -1002222222222


def _use_tmp_db(tmp_path, name="dashboard_chat.db") -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(cities=None, settings=None, users=None, chat_members=None,
                       chat_activity=None, chat_events=None, staff=None):
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
            await conn.execute(f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(row.values()))
        for row in chat_members or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(f"INSERT INTO chat_members ({cols}) VALUES ({placeholders})", tuple(row.values()))
        for row in chat_activity or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(f"INSERT INTO chat_activity ({cols}) VALUES ({placeholders})", tuple(row.values()))
        for row in chat_events or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(f"INSERT INTO chat_events ({cols}) VALUES ({placeholders})", tuple(row.values()))
        for telegram_id, role, city in staff or []:
            await conn.execute(
                "INSERT INTO staff (telegram_id, role, added_by, added_at, city) VALUES (?, ?, ?, ?, ?)",
                (telegram_id, role, ADMIN_ID, "2026-01-01 00:00:00", city),
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


# ── запросы (dashboard/queries.py) ───────────────────────────────────────────────────────

def test_chat_bindings_parses_global_and_per_city_and_skips_garbage(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("spb", "Санкт-Петербург", 1, 1)],
        settings={
            "delegate_chat_id": str(CHAT_ID),
            "delegate_chat_title": "Общий чат",
            "delegate_chat_id__city__spb": str(SPB_CHAT_ID),
            "delegate_chat_title__city__spb": "СПб чат",
            "delegate_chat_id__city__unknown": "не число",  # мусор — пропускается
        },
    )
    with dash_db.read_conn(path) as conn:
        bindings = chat_bindings(conn)
    by_city = {b["city"]: b for b in bindings}
    assert len(bindings) == 2
    assert by_city[None]["chat_id"] == CHAT_ID
    assert by_city[None]["title"] == "Общий чат"
    assert by_city["spb"]["chat_id"] == SPB_CHAT_ID
    assert by_city["spb"]["title"] == "СПб чат"
    assert by_city["spb"]["label"] == "Санкт-Петербург"


def test_chat_overview_counts_four_numbers_correctly(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "full_name": "А", "status": "approved", "event_city": None},
            {"telegram_id": 2, "full_name": "Б", "status": "approved", "event_city": None},
            {"telegram_id": 3, "full_name": "В", "status": "approved", "event_city": None},
        ],
        chat_members=[
            {"chat_id": CHAT_ID, "telegram_id": 1, "status": "member", "source": "test"},
            {"chat_id": CHAT_ID, "telegram_id": 2, "status": "left", "source": "test"},
            # присутствует в чате, но НЕ в users — «в чате, но не зарегистрирован».
            {"chat_id": CHAT_ID, "telegram_id": 999, "status": "member", "source": "test"},
        ],
    )
    chat = {"city": None, "chat_id": CHAT_ID, "title": "Общий чат", "label": "Общий чат"}
    with dash_db.read_conn(path) as conn:
        overview = chat_overview(conn, Scope(), chat)
    assert overview == {"approved": 3, "in_chat": 1, "not_in_chat": 2, "unknown_members": 1}


def test_chat_joins_and_messages_daily_are_dense_calendars(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        chat_events=[
            {"chat_id": CHAT_ID, "telegram_id": 1, "event": "join", "ts": "2026-01-01 10:00:00"},
            {"chat_id": CHAT_ID, "telegram_id": 2, "event": "join", "ts": "2026-01-03 10:00:00"},
            # не join — не должен попасть в календарь вступлений.
            {"chat_id": CHAT_ID, "telegram_id": 2, "event": "leave", "ts": "2026-01-04 10:00:00"},
        ],
        chat_activity=[
            {"chat_id": CHAT_ID, "telegram_id": 1, "day": "2026-01-01", "messages": 2, "replies": 0, "media": 0},
            {"chat_id": CHAT_ID, "telegram_id": 1, "day": "2026-01-03", "messages": 1, "replies": 1, "media": 0},
        ],
    )
    with dash_db.read_conn(path) as conn:
        joins = dict(chat_joins_daily(conn, CHAT_ID))
        messages = dict(chat_messages_daily(conn, CHAT_ID))

    assert joins["2026-01-01"] == 1
    assert joins["2026-01-02"] == 0  # дыра заполнена нулём
    assert joins["2026-01-03"] == 1
    assert messages["2026-01-01"] == 2
    assert messages["2026-01-02"] == 0
    assert messages["2026-01-03"] == 1


def test_chat_not_joined_excludes_those_present(tmp_path):
    """Отклонение от планового текста («ФИО, @ник»): `chat_not_joined` отдаёт telegram_id,
    НЕ full_name/username — dashboard/queries.py под жёстким сторожем «без ПД» (D-17), см.
    докстринг функции."""
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            {"telegram_id": 1, "full_name": "Уже в чате", "username": "@a", "status": "approved",
             "event_city": None, "approved_at": "2026-01-01 10:00:00"},
            {"telegram_id": 2, "full_name": "Не в чате", "username": "@b", "status": "approved",
             "event_city": None, "approved_at": "2026-01-02 10:00:00"},
        ],
        chat_members=[{"chat_id": CHAT_ID, "telegram_id": 1, "status": "member", "source": "test"}],
    )
    chat = {"city": None, "chat_id": CHAT_ID, "title": "x", "label": "x"}
    with dash_db.read_conn(path) as conn:
        rows = chat_not_joined(conn, Scope(), chat)
    assert [r["telegram_id"] for r in rows] == [2]
    assert "full_name" not in rows[0] and "username" not in rows[0]


# ── маршрут /chat (TestClient) ───────────────────────────────────────────────────────────

def _cfg(db_path: str, **overrides) -> DashboardConfig:
    base = dict(
        db_path=db_path,
        public_url="https://yl26.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token=BOT_TOKEN,
        admin_ids=(ADMIN_ID,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    )
    base.update(overrides)
    return DashboardConfig(**base)


def _client(cfg: DashboardConfig, **kwargs) -> TestClient:
    app = create_app(cfg=cfg)
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(app, **kwargs)


def _sign(payload: dict, bot_token: str = BOT_TOKEN) -> dict:
    data = {k: v for k, v in payload.items() if k != "hash"}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return {**data, "hash": signature}


def _login_payload(telegram_id: int, **extra) -> dict:
    base = {"id": str(telegram_id), "first_name": "Тест", "auth_date": str(int(time.time()) - 5)}
    base.update(extra)
    return _sign(base)


def _login(client: TestClient, telegram_id: int, **extra):
    payload = _login_payload(telegram_id, **extra)
    return client.get("/auth/callback", params=payload, follow_redirects=False)


def test_chat_route_without_session_redirects_to_login(tmp_path):
    path = _use_tmp_db(tmp_path)
    client = _client(_cfg(path))

    resp = client.get("/chat", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_chat_route_without_stats_capability_gives_403_no_access(tmp_path):
    path = _use_tmp_db(tmp_path)
    client = _client(_cfg(path))
    _login(client, STATS_MANAGER_ID)

    resp = client.get("/chat")

    assert resp.status_code == 403
    assert "Нет доступа" in resp.text


def test_chat_route_with_stats_capability_returns_page_with_both_canvases(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        staff=[(STATS_MANAGER_ID, "reg_manager", None)],
        settings={
            "role_caps_reg_manager": "moderate_reg;stats",
            "delegate_chat_id": str(CHAT_ID),
            "delegate_chat_title": "Общий чат",
        },
        users=[
            {"telegram_id": 1, "full_name": "А", "status": "approved", "event_city": None,
             "approved_at": "2026-01-01 10:00:00"},
        ],
        chat_members=[{"chat_id": CHAT_ID, "telegram_id": 1, "status": "member", "source": "test"}],
        chat_events=[{"chat_id": CHAT_ID, "telegram_id": 1, "event": "join", "ts": "2026-01-01 10:00:00"}],
        chat_activity=[
            {"chat_id": CHAT_ID, "telegram_id": 1, "day": "2026-01-01", "messages": 3, "replies": 0, "media": 0},
        ],
    )
    client = _client(_cfg(path))
    _login(client, STATS_MANAGER_ID)

    resp = client.get("/chat")

    assert resp.status_code == 200
    assert "Чат делегатов" in resp.text
    assert 'id="joins-chart-1"' in resp.text
    assert 'id="messages-chart-1"' in resp.text


def test_manager_bound_to_city_sees_only_own_city_numbers(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[("spb", "Санкт-Петербург", 1, 1), ("msk", "Москва", 1, 0)],
        staff=[(STATS_MANAGER_ID, "reg_manager", "spb")],
        settings={
            "role_caps_reg_manager": "moderate_reg;stats",
            "delegate_chat_id__city__spb": str(SPB_CHAT_ID),
            "delegate_chat_title__city__spb": "СПб чат",
            "delegate_chat_id": str(CHAT_ID),
            "delegate_chat_title": "Общий чат (чужой)",
        },
        users=[
            {"telegram_id": 1, "full_name": "СПб-делегат", "status": "approved", "event_city": "spb",
             "approved_at": "2026-01-01 10:00:00"},
        ],
        chat_members=[{"chat_id": SPB_CHAT_ID, "telegram_id": 1, "status": "member", "source": "test"}],
    )
    client = _client(_cfg(path))
    _login(client, STATS_MANAGER_ID)

    resp = client.get("/chat")

    assert resp.status_code == 200
    assert "СПб чат" in resp.text
    assert "Общий чат (чужой)" not in resp.text


def test_chat_route_without_bindings_shows_instruction(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        staff=[(STATS_MANAGER_ID, "reg_manager", None)],
        settings={"role_caps_reg_manager": "moderate_reg;stats"},
    )
    client = _client(_cfg(path))
    _login(client, STATS_MANAGER_ID)

    resp = client.get("/chat")

    assert resp.status_code == 200
    assert "ещё не подключён" in resp.text


def test_chat_route_redirects_to_compare_in_multi_mode(tmp_path):
    path_a = _use_tmp_db(tmp_path, "a.db")
    path_b = _use_tmp_db(tmp_path, "b.db")
    cfg = _cfg(
        path_a,
        events=(EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)),
    )
    client = _client(cfg)

    resp = client.get("/chat", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/compare"
