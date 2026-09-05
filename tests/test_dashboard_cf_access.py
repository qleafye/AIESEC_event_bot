"""Phase 26.1 Plan 02 Task 1 (SD-03, T-26.1-02-01..07): гейт Cloudflare Access — подпись
RS256/JWKS, допуск по списку e-mail. Ключи — фикстурная RSA-пара через `cryptography`, без
сети (JWKS подставляется через шов `jwks_client`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dashboard.cf_access import (
    ACCESS_HEADER,
    require_superadmin_email,
    reset_jwks_cache,
    verify_access_token,
)
from dashboard.config import DashboardConfig

CF_ACCESS_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "cf_access.py"

TEAM_DOMAIN = "aiesec"
AUD = "app-aud-tag"
ISSUER = f"https://{TEAM_DOMAIN}.cloudflareaccess.com"


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_jwks_cache()
    yield
    reset_jwks_cache()


def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _client_for(public_key):
    """Заменяет `jwt.PyJWKClient` целиком — возвращает объект с `.key`, как настоящий
    `PyJWK`, без сети и без реального kid-разбора JWKS."""
    return SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key))


class _RaisingClient:
    """Симулирует недоступный JWKS-эндпоинт команды."""

    def get_signing_key_from_jwt(self, token):
        raise jwt.PyJWKClientError("simulated network failure")


def _token(
    private_key,
    *,
    email="admin@aiesec.ru",
    aud=AUD,
    iss=ISSUER,
    exp_delta=3600,
    iat_delta=-10,
    algorithm="RS256",
    key=None,
    extra_claims=None,
):
    now = int(time.time())
    payload = {"email": email, "aud": aud, "iss": iss, "iat": now + iat_delta, "exp": now + exp_delta}
    if extra_claims:
        payload.update(extra_claims)
    signing_key = key if key is not None else private_key
    return jwt.encode(payload, signing_key, algorithm=algorithm, headers={"kid": "test-kid"})


def _cfg(**overrides) -> DashboardConfig:
    base = dict(
        db_path="data/forum.db",
        public_url="https://stats.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token="123:abc",
        admin_ids=(1,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
        access_team_domain=TEAM_DOMAIN,
        access_aud=AUD,
        superadmin_emails=("admin@aiesec.ru",),
        access_dev_bypass=False,
    )
    base.update(overrides)
    return DashboardConfig(**base)


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


# ── verify_access_token: позитивный и негативные кейсы ──────────────────────────────────

def test_valid_token_returns_lowercased_email():
    private_key, public_key = _keypair()
    token = _token(private_key, email="Admin@AIESEC.ru")
    cfg = _cfg()
    assert verify_access_token(token, cfg, jwks_client=_client_for(public_key)) == "admin@aiesec.ru"


def test_expired_token_rejected():
    private_key, public_key = _keypair()
    token = _token(private_key, exp_delta=-100, iat_delta=-200)
    cfg = _cfg()
    assert verify_access_token(token, cfg, jwks_client=_client_for(public_key)) is None


def test_wrong_audience_rejected():
    private_key, public_key = _keypair()
    token = _token(private_key, aud="someone-elses-app")
    cfg = _cfg()
    assert verify_access_token(token, cfg, jwks_client=_client_for(public_key)) is None


def test_wrong_issuer_rejected():
    private_key, public_key = _keypair()
    token = _token(private_key, iss="https://other-team.cloudflareaccess.com")
    cfg = _cfg()
    assert verify_access_token(token, cfg, jwks_client=_client_for(public_key)) is None


def test_signed_with_different_key_rejected():
    private_key, _public_key = _keypair()
    _other_private, other_public = _keypair()
    token = _token(private_key)
    cfg = _cfg()
    # JWKS "команды" отдаёт ЧУЖОЙ публичный ключ — подпись не сойдётся.
    assert verify_access_token(token, cfg, jwks_client=_client_for(other_public)) is None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _forge_hs256_token_with_pem_secret(public_pem: bytes, *, email="admin@aiesec.ru") -> str:
    """Собирает JWT вручную (не через `jwt.encode` — та версия PyJWT сама отказывается
    подписывать HMAC PEM-похожим ключом, `InvalidKeyError`). Атакующему собственный код
    PyJWT не мешает: он просто склеивает JOSE вручную, поэтому тест обязан склеить его так же —
    иначе классическая атака alg-confusion (сервер издаёт RS256, приложение по ошибке
    доверяет `alg` из заголовка и проверяет HS256-подпись публичным ключом как секретом)
    осталась бы непроверенной."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": "test-kid"}
    payload = {"email": email, "aud": AUD, "iss": ISSUER, "iat": now - 10, "exp": now + 3600}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def test_hs256_alg_confusion_with_public_key_as_secret_rejected():
    private_key, public_key = _keypair()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = _forge_hs256_token_with_pem_secret(public_pem)
    cfg = _cfg()
    # Явный algorithms=["RS256"] в jwt.decode должен отбить HS256 ДО проверки подписи —
    # то, что "ключ" в jwks_client ниже — RSA-объект, а не PEM-секрет, здесь неважно вовсе:
    # decode обязан споткнуться на алгоритме, а не на несовпадении типа ключа.
    assert verify_access_token(token, cfg, jwks_client=_client_for(public_key)) is None


def test_missing_email_claim_rejected():
    private_key, public_key = _keypair()
    now = int(time.time())
    payload = {"aud": AUD, "iss": ISSUER, "iat": now - 10, "exp": now + 3600}
    token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-kid"})
    cfg = _cfg()
    assert verify_access_token(token, cfg, jwks_client=_client_for(public_key)) is None


