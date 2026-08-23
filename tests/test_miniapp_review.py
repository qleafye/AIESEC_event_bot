"""Phase 19 Plan 05 Task 1 (WEBAPP-01, D-07/D-09, T-19-27/28/29/30/31/33): очередь проверки
сдач в Mini App — `GET /app/api/review/next`, `POST /app/api/review/{sid}/approve|reject`.

Главное здесь — доказательство «ровно одно начисление на сдачу» СТРОКАМИ таблицы `coins`,
а не кодами ответов. Bot API (sendMessage делегату) — `httpx.MockTransport`.
Харнесс — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import aiosqlite
import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

BOUND_GAME_MANAGER = 900602  # game_manager, привязан к spb
OTHER_DELEGATE = 900104


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


def _coins_rows(user_id=DELEGATE_ID):
    return _run(_fetchall("SELECT * FROM coins WHERE user_id = ?", (user_id,)))


def _outbox_rows(kind="submission_reviewed"):
    return _run(_fetchall("SELECT * FROM miniapp_outbox WHERE kind = ?", (kind,)))


def _submission(sid):
    return _run(bot_db.get_submission(sid))


class FakeBotApi:
    def __init__(self):
        self.messages: list[dict] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "sendMessage":
            import json
            self.messages.append(json.loads(request.content))
        if self.fail:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


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
    db_path = _use_tmp_db(tmp_path, "miniapp_review.db")
    _standard_seed()
    _seed(
        users=[(OTHER_DELEGATE, "approved")],
        staff=[(BOUND_GAME_MANAGER, "game_manager", "spb")],
    )
    return _client(_cfg(db_path))


def _task(coins=10, **kw) -> int:
    deadline = kw.get("deadline") or (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    return _run(bot_db.create_task(
        kw.get("text", "Сфоткай стенд AIESEC"), kw.get("category", "Light"), coins, "photo",
        deadline, None, title=kw.get("title", "Стенд"),
    ))


def _submit(task_id, user_id=DELEGATE_ID, *, parts=None, at=None) -> int:
    at = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid = _run(bot_db.create_submission(task_id, user_id, content_type="photo",
                                        content="AgACphoto1", submitted_at=at))
    assert sid is not None
    for i, (kind, content, caption) in enumerate(parts or [("photo", "AgACphoto1", None)]):
        _run(bot_db.add_submission_part(sid, i, kind, content, caption))
    return sid


def _set_city(user_id: int, city: str):
    _run(_exec("UPDATE users SET event_city = ? WHERE telegram_id = ?", (city, user_id)))


def _approve(client, sid, user=GAME_MANAGER_ID, body=None):
    return client.post(f"/app/api/review/{sid}/approve", json=body, headers=_hdr(user))


def _reject(client, sid, user=GAME_MANAGER_ID, reason="Фото не читается"):
    return client.post(f"/app/api/review/{sid}/reject", json={"reason": reason}, headers=_hdr(user))


# ── GET /app/api/review/next ─────────────────────────────────────────────────────────────

def test_next_returns_exactly_one_card_with_remaining(client, bot_api):
    t = _task()
    s1 = _submit(t, parts=[("photo", "AgACphoto1", "вид сбоку"), ("text", "вот стенд", None)])
    s2 = _submit(t, OTHER_DELEGATE)
    resp = client.get("/app/api/review/next", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["submission"]["id"] == s1          # старейшая первой
    assert body["remaining"] == 2 and body["position"] == 1 and body["offset"] == 0
    assert body["task"]["title"] == "Стенд" and body["task"]["coins"] == 10
    assert body["task"]["category_label"] == "Лёгкое"   # из реестра game_labels
    assert body["delegate"]["name"] == f"User {DELEGATE_ID}"
    assert body["parts"] == [
        {"kind": "photo", "content": "AgACphoto1", "caption": "вид сбоку"},
        {"kind": "text", "content": "вот стенд", "caption": None},
    ]
    assert body["after_deadline"] is False and body["archived_task"] is False
    assert body["attempt"] is None  # лимит перезаливов не задан
    assert "empty" not in body
    assert s2 != s1


def test_next_offset_pages_and_skips_are_client_side(client, bot_api):
    t = _task()
    s1 = _submit(t)
    s2 = _submit(t, OTHER_DELEGATE)
    second = client.get("/app/api/review/next", params={"offset": 1}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert second["submission"]["id"] == s2 and second["position"] == 2
    # «Пропустить» ничего не меняет на сервере: первая по-прежнему pending и первая.
    assert _submission(s1)["status"] == "pending"
    assert client.get("/app/api/review/next", headers=_hdr(GAME_MANAGER_ID)).json()["submission"]["id"] == s1
    past_end = client.get("/app/api/review/next", params={"offset": 5}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert past_end["empty"] is True and past_end["remaining"] == 2
    garbage = client.get("/app/api/review/next", params={"offset": "abc"}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert garbage["submission"]["id"] == s1


def test_next_empty_queue(client, bot_api):
    body = client.get("/app/api/review/next", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body == {"empty": True, "remaining": 0, "offset": 0}


def test_next_flags_after_deadline_archive_and_attempt(client, bot_api):
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    t = _task(deadline=past)
    _set("game_resubmit_limit", "3")
    sid = _submit(t)
    # одна отклонённая попытка в прошлом
    _run(_exec("INSERT INTO game_submissions (task_id, user_id, status, content_type, content, submitted_at) VALUES (?, ?, 'rejected', 'text', 'x', ?)",
               (t, DELEGATE_ID, past)))
    _run(bot_db.archive_task(t))
    body = client.get("/app/api/review/next", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["submission"]["id"] == sid
    assert body["after_deadline"] is True
    assert body["archived_task"] is True
    assert body["attempt"] == {"k": 2, "n": 3}


def test_next_without_moderate_game_403_no_cap(client, bot_api):
    resp = client.get("/app/api/review/next", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "no_cap", "cap": "moderate_game"}


def test_next_section_off_403(client, bot_api):
    _set("miniapp_section_review", "off")
    resp = client.get("/app/api/review/next", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "review"}


# ── approve: ровно одно начисление ──────────────────────────────────────────────────────

def test_approve_credits_once_and_notifies(client, bot_api):
    t = _task(coins=10)
    sid = _submit(t)
    resp = _approve(client, sid)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "status": "approved", "coins": 10}

    rows = _coins_rows()
    assert len(rows) == 1
    assert rows[0]["delta"] == 10 and rows[0]["source"] == "task"
    assert rows[0]["changed_by"] == GAME_MANAGER_ID            # T-19-31
    assert rows[0]["reason"].startswith("Задание: Сфоткай стенд")

    sub = _submission(sid)
    assert sub["status"] == "approved" and sub["coins_awarded"] == 10
    assert sub["reviewed_by"] == GAME_MANAGER_ID

    outbox = _outbox_rows()
    assert len(outbox) == 1
    assert '"status": "approved"' in outbox[0]["payload"] and '"coins": 10' in outbox[0]["payload"]

    assert len(bot_api.messages) == 1
    msg = bot_api.messages[0]
    assert msg["chat_id"] == DELEGATE_ID
    assert "одобрено" in msg["text"] and "+10🪙" in msg["text"]


def test_double_tap_second_approve_is_already_and_still_one_coin_row(client, bot_api):
    t = _task(coins=10)
    sid = _submit(t)
    assert _approve(client, sid).json()["ok"] is True
    again = _approve(client, sid)
    assert again.status_code == 200
    assert again.json() == {"ok": False, "reason": "already"}
    assert len(_coins_rows()) == 1
    assert len(_outbox_rows()) == 1
    assert len(bot_api.messages) == 1


def test_two_managers_same_submission_one_credit(client, bot_api):
    """Два менеджера жмут «Принять» по одной сдаче — кто-то один побеждает claim."""
    t = _task(coins=7)
    sid = _submit(t)
    first = _approve(client, sid, GAME_MANAGER_ID).json()
    second = _approve(client, sid, BOUND_GAME_MANAGER).json()
    assert [first["ok"], second["ok"]] == [True, False]
    rows = _coins_rows()
    assert len(rows) == 1 and rows[0]["delta"] == 7
    assert _submission(sid)["reviewed_by"] == GAME_MANAGER_ID


def test_approve_custom_coins_and_bounds(client, bot_api):
    t = _task(coins=10)
    sid = _submit(t)
    bad = _approve(client, sid, body={"coins": 0})
    assert bad.status_code == 400 and bad.json()["reason"] == "bad_coins"
    assert "например" in bad.json()["text"]
    assert _submission(sid)["status"] == "pending" and _coins_rows() == []
    huge = _approve(client, sid, body={"coins": 10 ** 9})
    assert huge.status_code == 400
    not_int = _approve(client, sid, body={"coins": "много"})
    assert not_int.status_code == 422
    ok = _approve(client, sid, body={"coins": 15})
    assert ok.json() == {"ok": True, "status": "approved", "coins": 15}
    rows = _coins_rows()
    assert len(rows) == 1 and rows[0]["delta"] == 15
    assert _submission(sid)["coins_awarded"] == 15   # T-19-30: одна переменная в обе записи
    assert "+15🪙" in bot_api.messages[0]["text"]


def test_approve_delegate_notify_failure_does_not_roll_back(client, bot_api):
    t = _task()
    sid = _submit(t)
    bot_api.fail = True
    resp = _approve(client, sid)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert len(_coins_rows()) == 1
    assert _submission(sid)["status"] == "approved"
    assert len(_outbox_rows()) == 1


def test_approve_unknown_submission_404(client, bot_api):
    resp = _approve(client, 99999)
    assert resp.status_code == 404
    assert resp.json() == {"reason": "not_found"}


def test_approve_after_reject_is_already_no_coins(client, bot_api):
    t = _task()
    sid = _submit(t)
    assert _reject(client, sid).json()["ok"] is True
    assert _approve(client, sid).json() == {"ok": False, "reason": "already"}
    assert _coins_rows() == []


# ── reject: ни одной монеты ─────────────────────────────────────────────────────────────

def test_reject_stores_reason_no_coins_notifies(client, bot_api):
    t = _task()
    sid = _submit(t)
    resp = _reject(client, sid, reason="  Фото не читается  ")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "status": "rejected"}
    assert _coins_rows() == []
    sub = _submission(sid)
    assert sub["status"] == "rejected" and sub["reject_reason"] == "Фото не читается"
    assert sub["reviewed_by"] == GAME_MANAGER_ID
    outbox = _outbox_rows()
    assert len(outbox) == 1
    assert '"status": "rejected"' in outbox[0]["payload"] and '"coins": 0' in outbox[0]["payload"]
    assert len(bot_api.messages) == 1
    assert "отклонено" in bot_api.messages[0]["text"]
    assert "Причина: Фото не читается" in bot_api.messages[0]["text"]


def test_reject_requires_reason(client, bot_api):
    t = _task()
    sid = _submit(t)
    for payload in ({"reason": ""}, {"reason": "   "}, {}):
        resp = client.post(f"/app/api/review/{sid}/reject", json=payload, headers=_hdr(GAME_MANAGER_ID))
        assert resp.status_code == 400, payload
        assert resp.json()["reason"] == "reason_required"
        assert "причину" in resp.json()["text"]
    assert _submission(sid)["status"] == "pending"
    assert bot_api.messages == [] and _outbox_rows() == []


def test_reject_twice_second_is_already(client, bot_api):
    t = _task()
    sid = _submit(t)
    assert _reject(client, sid).json()["ok"] is True
    assert _reject(client, sid).json() == {"ok": False, "reason": "already"}
    assert len(_outbox_rows()) == 1 and len(bot_api.messages) == 1


# ── городской скоуп (T-19-28) ───────────────────────────────────────────────────────────

def test_bound_manager_other_city_403_and_untouched(client, bot_api):
    _set("event_city_enabled", "on")
    _set_city(DELEGATE_ID, "msk")
    t = _task()
    sid = _submit(t)
    # очередь привязанного к spb менеджера её не показывает
    queue = client.get("/app/api/review/next", headers=_hdr(BOUND_GAME_MANAGER)).json()
    assert queue["empty"] is True and queue["remaining"] == 0
    # …а прямой POST по id — 403 с тем же смыслом, что алерт бота
    for resp in (_approve(client, sid, BOUND_GAME_MANAGER), _reject(client, sid, BOUND_GAME_MANAGER)):
        assert resp.status_code == 403
        assert resp.json()["reason"] == "out_of_scope"
        assert "другого города" in resp.json()["text"]
    assert _submission(sid)["status"] == "pending"
    assert _coins_rows() == [] and bot_api.messages == []
    # делегат из spb — можно
    _set_city(DELEGATE_ID, "spb")
    assert client.get("/app/api/review/next", headers=_hdr(BOUND_GAME_MANAGER)).json()["submission"]["id"] == sid
    assert _approve(client, sid, BOUND_GAME_MANAGER).json()["ok"] is True
    assert len(_coins_rows()) == 1


def test_unbound_manager_sees_all_cities(client, bot_api):
    _set("event_city_enabled", "on")
    _set_city(DELEGATE_ID, "spb")
    t = _task()
    sid = _submit(t)
    assert client.get("/app/api/review/next", headers=_hdr(GAME_MANAGER_ID)).json()["submission"]["id"] == sid
    assert _approve(client, sid).json()["ok"] is True


# ── права ───────────────────────────────────────────────────────────────────────────────

def test_decisions_without_moderate_game_403_no_cap(client, bot_api):
    t = _task()
    sid = _submit(t)
    for resp in (_approve(client, sid, DELEGATE_ID), _reject(client, sid, DELEGATE_ID)):
        assert resp.status_code == 403
        assert resp.json() == {"reason": "no_cap", "cap": "moderate_game"}
    assert _submission(sid)["status"] == "pending" and _coins_rows() == []
