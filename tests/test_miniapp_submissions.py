"""Phase 19 Plan 04 Task 2 (WEBAPP-01, D-03/D-05, T-19-18/T-19-21/T-19-22/T-19-23/T-19-77):
`POST /app/api/uploads` и `POST /app/api/submissions`.

Bot API подменяется через `httpx.MockTransport` (monkeypatch `telegram_api._make_client`) —
ни одного реального сетевого вызова. Харнесс — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import aiosqlite
import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api
from miniapp.routers.submissions import make_part_token

from tests.test_miniapp_auth import TOKEN
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    GAME_MANAGER_ID,
    PENDING_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

SECRET = "test-session-secret"
MB = 1024 * 1024


def _run(coro):
    return asyncio.run(coro)


async def _exec(query, params=()):
    async with bot_db._connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def _fetchall(query, params=()):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


class FakeBotApi:
    """Записывает вызовы Bot API; отвечает как Telegram: photo -> result.photo[], документ ->
    result.document."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert f"/bot{TOKEN}/" in request.url.path  # токен — только в URL к Telegram
        body = request.content
        call = {"method": method, "content_length": len(body)}
        # Многочастное тело: вытащим chat_id и caption, не разбирая файл целиком.
        for field in ("chat_id", "caption"):
            marker = f'name="{field}"\r\n\r\n'.encode()
            i = body.find(marker)
            if i >= 0:
                j = body.find(b"\r\n", i + len(marker))
                call[field] = body[i + len(marker):j].decode()
        self.calls.append(call)
        if self.fail:
            return httpx.Response(502, text="bad gateway")
        if method == "sendPhoto":
            result = {"message_id": 1, "photo": [{"file_id": "small"}, {"file_id": "AgACphotoBIG"}]}
        elif method == "sendDocument":
            result = {"message_id": 2, "document": {"file_id": "BQACdocument"}}
        else:
            result = {"message_id": 3}
        return httpx.Response(200, json={"ok": True, "result": result})


@pytest.fixture
def bot_api(monkeypatch):
    fake = FakeBotApi()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_submissions.db")
    _standard_seed()
    return _client(_cfg(db_path))


def _upload(client, user_id, data: bytes, *, name="pic.jpg", ctype="image/jpeg", headers=None):
    return client.post(
        "/app/api/uploads", files={"file": (name, data, ctype)},
        headers={**_hdr(user_id), **(headers or {})},
    )


def _task(**kw) -> int:
    deadline = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    return _run(bot_db.create_task(
        "Сфоткай стенд", "Light", 10, "photo", deadline, None,
        title=kw.get("title", "Стенд"), event_city=kw.get("city"),
    ))


def _file_part(user_id, kind, file_id, secret=SECRET):
    return {"kind": kind, "content": file_id, "part_token": make_part_token(secret, user_id, kind, file_id)}


# ── POST /app/api/uploads ────────────────────────────────────────────────────────────────

