"""Phase 19 Plan 03 (WEBAPP-01, D-07/D-08): читающие делегатские маршруты Mini App —
профиль, задания и карточка, баланс/история/рейтинг. Харнесс — `tests/test_miniapp_routes.py`
(`TestClient` + `make_init_data`). Все данные сидируются прямо в БД через `database.db`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    REJECTED_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

OTHER_ID = 900110  # второй одобренный делегат (для рейтинга)


def _run(coro):
    return asyncio.run(coro)


async def _sql(query: str, params=()):
    async with bot_db._connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


def _fill_profile(telegram_id: int, **columns):
    sets = ", ".join(f"{c} = ?" for c in columns)
    _run(_sql(f"UPDATE users SET {sets} WHERE telegram_id = ?", (*columns.values(), telegram_id)))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_delegate.db")
    _standard_seed()
    _seed(users=[(OTHER_ID, "approved")])
    return _client(_cfg(db_path))


# ── профиль (D-08) ──────────────────────────────────────────────────────────────────────

def test_profile_returns_labeled_nonempty_fields_and_rereg_deeplink(client):
    _fill_profile(DELEGATE_ID, phone="+7 999", city="Москва", email="", work_status=1,
                  resume_file_id="AgACfile", receipt_file_id="AgACrcpt", payment_status="paid")
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {f["key"]: f for f in body["fields"]}
    assert by_key["reg_q_phone"] == {"key": "reg_q_phone", "label": "\U0001f4f1 Телефон", "value": "+7 999"}
    assert by_key["reg_q_city"]["value"] == "Москва"
    assert by_key["reg_q_work"]["value"] == "Да"
    assert "reg_q_email" not in by_key  # пустое — не показываем
    assert "reg_q_resume" not in by_key  # только file_id — не текст/ссылка
    # порядок — как в REG_LABELS: телефон раньше города
    keys = [f["key"] for f in body["fields"]]
    assert keys.index("reg_q_phone") < keys.index("reg_q_city")
    # служебных колонок в ответе нет нигде
    assert "AgACfile" not in resp.text and "AgACrcpt" not in resp.text
    assert body["status"] == "approved" and body["status_label"] == "Одобрена"
    # Модуль оплаты выключен (дефолт реестра) -> статуса оплаты как понятия нет: сервер шлёт
    # пустые значения, профиль не рисует «Не оплатил» (фикс 03d62a8 по живой приёмке 19-10).
    assert body["payment_status"] == "" and body["payment_status_label"] == ""
    assert body["edit_deeplink"] == "https://t.me/YouLead_test_bot?start=rereg"
    assert body["edit_hint"]  # дефолт реестра miniapp_profile_edit_hint

    _set("payment_enabled", "on")
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    assert body["payment_status"] == "paid" and body["payment_status_label"] == "Оплатил"


@pytest.mark.parametrize("user_id,kind", [(PENDING_ID, "pending"), (REJECTED_ID, "rejected"),
                                          (UNREGISTERED_ID, "unregistered")])
def test_profile_gated_for_non_approved(client, user_id, kind):
    resp = client.get("/app/api/profile", headers=_hdr(user_id))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": kind}


def test_profile_section_off(client):
    _set("miniapp_section_profile", "off")
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"


# ── задания ─────────────────────────────────────────────────────────────────────────────

def _deadline(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _task(title: str, *, days: int = 3, city: str | None = None, category: str = "Light",
          coins: int = 10, proof: str = "photo") -> int:
    return _run(bot_db.create_task(
        f"{title} — описание", category, coins, proof, _deadline(days), None,
        event_city=city, title=title,
    ))


def _submission(task_id: int, user_id: int, status: str = "pending", coins_awarded=None) -> int:
    sid = _run(bot_db.create_submission(task_id, user_id, "text", "ok", "2026-08-20 10:00:00"))
    assert sid
    _run(_sql("UPDATE game_submissions SET status = ?, coins_awarded = ? WHERE id = ?",
              (status, coins_awarded, sid)))
    return sid


def _tasks(client, user_id=DELEGATE_ID, **params):
    resp = client.get("/app/api/tasks", params=params, headers=_hdr(user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_task_list_all_five_states_and_overdue_stays(client):
    t_new = _task("Новое")
    t_pending = _task("На проверке")
    _submission(t_pending, DELEGATE_ID, "pending")
    t_approved = _task("Принято")
    _submission(t_approved, DELEGATE_ID, "approved", coins_awarded=10)
    t_rejected = _task("Отклонено")
    _submission(t_rejected, DELEGATE_ID, "rejected")
    t_overdue = _task("Просрочено", days=-2)
    # чужая сдача не влияет на мой статус (T-19-12)
    _submission(t_new, OTHER_ID, "approved", coins_awarded=10)

    body = _tasks(client)
    by_id = {i["id"]: i for i in body["items"]}
    assert body["total"] == 5 and len(by_id) == 5
    assert by_id[t_new]["status"] == "new" and by_id[t_new]["can_submit"] is True
    assert by_id[t_pending]["status"] == "pending" and by_id[t_pending]["can_submit"] is False
    assert by_id[t_approved]["status"] == "approved" and by_id[t_approved]["coins_awarded"] == 10
    assert by_id[t_rejected]["status"] == "rejected" and by_id[t_rejected]["attempt"] == 1
    assert by_id[t_rejected]["can_submit"] is True  # лимит перезаливов не задан
    assert by_id[t_overdue]["overdue"] is True and by_id[t_overdue]["status"] == "new"
    assert all(i["overdue"] is False for tid, i in by_id.items() if tid != t_overdue)
    assert by_id[t_new]["category_label"] and by_id[t_new]["category_label"] != "Light"  # RU из реестра
    assert by_id[t_new]["title"] == "Новое" and by_id[t_new]["coins"] == 10
    assert body["empty_text"] is None


def test_task_list_rejected_limit_reached_blocks_submit(client):
    _set("game_resubmit_limit", "1")
    t = _task("Лимит")
    _submission(t, DELEGATE_ID, "rejected")
    item = _tasks(client)["items"][0]
    assert item["status"] == "rejected" and item["attempt"] == 1 and item["limit"] == 1
    assert item["can_submit"] is False


def test_task_list_empty_text_from_registry(client):
    _set("game_task_list_empty", "Пока пусто, загляни позже")
    body = _tasks(client)
    assert body["items"] == [] and body["total"] == 0
    assert body["empty_text"] == "Пока пусто, загляни позже"


def test_task_list_pagination_and_limit_ceiling(client):
    ids = [_task(f"Задание {i}", days=i + 1) for i in range(3)]
    page = _tasks(client, limit="1", offset="1")
    assert [i["id"] for i in page["items"]] == [ids[1]]  # сортировка по дедлайну
    assert page["total"] == 3 and page["limit"] == 1 and page["offset"] == 1
    assert _tasks(client, limit="999")["limit"] == 50
    junk = _tasks(client, limit="abc", offset="-5")
    assert junk["limit"] == 25 and junk["offset"] == 0 and len(junk["items"]) == 3


def test_task_list_city_scope_mirrors_bot(client):
    mine = _task("Всем", city=None)
    msk = _task("Москва", city="msk")
    spb = _task("Питер", city="spb")
    # модуль городов выключен — видно всё
    assert {i["id"] for i in _tasks(client)["items"]} == {mine, msk, spb}
    # включён — делегат без event_city = город по умолчанию (msk): чужой город не виден
    _set("event_city_enabled", "on")
    assert {i["id"] for i in _tasks(client)["items"]} == {mine, msk}
    _fill_profile(DELEGATE_ID, event_city="spb")
    assert {i["id"] for i in _tasks(client)["items"]} == {mine, spb}


def test_task_list_archived_not_shown(client):
    t = _task("Архив")
    _run(_sql("UPDATE game_tasks SET archived_at = '2026-08-01 00:00:00' WHERE id = ?", (t,)))
    assert _tasks(client)["total"] == 0
    resp = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 404 and resp.json() == {"reason": "task_not_found"}


@pytest.mark.parametrize("user_id,kind", [(PENDING_ID, "pending"), (REJECTED_ID, "rejected")])
def test_task_list_gated(client, user_id, kind):
    resp = client.get("/app/api/tasks", headers=_hdr(user_id))
    assert resp.status_code == 403 and resp.json() == {"reason": "delegate_gate", "kind": kind}


def test_task_list_section_off(client):
    _set("miniapp_section_tasks", "off")
    resp = client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403 and resp.json() == {"reason": "section_off", "section": "tasks"}


def test_task_card_uses_shared_render(client):
    import game_labels

    t = _task("Карточка", proof="photo,link")
    _submission(t, DELEGATE_ID, "rejected")
    _set("game_resubmit_limit", "3")
    resp = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    task = _run(bot_db.get_task(t))
    expected = _run(game_labels.render_task_card_text(task, "новое · попытка 1 из 3", 1))
    assert body["card_text"] == expected
    assert body["status_line"] == "новое · попытка 1 из 3"
    assert body["proof_hint"] == _run(game_labels.proof_types_label("photo,link"))
    assert body["can_submit"] is True and body["attempt"] == 1
    assert body["text"] == "Карточка — описание"


def test_task_card_pending_and_missing(client):
    t = _task("Сдано")
    _submission(t, DELEGATE_ID, "pending")
    body = client.get(f"/app/api/tasks/{t}", headers=_hdr(DELEGATE_ID)).json()
    assert body["status"] == "pending" and body["can_submit"] is False
    assert body["status_line"] == "на проверке"
    assert client.get("/app/api/tasks/424242", headers=_hdr(DELEGATE_ID)).status_code == 404
    assert client.get("/app/api/tasks/abc", headers=_hdr(DELEGATE_ID)).status_code == 422


# ── монеты ──────────────────────────────────────────────────────────────────────────────

def test_balance_without_operations_rank_is_null(client):
    resp = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"balance": 0, "rank": None, "participants": 0}


def test_balance_rank_and_participants(client):
    _run(bot_db.add_coins(OTHER_ID, 30, "много", source="manual"))
    _run(bot_db.add_coins(DELEGATE_ID, 10, None, source="task"))
    body = client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID)).json()
    assert body == {"balance": 10, "rank": 2, "participants": 2}


def test_history_paginated_with_source_labels(client):
    for i in range(4):
        _run(bot_db.add_coins(DELEGATE_ID, i + 1, f"причина {i}" if i % 2 else None,
                              source="manual" if i < 2 else "task"))
    body = client.get("/app/api/coins/history", params={"limit": "2"}, headers=_hdr(DELEGATE_ID)).json()
    assert body["total"] == 4 and len(body["items"]) == 2 and body["limit"] == 2
    assert [i["delta"] for i in body["items"]] == [4, 3]  # новые сверху
    assert body["items"][0]["reason"] == "причина 3" and body["items"][0]["source_label"] == "задание"
    assert body["items"][1]["reason"] is None
    page2 = client.get("/app/api/coins/history", params={"limit": "2", "offset": "2"},
                       headers=_hdr(DELEGATE_ID)).json()
    assert [i["delta"] for i in page2["items"]] == [2, 1]
    assert page2["items"][1]["source_label"] == "вручную"
    assert body["empty_text"] is None
    # чужие операции не попадают
    _run(bot_db.add_coins(OTHER_ID, 99, "чужое", source="manual"))
    assert client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID)).json()["total"] == 4


def test_history_empty_text(client):
    body = client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID)).json()
    assert body["items"] == [] and body["total"] == 0 and body["empty_text"]


def test_leaderboard_capped_at_50_with_me_block(client):
    for n in range(60):
        uid = 910000 + n
        _run(_sql("INSERT INTO users (telegram_id, full_name, status) VALUES (?, ?, 'approved')",
                  (uid, f"Участник {n}")))
        _run(bot_db.add_coins(uid, 100 + n, None, source="task"))
    _run(bot_db.add_coins(DELEGATE_ID, 5, None, source="task"))
    resp = client.get("/app/api/leaderboard", params={"limit": "500"}, headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 50
    assert body["items"][0] == {"rank": 1, "name": "Участник 59", "balance": 159, "is_me": False}
    assert body["me"] == {"rank": 61, "balance": 5}
    assert body["total"] == 61
    assert not any(i["is_me"] for i in body["items"])
    small = client.get("/app/api/leaderboard", params={"limit": "3"}, headers=_hdr(DELEGATE_ID)).json()
    assert len(small["items"]) == 3
    # ни телефонов, ни e-mail
    assert "phone" not in resp.text and "email" not in resp.text


def test_leaderboard_marks_me(client):
    _run(bot_db.add_coins(DELEGATE_ID, 7, None, source="task"))
    body = client.get("/app/api/leaderboard", headers=_hdr(DELEGATE_ID)).json()
    assert body["items"] == [{"rank": 1, "name": f"User {DELEGATE_ID}", "balance": 7, "is_me": True}]
    assert body["me"] == {"rank": 1, "balance": 7} and body["empty_text"] is None


def test_leaderboard_empty_text_and_section_off(client):
    body = client.get("/app/api/leaderboard", headers=_hdr(DELEGATE_ID)).json()
    assert body["items"] == [] and body["empty_text"] and body["me"]["rank"] is None
    _set("miniapp_section_leaderboard", "off")
    resp = client.get("/app/api/leaderboard", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403 and resp.json()["reason"] == "section_off"
    _set("miniapp_section_coins", "off")
    assert client.get("/app/api/coins/balance", headers=_hdr(DELEGATE_ID)).status_code == 403
    assert client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID)).status_code == 403


def test_coins_gated_for_pending(client):
    for path in ("/app/api/coins/balance", "/app/api/coins/history", "/app/api/leaderboard"):
        resp = client.get(path, headers=_hdr(PENDING_ID))
        assert resp.status_code == 403 and resp.json()["kind"] == "pending", path
