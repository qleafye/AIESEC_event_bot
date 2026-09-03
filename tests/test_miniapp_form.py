"""Phase 21 Plan 10 (FORM-SYNC-03/04/05) — HTTP-контракт анкеты Mini App: `GET/PATCH
/app/api/reg/draft`, `POST /app/api/reg/draft/submit`, `POST /app/api/reg/consent/{key}`,
резюме через `POST /app/api/uploads?target=resume`.

Красное состояние (до реализации) — 404 на новых маршрутах / `ModuleNotFoundError:
miniapp.routers.form`. Харнесс — `tests/test_miniapp_routes.py` (`TestClient` + временная БД),
подпись initData — `tests/test_miniapp_auth.py::make_init_data`. Тексты ошибок валидации
сверяются с `VALIDATION_GOLDEN` (`tests/test_reg_engine_parity.py`) — расхождение с текстом
бота ловится автоматически (T-21-05), не читкой глазами.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    REJECTED_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _cookie_client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)
from tests.test_reg_engine_parity import AGE_ERROR, VALIDATION_GOLDEN

ADMIN_ID_FOR_COOKIE = 900001


def _run(coro):
    return asyncio.run(coro)


async def _fetchall(query, params=()):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _fill(telegram_id: int, **columns):
    sets = ", ".join(f"{c} = ?" for c in columns)

    async def go():
        async with bot_db._connect() as conn:
            await conn.execute(f"UPDATE users SET {sets} WHERE telegram_id = ?", (*columns.values(), telegram_id))
            await conn.commit()

    _run(go())


def _draft_row(telegram_id: int) -> dict | None:
    return _run(bot_db.get_reg_draft(telegram_id))


def _seed_draft(telegram_id: int, *, kind="new", event_city=None, patch=None, source="bot"):
    return _run(bot_db.upsert_reg_draft(
        telegram_id, kind=kind, participant_type="full", event_city=event_city,
        step=None, patch=patch or {}, source=source,
    ))


def _outbox_rows(kind: str) -> list[dict]:
    return _run(_fetchall("SELECT * FROM miniapp_outbox WHERE kind = ?", (kind,)))


class FakeBotApi:
    def __init__(self):
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if self.fail:
            return httpx.Response(502, text="bad gateway")
        if method == "sendMessage":
            import json
            self.messages.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if method == "sendDocument":
            body = request.content
            marker = b'name="document"; filename="'
            i = body.find(marker)
            filename = None
            if i >= 0:
                j = body.find(b'"', i + len(marker))
                filename = body[i + len(marker):j].decode()
            self.documents.append({"filename": filename})
            return httpx.Response(200, json={"ok": True, "result": {"document": {"file_id": "BQACresume"}}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})


@pytest.fixture
def bot_api(monkeypatch):
    fake = FakeBotApi()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


@pytest.fixture
def db_path(tmp_path):
    path = _use_tmp_db(tmp_path, "miniapp_form.db")
    _standard_seed()
    return path


@pytest.fixture
def client(db_path):
    return _client(_cfg(db_path))


# ── Права: form_gate, а не delegate_gate (Pitfall 9) ────────────────────────────────────────

def test_draft_get_requires_initdata_401(client):
    resp = client.get("/app/api/reg/draft")
    assert resp.status_code == 401
    assert resp.json() == {"reason": "no_auth"}


def test_draft_get_rejects_cookie_branch_403(db_path):
    cfg = _cfg(db_path)
    cookie_client = _cookie_client(cfg, ADMIN_ID_FOR_COOKIE)
    resp = cookie_client.get("/app/api/reg/draft")
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": "cookie"}


@pytest.mark.parametrize("user_id", [PENDING_ID, REJECTED_ID, UNREGISTERED_ID])
def test_draft_get_200_for_non_approved_unlike_delegate_gate(client, user_id):
    """Pitfall 9: незарегистрированный/pending/rejected — законный пользователь анкеты."""
    resp = client.get("/app/api/reg/draft", headers=_hdr(user_id))
    assert resp.status_code == 200, resp.text


def test_draft_get_403_when_section_off(client):
    _set("miniapp_section_form", "off")
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "form"}


def test_draft_get_no_telegram_id_in_contract(client):
    """В контракте нет параметра telegram_id ни в пути, ни в теле — id только из initData."""
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200
    assert "telegram_id" not in resp.json()


# ── GET: kind/exists, спека формы ───────────────────────────────────────────────────────────

def test_draft_get_approved_delegate_is_edit_kind(client):
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["kind"] == "edit"
    assert body["exists"] is False  # черновика ещё не заводилось


def test_edit_review_seeds_answers_from_users_row_without_draft(client):
    """UAT 21-12 находка 4 / БАГ: approved-делегат без строки `reg_drafts` открывает обзор
    «Изменить анкету» — сервер обязан подставить текущие ответы из `users` (тем же приёмом,
    что чат-recall `reg_engine.prior_answers_for`/`STEP_TO_COLUMN`), а не отдавать пустые
    поля. `exists` остаётся False (черновика физически нет) — но `steps[].value` не пуст."""
    _fill(
        DELEGATE_ID, age=25, education_status="Нет, не получал(а) образование",
        source="Самостоятельно",
    )
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["kind"] == "edit"
    assert body["exists"] is False
    for key, expected in (
        ("age", 25),
        ("education_status", "Нет, не получал(а) образование"), ("source", "Самостоятельно"),
    ):
        step = next(s for s in body["steps"] if s["key"] == key)
        assert step["value"] == expected, f"{key}: {step}"
        assert step["value_source"] == "answer", f"{key}: {step}"


def test_draft_get_unregistered_newcomer_is_new_kind_no_prior(client):
    resp = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID))
    body = resp.json()
    assert body["kind"] == "new"
    assert body["prior_badge_text"] is None
    for step in body["steps"]:
        assert step["prior"] is None
        assert step["value_source"] != "prior"


# ── Регистрация закрыта (D-11) ──────────────────────────────────────────────────────────────

def test_new_draft_closed_when_city_disabled(client):
    _set("event_city_enabled", "on")
    _set("city_enabled__spb", "off")
    _seed_draft(UNREGISTERED_ID, kind="new", event_city="spb")
    resp = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID))
    body = resp.json()
    assert body["closed"] is True
    assert body["closed_text"]

    patch_resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "answers": {"age": "25"}},
    )
    assert patch_resp.status_code == 403
    assert patch_resp.json()["reason"] == "registration_closed"


def test_edit_of_approved_works_even_when_city_closed(client):
    _fill(DELEGATE_ID, event_city="spb")
    _set("event_city_enabled", "on")
    _set("city_enabled__spb", "off")
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["kind"] == "edit"
    assert body["closed"] is False

    patch_resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"age": "25"}},
    )
    assert patch_resp.status_code == 200, patch_resp.text


# ── PATCH: allowlist, ошибки, паритет с ботом ────────────────────────────────────────────────

def test_patch_unknown_column_is_bad_field_and_writes_nothing(client):
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"not_a_real_column": "x"}},
    )
    assert resp.status_code == 400
    assert resp.json() == {"reason": "bad_field", "field": "not_a_real_column"}
    assert _draft_row(DELEGATE_ID) is None


def test_patch_invalid_format_matches_bot_error_text(client):
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"age": "abc"}},
    )
    assert resp.status_code == 400
    assert resp.json() == {"reason": "invalid", "errors": {"age": AGE_ERROR}}
    # Сверка с golden-таблицей паритета бота, не с константой в этом файле.
    golden = next(e for e in VALIDATION_GOLDEN if e["step"] == "age" and e["raw"] == "abc")
    assert resp.json()["errors"]["age"] == golden["error"]


def test_patch_bumps_version_and_activity(client):
    before = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": before["version"], "answers": {"age": "25"}},
    )
    assert resp.status_code == 200, resp.text
    after = resp.json()
    assert after["version"] > before["version"]
    row = _draft_row(DELEGATE_ID)
    assert row["updated_at"]


def test_patch_stale_version_saves_field_and_reports_conflicts(client):
    # Клиент рисует форму на version=0. Пока он печатал, "бот" (чат) поменял тот же phone.
    _set("reg_q_phone", "on")  # выключен по умолчанию — этому тесту нужен шаг в спеке формы
    r1 = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"phone": "+79990000001"}},
    )
    assert r1.status_code == 200, r1.text
    base_version = r1.json()["version"]  # клиент запомнил этот version

    _seed_draft(DELEGATE_ID, kind="edit", patch={"phone": "+79990000002"}, source="bot")

    r2 = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": base_version, "answers": {"phone": "+79995551122"}},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "phone" in body["conflicts"]
    phone_step = next(s for s in body["steps"] if s["key"] == "phone")
    assert phone_step["value"] == "+79995551122"  # patch всегда побеждает


def test_patch_bad_format_writes_nothing(client):
    before = _draft_row(DELEGATE_ID)
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"age": "abc"}},
    )
    assert resp.status_code == 400
    assert _draft_row(DELEGATE_ID) == before


# ── Префилл возвращенца (D-07) ──────────────────────────────────────────────────────────────

def test_returning_delegate_gets_prior_prefill(client):
    _fill(REJECTED_ID, age=25)
    resp = client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["kind"] == "new"
    assert body["prior_badge_text"]
    age_step = next(s for s in body["steps"] if s["key"] == "age")
    assert age_step["value_source"] == "prior"
    assert age_step["prior"] == {"value": 25, "display": "25"}
    assert age_step["value"] == 25


def test_prior_not_persisted_until_patch_confirms(client):
    _fill(REJECTED_ID, age=25)
    client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID))
    assert _draft_row(REJECTED_ID) is None  # GET ничего не пишет

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(REJECTED_ID),
        json={"version": 0, "answers": {"age": "25"}},
    )
    assert resp.status_code == 200, resp.text
    row = _draft_row(REJECTED_ID)
    assert row["answers"]["age"] == 25
    age_step = next(s for s in resp.json()["steps"] if s["key"] == "age")
    assert age_step["value_source"] == "answer"


def test_resume_prior_is_flag_only_no_raw_value_leaks(client):
    _set("reg_q_resume", "on")  # выключен по умолчанию — этому тесту нужен шаг в спеке формы
    _fill(REJECTED_ID, resume_text="Секретный текст моего старого резюме")
    resp = client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID))
    assert resp.status_code == 200
    assert "Секретный текст" not in resp.text
    resume_step = next(s for s in resp.json()["steps"] if s["key"] == "resume")
    assert resume_step["has_prior_resume"] is True
    assert resume_step["prior"] is None


# ── Согласия ─────────────────────────────────────────────────────────────────────────────────

def test_consent_post_is_idempotent(client):
    r1 = client.post("/app/api/reg/consent/personal_data", headers=_hdr(DELEGATE_ID))
    assert r1.status_code == 200
    assert r1.json() == {"ok": True, "key": "personal_data"}
    r2 = client.post("/app/api/reg/consent/personal_data", headers=_hdr(DELEGATE_ID))
    assert r2.status_code == 200
    rows = _run(bot_db.get_user_consents(DELEGATE_ID))
    assert rows == ["personal_data"]  # не задвоилось


def test_consent_post_unknown_key_400(client):
    resp = client.post("/app/api/reg/consent/bogus", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 400
    assert resp.json() == {"reason": "bad_key"}


# ── submit: гейт согласий, claim, финал ─────────────────────────────────────────────────────

def test_submit_without_required_consents_409(client):
    _set("consent_enabled", "on")
    _seed_draft(DELEGATE_ID, kind="edit", patch={"age": 25})
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "consent_required"
    assert "personal_data" in body["keys"]


def test_submit_already_submitting_409_no_second_write(client, bot_api):
    _seed_draft(DELEGATE_ID, kind="edit", patch={"age": 25})
    claimed = _run(bot_db.claim_reg_draft(DELEGATE_ID))
    assert claimed is not None  # симулируем «уже отправляется» (чат отправляет прямо сейчас)

    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 409
    assert resp.json() == {"reason": "already_submitting"}
    assert _outbox_rows("reg_edited") == []
    assert _outbox_rows("reg_finalized") == []


def test_submit_edit_success_queues_one_event_and_replies(client, bot_api):
    _seed_draft(DELEGATE_ID, kind="edit", patch={"phone": "+79997776655"})
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "edit"
    assert body["heading"]

    assert len(_outbox_rows("reg_edited")) == 1
    assert _outbox_rows("reg_finalized") == []
    assert _draft_row(DELEGATE_ID) is None  # claim -> finalize -> delete
    user = _run(bot_db.get_user(DELEGATE_ID))
    assert user["phone"] == "+79997776655"
    assert len(bot_api.messages) == 1  # мгновенный ответ в чат


def test_submit_new_success_creates_user_and_queues_reg_finalized(client, bot_api):
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"age": 22, "full_name": "Иван Иванов"})
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "new"
    assert body["heading"]
    assert body["body"]

    assert len(_outbox_rows("reg_finalized")) == 1
    user = _run(bot_db.get_user(UNREGISTERED_ID))
    assert user is not None
    assert user["age"] == 22


# ── Резюме: POST /app/api/uploads?target=resume ─────────────────────────────────────────────

def _upload_resume(client, user_id, data: bytes, *, name="resume.pdf", ctype="application/pdf"):
    return client.post(
        "/app/api/uploads?target=resume", files={"file": (name, data, ctype)}, headers=_hdr(user_id),
    )


def test_resume_upload_pdf_accepted_and_queued(client, bot_api):
    _seed_draft(DELEGATE_ID, kind="edit")
    resp = _upload_resume(client, DELEGATE_ID, b"%PDF-1.4 fake")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["file_id"] == "BQACresume"
    row = _draft_row(DELEGATE_ID)
    assert row["answers"]["resume_file_id"] == "BQACresume"
    assert row["answers"]["resume_file_name"] == "resume.pdf"
    assert len(_outbox_rows("reg_resume_upload")) == 1
    assert bot_api.documents[0]["filename"] == "resume.pdf"


def test_resume_upload_wrong_type_rejected(client, bot_api):
    _seed_draft(DELEGATE_ID, kind="edit")
    resp = _upload_resume(client, DELEGATE_ID, b"plain text", name="resume.txt", ctype="text/plain")
    assert resp.status_code == 400
    assert resp.json()["reason"] == "bad_type"
    assert resp.json()["text"]
    assert bot_api.documents == []


def test_resume_upload_too_large_rejected(client, bot_api):
    _seed_draft(DELEGATE_ID, kind="edit")
    big = b"x" * (11 * 1024 * 1024)  # > 10 МБ (лимит резюме, не общий потолок сервиса)
    resp = _upload_resume(client, DELEGATE_ID, big)
    assert resp.status_code == 413
    assert resp.json()["reason"] == "too_large"


def test_resume_upload_without_draft_404(client, bot_api):
    # DELEGATE_ID проходит upload_actor напрямую (одобрен), но ещё не открывал форму правки —
    # черновика нет; PENDING_ID для этого не годится: без черновика его отсекает сам upload_actor.
    resp = _upload_resume(client, DELEGATE_ID, b"%PDF-1.4 fake")
    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_draft"}


def test_resume_upload_denied_for_pending_without_draft(client, bot_api):
    """`upload_actor` третья ветка требует ЖИВОЙ черновик — без него pending получает
    обычный `delegate_gate`, а не «404 no_draft» (тот приходит уже ПОСЛЕ актора)."""
    resp = _upload_resume(client, PENDING_ID, b"%PDF-1.4 fake")
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": "pending"}


def test_resume_upload_allowed_for_unregistered_with_draft(client, bot_api):
    """Актор `upload_actor` — третья ветка: unregistered/pending/rejected + живой черновик."""
    _seed_draft(UNREGISTERED_ID, kind="new")
    resp = _upload_resume(client, UNREGISTERED_ID, b"%PDF-1.4 fake")
    assert resp.status_code == 200, resp.text


# ── Тело > 1 МиБ ─────────────────────────────────────────────────────────────────────────────

def test_patch_body_over_1mib_413(client):
    huge_answer = "x" * (2 * 1024 * 1024)
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"comments": huge_answer}},
    )
    assert resp.status_code == 413


# ── Логи: только telegram_id/step/version/коды, никогда ответы ──────────────────────────────

def test_patch_does_not_log_answer_values(client, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        client.patch(
            "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
            json={"version": 0, "answers": {"comments": "секретный текст ответа делегата"}},
        )
    assert "секретный текст ответа делегата" not in caplog.text

# ── Развилки город/трек: PATCH через движок (gap closure, D-01/FORM-SYNC-02) ────────────────
# Выбор города и формата участия в мастере приложения идёт теми же валидаторами и тем же
# `resolve_track`, что тап по кнопке развилки в боте; варианты пикера — из ответа сервера
# (те же города и те же подписи, что у клавиатур бота); deep-link/edit приоритетны (409).

def test_pre_items_city_fork_carries_field_and_server_options(client):
    import cities

    _set("event_city_enabled", "on")
    body = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID)).json()
    assert "city_fork" in body["pre"]
    item = next(i for i in body["pre_items"] if i["type"] == "city_fork")
    assert item["field"] == "event_city"
    assert item["text"]
    assert item["value"] is None
    assert [o["code"] for o in item["options"]] == ["msk", "spb", "tyumen"]
    for opt in item["options"]:
        assert opt["label"]
        assert opt["label"] == asyncio.run(cities.city_label(opt["code"]))


def test_pre_items_party_fork_options_match_bot_keyboard(client):
    import handlers.registration as reg

    _set("party_enabled", "on")
    _set("party_fork_question", "on")
    body = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID)).json()
    assert "party_fork" in body["pre"]
    item = next(i for i in body["pre_items"] if i["type"] == "party_fork")
    assert item["field"] == "participant_type"
    assert item["text"]
    assert [o["code"] for o in item["options"]] == ["full", "party_overnight", "party_noovernight"]
    kb = reg._party_fork_kb().inline_keyboard
    assert [o["label"] for o in item["options"]] == [btn.text for row in kb for btn in row]
    assert [btn.callback_data for row in kb for btn in row] == [
        "party_pick:full", "party_pick:party_over", "party_pick:party_noover",
    ]


def test_patch_city_choice_persists_and_hides_fork(client):
    _set("event_city_enabled", "on")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "event_city": "spb"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "city_fork" not in body["pre"]
    assert body["kind"] == "new"
    row = _draft_row(UNREGISTERED_ID)
    assert row["event_city"] == "spb"
    assert row["updated_by"] == "miniapp"


def test_patch_city_choice_invalid_and_closed_match_bot_texts(client):
    import reg_engine

    assert reg_engine.CITY_CHOICE_INVALID_TEXT == "Некорректный выбор."
    assert reg_engine.CITY_CLOSED_TEXT == "Регистрация на этот город закрыта."
    _set("event_city_enabled", "on")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "event_city": "xxx"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"reason": "invalid", "errors": {"event_city": reg_engine.CITY_CHOICE_INVALID_TEXT}}

    _set("city_enabled__spb", "off")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "event_city": "spb"},
    )
    assert resp.status_code == 400
    assert resp.json()["errors"]["event_city"] == reg_engine.CITY_CLOSED_TEXT
    assert _draft_row(UNREGISTERED_ID) is None


def test_patch_city_when_already_set_409_deeplink_wins(client):
    _set("event_city_enabled", "on")
    _seed_draft(UNREGISTERED_ID, event_city="msk")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "event_city": "spb"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"reason": "already_set", "field": "event_city"}
    assert _draft_row(UNREGISTERED_ID)["event_city"] == "msk"


def test_patch_track_choice_resolves_like_bot(client):
    import reg_engine

    _set("party_enabled", "on")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "participant_type": "party_overnight"},
    )
    assert resp.status_code == 200, resp.text
    assert "party_fork" not in resp.json()["pre"]
    assert _draft_row(UNREGISTERED_ID)["participant_type"] == "party_overnight"

    # Правило `_resolve_track` шаг 2: глобальный «short» перебивает выбор «Полная регистрация».
    _set("registration_mode", "short")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(REJECTED_ID),
        json={"version": 0, "participant_type": "full"},
    )
    assert resp.status_code == 200, resp.text
    assert _draft_row(REJECTED_ID)["participant_type"] == "short"

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID + 7),
        json={"version": 0, "participant_type": "bogus"},
    )
    assert resp.status_code == 400
    assert resp.json()["errors"]["participant_type"] == reg_engine.CITY_CHOICE_INVALID_TEXT

    _set("party_enabled", "off")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID + 7),
        json={"version": 0, "participant_type": "party_noovernight"},
    )
    assert resp.status_code == 400
    assert resp.json()["errors"]["participant_type"] == reg_engine.PARTY_CLOSED_TEXT
    assert reg_engine.PARTY_CLOSED_TEXT == "Регистрация на вечеринку уже закрыта."
    assert _draft_row(UNREGISTERED_ID + 7) is None


def test_patch_pre_choice_for_edit_kind_409(client):
    for field, value in (("event_city", "spb"), ("participant_type", "full")):
        resp = client.patch(
            "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
            json={"version": 0, field: value},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json() == {"reason": "already_set", "field": field}


def test_patch_pre_choice_and_answer_in_one_body(client):
    _set("event_city_enabled", "on")
    # Атомарно: ошибка в ответе — город тоже не записан (ошибки собираются до upsert).
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "event_city": "spb", "answers": {"age": "abc"}},
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid"
    assert "age" in resp.json()["errors"]
    assert _draft_row(UNREGISTERED_ID) is None

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "event_city": "spb", "answers": {"age": "25"}},
    )
    assert resp.status_code == 200, resp.text
    row = _draft_row(UNREGISTERED_ID)
    assert row["event_city"] == "spb"
    assert row["answers"]["age"] == 25


def test_engine_aliases_are_same_objects():
    from pathlib import Path

    import handlers.registration as reg
    import reg_engine

    assert reg._resolve_track is reg_engine.resolve_track
    assert reg._PARTY_TAG_MAP is reg_engine.PARTY_TAG_MAP
    src = (Path(__file__).resolve().parent.parent / "handlers" / "reg_flow.py").read_text(encoding="utf-8")
    for literal in ("Некорректный выбор.", "Регистрация на этот город закрыта.",
                    "Регистрация на вечеринку уже закрыта."):
        assert literal not in src, literal
    for name in ("CITY_CHOICE_INVALID_TEXT", "CITY_CLOSED_TEXT", "PARTY_CLOSED_TEXT"):
        assert name in src, name


def test_form_spec_hides_party_fork_when_track_known(db_path):
    import reg_engine

    _set("party_enabled", "on")
    _set("party_fork_question", "on")
    spec = asyncio.run(reg_engine.form_spec({}, "party_overnight", None))
    assert "party_fork" not in spec["pre"]
    spec = asyncio.run(reg_engine.form_spec({}, None, None))
    assert "party_fork" in spec["pre"]


# ── D-27: возвращенец видит развилки города/трека так же, как бот, с предзаполнением ────────
# (владелец, 02.09) — `is_registered=bool(prior)` больше не прячет развилку; см. reg_engine.py
# `form_spec` и `miniapp/routers/form.py::_load_context`/`_pre_items`.

def test_pre_items_preselect_prior_value_for_returning_delegate(client):
    _set("event_city_enabled", "on")
    _set("party_enabled", "on")
    _set("party_fork_question", "on")
    _fill(REJECTED_ID, event_city="spb", participant_type="party_overnight")

    body = client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID)).json()
    assert "city_fork" in body["pre"]
    assert "party_fork" in body["pre"]
    city_item = next(i for i in body["pre_items"] if i["type"] == "city_fork")
    party_item = next(i for i in body["pre_items"] if i["type"] == "party_fork")
    assert city_item["value"] == "spb"
    assert party_item["value"] == "party_overnight"


def test_pre_items_no_fork_for_returning_delegate_when_module_off(client):
    _fill(REJECTED_ID, event_city="spb", participant_type="party_overnight")
    body = client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID)).json()
    assert "city_fork" not in body["pre"]
    assert "party_fork" not in body["pre"]
    assert body["pre_items"] == [] or all(i["type"] not in ("city_fork", "party_fork") for i in body["pre_items"])


def test_patch_city_choice_accepted_for_returning_delegate_not_already_set(client):
    """D-27: у возвращенца (kind='new', есть прошлая заявка) прошлый город НЕ переносится в
    `ctx["event_city"]` (это решало бы за него, как rereg_start бота никогда не делает) — PATCH
    новой развилки принимается, а не 409 already_set."""
    _set("event_city_enabled", "on")
    _fill(REJECTED_ID, event_city="spb")

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(REJECTED_ID),
        json={"version": 0, "event_city": "msk"},
    )
    assert resp.status_code == 200, resp.text
    assert _draft_row(REJECTED_ID)["event_city"] == "msk"

