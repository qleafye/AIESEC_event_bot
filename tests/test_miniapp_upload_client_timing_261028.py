"""Backlog п.25: серверная часть загрузки быстрая (медиана ~1 с), а путь телефон → Cloudflare →
туннель → сервер не измерен. Теперь каждый ответ `POST /app/api/uploads` несёт `X-Upload-Ref`,
строка лога загрузки оканчивается `ref=…`, а Mini App отдельным лёгким запросом
`POST /app/api/uploads/timing` сообщает своё время (`client_ms`) — сервер пишет его строкой
`uploads.client:` рядом с серверными этапами той же загрузки.

Попутно: отказ резюме по формату (`bad_type`) называет принимаемые форматы («PDF, DOCX») и
объясняет, как пересохранить файл — текстом реестра и в Mini App, и в чате.
"""
from __future__ import annotations

import asyncio
import logging
import re

import pytest

import domain.regform.engine as reg_engine
from miniapp.routers import submissions

from tests.test_miniapp_routes import ADMIN_ID, _hdr
from tests.test_miniapp_submissions import (  # noqa: F401 — фикстура client
    DELEGATE_ID,
    _upload,
    client,
)
from tests.test_miniapp_upload_photo_fallback_260927 import _reject, api  # noqa: F401 — фикстура api
from tests.test_miniapp_upload_timing_261011 import LOGGER, _upload_lines

REF_RE = re.compile(r"^[0-9a-f]{12}$")
CLIENT_RE = re.compile(
    r"^uploads\.client: ref=(?P<ref>\S+) target=(?P<target>\S+) size=(?P<size>\S+) status=(?P<status>\S+) "
    r"read_ms=(?P<read>\S+) telegram_ms=(?P<tg>\S+) total_ms=(?P<total>\S+) client_ms=(?P<client>\S+)$"
)


@pytest.fixture(autouse=True)
def _clean_recent():
    submissions._RECENT_UPLOADS.clear()
    yield
    submissions._RECENT_UPLOADS.clear()


