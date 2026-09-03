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

import pytest

from config import config
from database import db
from miniapp import avatars, telegram_api
from miniapp.telegram_api import TelegramApiError

PHOTO_ID = "AgACAgIAAxkBAAIavatarSmall01"
BIG_PHOTO_ID = "AgACAgIAAxkBAAIavatarBig0001"

CFG = SimpleNamespace(bot_token="123456:TEST-TOKEN", proxy_url=None)


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
