"""D-29 (FORUM-CHECKIN.md, «Решения владельца 24.09») — плитка делегата «📅 Программа» в
Mini App: `GET /app/api/program` (`miniapp/routers/program.py`).

Харнесс — `tests/test_miniapp_routes.py` (тот же процесс/БД, что у соседних роутеров, тот же
приём, что `tests/test_miniapp_faq_260906.py`)."""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import _cfg, _client, _hdr, _seed, _set, _use_tmp_db

DELEGATE_ID = 900924501


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_program.db")
    _seed(
        users=[(DELEGATE_ID, "approved")],
        settings={"miniapp_enabled": "on", "event_name": "форума YouLead"},
    )
    return _client(_cfg(db_path))


def _set_user_city(tid, city):
    _run(bot_db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}", "event_city": city,
        "registration_date": "2026-01-01 00:00:00",
    }))


# ── auth ──────────────────────────────────────────────────────────────────────────────────

def test_program_no_auth_401(client):
    resp = client.get("/app/api/program")
    assert resp.status_code == 401
    assert resp.json() == {"reason": "no_auth"}


def test_program_unregistered_user_403_delegate_gate(client):
    resp = client.get("/app/api/program", headers=_hdr(900999))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "delegate_gate"


# ── гейт: тот же, что у кнопки программы в чате ────────────────────────────────────────────

PENDING_ID = 900924502


def test_program_no_content_403_section_off(client):
    """Нет ни фото, ни сессий — кнопки в чате нет, раздела в Mini App нет, ручка закрыта."""
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "program"}


def test_program_menu_toggle_off_403_even_with_photo(client):
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    _set("menu_program", "off")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"


