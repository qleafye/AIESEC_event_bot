"""Phase 15 Plan 04 (STAT-02): подпись Login Widget, сессия, права и антиспам дашборда.

Task 1 — `dashboard.auth`: HMAC-подпись Login Widget, параметры сессионной cookie, адрес
клиента за Cloudflare. Дальнейшие блоки (Task 2 — `dashboard.access`, Task 3 —
`dashboard.notify`) дописываются в этот же файл теми же задачами плана.
"""
import hashlib
import hmac
import inspect

import pytest

from dashboard.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    client_ip,
    session_middleware_kwargs,
    verify_login_payload,
)
from dashboard.config import load_config

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