def test_unavailable_jwks_returns_none_not_exception():
    private_key, _public_key = _keypair()
    token = _token(private_key)
    cfg = _cfg()
    assert verify_access_token(token, cfg, jwks_client=_RaisingClient()) is None


def test_empty_token_returns_none_without_calling_jwks_client():
    cfg = _cfg()

    class _MustNotBeCalled:
        def get_signing_key_from_jwt(self, token):
            raise AssertionError("не должен вызываться для пустого токена")

    assert verify_access_token("", cfg, jwks_client=_MustNotBeCalled()) is None


# ── require_superadmin_email: заголовок, список e-mail, dev-bypass ──────────────────────

def test_require_superadmin_email_no_header_returns_no_header_reason():
    cfg = _cfg()
    email, reason = require_superadmin_email(_FakeRequest(), cfg)
    assert email is None
    assert reason == "no_header"


def test_require_superadmin_email_valid_token_email_in_list_ok():
    private_key, public_key = _keypair()
    token = _token(private_key, email="admin@aiesec.ru")
    cfg = _cfg(superadmin_emails=("admin@aiesec.ru",))
    request = _FakeRequest({ACCESS_HEADER: token})
    email, reason = require_superadmin_email(request, cfg, jwks_client=_client_for(public_key))
    assert email == "admin@aiesec.ru"
    assert reason == "ok"


def test_require_superadmin_email_valid_token_not_in_list_denied():
    private_key, public_key = _keypair()
    token = _token(private_key, email="stranger@aiesec.ru")
    cfg = _cfg(superadmin_emails=("admin@aiesec.ru",))
    request = _FakeRequest({ACCESS_HEADER: token})
    email, reason = require_superadmin_email(request, cfg, jwks_client=_client_for(public_key))
    assert email is None
    assert reason == "not_allowed"


def test_require_superadmin_email_empty_list_denies_even_valid_token():
    private_key, public_key = _keypair()
    token = _token(private_key, email="admin@aiesec.ru")
    cfg = _cfg(superadmin_emails=())
    request = _FakeRequest({ACCESS_HEADER: token})
    email, reason = require_superadmin_email(request, cfg, jwks_client=_client_for(public_key))
    assert email is None
    assert reason == "not_allowed"


def test_require_superadmin_email_case_and_whitespace_normalized():
    private_key, public_key = _keypair()
    token = _token(private_key, email="  Admin@AIESEC.ru  ")
    cfg = _cfg(superadmin_emails=("admin@aiesec.ru",))
    request = _FakeRequest({ACCESS_HEADER: token})
    email, reason = require_superadmin_email(request, cfg, jwks_client=_client_for(public_key))
    assert email == "admin@aiesec.ru"
    assert reason == "ok"


def test_require_superadmin_email_bad_token_reason():
    cfg = _cfg()
    request = _FakeRequest({ACCESS_HEADER: "not-a-real-jwt"})
    email, reason = require_superadmin_email(request, cfg)
    assert email is None
    assert reason == "bad_token"


def test_require_superadmin_email_dev_bypass_grants_access_and_warns(caplog):
    import logging

    cfg = _cfg(access_dev_bypass=True)
    with caplog.at_level(logging.WARNING, logger="dashboard.cf_access"):
        email, reason = require_superadmin_email(_FakeRequest(), cfg)
    assert email == "dev@local"
    assert reason == "ok"
    assert any("DASHBOARD_ACCESS_DEV_BYPASS" in r.message for r in caplog.records)


# ── логи не содержат токен/claims ────────────────────────────────────────────────────────

def test_rejection_log_has_no_token_or_claims(caplog):
    import logging

    private_key, public_key = _keypair()
    token = _token(private_key, aud="someone-elses-app")
    cfg = _cfg()
    with caplog.at_level(logging.WARNING, logger="dashboard.cf_access"):
        verify_access_token(token, cfg, jwks_client=_client_for(public_key))
    for record in caplog.records:
        assert token not in record.message
        assert "admin@aiesec.ru" not in record.message


# ── JWKS кэш: клиент создаётся один раз на процесс ───────────────────────────────────────

def test_jwks_client_is_a_module_level_singleton(monkeypatch):
    created = []

    class _CountingClient:
        def __init__(self, url, **kwargs):
            created.append(url)

        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientError("stub")

    monkeypatch.setattr("dashboard.cf_access.jwt.PyJWKClient", _CountingClient)
    cfg = _cfg()
    private_key, _public_key = _keypair()
    token = _token(private_key)
    verify_access_token(token, cfg)
    verify_access_token(token, cfg)
    assert len(created) == 1


def test_reset_jwks_cache_forces_new_client(monkeypatch):
    created = []

    class _CountingClient:
        def __init__(self, url, **kwargs):
            created.append(url)

        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientError("stub")

    monkeypatch.setattr("dashboard.cf_access.jwt.PyJWKClient", _CountingClient)
    cfg = _cfg()
    private_key, _public_key = _keypair()
    token = _token(private_key)
    verify_access_token(token, cfg)
    reset_jwks_cache()
    verify_access_token(token, cfg)
    assert len(created) == 2


# ── греп-сторожа: алгоритм задан явно, не читается из токена ────────────────────────────

def test_algorithms_list_is_explicit_rs256_only():
    text = CF_ACCESS_FILE.read_text(encoding="utf-8")
    assert 'algorithms=["RS256"]' in text
    assert "get_unverified_header" not in text
    assert "options.get(\"alg\")" not in text
