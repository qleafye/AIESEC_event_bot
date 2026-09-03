"""Quick 260904-2cj (QJRN-01..04): API журнала вопросов делегатов Mini App —
`GET /app/api/questions`, `POST /app/api/questions/{qid}/answer`.

Харнесс — `tests/test_miniapp_routes.py` (тот же процесс/БД, что у соседних роутеров).
Bot API (sendMessage делегату) — `httpx.MockTransport`, фикстура `bot_api` скопирована с
`tests/test_miniapp_review.py::bot_api` (включая ветку `fail`). Правило статуса и
постраничная выборка — общие с ботом (`services/questions.py`, `database.db`), здесь их не
дублируем — только контракт HTTP поверх них.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api

from tests.test_miniapp_auth import make_init_data
from tests.test_miniapp_routes import (
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _use_tmp_db,
)

REG_MANAGER_ID = 900650        # reg_manager, без привязки к городу (видит все)
BOUND_REG_MANAGER_ID = 900651  # reg_manager, привязан к spb
DELEGATE_ID = 900652
OTHER_CITY_DELEGATE_ID = 900653


def _run(coro):
    return asyncio.run(coro)


class FakeBotApi:
    def __init__(self):
        self.messages: list[dict] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "sendMessage":
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
    db_path = _use_tmp_db(tmp_path, "miniapp_questions.db")
    _seed(
        staff=[(REG_MANAGER_ID, "reg_manager", None), (BOUND_REG_MANAGER_ID, "reg_manager", "spb")],
        users=[(DELEGATE_ID, "approved"), (OTHER_CITY_DELEGATE_ID, "approved")],
        settings={"miniapp_enabled": "on", "event_name": "форума YouLead"},
    )
    return _client(_cfg(db_path))


def _seed_question(user_id, text="Когда дедлайн?"):
    return _run(bot_db.create_question(user_id, text))


def _get_question(qid):
    return _run(bot_db.get_question(qid))


def _set_user_city(tid, city):
    """Форма `tests/test_miniapp_applications.py::_seed_user` — `add_user` ON CONFLICT
    перезаписывает `event_city` даже для уже засеянного `_seed()` делегата."""
    _run(bot_db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}", "event_city": city,
        "registration_date": "2026-01-01 00:00:00",
    }))


# ── права / раздел ───────────────────────────────────────────────────────────────────────

def test_questions_without_moderate_reg_403_no_cap(client, bot_api):
    headers = {"X-Telegram-Init-Data": make_init_data(user_id=900999)}
    resp = client.get("/app/api/questions", headers=headers)
    assert resp.status_code == 403
    assert resp.json() == {"reason": "no_cap", "cap": "moderate_reg"}


def test_questions_section_off_403(client, bot_api):
    _set("miniapp_section_questions", "off")
    resp = client.get("/app/api/questions", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "questions"}


def test_delegate_without_cap_403(client, bot_api):
    resp = client.get("/app/api/questions", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


# ── GET /app/api/questions — журнал, счётчики, фильтры ──────────────────────────────────

def test_questions_list_returns_page_counts_and_texts(client, bot_api):
    qid = _seed_question(DELEGATE_ID, "Когда дедлайн?")
    resp = client.get("/app/api/questions", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total"] == 1
    assert body["counts"] == {"all": 1, "new": 1, "in_work": 0, "answered": 0}
    assert body["offset"] == 0 and body["limit"] == 20
    item = body["items"][0]
    assert item["id"] == qid
    assert item["user_id"] == DELEGATE_ID
    assert item["question_text"] == "Когда дедлайн?"
    assert item["status"] == "new"
    assert item["status_label"] == "🆕 без ответа"
    assert item["can_answer"] is True
    assert item["stuck"] is False
    assert item["answer_text"] is None

    keys = {f["key"] for f in body["filters"]}
    assert keys == {"all", "new", "in_work", "answered"}
    assert body["empty_text"] == "Вопросов пока нет."
    assert body["answer_button"] == "Отправить ответ"
    assert body["sent_toast"] == "Ответ отправлен"


def test_questions_list_unknown_status_treated_as_all_not_400(client, bot_api):
    _seed_question(DELEGATE_ID)
    resp = client.get("/app/api/questions", params={"status": "bogus"}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_questions_list_filter_narrows_items_and_total(client, bot_api):
    new_qid = _seed_question(DELEGATE_ID, "Новый")
    in_work_qid = _seed_question(DELEGATE_ID, "В работе")
    _run(bot_db.claim_question(in_work_qid, REG_MANAGER_ID, "Менеджер"))

    resp = client.get("/app/api/questions", params={"status": "new"}, headers=_hdr(REG_MANAGER_ID))
    body = resp.json()
    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [new_qid]

    resp2 = client.get("/app/api/questions", params={"status": "in_work"}, headers=_hdr(REG_MANAGER_ID))
    body2 = resp2.json()
    assert body2["total"] == 1
    assert [i["id"] for i in body2["items"]] == [in_work_qid]
    assert body2["items"][0]["answered_by_name"] == "Менеджер"


def test_questions_list_pagination_offset_limit(client, bot_api):
    for i in range(3):
        _seed_question(DELEGATE_ID, f"Вопрос {i}")
    resp = client.get("/app/api/questions", params={"offset": 1, "limit": 1}, headers=_hdr(REG_MANAGER_ID))
    body = resp.json()
    assert body["offset"] == 1 and body["limit"] == 1
    assert len(body["items"]) == 1
    assert body["total"] == 3


def test_questions_list_city_scope_hides_other_city(client, bot_api):
    _set_user_city(DELEGATE_ID, "spb")
    _set_user_city(OTHER_CITY_DELEGATE_ID, "tyumen")
    _set("event_city_enabled", "on")

    spb_qid = _seed_question(DELEGATE_ID, "Вопрос из Питера")
    _seed_question(OTHER_CITY_DELEGATE_ID, "Вопрос из Тюмени")

    resp = client.get("/app/api/questions", headers=_hdr(BOUND_REG_MANAGER_ID))
    body = resp.json()
    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [spb_qid]
    assert body["counts"]["all"] == 1


# ── POST /app/api/questions/{qid}/answer ─────────────────────────────────────────────────

def test_answer_success_marks_answered_and_delivers(client, bot_api):
    qid = _seed_question(DELEGATE_ID, "Когда дедлайн?")
    resp = client.post(
        f"/app/api/questions/{qid}/answer", json={"text": "Завтра в 18:00"},
        headers=_hdr(REG_MANAGER_ID),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "status": "answered"}

    assert len(bot_api.messages) == 1
    sent = bot_api.messages[0]
    assert sent["chat_id"] == DELEGATE_ID
    assert sent["text"] == "💬 Ответ от организаторов:\n\nЗавтра в 18:00"

    row = _get_question(qid)
    assert row["answer_text"] == "Завтра в 18:00"
    assert row["delivered_at"] is not None
    assert row["answered_by"] == REG_MANAGER_ID


def test_answer_not_found_404(client, bot_api):
    resp = client.post("/app/api/questions/999999/answer", json={"text": "Ответ"}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 404
    assert resp.json() == {"reason": "not_found"}


def test_answer_empty_text_400_no_bot_api_call(client, bot_api):
    qid = _seed_question(DELEGATE_ID)
    resp = client.post(f"/app/api/questions/{qid}/answer", json={"text": "   "}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 400
    assert resp.json()["reason"] == "empty_text"
    assert bot_api.messages == []


def test_answer_too_long_400_no_bot_api_call(client, bot_api):
    qid = _seed_question(DELEGATE_ID)
    resp = client.post(
        f"/app/api/questions/{qid}/answer", json={"text": "x" * 4001}, headers=_hdr(REG_MANAGER_ID),
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "too_long"
    assert bot_api.messages == []


def test_answer_already_answered_returns_ok_false_by(client, bot_api):
    qid = _seed_question(DELEGATE_ID)
    _run(bot_db.claim_question(qid, BOUND_REG_MANAGER_ID, "Другой Менеджер"))
    _run(bot_db.set_question_answer(qid, "Уже отвечено"))

    resp = client.post(f"/app/api/questions/{qid}/answer", json={"text": "Ещё раз"}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "already", "by": "Другой Менеджер"}
    assert bot_api.messages == []


def test_answer_claim_lost_to_other_manager_delegate_gets_nothing(client, bot_api):
    qid = _seed_question(DELEGATE_ID)
    _run(bot_db.claim_question(qid, BOUND_REG_MANAGER_ID, "Другой Менеджер"))

    resp = client.post(f"/app/api/questions/{qid}/answer", json={"text": "Мой ответ"}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "already", "by": "Другой Менеджер"}
    assert bot_api.messages == []
    row = _get_question(qid)
    assert row["delivered_at"] is None


def test_answer_same_manager_retry_after_own_failed_claim_succeeds(client, bot_api):
    """Тот же приём, что T-08-33 часть C: захват твой же, доставки ещё не было — это retry,
    а не «уже ответил кто-то другой»."""
    qid = _seed_question(DELEGATE_ID)
    _run(bot_db.claim_question(qid, REG_MANAGER_ID, "Менеджер"))

    resp = client.post(f"/app/api/questions/{qid}/answer", json={"text": "Ответ"}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "answered"}
    assert len(bot_api.messages) == 1


def test_answer_delivery_failure_keeps_claim_no_delivered_at(client, bot_api):
    qid = _seed_question(DELEGATE_ID)
    bot_api.fail = True

    resp = client.post(f"/app/api/questions/{qid}/answer", json={"text": "Ответ"}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "delivery_failed"
    assert "text" in body

    row = _get_question(qid)
    assert row["delivered_at"] is None
    assert row["answered_by"] == REG_MANAGER_ID  # захват не отпущен


def test_answer_section_off_403(client, bot_api):
    qid = _seed_question(DELEGATE_ID)
    _set("miniapp_section_questions", "off")
    resp = client.post(f"/app/api/questions/{qid}/answer", json={"text": "Ответ"}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 403
    assert bot_api.messages == []
