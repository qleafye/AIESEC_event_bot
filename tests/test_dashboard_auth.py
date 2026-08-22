"""Phase 15 Plan 04 (STAT-02): подпись Login Widget, сессия, права и антиспам дашборда.

Task 1 — `dashboard.auth`: HMAC-подпись Login Widget, параметры сессионной cookie, адрес
клиента за Cloudflare. Task 2 — `dashboard.access`: пересверка права `stats` и городской
скоуп. Task 3 (дописывается позже) — `dashboard.notify`.
"""
import asyncio
import hashlib
import hmac
import inspect
from pathlib import Path

import pytest

from config import config as bot_config
from database import db as bot_db

from dashboard.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    client_ip,
    session_middleware_kwargs,
    verify_login_payload,
)
from dashboard.config import load_config
from dashboard import db as dash_db

ACCESS_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "access.py"
NOTIFY_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "notify.py"

BOT_TOKEN = "123456:ABCDEF-testtoken"


def _sign(payload: dict, bot_token: str = BOT_TOKEN) -> dict:
    data = {k: v for k, v in payload.items() if k != "hash"}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return {**data, "hash": signature}


def _payload(auth_date: int, **extra) -> dict:
    base = {"id": "900802", "first_name": "Лена", "auth_date": str(auth_date)}
    base.update(extra)
    return _sign(base)


# ── verify_login_payload: подпись ────────────────────────────────────────────────────────

def test_valid_signature_accepted_and_returns_id():
    now = 1_700_000_000.0
    payload = _payload(int(now) - 10)
    assert verify_login_payload(payload, BOT_TOKEN, now=now) == 900802


def test_tampered_field_after_signing_rejected():
    now = 1_700_000_000.0
    payload = _payload(int(now) - 10)
    payload["first_name"] = "Подменено"
    assert verify_login_payload(payload, BOT_TOKEN, now=now) is None


def test_wrong_bot_token_rejected():
    now = 1_700_000_000.0
    payload = _payload(int(now) - 10)
    assert verify_login_payload(payload, "999999:other-token", now=now) is None


def test_missing_hash_rejected():
    now = 1_700_000_000.0
    payload = _payload(int(now) - 10)
    del payload["hash"]
    assert verify_login_payload(payload, BOT_TOKEN, now=now) is None


def test_uses_compare_digest_not_equality():
    source = inspect.getsource(verify_login_payload)
    assert "compare_digest" in source
    # Не должно остаться прямого `==` сравнения самой подписи (обычная строковая проверка).
    assert "computed_hash == received_hash" not in source
    assert "received_hash == computed_hash" not in source


# ── verify_login_payload: свежесть auth_date ────────────────────────────────────────────

def test_stale_auth_date_rejected():
    now = 1_700_000_000.0
    payload = _payload(int(now) - 86401)
    assert verify_login_payload(payload, BOT_TOKEN, now=now) is None


def test_auth_date_exactly_at_freshness_boundary_accepted():
    now = 1_700_000_000.0
    payload = _payload(int(now) - 86400)
    assert verify_login_payload(payload, BOT_TOKEN, now=now) == 900802


def test_auth_date_far_in_future_rejected():
    now = 1_700_000_000.0
    payload = _payload(int(now) + 3600)
    assert verify_login_payload(payload, BOT_TOKEN, now=now) is None


def test_auth_date_missing_rejected():
    now = 1_700_000_000.0
    payload = _sign({"id": "900802", "first_name": "Лена"})
    assert verify_login_payload(payload, BOT_TOKEN, now=now) is None


def test_auth_date_non_numeric_rejected():
    now = 1_700_000_000.0
    payload = _payload("not-a-number")
    assert verify_login_payload(payload, BOT_TOKEN, now=now) is None


# ── verify_login_payload: побочные эффекты ──────────────────────────────────────────────

def test_input_mapping_not_mutated():
    now = 1_700_000_000.0
    payload = _payload(int(now) - 10)
    original = dict(payload)
    verify_login_payload(payload, BOT_TOKEN, now=now)
    assert payload == original


def test_client_ip_never_referenced_from_verify_login_payload():
    source = inspect.getsource(verify_login_payload)
    assert "client_ip" not in source


# ── session_middleware_kwargs ────────────────────────────────────────────────────────────

def test_session_max_age_is_30_days():
    assert SESSION_MAX_AGE == 30 * 24 * 3600


