"""Точка расширения «токен QR -> человек» (`services.forum.checkin.register_token_resolver`/
`resolve_token`) под будущие гостевые пропуска: встроенный резолвер делегатов работает как
раньше, внешний зовётся только для токена, которого нет в `users`, падающий резолвер не
ломает скан, чужой вид пропуска без записи отметки — отказ «неизвестный тип пропуска».
Харнесс Mini App — тот же, что `tests/test_miniapp_checkin_260924.py`."""
from __future__ import annotations

import pytest

from database import db as bot_db
from services.forum import checkin, venue_log
from services.forum.checkin import build_payload

from tests.test_miniapp_checkin_260924 import (
    BASE,
    TAG,
    _grant_checkin_to_game_manager,
    _insert_user,
    _qr,
    _run,
    client_with,
)
from tests.test_miniapp_routes import GAME_MANAGER_ID, _hdr


@pytest.fixture(autouse=True)
def _clean_registry():
    checkin.clear_token_resolvers()
    yield
    checkin.clear_token_resolvers()


def _denials():
    rows, _total = _run(bot_db.venue_log_page(limit=50))
    return [r for r in rows if r["action"] == venue_log.ACTION_DENIED]


def _guest_scan(client, token):
    payload = build_payload(TAG, "Гость Гостев", "Москва", token)
    return client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID)).json()


def test_builtin_resolver_is_first_and_survives_clear():
    checkin.register_token_resolver(lambda *a, **k: None)
    checkin.clear_token_resolvers()
    assert checkin._token_resolvers == [checkin._users_token_resolver]


def test_delegate_scan_unchanged_and_external_resolver_not_called(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    calls = []

    async def guest(token, **ctx):
        calls.append(token)
        return {"kind": "guest", "id": "g1", "denial": None}

    checkin.register_token_resolver(guest)
    uid = 953001
    _run(_insert_user(uid, full_name="Делегатов Дмитрий"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert body["telegram_id"] == uid
    assert calls == []  # токен делегата встроенный резолвер забрал сам


def test_builtin_resolved_fields(tmp_path):
    client_with(tmp_path)
    uid = 953002
    _run(_insert_user(uid, full_name="Полев Пётр", status="pending"))
    token = _run(bot_db.get_or_create_checkin_token(uid))
    resolved = _run(checkin.resolve_token(token))
    assert resolved["kind"] == "delegate"
    assert resolved["telegram_id"] == resolved["id"] == uid
    assert resolved["full_name"] == "Полев Пётр"
    assert resolved["denial"] == "not_approved"
    assert resolved["user"]["telegram_id"] == uid


def test_external_resolver_called_with_ctx_for_unknown_token(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    seen = []

    async def guest(token, **ctx):
        seen.append((token, ctx))
        return None  # «не мой токен»

    checkin.register_token_resolver(guest)
    body = _guest_scan(client, "guest-token-1")
    assert seen == [("guest-token-1", {"point": "entry", "source": "miniapp"})]
    assert body["status"] == "not_found"  # никто не узнал — как раньше


def test_failing_resolver_does_not_break_scan(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()

    async def boom(token, **ctx):
        raise RuntimeError("guest table down")

    async def guest(token, **ctx):
        return {"kind": "guest", "id": "g2", "full_name": "Гость", "denial": None}

    checkin.register_token_resolver(boom)
    checkin.register_token_resolver(guest)
    assert _run(checkin.resolve_token("guest-token-2"))["id"] == "g2"  # переход к следующему

    checkin.clear_token_resolvers()
    checkin.register_token_resolver(boom)
    uid = 953003
    _run(_insert_user(uid))
    ok = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert ok["status"] == "new"
    resp = _guest_scan(client, "guest-token-3")
    assert resp["status"] == "not_found"


def test_unknown_kind_is_denied_with_reason_and_logged(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()

    async def guest(token, **ctx):
        return {"kind": "guest", "id": "g4", "telegram_id": None, "city": "msk",
                "full_name": "Гость Гостев", "denial": None}

    checkin.register_token_resolver(guest)
    body = _guest_scan(client, "guest-token-4")
    assert body["status"] == "denied"
    assert body["reason_text"] == checkin.DENIAL_REASON_TEXT[checkin.UNKNOWN_PASS_KIND]
    [row] = _denials()
    assert row["details"] == {"reason": "unknown_pass_kind"}
    assert row["telegram_id"] is None
    assert venue_log.DENIAL_LABELS["unknown_pass_kind"]
    assert _run(checkin.resolve_scanned_user("guest-token-4")) == (None, "unknown_pass_kind")


def test_register_is_idempotent():
    async def guest(token, **ctx):
        return None

    checkin.register_token_resolver(guest)
    checkin.register_token_resolver(guest)
    assert checkin._token_resolvers.count(guest) == 1
