"""Приёмник вебхука Яндекс Форм: POST /app/hooks/yform/{secret}."""
from __future__ import annotations

import asyncio
import logging

import pytest

from database import db as bot_db
from database import ext_forms_db

from tests.test_miniapp_routes import _cfg, _client, _set, _use_tmp_db

SECRET = "SECRET123abc"
FORM_EXT = "0123456789abcdef01234567"


@pytest.fixture
def env(tmp_path):
    path = _use_tmp_db(tmp_path)
    form_id = asyncio.run(
        ext_forms_db.create_form(platform="yandex", external_id=FORM_EXT, title="T", secret=SECRET)
    )
    return path, form_id


def _count() -> int:
    async def run():
        async with bot_db._connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM external_form_pending")
            return (await cur.fetchone())[0]

    return asyncio.run(run())


def _rows():
    async def run():
        async with bot_db._connect() as db:
            cur = await db.execute("SELECT form_id, answer_id, delivery_id FROM external_form_pending")
            return [tuple(r) for r in await cur.fetchall()]

    return asyncio.run(run())


def _post(client, secret=SECRET, form_ext=FORM_EXT, answer="123", body=None, delivery="d-1"):
    headers = {}
    if form_ext is not None:
        headers["X-Form-Id"] = form_ext
    if answer is not None:
        headers["X-Form-Answer-Id"] = answer
    if delivery is not None:
        headers["X-Delivery-Id"] = delivery
    kw = {"json": body} if body is not None else {"content": b""}
    return client.post(f"/app/hooks/yform/{secret}", headers=headers, **kw)


BODY = {"jsonrpc": "2.0", "method": "x", "params": {}, "id": 7}


def test_ok_writes_pending_and_rpc_reply(env):
    path, form_id = env
    r = _post(_client(_cfg(path)), body=BODY)
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "result": "ok", "id": 7}
    assert _rows() == [(form_id, "123", "d-1")]


def test_repeat_delivery_is_idempotent(env):
    path, _ = env
    c = _client(_cfg(path))
    assert _post(c, body=BODY).status_code == 200
    assert _post(c, body=BODY).status_code == 200
    assert _count() == 1


def test_unknown_secret_403(env):
    path, _ = env
    r = _post(_client(_cfg(path)), secret="nope", body=BODY)
    assert r.status_code == 403
    assert _count() == 0


def test_foreign_form_id_403(env):
    path, _ = env
    r = _post(_client(_cfg(path)), form_ext="f" * 24, body=BODY)
    assert r.status_code == 403
    assert _count() == 0


def test_missing_form_id_403(env):
    path, _ = env
    r = _post(_client(_cfg(path)), form_ext=None, body=BODY)
    assert r.status_code == 403
    assert _count() == 0


@pytest.mark.parametrize("status", ["paused", "disabled"])
def test_inactive_form_ignored(env, status):
    path, form_id = env

    async def run():
        async with bot_db._connect() as db:
            await db.execute("UPDATE external_forms SET status = ? WHERE id = ?", (status, form_id))
            await db.commit()

    asyncio.run(run())
    r = _post(_client(_cfg(path)), body=BODY)
    assert r.status_code == 200
    assert r.json()["result"] == "ignored"
    assert _count() == 0


@pytest.mark.parametrize("answer", [None, "abc", "12x", "1" * 21, ""])
def test_bad_answer_id_ok_without_row(env, answer):
    path, _ = env
    r = _post(_client(_cfg(path)), answer=answer, body=BODY)
    assert r.status_code == 200
    assert r.json()["result"] == "ok"
    assert _count() == 0


def test_non_json_body_gives_null_id(env):
    path, _ = env
    r = _client(_cfg(path)).post(
        f"/app/hooks/yform/{SECRET}",
        headers={"X-Form-Id": FORM_EXT, "X-Form-Answer-Id": "5"},
        content=b"not json",
    )
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "result": "ok", "id": None}
    assert _count() == 1


def test_empty_body_gives_null_id(env):
    path, _ = env
    r = _post(_client(_cfg(path)))
    assert r.status_code == 200
    assert r.json()["id"] is None


def test_non_yandex_platform_403(env):
    path, form_id = env

    async def run():
        async with bot_db._connect() as db:
            await db.execute("UPDATE external_forms SET platform = 'google' WHERE id = ?", (form_id,))
            await db.commit()

    asyncio.run(run())
    r = _post(_client(_cfg(path)), body=BODY)
    assert r.status_code == 403
    assert _count() == 0


def test_hook_works_when_miniapp_off_but_api_gated(env):
    path, _ = env
    _set("miniapp_enabled", "off")
    c = _client(_cfg(path))
    r = _post(c, body=BODY)
    assert r.status_code == 200
    assert _count() == 1
    # Выключено = закрыто делегатам; без личности API по-прежнему закрыт (401, не данные).
    gated = c.get("/app/api/hub")
    assert gated.status_code == 401
    assert gated.json()["reason"] == "no_auth"


def test_access_log_masks_secret():
    from miniapp.logging_config import HookSecretFilter

    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", "/app/hooks/yform/SECRET123", "1.1", 200), None,
    )
    assert HookSecretFilter().filter(rec) is True
    text = rec.getMessage()
    assert "/app/hooks/yform/***" in text
    assert "SECRET123" not in text


def test_configure_logging_attaches_filter_to_access_logger():
    from miniapp.logging_config import HookSecretFilter, configure_logging

    configure_logging()
    assert any(isinstance(f, HookSecretFilter) for f in logging.getLogger("uvicorn.access").filters)
