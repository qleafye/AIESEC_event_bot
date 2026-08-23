"""Phase 19 Plan 05 Task 2 (WEBAPP-01, T-19-29/T-19-32): `GET /app/api/stats/game`.

Числа — те же, что у `get_game_stats()` (экран 9 бота); подписи категорий — из реестра
`game_labels`; в ответе нет ПД. Харнесс — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    GAME_MANAGER_ID,
    PENDING_ID,
    REJECTED_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


def _task(category="Light", coins=10) -> int:
    deadline = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    return _run(bot_db.create_task("Описание задания", category, coins, "photo", deadline, None, title="Задание"))


def _submit(task_id, user_id, status="pending") -> int:
    sid = _run(bot_db.create_submission(
        task_id, user_id, content_type="text", content="x",
        submitted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))
    assert sid is not None
    if status != "pending":
        assert _run(bot_db.claim_submission(sid, GAME_MANAGER_ID, status, coins_awarded=5))
    return sid


def _client_with_data(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_stats.db")
    _standard_seed()
    light, hard = _task("Light"), _task("Hard")
    _submit(light, DELEGATE_ID, "approved")
    _submit(hard, DELEGATE_ID, "approved")
    _submit(light, PENDING_ID, "approved")
    _submit(hard, PENDING_ID, "rejected")
    _submit(hard, REJECTED_ID)  # pending
    return _client(_cfg(db_path))


# Ключи, которых в агрегатах быть не может (D-17 / T-19-32).
FORBIDDEN_KEYS = {
    "full_name", "name", "username", "phone", "email", "telegram_id", "user_id",
    "first_name", "last_name", "items", "users", "delegates",
}


def _all_keys(value, acc=None) -> set:
    acc = set() if acc is None else acc
    if isinstance(value, dict):
        for k, v in value.items():
            acc.add(k)
            _all_keys(v, acc)
    elif isinstance(value, list):
        for v in value:
            _all_keys(v, acc)
    return acc


def test_numbers_match_get_game_stats(tmp_path):
    client = _client_with_data(tmp_path)
    resp = client.get("/app/api/stats/game", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    direct = _run(bot_db.get_game_stats())
    assert body["participants"] == direct["participants"] == 3
    assert body["submissions"] == {
        "pending": direct["pending"], "approved": direct["approved"], "rejected": direct["rejected"],
    } == {"pending": 1, "approved": 3, "rejected": 1}
    counts = {row["code"]: row["count"] for row in body["by_category"]}
    for code in bot_db.GAME_CATEGORIES:
        assert counts[code] == direct["by_category"].get(code, 0)
    assert counts == {"Light": 2, "Medium": 0, "Hard": 1, "Referral": 0, "Special": 0}
    assert [row["code"] for row in body["by_category"]] == bot_db.GAME_CATEGORIES  # порядок как в боте


def test_category_labels_come_from_registry(tmp_path):
    client = _client_with_data(tmp_path)
    labels = {r["code"]: r["label"] for r in client.get("/app/api/stats/game", headers=_hdr(GAME_MANAGER_ID)).json()["by_category"]}
    assert labels["Light"] == "Лёгкое" and labels["Hard"] == "Сложное"
    _set("game_category_label_light", "Разминка")
    labels = {r["code"]: r["label"] for r in client.get("/app/api/stats/game", headers=_hdr(GAME_MANAGER_ID)).json()["by_category"]}
    assert labels["Light"] == "Разминка"


def test_empty_database_is_zeroes_not_error(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_stats_empty.db")
    _standard_seed()
    body = _client(_cfg(db_path)).get("/app/api/stats/game", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["participants"] == 0
    assert body["submissions"] == {"pending": 0, "approved": 0, "rejected": 0}
    assert all(row["count"] == 0 for row in body["by_category"])


def test_no_personal_data_in_response(tmp_path):
    client = _client_with_data(tmp_path)
    resp = client.get("/app/api/stats/game", headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert not (_all_keys(body) & FORBIDDEN_KEYS)
    text = json.dumps(body, ensure_ascii=False)
    for uid in (DELEGATE_ID, PENDING_ID, REJECTED_ID):
        assert str(uid) not in text and f"User {uid}" not in text


def test_without_moderate_game_403_no_cap(tmp_path):
    client = _client_with_data(tmp_path)
    resp = client.get("/app/api/stats/game", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "no_cap", "cap": "moderate_game"}


def test_section_stats_off_403(tmp_path):
    client = _client_with_data(tmp_path)
    _set("miniapp_section_stats", "off")
    resp = client.get("/app/api/stats/game", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "stats"}