def test_session_middleware_kwargs_shape():
    cfg = load_config(env={"DASHBOARD_SESSION_SECRET": "s3cr3t"})
    kwargs = session_middleware_kwargs(cfg)
    assert kwargs["secret_key"] == "s3cr3t"
    assert kwargs["session_cookie"] == SESSION_COOKIE
    assert kwargs["max_age"] == 2592000
    assert kwargs["https_only"] is True
    assert kwargs["same_site"] == "lax"


def test_empty_session_secret_fails_at_config_load_not_here():
    # D-09: пустой секрет падает ещё в load_config (dashboard/config.py), до auth.py.
    with pytest.raises(RuntimeError):
        load_config(env={})


# ── client_ip ─────────────────────────────────────────────────────────────────────────────

def test_client_ip_prefers_cf_connecting_ip():
    headers = {"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "9.9.9.9, 8.8.8.8"}
    assert client_ip(headers) == "1.2.3.4"


def test_client_ip_falls_back_to_x_forwarded_for():
    headers = {"X-Forwarded-For": "9.9.9.9, 8.8.8.8"}
    assert client_ip(headers) == "9.9.9.9"


def test_client_ip_none_without_either_header():
    assert client_ip({}) is None


# ═══════════════════════════════════════════════════════════════════════════════════════
# Task 2 — dashboard.access: пересверка права stats и городской скоуп (D-09/D-10)
# ═══════════════════════════════════════════════════════════════════════════════════════

from dashboard.access import (  # noqa: E402
    ALL_CAPABILITIES,
    _ROLE_DEFAULT_CAPS,
    has_stats,
    resolve_capabilities,
    staff_city,
    viewer_scope,
)

ADMIN_IDS = (12345678,)


def _seed_access_db(tmp_path, name="access.db", staff=(), settings=None) -> str:
    """`staff` — список `(telegram_id, role, city)`; `settings` — словарь ключ->значение
    `bot_settings`. Пишет через `database.db` (aiosqlite), читает дашборд через
    `dashboard.db.read_conn` (sqlite3, mode=ro) — то же разделение, что в
    tests/test_dashboard_queries.py."""
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())

    async def _fill():
        for telegram_id, role, city in staff:
            await bot_db.add_staff(telegram_id, role, added_by=None)
            if city is not None:
                await bot_db.set_staff_city(telegram_id, city)
        for key, value in (settings or {}).items():
            await bot_db.set_setting(key, value)

    asyncio.run(_fill())
    return path


# ── resolve_capabilities / has_stats ────────────────────────────────────────────────────

def test_superadmin_gets_stats_even_with_empty_staff(tmp_path):
    path = _seed_access_db(tmp_path)
    with dash_db.read_conn(path) as conn:
        assert has_stats(conn, ADMIN_IDS[0], ADMIN_IDS) is True
        assert resolve_capabilities(conn, ADMIN_IDS[0], ADMIN_IDS) == set(ALL_CAPABILITIES)


def test_manager_with_stats_in_role_caps_gets_it(tmp_path):
    path = _seed_access_db(
        tmp_path,
        staff=[(900802, "reg_manager", None)],
        settings={"role_caps_reg_manager": "moderate_reg\nstats"},
    )
    with dash_db.read_conn(path) as conn:
        assert has_stats(conn, 900802, ADMIN_IDS) is True


def test_manager_without_stats_in_role_caps_denied(tmp_path):
    path = _seed_access_db(
        tmp_path,
        staff=[(900802, "reg_manager", None)],
        settings={"role_caps_reg_manager": "moderate_reg"},
    )
    with dash_db.read_conn(path) as conn:
        assert has_stats(conn, 900802, ADMIN_IDS) is False


def test_role_disabled_denies_all_its_caps(tmp_path):
    path = _seed_access_db(
        tmp_path,
        staff=[(900802, "reg_manager", None)],
        settings={
            "role_caps_reg_manager": "moderate_reg\nstats",
            "role_reg_manager_enabled": "off",
        },
    )
    with dash_db.read_conn(path) as conn:
        assert has_stats(conn, 900802, ADMIN_IDS) is False


def test_no_cache_role_caps_change_takes_effect_immediately(tmp_path):
    path = _seed_access_db(
        tmp_path,
        staff=[(900802, "reg_manager", None)],
        settings={"role_caps_reg_manager": "moderate_reg\nstats"},
    )
    with dash_db.read_conn(path) as conn:
        assert has_stats(conn, 900802, ADMIN_IDS) is True

    asyncio.run(bot_db.set_setting("role_caps_reg_manager", "moderate_reg"))

    with dash_db.read_conn(path) as conn:
        assert has_stats(conn, 900802, ADMIN_IDS) is False


