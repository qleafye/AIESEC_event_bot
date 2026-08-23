"""Phase 19 Plan 06 Task 1 (WEBAPP-01, T-19-35..T-19-41): задания менеджера в Mini App —
`/app/api/admin/tasks*`: список активных/архивных со счётчиками, карточка с `card_text`
делегата, точечные правки (ровно одно поле за PATCH), создание, архив/возврат/удаление,
городской скоуп на чтении и на записи, `task_changed` в outbox после каждой мутации.
Харнесс — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import aiosqlite
import pytest

from database import db as bot_db
from game_labels import render_task_card_text

from miniapp.routers.submissions import make_part_token

from tests.test_miniapp_routes import (
    ADMIN_ID,
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
SECRET = "test-session-secret"
BASE = "/app/api/admin/tasks"


def _run(coro):
    return asyncio.run(coro)


async def _fetchall(query, params=()):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _outbox(kind="task_changed"):
    return _run(_fetchall("SELECT * FROM miniapp_outbox WHERE kind = ?", (kind,)))


def _task_row(task_id):
    return _run(bot_db.get_task(task_id))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_admin_tasks.db")
    _standard_seed()
    _seed(staff=[(BOUND_GAME_MANAGER, "game_manager", "spb")])
    return _client(_cfg(db_path))


def _future(days=3) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _task(**kw) -> int:
    return _run(bot_db.create_task(
        kw.get("text", "Сфоткай стенд AIESEC"), kw.get("category", "Light"), kw.get("coins", 10),
        kw.get("proof_type", "photo"), kw.get("deadline") or _future(), None,
        event_city=kw.get("city"), title=kw.get("title", "Стенд"), photo_file_id=kw.get("photo"),
    ))


def _submit(task_id, user_id=DELEGATE_ID, status="pending") -> int:
    sid = _run(bot_db.create_submission(task_id, user_id, content_type="photo",
                                        content="AgACphoto1", submitted_at=_future(-1)))
    assert sid is not None
    if status != "pending":
        _run(bot_db.claim_submission(sid, ADMIN_ID, status, coins_awarded=5))
    return sid


def _get(client, path="", user=GAME_MANAGER_ID, **params):
    return client.get(BASE + path, params=params or None, headers=_hdr(user))


def _patch(client, task_id, body, user=GAME_MANAGER_ID):
    return client.patch(f"{BASE}/{task_id}", json=body, headers=_hdr(user))


def _create(client, body, user=GAME_MANAGER_ID):
    return client.post(BASE, json=body, headers=_hdr(user))


def _good_create_body(**over) -> dict:
    body = {
        "title": "Новое задание", "text": "Опиши стенд", "category": "Medium", "coins": 20,
        "proof_types": ["photo", "text"], "deadline_at": "plus3",
    }
    body.update(over)
    return body


# ── список ───────────────────────────────────────────────────────────────────────────────

def test_list_active_with_numbers_labels_and_counters(client):
    t1 = _task(title="Первое")
    t2 = _task(title="Второе", category="Hard", coins=30)
    _submit(t1)                             # pending
    _submit(t1, user_id=900101, status="approved")
    body = _get(client).json()
    assert body["total"] == 2 and body["archived"] is False
    assert body["active_count"] == 2 and body["archived_count"] == 0
    by_id = {i["id"]: i for i in body["items"]}
    assert by_id[t1]["pending"] == 1 and by_id[t1]["approved"] == 1
    assert by_id[t2]["pending"] == 0 and by_id[t2]["approved"] == 0
    assert [i["number"] for i in body["items"]] == [1, 2]
    assert by_id[t2]["category_label"] == "Сложное" and by_id[t2]["coins"] == 30
    assert by_id[t1]["title"] == "Первое" and by_id[t1]["deadline_short"]
    assert all(i["archived"] is False for i in body["items"])
    assert "Light" not in str([i["category_label"] for i in body["items"]])


def test_list_archived_flag_splits_lists(client):
    t1 = _task(title="Активное")
    t2 = _task(title="Старое")
    _run(bot_db.archive_task(t2))
    active = _get(client).json()
    archived = _get(client, archived=1).json()
    assert [i["id"] for i in active["items"]] == [t1]
    assert [i["id"] for i in archived["items"]] == [t2]
    assert archived["items"][0]["archived"] is True and archived["archived"] is True
    assert active["archived_count"] == 1 and archived["active_count"] == 1


def test_list_empty_texts_and_limit_ceiling(client):
    assert _get(client).json()["empty_text"] == "Заданий пока нет."
    assert _get(client, archived=1).json()["empty_text"] == "Архив пуст."
    assert _get(client, limit=500).json()["limit"] == 50


# ── карточка ─────────────────────────────────────────────────────────────────────────────

def test_card_matches_delegate_render_and_flags(client):
    t = _task(text="Сделай <b>фото</b>", photo="AgACcover")
    body = _get(client, f"/{t}").json()
    task = _task_row(t)
    assert body["card_text"] == _run(render_task_card_text(task, "новое", None))
    assert "&lt;b&gt;" in body["card_text"]
    assert body["photo_file_id"] == "AgACcover"
    assert body["submissions_count"] == 0 and body["can_delete"] is True
    assert body["proof_label"] and body["proof_label"] != "photo" and body["category_label"] == "Лёгкое"
    assert body["deadline_display"].count(".") == 2
    _submit(t)
    again = _get(client, f"/{t}").json()
    assert again["submissions_count"] == 1 and again["can_delete"] is False
    assert "сдачи" in again["cannot_delete_text"]


def test_card_404_for_unknown(client):
    resp = _get(client, "/999999")
    assert resp.status_code == 404 and resp.json()["reason"] == "not_found"


# ── точечные правки ──────────────────────────────────────────────────────────────────────

def _snapshot(task_id, skip):
    row = _task_row(task_id)
    return {k: v for k, v in row.items() if k != skip}


@pytest.mark.parametrize("field,value,column,expected", [
    ("title", "  Новое\nназвание  ", "title", "Новое название"),
    ("text", "Новое описание", "text", "Новое описание"),
    ("coins", 42, "coins", 42),
    ("deadline_at", "31.12.2030 18:00", "deadline_at", "2030-12-31 18:00:00"),
])
def test_each_patch_changes_exactly_its_field(client, field, value, column, expected):
    t = _task()
    before = _snapshot(t, column)
    resp = _patch(client, t, {field: value})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True and resp.json()["field"] == field
    after = _task_row(t)
    assert after[column] == expected
    assert _snapshot(t, column) == before
    outbox = _outbox()
    assert len(outbox) == 1 and outbox[0]["payload"] == f'{{"task_id": {t}}}'


def test_patch_photo_requires_own_part_token(client):
    t = _task()
    # без токена — 403, строка не тронута
    resp = _patch(client, t, {"photo_file_id": "AgACnew"})
    assert resp.status_code == 403 and resp.json()["reason"] == "bad_part_token"
    # чужой токен (делегата) — 403
    foreign = make_part_token(SECRET, DELEGATE_ID, "photo", "AgACnew")
    assert _patch(client, t, {"photo_file_id": "AgACnew", "part_token": foreign}).status_code == 403
    # подпись документа — не обложка
    doc = make_part_token(SECRET, GAME_MANAGER_ID, "document", "BQACdoc")
    resp = _patch(client, t, {"photo_file_id": "BQACdoc", "part_token": doc})
    assert resp.status_code == 400 and resp.json()["reason"] == "not_a_photo"
    assert _task_row(t)["photo_file_id"] is None and _outbox() == []
    # свой токен — ок
    mine = make_part_token(SECRET, GAME_MANAGER_ID, "photo", "AgACnew")
    resp = _patch(client, t, {"photo_file_id": "AgACnew", "part_token": mine})
    assert resp.status_code == 200 and _task_row(t)["photo_file_id"] == "AgACnew"
    # убрать фото — без токена, неразрушительно
    assert _patch(client, t, {"remove_photo": True}).status_code == 200
    assert _task_row(t)["photo_file_id"] is None
    assert len(_outbox()) == 2


def test_patch_deadline_presets_are_in_the_future(client):
    t = _task()
    for code in ("today", "plus3", "plus7"):
        resp = _patch(client, t, {"deadline_at": code})
        assert resp.status_code == 200, resp.text
        stored = datetime.strptime(_task_row(t)["deadline_at"], "%Y-%m-%d %H:%M:%S")
        assert stored.hour == 23 and stored.minute == 59
        assert stored > datetime.now() - timedelta(hours=4)  # контейнер UTC vs МСК


def test_patch_validation_400_with_human_text(client):
    t = _task()
    before = _task_row(t)
    cases = [
        ({"title": "Новое", "coins": 5}, "one_field"),
        ({}, "one_field"),
        ({"coins": -5}, "bad_coins"),
        ({"coins": 0}, "bad_coins"),
        ({"title": "   "}, "title_empty"),
        ({"text": ""}, "text_empty"),
        ({"deadline_at": "вчера"}, "bad_deadline"),
        ({"deadline_at": "01.01.2020 10:00"}, "deadline_past"),
    ]
    for body, reason in cases:
        resp = _patch(client, t, body)
        assert resp.status_code == 400, (body, resp.text)
        assert resp.json()["reason"] == reason and resp.json()["text"], body
    assert _task_row(t) == before
    assert _outbox() == []


def test_patch_unknown_task_404(client):
    resp = _patch(client, 424242, {"coins": 5})
    assert resp.status_code == 404


# ── создание ─────────────────────────────────────────────────────────────────────────────

def test_create_stores_like_the_bot_wizard(client):
    resp = _create(client, _good_create_body(proof_types=["text", "photo"]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    row = _task_row(body["id"])
    assert row["title"] == "Новое задание" and row["text"] == "Опиши стенд"
    assert row["category"] == "Medium" and row["coins"] == 20
    assert row["proof_type"] == "photo,text"          # порядок GAME_PROOF_TYPES, как gtproof_done
    assert row["created_by"] == GAME_MANAGER_ID
    assert row["event_city"] is None and row["archived_at"] is None
    assert row["deadline_at"].endswith("23:59:00")
    assert body["task"]["card_text"] == _run(render_task_card_text(row, "новое", None))
    assert _outbox()[0]["payload"] == f'{{"task_id": {body["id"]}}}'
    # видно в списке
    assert [i["id"] for i in _get(client).json()["items"]] == [body["id"]]


def test_create_validation(client):
    cases = [
        (_good_create_body(category="Epic"), "bad_category"),
        (_good_create_body(proof_types=["photo", "video"]), "bad_proof_type"),
        (_good_create_body(coins=-5), "bad_coins"),
        (_good_create_body(title=""), "title_empty"),
        (_good_create_body(text="  "), "text_empty"),
        (_good_create_body(deadline_at="скоро"), "bad_deadline"),
        (_good_create_body(deadline_at="01.01.2020 10:00"), "deadline_past"),
        (_good_create_body(photo_file_id="AgACx"), "bad_part_token"),
    ]
    for body, reason in cases:
        resp = _create(client, body)
        assert resp.status_code in (400, 403), (body, resp.text)
        assert resp.json()["reason"] == reason and resp.json()["text"], body
    assert _run(bot_db.list_all_tasks()) == [] and _outbox() == []


def test_create_with_cover_and_empty_proof_types(client):
    token = make_part_token(SECRET, GAME_MANAGER_ID, "photo", "AgACcover")
    resp = _create(client, _good_create_body(proof_types=[], photo_file_id="AgACcover", part_token=token))
    assert resp.status_code == 201
    row = _task_row(resp.json()["id"])
    assert row["photo_file_id"] == "AgACcover" and row["proof_type"] == ""


def test_create_city_rules(client):
    _set("event_city_enabled", "on")
    # непривязанный: «all» и пусто -> всем; известный включённый код -> этот город; мусор -> 400
    assert _task_row(_create(client, _good_create_body(event_city="all")).json()["id"])["event_city"] is None
    assert _task_row(_create(client, _good_create_body(event_city="spb")).json()["id"])["event_city"] == "spb"
    bad = _create(client, _good_create_body(event_city="nsk"))
    assert bad.status_code == 400 and bad.json()["reason"] == "bad_city"
    # привязанный к spb: поле игнорируется, ставится его город
    forced = _create(client, _good_create_body(event_city="msk"), user=BOUND_GAME_MANAGER)
    assert forced.status_code == 201
    assert _task_row(forced.json()["id"])["event_city"] == "spb"
    forced_all = _create(client, _good_create_body(event_city="all"), user=BOUND_GAME_MANAGER)
    assert _task_row(forced_all.json()["id"])["event_city"] == "spb"


def test_create_city_ignored_when_module_off(client):
    resp = _create(client, _good_create_body(event_city="spb"))
    assert resp.status_code == 201
    assert _task_row(resp.json()["id"])["event_city"] is None


def test_options_labels_without_codes_for_humans(client):
    body = _get(client, "/options").json()
    assert [c["label"] for c in body["categories"]] == ["Лёгкое", "Среднее", "Сложное", "Реферальное", "Особое"]
    assert [c["code"] for c in body["categories"]] == ["Light", "Medium", "Hard", "Referral", "Special"]
    assert {p["code"] for p in body["proof_types"]} == {"photo", "pdf", "text", "link"}
    assert all(p["label"] and p["label"] != p["code"] for p in body["proof_types"])
    assert [d["code"] for d in body["deadline_presets"]] == ["today", "plus3", "plus7"]
    assert body["city_choice"] is False and body["cities"] == []  # модуль выключен
    _set("event_city_enabled", "on")
    on = _get(client, "/options").json()
    assert on["city_choice"] is True and {c["code"] for c in on["cities"]} >= {"msk", "spb"}
    bound = _get(client, "/options", user=BOUND_GAME_MANAGER).json()
    assert bound["city_choice"] is False and bound["bound_city_label"]


# ── архив / возврат / удаление ───────────────────────────────────────────────────────────

def test_archive_and_unarchive_flip_flag_and_enqueue(client):
    t = _task()
    resp = client.post(f"{BASE}/{t}/archive", headers=_hdr(GAME_MANAGER_ID))
    assert resp.json() == {"ok": True, "changed": True, "archived": True}
    assert _task_row(t)["archived_at"] is not None
    # повтор — без изменений и без второй строки в outbox
    assert client.post(f"{BASE}/{t}/archive", headers=_hdr(GAME_MANAGER_ID)).json()["changed"] is False
    assert len(_outbox()) == 1
    resp = client.post(f"{BASE}/{t}/unarchive", headers=_hdr(GAME_MANAGER_ID))
    assert resp.json() == {"ok": True, "changed": True, "archived": False}
    assert _task_row(t)["archived_at"] is None
    assert len(_outbox()) == 2


def test_delete_refused_with_submissions_409_allowed_without(client):
    t_with = _task()
    _submit(t_with, status="rejected")  # даже отклонённая сдача — история
    resp = client.delete(f"{BASE}/{t_with}", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "has_submissions" and "сдачи" in resp.json()["text"]
    assert _task_row(t_with) is not None and _outbox() == []

    t_free = _task()
    resp = client.delete(f"{BASE}/{t_free}", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 200 and resp.json() == {"ok": True, "deleted": True}
    assert _task_row(t_free) is None
    assert len(_outbox()) == 1 and _outbox()[0]["payload"] == f'{{"task_id": {t_free}}}'
    assert client.delete(f"{BASE}/{t_free}", headers=_hdr(GAME_MANAGER_ID)).status_code == 404


# ── городской скоуп (T-19-35) ────────────────────────────────────────────────────────────

def test_bound_manager_other_city_403_on_read_and_every_write(client):
    _set("event_city_enabled", "on")
    msk = _task(city="msk", title="Москва")
    everyone = _task(city=None, title="Всем")
    spb = _task(city="spb", title="Питер")
    listed = _get(client, user=BOUND_GAME_MANAGER).json()
    assert {i["id"] for i in listed["items"]} == {everyone, spb}
    before = _task_row(msk)
    for resp in (
        _get(client, f"/{msk}", user=BOUND_GAME_MANAGER),
        _patch(client, msk, {"coins": 99}, user=BOUND_GAME_MANAGER),
        client.post(f"{BASE}/{msk}/archive", headers=_hdr(BOUND_GAME_MANAGER)),
        client.post(f"{BASE}/{msk}/unarchive", headers=_hdr(BOUND_GAME_MANAGER)),
        client.delete(f"{BASE}/{msk}", headers=_hdr(BOUND_GAME_MANAGER)),
    ):
        assert resp.status_code == 403, resp.text
        assert resp.json()["reason"] == "out_of_scope" and "другого города" in resp.json()["text"]
    assert _task_row(msk) == before and _outbox() == []
    # свой город и «всем» — можно
    assert _patch(client, spb, {"coins": 7}, user=BOUND_GAME_MANAGER).status_code == 200
    assert _patch(client, everyone, {"coins": 8}, user=BOUND_GAME_MANAGER).status_code == 200
    # непривязанный видит всё
    assert {i["id"] for i in _get(client).json()["items"]} == {msk, everyone, spb}
    assert _get(client, f"/{msk}").status_code == 200


def test_superadmin_is_never_bound(client):
    _set("event_city_enabled", "on")
    _seed(staff=[(ADMIN_ID, "game_manager", "spb")])
    msk = _task(city="msk")
    assert _get(client, f"/{msk}", user=ADMIN_ID).status_code == 200
    assert _get(client, "/options", user=ADMIN_ID).json()["city_choice"] is True


# ── права ────────────────────────────────────────────────────────────────────────────────

def test_without_moderate_game_403_no_cap(client):
    t = _task()
    for resp in (
        _get(client, user=DELEGATE_ID),
        _get(client, f"/{t}", user=DELEGATE_ID),
        _patch(client, t, {"coins": 5}, user=DELEGATE_ID),
        _create(client, _good_create_body(), user=DELEGATE_ID),
        client.delete(f"{BASE}/{t}", headers=_hdr(DELEGATE_ID)),
    ):
        assert resp.status_code == 403 and resp.json() == {"reason": "no_cap", "cap": "moderate_game"}
    assert _task_row(t)["coins"] == 10


def test_section_off_403(client):
    _set("miniapp_section_admin_tasks", "off")
    resp = _get(client)
    assert resp.status_code == 403 and resp.json()["reason"] == "section_off"
