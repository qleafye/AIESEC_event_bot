"""Phase 23 Plan 03 (APP-TINDER-03, D-02): аватар делегата на карточке заявки.

Task 1 — `miniapp/avatars.py::resolve_avatar`/`initials` (кеш `getUserProfilePhotos`, fail-soft
на сбой Bot API, инициалы для фолбэка); `telegram_api.get_user_profile_photos` подменяется
счётчиком вызовов (сеть в тестах не трогаем — правило `<local_rules>`).

Task 2 дописывает сюда же тесты allow-list для `/app/api/file/{file_id}` — харнесс
`tests/test_miniapp_files.py`/`tests/test_miniapp_routes.py` (`_client`/`_hdr`/`_seed`/`_set`).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from config import config
from database import db
from miniapp import avatars, telegram_api
from miniapp.telegram_api import TelegramApiError

from tests.test_miniapp_routes import (
    BOUND_MANAGER_ID,
    DELEGATE_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db as _use_tmp_route_db,
)

PHOTO_ID = "AgACAgIAAxkBAAIavatarSmall01"
BIG_PHOTO_ID = "AgACAgIAAxkBAAIavatarBig0001"

CFG = SimpleNamespace(bot_token="123456:TEST-TOKEN", proxy_url=None)

# ── Task 2: доступ к аватару через прокси файлов ────────────────────────────────────────
AVATAR_FILE_ID = "AgACAvatarDelegateFileId01"
RESUME_FILE_ID = "BQACResumeDelegateFileId01"
AVATAR_FILE_PATH = "photos/avatar_42.jpg"
AVATAR_BODY = b"\xff\xd8\xff" + b"A" * 100


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "applications_avatar.db")


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, full_name="Иван Петров"):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name,
        "registration_date": "2026-01-01 00:00:00",
    }))


def _photo_response():
    # Внутренний список Bot API — от меньшего размера к большему; кешируется ПЕРВЫЙ (D-02).
    return [[
        {"file_id": PHOTO_ID, "width": 160, "height": 160},
        {"file_id": BIG_PHOTO_ID, "width": 640, "height": 640},
    ]]


class FakePhotos:
    """Подмена `telegram_api.get_user_profile_photos` — сеть в тестах не трогаем."""

    def __init__(self, photos=None, total_count=None, error=False):
        self.calls = 0
        self._photos = photos if photos is not None else []
        self._total_count = total_count if total_count is not None else len(self._photos)
        self._error = error

    async def __call__(self, cfg, user_id, limit=1):
        self.calls += 1
        if self._error:
            raise TelegramApiError("upstream_unavailable")
        return {"total_count": self._total_count, "photos": self._photos}


# ── resolve_avatar: кеш ──────────────────────────────────────────────────────────────────

def test_cached_avatar_skips_bot_api(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4901)
    _run(db.set_user_avatar(4901, PHOTO_ID, "2026-09-01 00:00:00"))
    fake = FakePhotos()
    monkeypatch.setattr(telegram_api, "get_user_profile_photos", fake)

    user = _run(db.get_user(4901))
    result = _run(avatars.resolve_avatar(CFG, user))

    assert result == PHOTO_ID
    assert fake.calls == 0


def test_empty_cache_fetches_and_caches_smallest_size(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4902)
    fake = FakePhotos(photos=_photo_response())
    monkeypatch.setattr(telegram_api, "get_user_profile_photos", fake)

    user = _run(db.get_user(4902))
    result = _run(avatars.resolve_avatar(CFG, user))

    assert result == PHOTO_ID
    assert fake.calls == 1
    stored = _run(db.get_user(4902))
    assert stored["avatar_file_id"] == PHOTO_ID


def test_second_call_reuses_cache_without_new_bot_api_call(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4903)
    fake = FakePhotos(photos=_photo_response())
    monkeypatch.setattr(telegram_api, "get_user_profile_photos", fake)

    user = _run(db.get_user(4903))
    _run(avatars.resolve_avatar(CFG, user))
    user2 = _run(db.get_user(4903))
    _run(avatars.resolve_avatar(CFG, user2))

    assert fake.calls == 1


# ── resolve_avatar: отрицательный кеш ────────────────────────────────────────────────────

def test_no_photos_returns_none_and_caches_negative_result(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4904)
    fake = FakePhotos(photos=[], total_count=0)
    monkeypatch.setattr(telegram_api, "get_user_profile_photos", fake)

    user = _run(db.get_user(4904))
    result = _run(avatars.resolve_avatar(CFG, user))

    assert result is None
    assert fake.calls == 1
    stored = _run(db.get_user(4904))
    assert stored["avatar_file_id"] is None
    assert stored["avatar_checked_at"] is not None


def test_negative_cache_is_not_rechecked_within_ttl(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4905)
    fake = FakePhotos(photos=[], total_count=0)
    monkeypatch.setattr(telegram_api, "get_user_profile_photos", fake)

    user = _run(db.get_user(4905))
    _run(avatars.resolve_avatar(CFG, user))
    user2 = _run(db.get_user(4905))
    result = _run(avatars.resolve_avatar(CFG, user2))

    assert result is None
    assert fake.calls == 1


# ── resolve_avatar: сбой Bot API — fail-soft ─────────────────────────────────────────────

def test_telegram_api_error_returns_none_without_caching(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _run(db.init_db())
    _seed_user(4906)
    fake_error = FakePhotos(error=True)
    monkeypatch.setattr(telegram_api, "get_user_profile_photos", fake_error)

    user = _run(db.get_user(4906))
    result = _run(avatars.resolve_avatar(CFG, user))

    assert result is None
    stored = _run(db.get_user(4906))
    assert stored["avatar_file_id"] is None
    assert stored["avatar_checked_at"] is None  # НИ ОДНОЙ записи в БД — следующий показ пробует снова

    fake_ok = FakePhotos(photos=_photo_response())
    monkeypatch.setattr(telegram_api, "get_user_profile_photos", fake_ok)
    result2 = _run(avatars.resolve_avatar(CFG, stored))

    assert result2 == PHOTO_ID
    assert fake_ok.calls == 1


# ── initials ─────────────────────────────────────────────────────────────────────────────

def test_initials_two_words():
    assert avatars.initials("Иван Петров") == "ИП"


def test_initials_one_word():
    assert avatars.initials("Иван") == "И"


def test_initials_lowercase_input_is_uppercased():
    assert avatars.initials("иван петров") == "ИП"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_initials_empty_is_question_mark(value):
    assert avatars.initials(value) == "?"


# ── can_read_file: allow-list для аватара (Task 2) ───────────────────────────────────────
#
# BOUND_MANAGER_ID (900601) — staff `reg_manager`, привязан к spb (caps moderate_reg +
# moderate_receipts, `_standard_seed`); GAME_MANAGER_ID (900600) — `game_manager` (только
# moderate_game), DELEGATE_ID (900100) — одобренный делегат без прав. Фикстуры БД/сети — свои
# (не разделяем харнесс `test_miniapp_files.py::client`, у которого своя сдача-фикстура).


class _AvatarTransport:
    def __init__(self):
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": "x", "file_unique_id": "u", "file_size": len(AVATAR_BODY),
                "file_path": AVATAR_FILE_PATH,
            }})
        return httpx.Response(200, content=AVATAR_BODY, headers={"content-type": "image/jpeg"})


@pytest.fixture
def avatar_download(monkeypatch):
    fake = _AvatarTransport()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


@pytest.fixture
def avatar_client(tmp_path):
    db_path = _use_tmp_route_db(tmp_path, "miniapp_applications_avatar.db")
    _standard_seed()
    return _client(_cfg(db_path))


def _set_field(user_id: int, field: str, value):
    async def _go():
        async with db._connect() as conn:
            await conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, user_id))
            await conn.commit()
    asyncio.run(_go())


def _set_avatar(user_id: int, file_id: str):
    asyncio.run(db.set_user_avatar(user_id, file_id, "2026-09-01 00:00:00"))


def _get_file(client, user_id: int, file_id: str):
    return client.get(f"/app/api/file/{file_id}", headers=_hdr(user_id))


def test_reg_manager_same_city_reads_avatar(avatar_client, avatar_download):
    _set("event_city_enabled", "on")
    _set_field(DELEGATE_ID, "event_city", "spb")
    _set_avatar(DELEGATE_ID, AVATAR_FILE_ID)

    resp = _get_file(avatar_client, BOUND_MANAGER_ID, AVATAR_FILE_ID)

    assert resp.status_code == 200, resp.text


def test_reg_manager_other_city_forbidden(avatar_client, avatar_download):
    _set("event_city_enabled", "on")
    _set_field(DELEGATE_ID, "event_city", "msk")
    _set_avatar(DELEGATE_ID, AVATAR_FILE_ID)

    resp = _get_file(avatar_client, BOUND_MANAGER_ID, AVATAR_FILE_ID)

    assert resp.status_code == 403
    assert avatar_download.calls == []


def test_game_manager_without_moderate_reg_is_forbidden(avatar_client, avatar_download):
    """`moderate_game` (без `moderate_reg`) не открывает аватар делегата заявки."""
    _set_avatar(DELEGATE_ID, AVATAR_FILE_ID)

    resp = _get_file(avatar_client, GAME_MANAGER_ID, AVATAR_FILE_ID)

    assert resp.status_code == 403
    assert avatar_download.calls == []


def test_delegate_cannot_read_own_avatar_via_manager_branch(avatar_client, avatar_download):
    _set_avatar(DELEGATE_ID, AVATAR_FILE_ID)

    resp = _get_file(avatar_client, DELEGATE_ID, AVATAR_FILE_ID)

    assert resp.status_code == 403
    assert avatar_download.calls == []


def test_resume_file_id_of_same_delegate_not_opened_via_avatar_branch(avatar_client, avatar_download):
    """Сторож границы (T-23-11): сверяется ИМЕННО колонка `avatar_file_id`, а не «любой
    file_id пользователя» — `resume_file_id` того же делегата этой веткой не открывается,
    даже держателю `moderate_reg` в его городском скоупе."""
    _set_field(DELEGATE_ID, "event_city", "spb")
    _set_avatar(DELEGATE_ID, AVATAR_FILE_ID)
    _set_field(DELEGATE_ID, "resume_file_id", RESUME_FILE_ID)

    resp = _get_file(avatar_client, BOUND_MANAGER_ID, RESUME_FILE_ID)

    assert resp.status_code == 403
    assert avatar_download.calls == []


def test_cities_module_off_avatar_open_regardless_of_city(avatar_client, avatar_download):
    """Модуль городов выключен — то же правило, что у очереди заявок: без фильтра."""
    _set_field(DELEGATE_ID, "event_city", "msk")
    _set_avatar(DELEGATE_ID, AVATAR_FILE_ID)

    resp = _get_file(avatar_client, BOUND_MANAGER_ID, AVATAR_FILE_ID)

    assert resp.status_code == 200, resp.text