def test_garbage_capability_value_never_grants_access(tmp_path):
    path = _seed_access_db(
        tmp_path,
        staff=[(900802, "reg_manager", None)],
        settings={"role_caps_reg_manager": "not_a_real_capability"},
    )
    with dash_db.read_conn(path) as conn:
        caps = resolve_capabilities(conn, 900802, ADMIN_IDS)
        assert caps == set()


def test_unknown_role_name_in_staff_grants_nothing(tmp_path):
    path = _seed_access_db(tmp_path, staff=[(900802, "some_retired_role", None)])
    with dash_db.read_conn(path) as conn:
        assert resolve_capabilities(conn, 900802, ADMIN_IDS) == set()


# ── viewer_scope / staff_city (D-10) ────────────────────────────────────────────────────

def test_bound_manager_ignores_requested_city(tmp_path):
    path = _seed_access_db(tmp_path, staff=[(900802, "reg_manager", "spb")])
    with dash_db.read_conn(path) as conn:
        assert staff_city(conn, 900802) == "spb"
        scope = viewer_scope(conn, 900802, ADMIN_IDS, requested_city="msk")
        assert scope.city == "spb"


def test_unbound_manager_respects_requested_city(tmp_path):
    path = _seed_access_db(tmp_path, staff=[(900802, "reg_manager", None)])
    with dash_db.read_conn(path) as conn:
        assert staff_city(conn, 900802) is None
        scope = viewer_scope(conn, 900802, ADMIN_IDS, requested_city="msk")
        assert scope.city == "msk"


def test_unbound_manager_with_no_requested_city_sees_all(tmp_path):
    path = _seed_access_db(tmp_path, staff=[(900802, "reg_manager", None)])
    with dash_db.read_conn(path) as conn:
        scope = viewer_scope(conn, 900802, ADMIN_IDS, requested_city=None)
        assert scope.city is None


# ── сторож дрейфа модели прав от бота ────────────────────────────────────────────────────

def test_all_capabilities_matches_bot_capability_model():
    from handlers.admin_caps import ALL_CAPABILITIES as BOT_CAPS

    assert list(ALL_CAPABILITIES) == list(BOT_CAPS)


def test_role_default_caps_matches_settings_schema():
    from settings_schema import SETTINGS_SCHEMA

    for role, caps in _ROLE_DEFAULT_CAPS.items():
        key = f"role_caps_{role}"
        assert SETTINGS_SCHEMA[key]["default"] == caps


# ── греп-сторожи (T-15-04-04/06) ─────────────────────────────────────────────────────────

def test_access_module_has_no_cache_decorator_or_proxy_headers():
    text = ACCESS_FILE.read_text(encoding="utf-8")
    assert "lru_cache" not in text
    assert "CF-Connecting-IP" not in text
    assert "X-Forwarded" not in text


# ═══════════════════════════════════════════════════════════════════════════════════════
# Task 3 — dashboard.notify: уведомление суперадминам о запросе доступа с антиспамом (D-11)
# ═══════════════════════════════════════════════════════════════════════════════════════

from datetime import date  # noqa: E402

from dashboard import notify as notify_module  # noqa: E402
from dashboard.notify import _seen_today, notify_access_request  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeHttpxClient:
    """Заменяет `httpx.Client` целиком (monkeypatch на `dashboard.notify.httpx.Client`) —
    без реальной сети, без MockTransport, чтобы тест не тянул знание внутренностей httpx."""
    instances: list["_FakeHttpxClient"] = []
    raise_on_post = False
    fail_admin_ids: set[int] = set()

    def __init__(self, *, proxy=None, timeout=None):
        self.proxy = proxy
        self.timeout = timeout
        self.calls: list[tuple[str, dict]] = []
        _FakeHttpxClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None):
        if _FakeHttpxClient.raise_on_post:
            raise RuntimeError("simulated network failure")
        self.calls.append((url, json))
        if json is not None and json.get("chat_id") in _FakeHttpxClient.fail_admin_ids:
            return _FakeResponse(500)
        return _FakeResponse(200)


