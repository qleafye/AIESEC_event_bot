"""Phase 19 Plan 04 Task 3 (WEBAPP-01, D-03, T-19-19/T-19-20): `GET /app/api/file/{file_id}`
— прокси getFile без утечки токена. Bot API — `httpx.MockTransport` через
`telegram_api._make_client`; харнесс — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api

from tests.test_miniapp_auth import TOKEN
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

OTHER_ID = 900120           # второй одобренный делегат — не владелец
BOUND_GAME_MANAGER = 900602  # game_manager, привязан к spb

FILE_ID = "AgACAgIAAxkBAAIphotoOwned01"
COVER_ID = "AgACAgIAAxkBAAIcoverFile001"
LOGO_ID = "AgACAgIAAxkBAAIlogoFile0001"
FILE_PATH = "photos/file_42.jpg"
BODY = b"\xff\xd8\xff" + b"J" * 500


def _run(coro):
    return asyncio.run(coro)


def _set_city(user_id: int, city: str):
    async def _go():
        async with bot_db._connect() as conn:
            await conn.execute("UPDATE users SET event_city = ? WHERE telegram_id = ?", (city, user_id))
            await conn.commit()
    _run(_go())


class FakeFiles:
    def __init__(self):
        self.calls: list[str] = []
        self.mode = "ok"  # ok | getfile_down | download_404 | network

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.mode == "network":
            raise httpx.ConnectError("no route", request=request)
        if path.endswith("/getFile"):
            if self.mode == "getfile_down":
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": FILE_ID, "file_unique_id": "u", "file_size": len(BODY), "file_path": FILE_PATH,
            }})
        if path.startswith(f"/file/bot{TOKEN}/"):
            if self.mode == "download_404":
                return httpx.Response(404, text="nope")
            return httpx.Response(200, content=BODY, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)


@pytest.fixture
def files_api(monkeypatch):
    fake = FakeFiles()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


def _deadline() -> str:
    return (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_files.db")
    _standard_seed()
    _seed(users=[(OTHER_ID, "approved")], staff=[(BOUND_GAME_MANAGER, "game_manager", "spb")])
    task_id = _run(bot_db.create_task("Стенд", "Light", 10, "photo", _deadline(), None, title="Стенд"))
    sid = _run(bot_db.create_submission(task_id, DELEGATE_ID, "photo", FILE_ID, "2026-08-20 10:00:00"))
    _run(bot_db.add_submission_part(sid, 0, "photo", FILE_ID, None))
    return _client(_cfg(db_path))


def _get(client, user_id, file_id=FILE_ID):
    return client.get(f"/app/api/file/{file_id}", headers=_hdr(user_id))


def _assert_no_leak(resp):
    assert TOKEN not in resp.text
    assert FILE_PATH not in resp.text
    for k, v in resp.headers.items():
        assert TOKEN not in v and FILE_PATH not in v, k


# ── валидация и доступ ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["short", "../../etc/passwd", "x" * 201, "AgAC%20with%20space%20and%20more"])
def test_garbage_file_id_is_404(client, files_api, bad):
    resp = client.get(f"/app/api/file/{bad}", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 404
    assert files_api.calls == []


def test_owner_gets_file_with_safe_headers(client, files_api):
    resp = _get(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text
    assert resp.content == BODY
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("inline; filename=")
    assert resp.headers["cache-control"] == "private, max-age=3000"
    _assert_no_leak(resp)
    assert [p.rsplit("/", 1)[-1] for p in files_api.calls] == ["getFile", "file_42.jpg"]


def test_other_delegate_is_forbidden(client, files_api):
    resp = _get(client, OTHER_ID)
    assert resp.status_code == 403 and resp.json()["reason"] == "forbidden"
    assert files_api.calls == []
    _assert_no_leak(resp)


def test_unknown_file_id_is_forbidden_even_for_manager(client, files_api):
    """Allow-list: file_id, которого нет ни в сдачах, ни в обложках (чек/резюме) — закрыт."""
    resp = _get(client, GAME_MANAGER_ID, "BQACAgIAAxkBAAIreceiptSecret")
    assert resp.status_code == 403
    assert files_api.calls == []


def test_game_manager_gets_file(client, files_api):
    assert _get(client, GAME_MANAGER_ID).status_code == 200
    assert _get(client, ADMIN_ID).status_code == 200


def test_bound_manager_other_city_forbidden_same_city_ok(client, files_api):
    _set("event_city_enabled", "on")
    _set_city(DELEGATE_ID, "msk")
    assert _get(client, BOUND_GAME_MANAGER).status_code == 403
    assert files_api.calls == []
    _set_city(DELEGATE_ID, "spb")
    assert _get(client, BOUND_GAME_MANAGER).status_code == 200


def test_bound_manager_sees_everything_with_cities_module_off(client, files_api):
    _set_city(DELEGATE_ID, "msk")
    assert _get(client, BOUND_GAME_MANAGER).status_code == 200  # модуль выключен — скоупа нет


def test_task_cover_and_logo_open_for_any_delegate(client, files_api):
    task_id = _run(bot_db.create_task("С обложкой", "Light", 5, "photo", _deadline(), None,
                                      title="Обложка", photo_file_id=COVER_ID))
    _set("miniapp_logo", LOGO_ID)
    assert _get(client, OTHER_ID, COVER_ID).status_code == 200
    assert _get(client, OTHER_ID, LOGO_ID).status_code == 200
    _run(bot_db.archive_task(task_id))
    assert _get(client, OTHER_ID, COVER_ID).status_code == 403  # архив — обложка больше не общая


def test_theme_asset_open_for_any_delegate(client, files_api):
    """Phase 19.1-02 (D-08/D-15/D-16/T-19.1-06): ассеты оформления (обложка/стикеры/иконка
    монеты/лого тёмной темы) — allow-list через `web_theme.ASSET_KEYS`, доступны любому
    принципалу как публичная графика мероприятия."""
    STICKER_ID = "AgACAgIAAxkBAAIstickerEmpty01"
    _set("miniapp_sticker_empty", STICKER_ID)
    assert _get(client, OTHER_ID, STICKER_ID).status_code == 200


def test_consent_pdf_open_for_any_delegate(client, files_api):
    """PDF согласия (`consent_pdf_{key}` из `consent_list`) — документ, который делегат
    обязан прочитать до подписи в мастере анкеты Mini App: allow-list, как логотип.
    Ключ вне `consent_list` (удалённое согласие) — file_id забыт, 403."""
    PDF_ID = "BQACAgIAAxkBAAIconsentPdf001"
    _set("consent_list", "Согласие на обработку данных | personal")
    _set("consent_pdf_personal", PDF_ID)
    assert _get(client, OTHER_ID, PDF_ID).status_code == 200
    _set("consent_list", "")
    assert _get(client, OTHER_ID, PDF_ID).status_code == 403


def test_unset_theme_asset_slot_stays_forbidden(client, files_api):
    """Пустой слот (менеджер не загрузил ассет) — file_id всё равно неизвестен, 403."""
    resp = _get(client, OTHER_ID, "AgACAgIAAxkBAAInotUploadedYet1")
    assert resp.status_code == 403


def test_no_auth_is_401(client, files_api):
    assert client.get(f"/app/api/file/{FILE_ID}").status_code == 401


# ── upstream ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["getfile_down", "download_404", "network"])
def test_unavailable_upstream_is_404_without_token_in_logs(client, files_api, mode, caplog):
    files_api.mode = mode
    with caplog.at_level(logging.WARNING):
        resp = _get(client, DELEGATE_ID)
    assert resp.status_code == 404, resp.text
    assert resp.json()["reason"] == "not_found"
    _assert_no_leak(resp)
    for record in caplog.records:
        msg = record.getMessage()
        assert TOKEN not in msg and FILE_PATH not in msg and "api.telegram.org" not in msg
