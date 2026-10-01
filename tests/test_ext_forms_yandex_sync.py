"""Дочитывание очереди Яндекс Форм, токен, бэкфилл, сверка (MockTransport, без сети)."""
import asyncio
import json
import os
from datetime import datetime, timedelta

import httpx

from config import config
from database import db
from database import ext_forms_db as ef
from services import ext_forms_yandex as Y
from services import ext_forms_yandex_sync as S
from services.sheet_arrival_sync import backoff_seconds
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ext_forms")
_FMT = "%Y-%m-%d %H:%M:%S"


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def _patch(monkeypatch, handler):
    monkeypatch.setattr(
        Y, "_make_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))


def _setup(tmp_path, expires_at=None):
    config.DB_PATH = str(tmp_path / "ext_sync.db")
    fast_init_db()

    async def go():
        cid = await ef.create_connection(
            platform="yandex", org_id=None, org_header=None, access_token="tok-" + "a" * 20,
            refresh_token="ref-" + "b" * 20, expires_at=expires_at, created_by=1)
        fid = await ef.create_form(platform="yandex", external_id="6a94ad5c1f1eb5c9649c12bd",
                                   title="Ф", connection_id=cid)
        return cid, fid
    return asyncio.run(go())


def _enqueue(fid, *ids):
    async def go():
        now = msk_now().strftime(_FMT)
        for i in ids:
            await ef.enqueue_pending(fid, i, None, now)
    asyncio.run(go())


def _pending(fid):
    async def go():
        async with db._connect() as c:
            async with c.execute(
                    "SELECT answer_id, attempts, next_try_at, last_error FROM external_form_pending "
                    "WHERE form_id = ? ORDER BY id", (fid,)) as cur:
                return await cur.fetchall()
    return asyncio.run(go())


def _conn(cid):
    return asyncio.run(ef.get_connection(cid))


def test_drain_done(tmp_path, monkeypatch):
    _, fid = _setup(tmp_path)
    _enqueue(fid, "101")
    _patch(monkeypatch, lambda r: httpx.Response(200, json=_load("synthetic_answer_single.json")))
    res = asyncio.run(S.drain_pending())
    assert res["done"] == 1
    assert _pending(fid) == []
    assert asyncio.run(ef.known_answer_ids(fid)) == {"101"}


def test_drain_retry_on_5xx_429_and_network(tmp_path, monkeypatch):
    _, fid = _setup(tmp_path)
    for mode in ("500", "429", "net"):
        async def clear():
            async with db._connect() as c:
                await c.execute("DELETE FROM external_form_pending")
                await c.commit()
        asyncio.run(clear())
        _enqueue(fid, "101")

        def handler(r, mode=mode):
            if mode == "net":
                raise httpx.ConnectError("x")
            return httpx.Response(int(mode), json={})
        _patch(monkeypatch, handler)
        before = msk_now()
        res = asyncio.run(S.drain_pending())
        assert res["retry"] == 1
        (aid, attempts, nxt, err), = _pending(fid)
        assert (aid, attempts) == ("101", 1)
        due = before + timedelta(seconds=backoff_seconds(1))
        assert abs((datetime.strptime(nxt, _FMT) - due).total_seconds()) < 5
        assert err in ("upstream_unavailable", "rate_limited")


def test_drain_404_drops(tmp_path, monkeypatch):
    _, fid = _setup(tmp_path)
    _enqueue(fid, "101")
    _patch(monkeypatch, lambda r: httpx.Response(404, json={}))
    assert asyncio.run(S.drain_pending())["dropped"] == 1
    assert _pending(fid) == []


def test_drain_401_marks_reauth_and_skips_rest(tmp_path, monkeypatch):
    cid, fid = _setup(tmp_path)
    _enqueue(fid, "101", "102")
    calls = []

    def handler(r):
        calls.append(r.url.query)
        return httpx.Response(401, json={})
    _patch(monkeypatch, handler)
    res = asyncio.run(S.drain_pending())
    assert len(calls) == 1
    assert res["reauth"] == 2
    c = _conn(cid)
    assert c["status"] == "needs_reauth" and c["alerted_at"] is None
    assert [p[0] for p in _pending(fid)] == ["101", "102"]
    assert all(p[1] == 0 for p in _pending(fid))


def test_drain_needs_reauth_no_api_call(tmp_path, monkeypatch):
    cid, fid = _setup(tmp_path)
    asyncio.run(ef.set_connection_status(cid, "needs_reauth"))
    _enqueue(fid, "101")

    def handler(r):
        raise AssertionError("API не должен вызываться")
    _patch(monkeypatch, handler)
    res = asyncio.run(S.drain_pending())
    assert res["reauth"] == 1
    (aid, attempts, nxt, _), = _pending(fid)
    assert attempts == 0
    assert nxt > (msk_now() + timedelta(minutes=29)).strftime(_FMT)


def test_drain_form_deleted(tmp_path, monkeypatch):
    _, fid = _setup(tmp_path)
    _enqueue(fid, "101")

    async def rm():
        async with db._connect() as c:
            await c.execute("DELETE FROM external_forms WHERE id = ?", (fid,))
            await c.commit()
    asyncio.run(rm())
    assert asyncio.run(S.drain_pending())["dropped"] == 1
    assert _pending(fid) == []


def test_token_refreshed_before_answer(tmp_path, monkeypatch):
    soon = (msk_now() + timedelta(days=3)).strftime(_FMT)
    cid, fid = _setup(tmp_path, expires_at=soon)
    _enqueue(fid, "101")
    seen = []

    def handler(r):
        if "oauth.yandex.ru" in str(r.url):
            seen.append("refresh")
            return httpx.Response(200, json={"access_token": "new-" + "c" * 20,
                                             "refresh_token": "ref2-" + "d" * 20,
                                             "expires_in": 31536000})
        seen.append(r.headers["Authorization"])
        return httpx.Response(200, json=_load("synthetic_answer_single.json"))
    _patch(monkeypatch, handler)

    async def creds():
        return ("cid", "secret")
    monkeypatch.setattr(Y, "get_yandex_app_creds", creds)
    assert asyncio.run(S.drain_pending())["done"] == 1
    assert seen[0] == "refresh" and seen[1] == "OAuth new-" + "c" * 20
    assert _conn(cid)["access_token"] == "new-" + "c" * 20


def test_token_refresh_unauthorized_marks_reauth(tmp_path, monkeypatch):
    soon = (msk_now() + timedelta(days=3)).strftime(_FMT)
    cid, fid = _setup(tmp_path, expires_at=soon)
    _enqueue(fid, "101")

    def handler(r):
        if "oauth.yandex.ru" in str(r.url):
            return httpx.Response(401, json={})
        raise AssertionError("get_answer не должен вызываться")
    _patch(monkeypatch, handler)

    async def creds():
        return ("cid", "secret")
    monkeypatch.setattr(Y, "get_yandex_app_creds", creds)
    assert asyncio.run(S.drain_pending())["reauth"] == 1
    assert _conn(cid)["status"] == "needs_reauth"


# ---------------- бэкфилл и сверка ----------------

def _list_handler(r):
    url = str(r.url)
    if "id=1002" in url:
        return httpx.Response(200, json=_load("synthetic_answers_list_page2.json"))
    return httpx.Response(200, json=_load("synthetic_answers_list_page1.json"))


def test_backfill_and_idempotent(tmp_path, monkeypatch):
    _, fid = _setup(tmp_path)
    _patch(monkeypatch, _list_handler)
    assert asyncio.run(S.backfill_form(fid)) == 3
    assert [p[0] for p in _pending(fid)] == ["1001", "1002", "1003"]
    assert asyncio.run(S.backfill_form(fid)) == 0


def test_reconcile_skips_known(tmp_path, monkeypatch):
    _, fid = _setup(tmp_path)
    _patch(monkeypatch, _list_handler)

    async def known():
        for a in ("1001", "1002"):
            await ef.insert_answer(form_id=fid, answer_id=a, answered_at=None,
                                   received_at=msk_now().strftime(_FMT), payload=[], raw=None,
                                   matched_telegram_id=None, match_how=None)
    asyncio.run(known())
    form = asyncio.run(ef.get_form(fid))
    assert asyncio.run(S.reconcile_form(form)) == 1
    assert [p[0] for p in _pending(fid)] == ["1003"]


def test_reconcile_all_skips_and_isolates_errors(tmp_path, monkeypatch):
    cid, fid = _setup(tmp_path)

    async def more():
        paused = await ef.create_form(platform="yandex", external_id="p", title="П", connection_id=cid)
        await ef.set_form_status(paused, "paused")
        return paused
    paused = asyncio.run(more())
    bad = asyncio.run(ef.create_form(platform="yandex", external_id="bad", title="Б", connection_id=cid))

    def handler(r):
        if "/surveys/bad/" in str(r.url):
            return httpx.Response(500, json={})
        assert "/surveys/p/" not in str(r.url)
        return _list_handler(r)
    _patch(monkeypatch, handler)
    res = asyncio.run(S.reconcile_all())
    assert res == {"enqueued": 3, "rematched": 0}
    assert asyncio.run(ef.get_form(bad))["sync_error"] == "upstream_unavailable"
    assert _pending(paused) == []
    assert len(_pending(fid)) == 3


def test_reconcile_all_skips_reauth_connection(tmp_path, monkeypatch):
    cid, fid = _setup(tmp_path)
    asyncio.run(ef.set_connection_status(cid, "needs_reauth"))

    def handler(r):
        raise AssertionError("API не должен вызываться")
    _patch(monkeypatch, handler)
    assert asyncio.run(S.reconcile_all()) == {"enqueued": 0, "rematched": 0}
