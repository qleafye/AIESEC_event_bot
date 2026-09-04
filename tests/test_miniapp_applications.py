"""Phase 23 План 04 (APP-TINDER-02, D-04..D-08): API отбора заявок Mini App —
`GET /app/api/applications/next`, `POST /{tid}/approve|reject`, `POST /undo`,
`POST /approve_all`.

Контракт зафиксирован ДО роутера (Wave 0-снимок плана): импорт `miniapp.routers.applications`
на уровне модуля красный, пока задача 2 не создаст файл.

Харнесс — `tests/test_miniapp_routes.py` (тот же процесс/БД, что у `tests/test_miniapp_review.py`).
Bot API (`getUserProfilePhotos` для аватара) мокается целиком — ни один тест не ходит в сеть.
Окно отмены (5с, `services.applications.UNDO_WINDOW_SECONDS`) проверяется прямой записью
просроченного `effects_due_at` в БД, а не `sleep`.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api

import miniapp.routers.applications  # noqa: F401 — RED до задачи 2 (ModuleNotFoundError)

from tests.test_miniapp_auth import make_init_data
from tests.test_miniapp_routes import (
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _use_tmp_db,
)

REG_MANAGER_ID = 900603        # reg_manager, без привязки к городу (видит все)
BOUND_REG_MANAGER_ID = 900604  # reg_manager, привязан к spb


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, **fields):
    """Тот же приём, что `tests/test_applications_service.py::_seed_user` — add_user
    (частичный набор колонок) + отдельный set_user_status."""
    row = {
        "telegram_id": tid,
        "full_name": fields.pop("full_name", f"Delegate {tid}"),
        "registration_date": fields.pop("registration_date", f"2026-01-01 00:00:{tid % 60:02d}"),
    }
    row.update(fields)
    _run(bot_db.add_user(row))
    _run(bot_db.set_user_status(tid, fields.get("status", "pending")))


async def _exec(query, params=()):
    async with bot_db._connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def _fetchall(query, params=()):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _outbox_rows(kind):
    return _run(_fetchall("SELECT * FROM miniapp_outbox WHERE kind = ?", (kind,)))


def _decision_rows(tid):
    return _run(_fetchall(
        "SELECT * FROM application_decisions WHERE telegram_id = ? ORDER BY id", (tid,)
    ))


def _user_status(tid):
    rows = _run(_fetchall("SELECT status FROM users WHERE telegram_id = ?", (tid,)))
    return rows[0]["status"] if rows else None


def _expire(decision_id):
    """Пишет просроченный `effects_due_at` напрямую — без sleep окно уже «истекло»."""
    _run(_exec(
        "UPDATE application_decisions SET effects_due_at = ? WHERE id = ?",
        ("2000-01-01 00:00:00", decision_id),
    ))


class FakeBotApi:
    def __init__(self):
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method == "getUserProfilePhotos":
            return httpx.Response(200, json={"ok": True, "result": {"total_count": 0, "photos": []}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


@pytest.fixture(autouse=True)
def bot_api(monkeypatch):
    fake = FakeBotApi()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


@pytest.fixture
def client(tmp_path):
    # НЕ _standard_seed() — она сидирует делегата PENDING_ID, который тихо всплыл бы в КАЖДОЙ
    # проверке очереди/remaining этого файла. Сидируем только то, что нужно API заявок.
    db_path = _use_tmp_db(tmp_path, "miniapp_applications.db")
    _seed(
        staff=[(REG_MANAGER_ID, "reg_manager", None), (BOUND_REG_MANAGER_ID, "reg_manager", "spb")],
        settings={"miniapp_enabled": "on", "event_name": "форума YouLead"},
    )
    return _client(_cfg(db_path))


# ── права / раздел ───────────────────────────────────────────────────────────────────────

def test_next_without_moderate_reg_403_no_cap(client):
    # переиспользуем make_init_data напрямую (не только через _hdr) — приём readonly-задачи.
    headers = {"X-Telegram-Init-Data": make_init_data(user_id=900999)}
    resp = client.get("/app/api/applications/next", headers=headers)
    assert resp.status_code == 403
    assert resp.json() == {"reason": "no_cap", "cap": "moderate_reg"}


def test_next_section_off_403(client):
    _set("miniapp_section_applications", "off")
    resp = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "applications"}


# ── пустая очередь: три ветки текста ────────────────────────────────────────────────────

def test_next_empty_queue_nothing_at_all(client):
    resp = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "empty": True, "remaining": 0, "offset": 0,
        "empty_text": "Заявок на модерации нет.",
    }


def test_next_empty_queue_all_skipped_by_offset(client):
    _seed_user(920001)
    body = client.get(
        "/app/api/applications/next", params={"offset": 5}, headers=_hdr(REG_MANAGER_ID)
    ).json()
    assert body["empty"] is True and body["remaining"] == 1
    assert body["empty_text"] == "Пропущено всё — осталось 1."


def test_next_empty_queue_filtered_by_track(client):
    _seed_user(920002, participant_type="full")
    body = client.get(
        "/app/api/applications/next", params={"track": "short"}, headers=_hdr(REG_MANAGER_ID)
    ).json()
    assert body["empty"] is True and body["remaining"] == 0
    assert body["empty_text"] == "По этому фильтру заявок нет — снимите фильтр."


# ── карточка ─────────────────────────────────────────────────────────────────────────────

def test_next_returns_one_card_oldest_first_with_avatar_and_fields(client):
    import moderation_card
    _seed_user(930001, age="20", full_name="Иван Петров", registration_date="2026-01-01 00:00:01")
    _seed_user(930002, age="21", full_name="Пётр Иванов", registration_date="2026-01-01 00:00:02")
    resp = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "empty" not in body
    assert body["application"]["telegram_id"] == 930001
    assert body["application"]["full_name"] == "Иван Петров"
    assert body["remaining"] == 2 and body["position"] == 1 and body["offset"] == 0
    assert body["avatar"] == {"url": None, "initials": "ИП"}
    assert {"label": moderation_card.CARD_STEPS["age"], "value": "20"} in body["main_fields"]
    assert body["resume"] == {"kind": "none"}
    assert body["badges"] == []
    assert body["history"] == []
    assert body["filters"]["reject_templates"][0] == "Анкета заполнена не полностью"


# ── история правок: сервер отдаёт готовые подписи, а не сырые коды (23-06, Known Stub 23-05) ──

def test_next_history_carries_labels_and_source_not_raw_columns(client):
    import moderation_card

    _seed_user(931001, age="20", registration_date="2026-01-01 00:00:01")
    _run(bot_db.record_answer_history(
        931001,
        [
            {"column": "age", "old": "20", "new": "21"},
            # маркер повторной подачи (D-10) — уже показан бейджем resubmit, в history не идёт.
            {"column": "status", "old": "rejected", "new": "pending"},
        ],
        "bot",
    ))
    body = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID)).json()
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert set(entry.keys()) == {"when", "source_label", "changes"}
    assert entry["source_label"] == "в чате"
    assert entry["changes"] == [
        {"label": moderation_card.CARD_STEPS["age"], "old": "20", "new": "21"}
    ]
    # ни один сырой код (имя колонки/источника) не утёк в JSON менеджеру.
    assert "column" not in entry["changes"][0]
    assert "status" not in [c["label"] for c in entry["changes"]]


def test_next_history_entry_with_only_resubmit_marker_is_dropped(client):
    _seed_user(931002, registration_date="2026-01-01 00:00:01")
    _run(bot_db.record_answer_history(
        931002, [{"column": "status", "old": "rejected", "new": "pending"}], "miniapp",
    ))
    body = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID)).json()
    assert body["history"] == []


# ── city_label для «Принять всех N» (D-07, Known Stub 23-05) ─────────────────────────────

def test_next_city_label_absent_when_cities_module_off(client):
    _seed_user(932001, registration_date="2026-01-01 00:00:01")
    body = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID)).json()
    assert body["city_label"] is None


def test_next_city_label_all_cities_for_unbound_manager(client):
    from cities import ALL_CITIES_LABEL

    _set("event_city_enabled", "on")
    _seed_user(932002, registration_date="2026-01-01 00:00:01")
    body = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID)).json()
    assert body["city_label"] == ALL_CITIES_LABEL


def test_next_city_label_matches_bound_manager_city(client):
    from cities import city_label as _city_label_fn

    _set("event_city_enabled", "on")
    _seed_user(932003, event_city="spb", registration_date="2026-01-01 00:00:01")
    body = client.get("/app/api/applications/next", headers=_hdr(BOUND_REG_MANAGER_ID)).json()
    assert body["city_label"] == _run(_city_label_fn("spb"))


def test_next_track_filter_party_only(client):
    _seed_user(940001, participant_type="full", registration_date="2026-01-01 00:00:01")
    _seed_user(940002, participant_type="party_overnight", registration_date="2026-01-01 00:00:02")
    body = client.get(
        "/app/api/applications/next", params={"track": "party"}, headers=_hdr(REG_MANAGER_ID)
    ).json()
    assert body["application"]["telegram_id"] == 940002
    assert body["remaining"] == 1


def test_next_changed_filter_only_edited(client):
    _seed_user(950001, registration_date="2026-01-01 00:00:01")
    _seed_user(950002, registration_date="2026-01-01 00:00:02")
    _run(bot_db.mark_user_edited(950002, "bot"))
    body = client.get(
        "/app/api/applications/next", params={"changed": "1"}, headers=_hdr(REG_MANAGER_ID)
    ).json()
    assert body["application"]["telegram_id"] == 950002
    assert body["remaining"] == 1
    assert any(b["kind"] == "edited" for b in body["badges"])


def test_next_scope_hides_other_city_offset_is_skip_only(client):
    _set("event_city_enabled", "on")
    _seed_user(960001, event_city="spb", registration_date="2026-01-01 00:00:01")
    _seed_user(960002, event_city="msk", registration_date="2026-01-01 00:00:02")
    body = client.get("/app/api/applications/next", headers=_hdr(BOUND_REG_MANAGER_ID)).json()
    assert body["application"]["telegram_id"] == 960001
    assert body["remaining"] == 1
    # «Пропустить» ничего не меняет на сервере
    client.get("/app/api/applications/next", params={"offset": 1}, headers=_hdr(BOUND_REG_MANAGER_ID))
    assert _user_status(960001) == "pending"


# ── решения: approve/reject атомарны, эффектов пока нет ────────────────────────────────────

def test_approve_flips_status_no_outbox_no_effects_yet(client, bot_api):
    _seed_user(970001)
    resp = client.post("/app/api/applications/970001/approve", headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["undo_seconds"] == 5 and "decision_id" in body
    # Quick 260904-dq1: тихие часы выключены по умолчанию -- приписка пустая.
    assert body["quiet_notice"] == ""
    assert _user_status(970001) == "approved"
    assert _outbox_rows("application_decided") == []
    assert "sendMessage" not in bot_api.calls


def test_approve_in_quiet_hours_returns_manager_notice(client, bot_api):
    """Quick 260904-dq1: снимок «попадёт ли в тихие часы» в ответе решения — окно 00:00-23:59
    накрывает любое «сейчас», проверяем только что приписка непустая и несёт время конца окна."""
    _set("quiet_hours_enabled", "on")
    _set("quiet_hours_start", "00:00")
    _set("quiet_hours_end", "23:59")
    _seed_user(970099)
    resp = client.post("/app/api/applications/970099/approve", headers=_hdr(REG_MANAGER_ID))
    body = resp.json()
    assert body["ok"] is True
    assert body["quiet_notice"] != ""
    assert "23:59" in body["quiet_notice"]


def test_approve_twice_second_is_already(client):
    _seed_user(970002)
    first = client.post("/app/api/applications/970002/approve", headers=_hdr(REG_MANAGER_ID)).json()
    second = client.post("/app/api/applications/970002/approve", headers=_hdr(REG_MANAGER_ID)).json()
    assert first["ok"] is True
    assert second == {"ok": False, "reason": "already"}
    assert len(_decision_rows(970002)) == 1


def test_reject_with_blank_reason_is_allowed(client):
    _seed_user(970003)
    resp = client.post("/app/api/applications/970003/reject", json={}, headers=_hdr(REG_MANAGER_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert _user_status(970003) == "rejected"
    assert _decision_rows(970003)[0]["reason"] is None


def test_reject_reason_truncated_to_limit(client):
    _seed_user(970004)
    resp = client.post(
        "/app/api/applications/970004/reject", json={"reason": "x" * 600}, headers=_hdr(REG_MANAGER_ID)
    )
    assert resp.json()["ok"] is True
    reason = _decision_rows(970004)[0]["reason"]
    assert reason is not None and len(reason) <= 500


# ── undo: один шаг, окно отмены ────────────────────────────────────────────────────────────

def test_undo_within_window_reverts_and_requeues(client):
    _seed_user(970005)
    decision_id = client.post(
        "/app/api/applications/970005/approve", headers=_hdr(REG_MANAGER_ID)
    ).json()["decision_id"]
    resp = client.post(
        "/app/api/applications/undo", json={"decision_id": decision_id}, headers=_hdr(REG_MANAGER_ID)
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert _user_status(970005) == "pending"
    assert _outbox_rows("application_decided") == []
    body = client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID)).json()
    assert body["application"]["telegram_id"] == 970005


def test_undo_after_flush_is_too_late(client):
    _seed_user(970006)
    decision_id = client.post(
        "/app/api/applications/970006/approve", headers=_hdr(REG_MANAGER_ID)
    ).json()["decision_id"]
    _expire(decision_id)
    client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID))  # триггерит flush
    resp = client.post(
        "/app/api/applications/undo", json={"decision_id": decision_id}, headers=_hdr(REG_MANAGER_ID)
    )
    assert resp.json() == {"ok": False, "reason": "too_late"}
    assert _user_status(970006) == "approved"


def test_undo_of_unknown_decision_404(client):
    resp = client.post(
        "/app/api/applications/undo", json={"decision_id": 999999}, headers=_hdr(REG_MANAGER_ID)
    )
    assert resp.status_code == 404
    assert resp.json() == {"reason": "not_found"}


def test_undo_of_someone_elses_decision_404(client):
    _seed_user(970009)
    decision_id = client.post(
        "/app/api/applications/970009/approve", headers=_hdr(REG_MANAGER_ID)
    ).json()["decision_id"]
    resp = client.post(
        "/app/api/applications/undo", json={"decision_id": decision_id},
        headers=_hdr(BOUND_REG_MANAGER_ID),
    )
    assert resp.status_code == 404
    assert _user_status(970009) == "approved"


# ── сметание: ровно одна строка outbox, идемпотентно ────────────────────────────────────────

def test_flush_enqueues_exactly_one_row_and_is_idempotent(client):
    _seed_user(970007)
    decision_id = client.post(
        "/app/api/applications/970007/reject", json={"reason": "тест"}, headers=_hdr(REG_MANAGER_ID)
    ).json()["decision_id"]
    _expire(decision_id)
    client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID))
    rows = _outbox_rows("application_decided")
    assert len(rows) == 1
    assert '"telegram_id": 970007' in rows[0]["payload"]
    assert '"status": "rejected"' in rows[0]["payload"]
    client.get("/app/api/applications/next", headers=_hdr(REG_MANAGER_ID))  # второй сметатель
    assert len(_outbox_rows("application_decided")) == 1


# ── городской скоуп на прямом POST ───────────────────────────────────────────────────────

def test_direct_post_on_wrong_city_is_forbidden(client):
    _set("event_city_enabled", "on")
    _seed_user(970008, event_city="msk")
    resp = client.post("/app/api/applications/970008/approve", headers=_hdr(BOUND_REG_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "out_of_scope"
    assert "другого города" in resp.json()["text"]
    assert _user_status(970008) == "pending"


# ── approve_all: город сверяется, без отмены ─────────────────────────────────────────────

def test_approve_all_requires_matching_city_and_succeeds_then_already(client):
    _set("event_city_enabled", "on")
    _seed_user(970101, event_city="spb")
    _seed_user(970102, event_city="spb")
    _seed_user(970103, event_city="msk")

    missing = client.post(
        "/app/api/applications/approve_all", json={}, headers=_hdr(BOUND_REG_MANAGER_ID)
    )
    assert missing.status_code == 400

    mismatch = client.post(
        "/app/api/applications/approve_all", json={"city": "msk"}, headers=_hdr(BOUND_REG_MANAGER_ID)
    )
    assert mismatch.status_code == 403

    ok = client.post(
        "/app/api/applications/approve_all", json={"city": "spb"}, headers=_hdr(BOUND_REG_MANAGER_ID)
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "count": 2}
    assert _user_status(970101) == "approved" and _user_status(970102) == "approved"
    assert _user_status(970103) == "pending"
    assert len(_outbox_rows("application_mass_approved")) == 1

    again = client.post(
        "/app/api/applications/approve_all", json={"city": "spb"}, headers=_hdr(BOUND_REG_MANAGER_ID)
    )
    assert again.json() == {"ok": False, "reason": "already"}