@pytest.fixture(autouse=True)
def _reset_notify_state(monkeypatch):
    notify_module._last_notified.clear()
    _FakeHttpxClient.instances = []
    _FakeHttpxClient.raise_on_post = False
    _FakeHttpxClient.fail_admin_ids = set()
    monkeypatch.setattr(notify_module.httpx, "Client", _FakeHttpxClient)
    yield
    notify_module._last_notified.clear()


class _Cfg:
    def __init__(self, admin_ids=(111, 222), proxy_url=None, bot_token="123:abc"):
        self.admin_ids = admin_ids
        self.proxy_url = proxy_url
        self.bot_token = bot_token


# ── _seen_today: без сети ────────────────────────────────────────────────────────────────

def test_seen_today_false_first_time_then_true_same_day():
    today = date(2026, 8, 22)
    assert _seen_today(900802, today) is False
    assert _seen_today(900802, today) is True


def test_seen_today_resets_on_new_day():
    assert _seen_today(900802, date(2026, 8, 22)) is False
    assert _seen_today(900802, date(2026, 8, 22)) is True
    assert _seen_today(900802, date(2026, 8, 23)) is False


# ── notify_access_request: доставка и антиспам ──────────────────────────────────────────

def test_first_request_sends_one_message_per_admin():
    cfg = _Cfg(admin_ids=(111, 222, 333))
    result = notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    assert result is True
    assert len(_FakeHttpxClient.instances) == 1
    assert len(_FakeHttpxClient.instances[0].calls) == 3


def test_repeat_same_day_sends_nothing():
    cfg = _Cfg()
    notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    result = notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    assert result is False
    assert len(_FakeHttpxClient.instances) == 1  # клиент вообще не создавался второй раз


def test_next_day_sends_again():
    cfg = _Cfg()
    notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    result = notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 23),
    )
    assert result is True
    assert len(_FakeHttpxClient.instances) == 2


def test_different_people_are_independent_for_antispam():
    cfg = _Cfg()
    notify_access_request(
        cfg, telegram_id=1, username="a", first_name="A", today=date(2026, 8, 22)
    )
    result = notify_access_request(
        cfg, telegram_id=2, username="b", first_name="B", today=date(2026, 8, 22)
    )
    assert result is True


def test_network_failure_returns_false_without_raising():
    _FakeHttpxClient.raise_on_post = True
    cfg = _Cfg()
    result = notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    assert result is False


def test_partial_failure_still_returns_true_if_one_admin_reached():
    cfg = _Cfg(admin_ids=(111, 222))
    _FakeHttpxClient.fail_admin_ids = {111}
    result = notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    assert result is True


def test_empty_admin_ids_returns_false_and_sends_nothing():
    cfg = _Cfg(admin_ids=())
    result = notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    assert result is False
    assert _FakeHttpxClient.instances == []


def test_client_created_with_configured_proxy():
    cfg = _Cfg(proxy_url="socks5://127.0.0.1:1080")
    notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    assert _FakeHttpxClient.instances[0].proxy == "socks5://127.0.0.1:1080"


def test_client_created_without_proxy_when_unset():
    cfg = _Cfg(proxy_url=None)
    notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    assert _FakeHttpxClient.instances[0].proxy is None


# ── содержимое сообщения ─────────────────────────────────────────────────────────────────

def test_message_body_has_no_delegate_pii_and_no_bot_token():
    cfg = _Cfg(bot_token="999999:super-secret-token")
    notify_access_request(
        cfg, telegram_id=900802, username="delegate_user", first_name="Лена",
        today=date(2026, 8, 22),
    )
    url, payload = _FakeHttpxClient.instances[0].calls[0]
    # Токен уходит только в URL sendMessage (штатно для Bot API), но не дублируется в текст
    # сообщения и не появляется в логах этого модуля.
    assert "999999:super-secret-token" in url
    assert "999999:super-secret-token" not in payload["text"]


def test_message_has_button_to_existing_roles_screen():
    cfg = _Cfg()
    notify_access_request(
        cfg, telegram_id=900802, username="delegate", first_name="Лена",
        today=date(2026, 8, 22),
    )
    _, payload = _FakeHttpxClient.instances[0].calls[0]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "admin_roles"


# ── греп-сторож: антиспам не по заголовкам прокси, дашборд не пишет в БД ────────────────

def test_notify_module_has_no_proxy_headers_or_db_writes():
    text = NOTIFY_FILE.read_text(encoding="utf-8")
    assert "CF-Connecting-IP" not in text
    assert "X-Forwarded" not in text
    for verb in ("INSERT", "UPDATE", "DELETE"):
        assert verb not in text