def _client_lines(caplog) -> list[re.Match]:
    lines = [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    return [m for m in (CLIENT_RE.match(line) for line in lines) if m]


def _timing(client, user_id, payload=None, *, content=None):
    headers = {**_hdr(user_id)}
    if content is not None:
        headers["Content-Type"] = "application/json"
        return client.post("/app/api/uploads/timing", content=content, headers=headers)
    return client.post("/app/api/uploads/timing", json=payload, headers=headers)


def _report(client, caplog, user_id, payload=None, *, content=None) -> re.Match:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        resp = _timing(client, user_id, payload, content=content)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    hits = _client_lines(caplog)
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    return hits[0]


# ── X-Upload-Ref и ref в строке загрузки ────────────────────────────────────────────────────

def test_success_carries_ref_header_and_ref_in_log_line(client, api, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        resp = _upload(client, DELEGATE_ID, b"\xff\xd8\xff" + b"j" * 100, name="a.jpg", ctype="image/jpeg")
    assert resp.status_code == 200, resp.text
    ref = resp.headers.get("X-Upload-Ref")
    assert ref and REF_RE.match(ref)
    assert set(resp.json()) == {"kind", "content", "part_token"}
    (m,) = _upload_lines(caplog)
    assert m["ref"] == ref


def test_telegram_failure_and_empty_file_carry_ref_too(client, api, caplog):
    api.responses["sendPhoto"] = _reject()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        bad = _upload(client, ADMIN_ID, b"img", name="cover.jpg", ctype="image/jpeg", target="task_cover")
        empty = _upload(client, DELEGATE_ID, b"", name="empty.jpg", ctype="image/jpeg")
    assert bad.status_code == 502 and empty.status_code == 400
    refs = [bad.headers.get("X-Upload-Ref"), empty.headers.get("X-Upload-Ref")]
    assert all(r and REF_RE.match(r) for r in refs)
    assert refs[0] != refs[1]
    assert [m["ref"] for m in _upload_lines(caplog)] == refs


def test_unauthorized_upload_has_no_ref(client, api):
    resp = client.post("/app/api/uploads", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert resp.status_code == 401
    assert "X-Upload-Ref" not in resp.headers


# ── POST /app/api/uploads/timing ───────────────────────────────────────────────────────────

def _do_upload(client, user_id=DELEGATE_ID, data=b"%PDF-1.4 doc"):
    resp = _upload(client, user_id, data, name="doc.pdf", ctype="application/pdf")
    assert resp.status_code == 200, resp.text
    return resp.headers["X-Upload-Ref"]


def test_report_joins_server_stages(client, api, caplog):
    data = b"%PDF-1.4 doc"
    ref = _do_upload(client, data=data)
    m = _report(client, caplog, DELEGATE_ID,
                {"ref": ref, "client_ms": 1234, "status": 200, "target": "task", "size": 999999})
    assert m["ref"] == ref and m["target"] == "task"
    assert m["size"] == str(len(data))  # серверный размер, а не из тела
    assert m["status"] == "200"
    assert m["read"].isdigit() and m["tg"].isdigit() and m["total"].isdigit()
    assert m["client"] == "1234"


def test_second_report_on_same_ref_is_unknown(client, api, caplog):
    ref = _do_upload(client)
    _report(client, caplog, DELEGATE_ID, {"ref": ref, "client_ms": 10, "status": 200})
    m = _report(client, caplog, DELEGATE_ID, {"ref": ref, "client_ms": 10, "status": 200})
    assert m["ref"] == "—" and m["tg"] == "—" and m["total"] == "—"


def test_foreign_ref_is_not_joined_and_not_consumed(client, api, caplog):
    ref = _do_upload(client, user_id=ADMIN_ID)
    m = _report(client, caplog, DELEGATE_ID, {"ref": ref, "client_ms": 50, "status": 200})
    assert m["ref"] == "—" and m["tg"] == "—"
    m = _report(client, caplog, ADMIN_ID, {"ref": ref, "client_ms": 50, "status": 200})
    assert m["ref"] == ref and m["tg"].isdigit()


@pytest.mark.parametrize("value", [-1, 600001, 12.5, "1200", True, None, "absent"])
def test_bad_client_ms_is_dash(client, api, caplog, value):
    payload = {"ref": None, "status": 200, "target": "task"}
    if value != "absent":
        payload["client_ms"] = value
    m = _report(client, caplog, DELEGATE_ID, payload)
    assert m["client"] == "—"


@pytest.mark.parametrize("value", [0, 600000])
def test_client_ms_bounds_inclusive(client, api, caplog, value):
    m = _report(client, caplog, DELEGATE_ID, {"ref": None, "client_ms": value, "status": 200})
    assert m["client"] == str(value)


def test_network_failure_on_phone(client, api, caplog):
    m = _report(client, caplog, DELEGATE_ID,
                {"ref": None, "client_ms": 30000, "status": 0, "target": "resume", "size": 2048})
    assert m.group(0) == (
        "uploads.client: ref=— target=resume size=2048 status=0 read_ms=— telegram_ms=— "
        "total_ms=— client_ms=30000"
    )


def test_body_fields_are_sanitized(client, api, caplog):
    m = _report(client, caplog, DELEGATE_ID, {
        "ref": "ABCDEF012345", "client_ms": 5, "status": 700, "target": "x\ny" * 30,
        "size": 10 ** 12,
    })
    assert m["ref"] == "—" and m["status"] == "—" and m["size"] == "—"
    assert "\n" not in m.group(0)
    assert len(m["target"]) <= submissions.LOG_VALUE_MAX


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b""])
def test_broken_body_still_204(client, api, caplog, content):
    m = _report(client, caplog, DELEGATE_ID, content=content)
    assert m["ref"] == "—" and m["client"] == "—" and m["target"] == "task"


def test_report_requires_auth(client, api):
    resp = client.post("/app/api/uploads/timing", json={"ref": None, "client_ms": 1})
    assert resp.status_code == 401


def test_recent_uploads_are_bounded(client, api, caplog, monkeypatch):
    monkeypatch.setattr(submissions, "RECENT_UPLOADS_MAX", 2)
    refs = [_do_upload(client) for _ in range(3)]
    assert len(submissions._RECENT_UPLOADS) == 2
    m = _report(client, caplog, DELEGATE_ID, {"ref": refs[0], "client_ms": 1, "status": 200})
    assert m["ref"] == "—"
    m = _report(client, caplog, DELEGATE_ID, {"ref": refs[2], "client_ms": 1, "status": 200})
    assert m["ref"] == refs[2]
