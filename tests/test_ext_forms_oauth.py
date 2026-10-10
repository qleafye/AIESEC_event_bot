"""OAuth Яндекса и аксессор ключей приложения (D-04, D-18): моки, без сети."""
import asyncio
import logging

import httpx
import pytest
from pydantic import SecretStr

from config import config
from database import ext_forms_db as ef
from shared.secret_redact import redact_secrets
from services.ext_forms import ext_forms_yandex as Y
from tests._dbtpl import fast_init_db

ACCESS = "acc-secret-token-1234567890"
REFRESH = "ref-secret-token-1234567890"


def _ready(tmp_path, monkeypatch, env_id="", env_secret=None):
    config.DB_PATH = str(tmp_path / "oauth.db")
    fast_init_db()
    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_ID", env_id)
    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_SECRET", env_secret)


def _patch(monkeypatch, handler):
    monkeypatch.setattr(
        Y, "_make_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))


def test_creds_db_first_then_env_then_none(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    assert asyncio.run(Y.get_yandex_app_creds()) is None

    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setattr(config, "YANDEX_OAUTH_CLIENT_SECRET", SecretStr("env-secret-value-123"))
    assert asyncio.run(Y.get_yandex_app_creds()) == ("env-id", "env-secret-value-123")

    asyncio.run(ef.set_app_secret("yandex_client_id", "db-id", 1))
    asyncio.run(ef.set_app_secret("yandex_client_secret", "db-secret-value-123", 1))
    assert asyncio.run(Y.get_yandex_app_creds()) == ("db-id", "db-secret-value-123")


def test_exchange_code_ok_and_no_leak(tmp_path, monkeypatch, caplog):
    _ready(tmp_path, monkeypatch, "cid", SecretStr("client-secret-value-123"))
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH,
                                         "expires_in": 3600, "token_type": "bearer"})

    _patch(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        res = asyncio.run(Y.exchange_code(" 1234567 "))
    assert res["access_token"] == ACCESS and res["refresh_token"] == REFRESH
    assert res["expires_at"] and len(res["expires_at"]) == 19
    assert "grant_type=authorization_code" in seen["body"] and "code=1234567" in seen["body"]
    assert ACCESS not in redact_secrets(f"x {ACCESS}")
    assert REFRESH not in redact_secrets(REFRESH)
    assert "client-secret-value-123" not in redact_secrets("client-secret-value-123")
    assert ACCESS not in caplog.text and REFRESH not in caplog.text


def test_exchange_code_bad_code(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, "cid", SecretStr("client-secret-value-123"))
    _patch(monkeypatch, lambda req: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(Y.YandexApiError) as ei:
        asyncio.run(Y.exchange_code("0000000"))
    assert ei.value.reason == "bad_code"


def test_exchange_code_no_keys(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    _patch(monkeypatch, lambda req: httpx.Response(200, json={}))
    with pytest.raises(Y.YandexApiError) as ei:
        asyncio.run(Y.exchange_code("1234567"))
    assert ei.value.reason == "no_app_keys"


def test_refresh_token(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, "cid", SecretStr("client-secret-value-123"))
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 100})

    _patch(monkeypatch, handler)
    res = asyncio.run(Y.refresh_token({"refresh_token": REFRESH}))
    assert "grant_type=refresh_token" in seen["body"]
    assert res["access_token"] == ACCESS
    assert res["refresh_token"] == REFRESH  # сервер не вернул новый — остаётся старый


def test_authorize_url():
    url = Y.authorize_url("my-id")
    assert url.startswith("https://oauth.yandex.ru/authorize?")
    assert "client_id=my-id" in url and "response_type=code" in url and "force_confirm=yes" in url
