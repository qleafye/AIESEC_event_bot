"""Phase 19 Plan 07 Task 1 (WEBAPP-01, D-04 волна 2, T-19-46/47/48/49): монеты вручную и
журнал из Mini App — `GET /app/api/admin/users/search`, `POST /app/api/admin/coins`,
`GET /app/api/admin/coins`, `GET /app/api/admin/coins/presets` + аксессор
`database.db.search_users_by_name`.

Главное — след в БД: ровно одна строка `coins` с `source='manual'` и `changed_by`
менеджера (неотличимо от визарда бота), и ни одного поля ПД в ответе поиска.
Харнесс — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import aiosqlite

from cities import city_scope
from database import db as bot_db
from settings_schema import get_setting_typed

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
SPB_DELEGATE = 900104
MSK_DELEGATE = 900105

FORBIDDEN_SEARCH_KEYS = {
    "phone", "email", "age", "university", "course", "specialty", "work_sphere",
    "missing_skills", "expectations", "local_committee", "position", "referrer_id",
    "source", "source_details", "status", "payment_status",
}


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


def _outbox_rows(kind="coins_manual"):
    return _run(_fetchall("SELECT * FROM miniapp_outbox WHERE kind = ?", (kind,)))


def _person(user_id: int, name: str, username: str | None = None, city: str | None = None,
            phone: str = "+79990000000", email: str = "x@example.com"):
    _run(_exec(
        "UPDATE users SET full_name = ?, username = ?, event_city = ?, phone = ?, email = ? "
        "WHERE telegram_id = ?",
        (name, username, city, phone, email, user_id),
    ))


def _setup(tmp_path, name="miniapp_coins_admin.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _seed(
        users=[(SPB_DELEGATE, "approved"), (MSK_DELEGATE, "approved")],
        staff=[(BOUND_GAME_MANAGER, "game_manager", "spb")],
    )
    _person(DELEGATE_ID, "Иван Петров", "@ivan_p", "msk")
    _person(SPB_DELEGATE, "Пётр Иванов", "@petr_i", "spb")
    _person(MSK_DELEGATE, "Анна 100%_Смирнова", None, "msk")
    return _client(_cfg(db_path))


def _search(client, q, user=GAME_MANAGER_ID):
    return client.get("/app/api/admin/users/search", params={"q": q}, headers=_hdr(user))


def _give(client, user_id, delta, reason="за помощь на стенде", user=GAME_MANAGER_ID):
    return client.post("/app/api/admin/coins",
                       json={"user_id": user_id, "delta": delta, "reason": reason}, headers=_hdr(user))


# ── аксессор search_users_by_name ────────────────────────────────────────────────────────

def test_accessor_finds_by_name_part_case_insensitive(tmp_path):
    _setup(tmp_path)
    rows = _run(bot_db.search_users_by_name("иван"))
    ids = {r["telegram_id"] for r in rows}
    assert ids == {DELEGATE_ID, SPB_DELEGATE}  # «Иван Петров» и «Пётр Иванов»
    assert set(rows[0].keys()) == {"telegram_id", "full_name", "username", "event_city"}


def test_accessor_respects_limit_and_city_scope(tmp_path):
    _setup(tmp_path)
    assert len(_run(bot_db.search_users_by_name("иван", limit=1))) == 1
    spb = _run(bot_db.search_users_by_name("иван", city_scope=city_scope("spb")))
    assert [r["telegram_id"] for r in spb] == [SPB_DELEGATE]
    assert _run(bot_db.search_users_by_name("   ")) == []


def test_accessor_treats_like_wildcards_literally(tmp_path):
    _setup(tmp_path)
    assert [r["telegram_id"] for r in _run(bot_db.search_users_by_name("100%_С"))] == [MSK_DELEGATE]
    # «%» сам по себе не превращается в «любой символ»: по нему находится только Анна.
    assert [r["telegram_id"] for r in _run(bot_db.search_users_by_name("%"))] == [MSK_DELEGATE]
    assert _run(bot_db.search_users_by_name("И_ан")) == []


# ── GET /app/api/admin/users/search ──────────────────────────────────────────────────────

def test_search_by_username_id_and_name_part(tmp_path):
    client = _setup(tmp_path)
    by_username = _search(client, "@Ivan_P").json()
    assert [u["telegram_id"] for u in by_username] == [DELEGATE_ID]
    by_id = _search(client, str(SPB_DELEGATE)).json()
    assert [u["telegram_id"] for u in by_id] == [SPB_DELEGATE]
    assert by_id[0]["name"] == "Пётр Иванов" and by_id[0]["username"] == "@petr_i"
    by_name = _search(client, "иван").json()
    assert {u["telegram_id"] for u in by_name} == {DELEGATE_ID, SPB_DELEGATE}
    assert _search(client, "@nobody").json() == []
    assert _search(client, "999999999").json() == []


def test_search_short_or_empty_query_is_empty_list_not_500(tmp_path):
    client = _setup(tmp_path)
    for q in ("", " ", "и"):
        resp = _search(client, q)
        assert resp.status_code == 200, q
        assert resp.json() == []


def test_search_returns_balance_and_no_personal_data(tmp_path):
    client = _setup(tmp_path)
    _run(bot_db.add_coins(DELEGATE_ID, 7, reason="seed", source="manual"))
    resp = _search(client, "@ivan_p")
    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["balance"] == 7
    assert set(item.keys()) == {"telegram_id", "name", "username", "city", "balance"}
    assert not (set(item.keys()) & FORBIDDEN_SEARCH_KEYS)
    assert "+7999" not in resp.text and "example.com" not in resp.text


def test_search_city_scope_hides_other_city_every_way(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    # Привязанный к spb: чужой делегат не находится ни по имени, ни по @username, ни по id.
    assert {u["telegram_id"] for u in _search(client, "иван", BOUND_GAME_MANAGER).json()} == {SPB_DELEGATE}
    assert _search(client, "@ivan_p", BOUND_GAME_MANAGER).json() == []
    assert _search(client, str(DELEGATE_ID), BOUND_GAME_MANAGER).json() == []
    assert _search(client, "@petr_i", BOUND_GAME_MANAGER).json()[0]["city"]
    # Непривязанный видит всех.
    assert {u["telegram_id"] for u in _search(client, "иван").json()} == {DELEGATE_ID, SPB_DELEGATE}


# ── POST /app/api/admin/coins ────────────────────────────────────────────────────────────

def test_manual_coins_write_exactly_one_manual_row_with_changed_by(tmp_path):
    client = _setup(tmp_path)
    resp = _give(client, DELEGATE_ID, 15, "за помощь на стенде")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["balance"] == 15 and body["name"] == "Иван Петров"
    rows = _coins_rows()
    assert len(rows) == 1
    assert rows[0]["delta"] == 15
    assert rows[0]["source"] == "manual"
    assert rows[0]["changed_by"] == GAME_MANAGER_ID
    assert rows[0]["reason"] == "за помощь на стенде"
    assert _run(bot_db.get_balance(DELEGATE_ID)) == 15

    # Списание — отрицательная delta, та же таблица.
    assert _give(client, DELEGATE_ID, -5, "штраф за опоздание").status_code == 200
    assert _run(bot_db.get_balance(DELEGATE_ID)) == 10


def test_manual_coins_enqueues_outbox_coins_manual(tmp_path):
    client = _setup(tmp_path)
    assert _give(client, DELEGATE_ID, 3).status_code == 200
    rows = _outbox_rows()
    assert len(rows) == 1
    assert f'"user_id": {DELEGATE_ID}' in rows[0]["payload"] and '"delta": 3' in rows[0]["payload"]


def test_manual_coins_zero_delta_400(tmp_path):
    client = _setup(tmp_path)
    resp = _give(client, DELEGATE_ID, 0)
    assert resp.status_code == 400
    assert resp.json()["reason"] == "bad_delta" and resp.json()["text"]
    assert _give(client, DELEGATE_ID, 10**9).status_code == 400
    assert _coins_rows() == []


def test_manual_coins_empty_reason_400(tmp_path):
    client = _setup(tmp_path)
    for reason in ("", "   "):
        resp = _give(client, DELEGATE_ID, 5, reason)
        assert resp.status_code == 400
        assert resp.json()["reason"] == "reason_required"
        assert "за что" in resp.json()["text"]
    assert _coins_rows() == []


def test_manual_coins_unknown_user_404(tmp_path):
    client = _setup(tmp_path)
    resp = _give(client, 424242, 5)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "not_found"


def test_manual_coins_other_city_403_no_row(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    resp = _give(client, DELEGATE_ID, 5, user=BOUND_GAME_MANAGER)  # msk-делегат, spb-менеджер
    assert resp.status_code == 403
    assert resp.json()["reason"] == "out_of_scope" and resp.json()["text"]
    assert _coins_rows() == []
    assert _outbox_rows() == []
    # Свой город — можно.
    assert _give(client, SPB_DELEGATE, 5, user=BOUND_GAME_MANAGER).status_code == 200
    assert _coins_rows(SPB_DELEGATE)[0]["changed_by"] == BOUND_GAME_MANAGER


def test_manual_coins_without_moderate_game_403(tmp_path):
    client = _setup(tmp_path)
    resp = _give(client, DELEGATE_ID, 5, user=DELEGATE_ID)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"
    assert _search(client, "иван", DELEGATE_ID).status_code == 403
    assert client.get("/app/api/admin/coins", headers=_hdr(DELEGATE_ID)).status_code == 403
    assert client.get("/app/api/admin/coins/presets", headers=_hdr(DELEGATE_ID)).status_code == 403


def test_section_coins_off_blocks_manager_routes(tmp_path):
    client = _setup(tmp_path)
    _set("miniapp_section_coins", "off")
    resp = _give(client, DELEGATE_ID, 5)
    assert resp.status_code == 403 and resp.json()["reason"] == "section_off"


# ── GET /app/api/admin/coins — журнал ────────────────────────────────────────────────────

def test_journal_paginates_and_hides_task_awards(tmp_path):
    client = _setup(tmp_path)
    for i in range(3):
        assert _give(client, DELEGATE_ID, i + 1, f"причина {i + 1}").status_code == 200
    _run(bot_db.add_coins(SPB_DELEGATE, 50, reason="Задание: стенд", changed_by=GAME_MANAGER_ID, source="task"))
    _run(bot_db.add_coins(SPB_DELEGATE, 5, reason="legacy", source=None))

    page1 = client.get("/app/api/admin/coins", params={"limit": 2}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert page1["total"] == 3 and page1["limit"] == 2 and page1["offset"] == 0
    assert [i["delta"] for i in page1["items"]] == [3, 2]  # новые сверху
    page2 = client.get("/app/api/admin/coins", params={"limit": 2, "offset": 2}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert [i["delta"] for i in page2["items"]] == [1]
    assert "Задание: стенд" not in str(page1) + str(page2)
    assert "legacy" not in str(page1) + str(page2)

    row = page1["items"][0]
    assert row["recipient"] == "Иван Петров"
    assert row["changed_by"] == GAME_MANAGER_ID
    # GAME_MANAGER_ID — staff-only (без строки в `users`); тот же голый id, что и у
    # `handlers/admin_gamification.py::_coinsman_display_name` в боте (fallback без "User ").
    assert row["changed_by_name"] == str(GAME_MANAGER_ID)
    assert row["reason"] == "причина 3"
    assert row["source_label"] == "Вручную"
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", row["when"])


def test_journal_limit_capped_and_empty_text(tmp_path):
    client = _setup(tmp_path)
    body = client.get("/app/api/admin/coins", params={"limit": 500, "offset": "x"}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["limit"] == 50 and body["offset"] == 0
    assert body["items"] == [] and body["total"] == 0
    assert body["empty_text"]


# ── GET /app/api/admin/coins/presets ─────────────────────────────────────────────────────

def test_presets_from_registry_garbage_ignored_and_default(tmp_path):
    client = _setup(tmp_path)
    url = "/app/api/admin/coins/presets"
    assert client.get(url, headers=_hdr(GAME_MANAGER_ID)).json()["presets"] == [5, 10, 20]
    _set("coins_manual_amount_presets", " 3, abc, -4, 0, 7,7, 15 ")
    assert client.get(url, headers=_hdr(GAME_MANAGER_ID)).json()["presets"] == [3, 7, 15]
    _set("coins_manual_amount_presets", "мусор")
    body = client.get(url, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["presets"] == [5, 10, 20]  # пусто -> дефолт реестра
    assert body["delta_max"] > 0 and body["reason_max"] > 0


# ── без SQL в miniapp/ ───────────────────────────────────────────────────────────────────

def test_no_sql_in_miniapp_coins_admin_router():
    text = (Path(__file__).resolve().parent.parent / "miniapp" / "routers" / "coins_admin.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", text)
    assert "search_users_by_name" in text
