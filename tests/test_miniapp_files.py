"""Phase 19 Plan 04 Task 3 (WEBAPP-01, D-03, T-19-19/T-19-20): `GET /app/api/file/{file_id}`
— прокси getFile без утечки токена. Bot API — `httpx.MockTransport` через
`telegram_api._make_client`; харнесс — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx
import pytest

from database import db as bot_db

from miniapp import telegram_api
from miniapp.file_tokens import file_url, mint_file_token, verify_file_token

from tests.test_miniapp_auth import TOKEN
from tests.test_miniapp_routes import (
    ADMIN_ID,
    BOUND_MANAGER_ID,
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
        self.content_type = "image/jpeg"  # заголовок, который отдаёт файловый сервер TG

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
            return httpx.Response(200, content=BODY, headers={"content-type": self.content_type})
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


# ── quick 260910-w3j (IMG-01..06): токен доступа к файлам ────────────────────────────────

def test_mint_and_verify_file_token_round_trip():
    token = mint_file_token(TOKEN, DELEGATE_ID)
    assert verify_file_token(token, TOKEN) == DELEGATE_ID


@pytest.mark.parametrize("bad", ["", "garbage", "1.2", "1.2.3.4", "not.an.int"])
def test_verify_file_token_rejects_garbage(bad):
    assert verify_file_token(bad, TOKEN) is None


def test_verify_file_token_rejects_tampered_signature():
    token = mint_file_token(TOKEN, DELEGATE_ID)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert verify_file_token(tampered, TOKEN) is None


def test_verify_file_token_rejects_wrong_bot_token():
    token = mint_file_token(TOKEN, DELEGATE_ID)
    assert verify_file_token(token, "999999:OTHER-token") is None


def test_verify_file_token_rejects_expired():
    now = time.time()
    token = mint_file_token(TOKEN, DELEGATE_ID, now=now - 100000)
    assert verify_file_token(token, TOKEN, now=now) is None


def test_verify_file_token_accepts_fresh_at_edge():
    now = time.time()
    token = mint_file_token(TOKEN, DELEGATE_ID, now=now)
    assert verify_file_token(token, TOKEN, now=now + 1000) == DELEGATE_ID


def test_file_url_appends_token_or_omits_it():
    assert file_url("AgAC123") == "/app/api/file/AgAC123"
    assert file_url("AgAC123", "tok") == "/app/api/file/AgAC123?t=tok"


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


# ── UAT 07.09 (T-d6t-01/T-d6t-03): свой аватар делегата ─────────────────────────────────

AVATAR_ID = "AgACAgIAAxkBAAIavatarOwned0001"
OTHER_AVATAR_ID = "AgACAgIAAxkBAAIavatarOther0001"


def _set_avatar(user_id: int, file_id: str):
    _run(bot_db.set_user_avatar(user_id, file_id, "2026-09-08 00:00:00"))


def test_own_avatar_is_visible_to_owner(client, files_api):
    _set_avatar(DELEGATE_ID, AVATAR_ID)
    resp = _get(client, DELEGATE_ID, AVATAR_ID)
    assert resp.status_code == 200
    _assert_no_leak(resp)


def test_other_delegate_avatar_is_forbidden(client, files_api):
    _set_avatar(OTHER_ID, OTHER_AVATAR_ID)
    resp = _get(client, DELEGATE_ID, OTHER_AVATAR_ID)
    assert resp.status_code == 403


def test_own_resume_and_receipt_are_still_forbidden(client, files_api):
    """Новая ветка self-view сверяет ТОЛЬКО колонку аватара — резюме и чек своего же
    делегата ей не открываются."""
    RESUME_ID = "BQACAgIAAxkBAAIresumeOwned0001"
    RECEIPT_ID = "BQACAgIAAxkBAAIreceiptOwned001"

    async def _go():
        async with bot_db._connect() as conn:
            await conn.execute(
                "UPDATE users SET resume_file_id = ?, receipt_file_id = ? WHERE telegram_id = ?",
                (RESUME_ID, RECEIPT_ID, DELEGATE_ID),
            )
            await conn.commit()
    _run(_go())

    assert _get(client, DELEGATE_ID, RESUME_ID).status_code == 403
    assert _get(client, DELEGATE_ID, RECEIPT_ID).status_code == 403


def test_bound_manager_still_sees_avatar_in_scope(client, files_api):
    """Прежнее поведение ветки moderate_reg не сломано self-view веткой."""
    _set_avatar(DELEGATE_ID, AVATAR_ID)
    resp = _get(client, BOUND_MANAGER_ID, AVATAR_ID)
    assert resp.status_code == 200


def test_octet_stream_avatar_gets_real_image_content_type(client, files_api):
    """T-d6t-03: TG отдаёт octet-stream, `file_path` — `.jpg` -> content-type честный, nosniff
    на месте, ни file_path, ни токен не утекают."""
    _set_avatar(DELEGATE_ID, AVATAR_ID)
    files_api.content_type = "application/octet-stream"
    resp = _get(client, DELEGATE_ID, AVATAR_ID)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"
    _assert_no_leak(resp)


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


# ── quick 260910-w3j: слой A (публичные ассеты) и слой B (токен) без заголовков ──────────

def test_logo_theme_asset_and_consent_pdf_open_with_zero_headers(client, files_api):
    """Ровно то, что делает настоящий тег <img> — GET совсем без headers=."""
    LOGO_ID = "AgACAgIAAxkBAAIlogoNoHead0001"
    STICKER_ID = "AgACAgIAAxkBAAIstickerNoHead1"
    PDF_ID = "BQACAgIAAxkBAAIconsentNoHead1"
    _set("miniapp_logo", LOGO_ID)
    _set("miniapp_sticker_empty", STICKER_ID)
    _set("consent_list", "Согласие | personal")
    _set("consent_pdf_personal", PDF_ID)

    assert client.get(f"/app/api/file/{LOGO_ID}").status_code == 200
    assert client.get(f"/app/api/file/{STICKER_ID}").status_code == 200
    assert client.get(f"/app/api/file/{PDF_ID}").status_code == 200


def test_avatar_without_headers_and_without_token_is_401(client, files_api):
    _set_avatar(DELEGATE_ID, AVATAR_ID)
    resp = client.get(f"/app/api/file/{AVATAR_ID}")
    assert resp.status_code == 401
    assert resp.json()["reason"] == "no_auth"


def test_avatar_with_own_token_is_200_with_others_token_is_403(client, files_api):
    _set_avatar(DELEGATE_ID, AVATAR_ID)
    own_token = mint_file_token(TOKEN, DELEGATE_ID)
    resp = client.get(f"/app/api/file/{AVATAR_ID}?t={own_token}")
    assert resp.status_code == 200
    _assert_no_leak(resp)

    other_token = mint_file_token(TOKEN, OTHER_ID)
    resp = client.get(f"/app/api/file/{AVATAR_ID}?t={other_token}")
    assert resp.status_code == 403