def test_program_pending_delegate_403_delegate_gate(client):
    _seed(users=[(PENDING_ID, "pending")])
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    resp = client.get("/app/api/program", headers=_hdr(PENDING_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "delegate_gate"


def test_program_explicit_photo_view_without_photo_falls_back_to_table(client):
    """Менеджер выбрал «фото», а загружены только сессии — показываем таблицу (как чат
    переходит от фото к тексту сессий), а не пустой экран под видимой плиткой."""
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _set("program_miniapp_view", "photo")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["view"] == "table"
    assert body["days"] and body["empty_text"] is None


def test_program_explicit_table_view_without_sessions_falls_back_to_photo(client):
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    _set("program_miniapp_view", "table")
    body = client.get("/app/api/program", headers=_hdr(DELEGATE_ID)).json()
    assert body["view"] == "photo"
    assert body["photo_url"] == "/app/api/file/GLOBAL_FILE_ID"


def test_program_texts_reuse_chat_literals(client):
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    body = client.get("/app/api/program", headers=_hdr(DELEGATE_ID)).json()
    assert body["lang"] == "ru"
    assert body["texts"]["now"] == "🔴 Идёт сейчас"
    assert body["texts"]["hall"] == "Зал:"


# ── вид "photo" с загруженным фото ──────────────────────────────────────────────────────────

def test_program_photo_view_returns_file_proxy_url(client):
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["view"] == "photo"
    assert body["photo_url"] == "/app/api/file/GLOBAL_FILE_ID"
    assert body["empty_text"] is None


def test_program_photo_url_is_proxy_never_telegram_url(client):
    """Фронт получает только ссылку на прокси приложения (как у лого): ни адреса
    api.telegram.org, ни токена бота в ответе нет — байты качает сервер (T-19-19)."""
    from tests.test_miniapp_routes import TOKEN
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    raw = client.get("/app/api/program", headers=_hdr(DELEGATE_ID)).text
    assert "api.telegram.org" not in raw
    assert TOKEN not in raw
    assert "/file/bot" not in raw


def test_program_photo_is_public_asset_no_auth_needed(client):
    """Тег `<img>` не может послать initData (память проекта) — фото программы должно
    отдаваться СОВСЕМ без принципала, как лого/обложка (`is_public_asset`)."""
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    resp = client.get("/app/api/file/GLOBAL_FILE_ID")
    # 404 — сам файл не существует у тестового бота (getFile не резолвится), но КЛЮЧЕВОЕ —
    # не 401: is_public_asset пропустил запрос без принципала дальше, к попытке скачивания.
    assert resp.status_code != 401


# ── вид "table" — сессии заведены ───────────────────────────────────────────────────────────

def test_program_table_view_when_sessions_exist(client):
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["view"] == "table"
    assert len(body["days"]) == 1
    assert body["days"][0]["day"] == "2026-10-30"
    slot = body["days"][0]["slots"][0]
    assert slot["sessions"][0]["title"] == "Открытие"


def test_program_table_view_groups_parallel_sessions(client):
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Трек А"))
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:30", "11:30", "Трек Б"))
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    slots = resp.json()["days"][0]["slots"]
    assert len(slots) == 1
    assert {s["title"] for s in slots[0]["sessions"]} == {"Трек А", "Трек Б"}


# ── per_city фото/вид — своё городское побеждает общее ──────────────────────────────────────

def test_program_per_city_photo_overrides_global(client):
    from cities import per_city_key
    _set("event_city_enabled", "on")
    _set_user_city(DELEGATE_ID, "msk")
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    _set(per_city_key("program_photo_file_id", "msk"), "MSK_FILE_ID")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.json()["photo_url"] == "/app/api/file/MSK_FILE_ID"


def test_program_per_city_view_overrides_global(client):
    from cities import per_city_key
    _set("event_city_enabled", "on")
    _set_user_city(DELEGATE_ID, "msk")
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _set("program_miniapp_view", "photo")
    _set(per_city_key("program_miniapp_view", "msk"), "table")
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.json()["view"] == "table"


# ── /app/api/me: вычисляемый раздел «program» ─────────────────────────────────────────────

def _me_sections(client, tid=DELEGATE_ID):
    resp = client.get("/app/api/me", headers=_hdr(tid))
    assert resp.status_code == 200
    return resp.json()["sections"]


def test_me_program_section_hidden_without_content(client):
    assert _me_sections(client)["program"] is False


def test_me_program_section_visible_with_photo(client):
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    assert _me_sections(client)["program"] is True


def test_me_program_section_visible_with_sessions_only(client):
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    assert _me_sections(client)["program"] is True


def test_me_program_section_hidden_when_menu_button_off(client):
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    _set("menu_program", "off")
    assert _me_sections(client)["program"] is False


def test_me_program_section_follows_per_city_menu_toggle(client):
    from cities import per_city_key
    _set("event_city_enabled", "on")
    _set_user_city(DELEGATE_ID, "msk")
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    _set(per_city_key("menu_program", "msk"), "off")
    assert _me_sections(client)["program"] is False


def test_me_program_section_hidden_for_pending_delegate(client):
    _seed(users=[(PENDING_ID, "pending")])
    _set("program_photo_file_id", "GLOBAL_FILE_ID")
    assert _me_sections(client, PENDING_ID)["program"] is False


def test_me_program_section_label_present(client):
    body = client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()
    assert body["section_labels"]["program"] == "📅 Программа"


@pytest.mark.parametrize("toggle,photo,expected", [
    ("on", True, True), ("on", False, False), ("off", True, False), ("off", False, False),
])
def test_program_flag_matches_chat_menu_button(client, toggle, photo, expected):
    """Паритет с чатом: раздел Mini App виден ровно тогда, когда в меню бота есть кнопка
    «📅 Программа форума» (keyboards.builders.get_main_menu_kb)."""
    from keyboards.builders import get_main_menu_kb
    _set("menu_program", toggle)
    if photo:
        _set("program_photo_file_id", "GLOBAL_FILE_ID")
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    in_chat = any(b.text.startswith("📅 Программа") for row in kb.keyboard for b in row)
    assert in_chat is expected
    assert _me_sections(client)["program"] is expected


# ── сбой чтения города — 503 retry, а не программа чужого/общего города ─────────────────────

def test_program_city_read_failure_503_retry_not_foreign_program(client, monkeypatch):
    import miniapp.routers.program as program_router

    _set("event_city_enabled", "on")
    _set("program_photo_file_id", "GLOBAL_FILE_ID")

    async def _boom(_tid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(program_router, "get_user", _boom)
    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "retry"
    assert body["text"] == program_router._RETRY_TEXT
    assert "photo_url" not in body


def test_program_retry_text_has_manual_english():
    from miniapp.routers.program import _RETRY_TEXT
    from services.i18n_miniapp_manual import MANUAL_EN
    assert MANUAL_EN[_RETRY_TEXT].startswith("Couldn't load the schedule")


# ── паритет «видимость ⇔ есть что показать»: меню чата, /me, содержимое ручки ────────────────

@pytest.fixture
def disk_photo(tmp_path, monkeypatch):
    """Подмена диск-фоллбэка `resources/program.jpg` — по умолчанию файла нет."""
    import services.program as program_service

    missing = tmp_path / "no_program.jpg"
    monkeypatch.setattr(program_service, "PROGRAM_DEFAULT_PHOTO_PATH", str(missing))

    def _put():
        path = tmp_path / "program.jpg"
        path.write_bytes(b"fake-jpeg-bytes")
        monkeypatch.setattr(program_service, "PROGRAM_DEFAULT_PHOTO_PATH", str(path))
        return path

    return _put


@pytest.mark.parametrize("source,expected_view,expected_photo", [
    ("db_photo", "photo", "/app/api/file/GLOBAL_FILE_ID"),
    ("disk_only", "photo", "/app/api/program/photo-default"),
    ("sessions_only", "table", None),
    ("nothing", None, None),
])
def test_visibility_iff_something_to_show(client, disk_photo, source, expected_view, expected_photo):
    from keyboards.builders import get_main_menu_kb

    if source == "db_photo":
        _set("program_photo_file_id", "GLOBAL_FILE_ID")
    elif source == "disk_only":
        disk_photo()
    elif source == "sessions_only":
        _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))

    visible = expected_view is not None
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    in_chat = any(b.text.startswith("📅 Программа") for row in kb.keyboard for b in row)
    assert in_chat is visible
    assert _me_sections(client)["program"] is visible

    resp = client.get("/app/api/program", headers=_hdr(DELEGATE_ID))
    if not visible:
        assert resp.status_code == 403
        return
    body = resp.json()
    assert body["view"] == expected_view
    assert body["photo_url"] == expected_photo
    assert body["empty_text"] is None
    if expected_view == "table":
        assert body["days"]


def test_default_photo_route_serves_disk_file_without_auth(client, disk_photo):
    path = disk_photo()
    resp = client.get("/app/api/program/photo-default")
    assert resp.status_code == 200
    assert resp.content == path.read_bytes()
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_default_photo_route_404_without_disk_file(client, disk_photo):
    assert client.get("/app/api/program/photo-default").status_code == 404


def test_program_marks_session_running_now_by_msk(client, monkeypatch):
    """«🔴 Идёт сейчас»: API помечает слот `now`, если по МСК сессия идёт; фронт рисует метку
    по `slot.now` текстом `texts.now` (tests/test_miniapp_program_js_260924.py)."""
    from datetime import datetime
    from services import timeutil
    monkeypatch.setattr(timeutil, "msk_now", lambda: datetime(2026, 10, 30, 10, 30))
    _run(bot_db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _run(bot_db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "Обед"))
    body = client.get("/app/api/program", headers=_hdr(DELEGATE_ID)).json()
    slots = body["days"][0]["slots"]
    assert [s["now"] for s in slots] == [True, False]
    assert not any(s["next"] for s in slots)  # что-то идёт — «следующая» не нужна
    assert body["texts"]["now"] == "🔴 Идёт сейчас"

    monkeypatch.setattr(timeutil, "msk_now", lambda: datetime(2026, 10, 30, 11, 30))
    slots = client.get("/app/api/program", headers=_hdr(DELEGATE_ID)).json()["days"][0]["slots"]
    assert [s["now"] for s in slots] == [False, False]
    assert [s["next"] for s in slots] == [False, True]