def test_upload_image_goes_as_photo_with_delegate_caption_and_token(client, bot_api):
    resp = _upload(client, DELEGATE_ID, b"\xff\xd8" + b"x" * 1000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "photo" and body["content"] == "AgACphotoBIG"
    assert body["part_token"] == make_part_token(SECRET, DELEGATE_ID, "photo", "AgACphotoBIG")
    assert len(bot_api.calls) == 1
    call = bot_api.calls[0]
    assert call["method"] == "sendPhoto"
    assert call["chat_id"] == str(DELEGATE_ID)  # в чат самого загружающего
    assert call["caption"] == "копия сдачи"     # дефолт miniapp_upload_caption_delegate
    assert TOKEN not in resp.text


def test_upload_uses_registry_caption_truncated_to_1024(client, bot_api):
    _set("miniapp_upload_caption_delegate", "к" * 3000)
    resp = _upload(client, DELEGATE_ID, b"img")
    assert resp.status_code == 200
    assert bot_api.calls[0]["caption"] == "к" * 1024


def test_upload_by_game_manager_uses_staff_caption(client, bot_api):
    _set("miniapp_upload_caption_staff", "обложка из приложения")
    resp = _upload(client, GAME_MANAGER_ID, b"img", name="cover.png", ctype="image/png")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "photo"
    call = bot_api.calls[0]
    assert call["chat_id"] == str(GAME_MANAGER_ID)
    assert call["caption"] == "обложка из приложения"
    assert resp.json()["part_token"] == make_part_token(SECRET, GAME_MANAGER_ID, "photo", "AgACphotoBIG")


def test_upload_non_image_goes_as_document(client, bot_api):
    resp = _upload(client, DELEGATE_ID, b"%PDF-1.4", name="proof.pdf", ctype="application/pdf")
    assert resp.status_code == 200
    assert resp.json() == {
        "kind": "document", "content": "BQACdocument",
        "part_token": make_part_token(SECRET, DELEGATE_ID, "document", "BQACdocument"),
    }
    assert bot_api.calls[0]["method"] == "sendDocument"


def test_upload_image_over_10mb_goes_as_document(client, bot_api):
    resp = _upload(client, DELEGATE_ID, b"x" * (10 * MB + 1))
    assert resp.status_code == 200
    assert resp.json()["kind"] == "document"
    assert bot_api.calls[0]["method"] == "sendDocument"


def test_upload_over_20mb_rejected_by_content_length_before_parsing(client, bot_api):
    resp = _upload(client, DELEGATE_ID, b"x" * 10, headers={"Content-Length": str(30 * MB)})
    assert resp.status_code == 413
    assert resp.json() == {"reason": "too_large", "limit": 20 * MB}
    assert bot_api.calls == []


def test_upload_over_20mb_rejected_by_chunked_read(client, bot_api):
    resp = _upload(client, DELEGATE_ID, b"x" * (20 * MB + 1))
    assert resp.status_code == 413
    assert resp.json()["reason"] == "too_large"
    assert bot_api.calls == []


def test_upload_pending_delegate_without_cap_forbidden(client, bot_api):
    resp = _upload(client, PENDING_ID, b"img")
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": "pending"}
    assert bot_api.calls == []


def test_upload_without_file_is_400(client, bot_api):
    resp = client.post("/app/api/uploads", data={"x": "1"}, headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 400
    assert resp.json()["reason"] == "no_file"


def test_upload_bot_api_failure_is_502_without_token(client, bot_api, caplog):
    bot_api.fail = True
    with caplog.at_level(logging.WARNING):
        resp = _upload(client, DELEGATE_ID, b"img")
    assert resp.status_code == 502
    assert resp.json()["reason"] == "telegram_unavailable"
    assert TOKEN not in resp.text
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_upload_unreachable_upstream_logs_only_exception_class(client, monkeypatch, caplog):
    def boom(request):
        raise httpx.ConnectError("kaboom", request=request)

    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
    )
    with caplog.at_level(logging.WARNING):
        resp = _upload(client, DELEGATE_ID, b"img")
    assert resp.status_code == 502
    assert any("ConnectError" in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in r.getMessage() and "api.telegram.org" not in r.getMessage()
               for r in caplog.records)


def test_real_client_factory_uses_proxy_from_config(tmp_path, monkeypatch):
    captured = {}

    class _Spy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(telegram_api.httpx, "AsyncClient", _Spy)
    cfg = _cfg(str(tmp_path / "x.db"), proxy_url="socks5://127.0.0.1:1080")
    telegram_api._make_client(cfg, 5.0)
    assert captured == {"proxy": "socks5://127.0.0.1:1080", "timeout": 5.0}


# ── POST /app/api/submissions ────────────────────────────────────────────────────────────

def _submit(client, user_id, task_id, parts):
    return client.post("/app/api/submissions", json={"task_id": task_id, "parts": parts},
                       headers=_hdr(user_id))


def test_finalize_creates_submission_parts_and_outbox_row(client, caplog):
    task_id = _task()
    parts = [
        _file_part(DELEGATE_ID, "photo", "AgACphotoBIG"),
        {"kind": "text", "content": "https://example.com/proof"},
        _file_part(DELEGATE_ID, "document", "BQACdocument") | {"caption": "чек"},
        {"kind": "text", "content": "комментарий"},
    ]
    with caplog.at_level(logging.INFO):
        resp = _submit(client, DELEGATE_ID, task_id, parts)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid = body["submission_id"]
    assert body["accepted_text"]  # дефолт game_submit_accepted_text

    sub = _run(bot_db.get_submission(sid))
    assert sub["task_id"] == task_id and sub["user_id"] == DELEGATE_ID
    assert sub["content_type"] == "photo" and sub["content"] == "AgACphotoBIG"  # как у бота
    assert sub["status"] == "pending"
    rows = _run(bot_db.list_submission_parts(sid))
    assert [(r["ord"], r["kind"], r["content"], r["caption"]) for r in rows] == [
        (0, "photo", "AgACphotoBIG", None),
        (1, "link", "https://example.com/proof", None),  # переклассифицировано как у _classify_part
        (2, "document", "BQACdocument", "чек"),
        (3, "text", "комментарий", None),
    ]
    outbox = _run(bot_db.list_unprocessed_miniapp_outbox())
    assert len(outbox) == 1
    assert outbox[0]["kind"] == "submission_created"
    assert outbox[0]["payload"] == {
        "submission_id": sid, "user_id": DELEGATE_ID, "task_id": task_id,
        "task_text": "Сфоткай стенд", "submitter_name": f"User {DELEGATE_ID}",
    }
    assert not [r for r in outbox if "coin" in r["kind"]]  # монет в outbox нет
    assert TOKEN not in resp.text
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_finalize_rejects_forged_or_foreign_part_token(client):
    task_id = _task()
    forged = {"kind": "photo", "content": "AgACphotoBIG", "part_token": "00" * 32}
    assert _submit(client, DELEGATE_ID, task_id, [forged]).json() == {"reason": "bad_part_token"}
    foreign = _file_part(PENDING_ID, "photo", "AgACphotoBIG")  # подписано для другого id
    resp = _submit(client, DELEGATE_ID, task_id, [foreign])
    assert resp.status_code == 403 and resp.json()["reason"] == "bad_part_token"
    missing = {"kind": "document", "content": "BQACdocument"}
    assert _submit(client, DELEGATE_ID, task_id, [missing]).status_code == 403
    wrong_kind = _file_part(DELEGATE_ID, "photo", "AgACphotoBIG") | {"kind": "document"}
    assert _submit(client, DELEGATE_ID, task_id, [wrong_kind]).status_code == 403
    assert _run(_fetchall("SELECT id FROM game_submissions")) == []


def test_finalize_limits_parts_and_truncates_text(client):
    task_id = _task()
    too_many = [{"kind": "text", "content": str(i)} for i in range(21)]
    resp = _submit(client, DELEGATE_ID, task_id, too_many)
    assert resp.status_code == 400 and resp.json() == {"reason": "too_many_parts", "limit": 20}

    resp = _submit(client, DELEGATE_ID, task_id, [{"kind": "text", "content": "д" * 1500}])
    assert resp.status_code == 200
    part = _run(bot_db.list_submission_parts(resp.json()["submission_id"]))[0]
    assert len(part["content"]) == 1000


def test_finalize_empty_unknown_kind_and_missing_task(client):
    task_id = _task()
    resp = _submit(client, DELEGATE_ID, task_id, [])
    assert resp.status_code == 400 and resp.json()["reason"] == "empty" and resp.json()["hint"]
    resp = _submit(client, DELEGATE_ID, task_id, [{"kind": "video", "content": "x"}])
    assert resp.status_code == 400 and resp.json()["reason"] == "bad_part"
    resp = _submit(client, DELEGATE_ID, 9999, [{"kind": "text", "content": "x"}])
    assert resp.status_code == 404 and resp.json()["reason"] == "task_not_found"


def test_finalize_twice_is_409_without_second_row(client):
    task_id = _task()
    first = _submit(client, DELEGATE_ID, task_id, [{"kind": "text", "content": "раз"}])
    assert first.status_code == 200
    second = _submit(client, DELEGATE_ID, task_id, [{"kind": "text", "content": "два"}])
    assert second.status_code == 409 and second.json() == {"reason": "already_submitted"}
    assert len(_run(_fetchall("SELECT id FROM game_submissions"))) == 1
    assert len(_run(bot_db.list_unprocessed_miniapp_outbox())) == 1


def test_finalize_race_with_bot_hits_unique_index(client, monkeypatch):
    """Состояние говорит «можно», а вставка отвергнута индексом (бот успел первым)."""
    import miniapp.routers.submissions as mod

    task_id = _task()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(mod, "create_submission", _none)
    resp = _submit(client, DELEGATE_ID, task_id, [{"kind": "text", "content": "x"}])
    assert resp.status_code == 409 and resp.json()["reason"] == "already_submitted"
    assert _run(bot_db.list_unprocessed_miniapp_outbox()) == []


def test_finalize_gated_pending_and_section_off(client):
    task_id = _task()
    resp = _submit(client, PENDING_ID, task_id, [{"kind": "text", "content": "x"}])
    assert resp.status_code == 403 and resp.json()["kind"] == "pending"
    _set("miniapp_section_tasks", "off")
    resp = _submit(client, DELEGATE_ID, task_id, [{"kind": "text", "content": "x"}])
    assert resp.status_code == 403 and resp.json()["reason"] == "section_off"


def test_finalize_other_city_task_is_not_found(client):
    """WR-06 как в боте: задание другого города для делегата не существует."""
    _set("event_city_enabled", "on")
    _run(_exec("UPDATE users SET event_city = ? WHERE telegram_id = ?", ("msk", DELEGATE_ID)))
    task_id = _task(city="spb")
    resp = _submit(client, DELEGATE_ID, task_id, [{"kind": "text", "content": "x"}])
    assert resp.status_code == 404 and resp.json()["reason"] == "task_not_found"
    own = _task(city="msk")
    assert _submit(client, DELEGATE_ID, own, [{"kind": "text", "content": "x"}]).status_code == 200
