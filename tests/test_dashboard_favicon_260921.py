"""Квик 260921: своя иконка вкладки браузера ДАШБОРДА статистики.

Два независимых сюжета:

(A) Раздача — `dashboard/main.py::_favicon_url` (порядок отката favicon -> лого -> статика
АЙСЕК) и `GET /api/file/{file_id}` (dashboard/files.py::ASSET_SETTING_KEYS). Харнесс скопирован
из tests/test_dashboard_render.py / tests/test_dashboard_event_assets.py.

(B) Приём в боте — handlers/admin_miniapp_theme.py (кнопка на экране «🎭 Пресеты и ручки»,
document-only приёмник вместо MiniAppTheme-состояния) и handlers/admin_settings.py
(`settings_receive_file_photo`/`settings_receive_file_doc`, ветка `raw_file_key ==
dashboard_favicon.SETTING_KEY`). Fake-объекты — из tests/test_admin_sections_ia20.py, тот же
приём, что tests/test_settings_flow_data_isolation_260831.py.

pytest-asyncio в этом окружении нет — каждый async-вызов через asyncio.run(), config.DB_PATH
смотрит в tmp_path (конвенция проекта).
"""
from __future__ import annotations

import asyncio
import time
import hashlib
import hmac

import httpx
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db
from database import db

import dashboard_favicon
from dashboard import files as dashboard_files
from dashboard.config import DashboardConfig
from dashboard.main import create_app

from handlers import admin_miniapp_theme as theme_mod
from handlers import admin_settings as st
from handlers.states import EditSetting
from settings_schema import SETTINGS_SCHEMA

from tests.test_admin_sections_ia20 import FakeAnswerMessage, FakeCallback, FakePhoto, FakeState
from tests.test_roles_phase8 import ADMIN_ID, _flat_callback_data, _roles_ready

BOT_TOKEN = "123456:ABCDEF-testtoken"
FAVICON_FILE_ID = "AgACAgIAAxkBAAI" + "f" * 15  # 30 символов, проходит FILE_ID_RE
LOGO_FILE_ID = "BgACAgIAAxkBAAI" + "l" * 15


# ── Сюжет (A): раздача дашбордом ──────────────────────────────────────────────────────────

def _use_tmp_db(tmp_path, name: str = "dashboard_favicon.db") -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(settings):
    async with bot_db._connect() as conn:
        for key, value in settings.items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await conn.commit()


def _seed(settings):
    asyncio.run(_seed_async(settings))


def _cfg(db_path: str, **overrides) -> DashboardConfig:
    base = dict(
        db_path=db_path,
        public_url="https://yl26.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token=BOT_TOKEN,
        admin_ids=(ADMIN_ID,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    )
    base.update(overrides)
    return DashboardConfig(**base)


def _client(cfg: DashboardConfig, **kwargs) -> TestClient:
    app = create_app(cfg=cfg)
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(app, **kwargs)


def _login_as(client: TestClient, telegram_id: int):
    payload = {
        "id": str(telegram_id),
        "first_name": "Тест",
        "auth_date": str(int(time.time()) - 5),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    payload["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return client.get("/auth/callback", params=payload, follow_redirects=False)


def _stats_manager_client(db_path: str, extra_settings=None) -> TestClient:
    settings = {"role_caps_reg_manager": "moderate_reg;stats"}
    settings.update(extra_settings or {})
    asyncio.run(_seed_staff_and_settings(settings))
    client = _client(_cfg(db_path))
    _login_as(client, 900700)
    return client


async def _seed_staff_and_settings(settings):
    async with bot_db._connect() as conn:
        await conn.execute(
            "INSERT INTO staff (telegram_id, role, added_by, added_at, city) VALUES (?, ?, ?, ?, ?)",
            (900700, "reg_manager", ADMIN_ID, "2026-01-01 00:00:00", None),
        )
        for key, value in settings.items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await conn.commit()


def test_favicon_wins_over_logo_and_logo_on_page_is_unchanged(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _stats_manager_client(
        db_path,
        {"dashboard_favicon": FAVICON_FILE_ID, "miniapp_logo": LOGO_FILE_ID, "event_name": "Демо"},
    )
    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.text
    assert f'<link rel="icon" href="/api/file/{FAVICON_FILE_ID}">' in text
    # ЛОГО на странице — по-прежнему event_logo_url (miniapp_logo), своя иконка вкладки его
    # НЕ подменяет (D-3 брифа квика 260921).
    assert f'<img class="event-logo" src="/api/file/{LOGO_FILE_ID}" alt="Демо">' in text


def test_favicon_falls_back_to_logo_when_not_set(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _stats_manager_client(db_path, {"miniapp_logo": LOGO_FILE_ID})
    resp = client.get("/")
    assert resp.status_code == 200
    assert f'<link rel="icon" href="/api/file/{LOGO_FILE_ID}">' in resp.text


def test_favicon_falls_back_to_static_icon_when_both_absent(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/api/file/" not in resp.text
    assert '<link rel="icon" href="/static/brand/aiesec-human-blue.svg"' in resp.text


def test_favicon_garbage_value_ignored_falls_back_to_logo(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _stats_manager_client(
        db_path, {"dashboard_favicon": "../../etc/passwd", "miniapp_logo": LOGO_FILE_ID},
    )
    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.text
    assert "../../etc/passwd" not in text
    assert f'<link rel="icon" href="/api/file/{LOGO_FILE_ID}">' in text


def _mock_handler(*, file_path="documents/icon.png", content=b"\x89PNGfakebytes", content_type="image/png"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": file_path}})
        return httpx.Response(200, content=content, headers={"content-type": content_type})
    return handler


def _patch_client(monkeypatch, handler) -> None:
    def factory(cfg, timeout=dashboard_files._TIMEOUT_SECONDS):
        return httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dashboard_files, "_make_client", factory)


def test_api_file_serves_favicon_key(tmp_path, monkeypatch):
    db_path = _use_tmp_db(tmp_path)
    _seed({"dashboard_favicon": FAVICON_FILE_ID})
    _patch_client(monkeypatch, _mock_handler())
    client = _client(_cfg(db_path))

    resp = client.get(f"/api/file/{FAVICON_FILE_ID}")
    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "image/png"


def test_api_file_unknown_id_still_404s(tmp_path, monkeypatch):
    db_path = _use_tmp_db(tmp_path)
    _seed({"dashboard_favicon": FAVICON_FILE_ID})
    _patch_client(monkeypatch, _mock_handler())
    client = _client(_cfg(db_path))

    other_id = "BgACAgIAAxkBAAI" + "z" * 15
    resp = client.get(f"/api/file/{other_id}")
    assert resp.status_code == 404


def test_asset_setting_keys_include_dashboard_favicon():
    assert dashboard_favicon.SETTING_KEY in dashboard_files.ASSET_SETTING_KEYS


def test_registry_entry_shape():
    entry = SETTINGS_SCHEMA[dashboard_favicon.SETTING_KEY]
    assert entry["group"] == "dashboard"
    assert entry["type"] == "file"


# ── Сюжет (B): приём в боте ────────────────────────────────────────────────────────────────

class _FakeDoc:
    def __init__(self, file_id, mime_type, file_name, file_size):
        self.file_id = file_id
        self.mime_type = mime_type
        self.file_name = file_name
        self.file_size = file_size


def test_theme_screen_offers_favicon_upload_button(tmp_path):
    _roles_ready(tmp_path)
    kb = asyncio.run(theme_mod.build_miniapp_theme_keyboard())
    data = _flat_callback_data(kb)
    assert f"miniapp_theme_photo:{dashboard_favicon.SETTING_KEY}" in data
    # Ничего не загружено -> кнопки «Убрать» ещё нет.
    assert f"miniapp_theme_remove_photo:{dashboard_favicon.SETTING_KEY}" not in data


def test_photo_start_uses_editsetting_waiting_for_file_not_a_new_state(tmp_path):
    _roles_ready(tmp_path)
    state = FakeState()
    cb = FakeCallback(f"miniapp_theme_photo:{dashboard_favicon.SETTING_KEY}")
    asyncio.run(theme_mod.miniapp_theme_photo_start(cb, state))

    assert asyncio.run(state.get_state()) == EditSetting.waiting_for_file.state
    assert asyncio.run(state.get_data()) == {"raw_file_key": dashboard_favicon.SETTING_KEY}
    assert cb.message.edit_calls == 1
    assert cb.message.text == dashboard_favicon.UPLOAD_PROMPT_HTML


def test_bot_upload_accepts_png_document_and_returns_to_theme_screen(tmp_path):
    _roles_ready(tmp_path)
    state = FakeState(data={"raw_file_key": dashboard_favicon.SETTING_KEY}, state=EditSetting.waiting_for_file)
    msg = FakeAnswerMessage(document=_FakeDoc("favicon-doc-id", "image/png", "icon.png", 2048))
    asyncio.run(st.settings_receive_file_doc(msg, state))

    assert asyncio.run(db.get_setting(dashboard_favicon.SETTING_KEY)) == "favicon-doc-id"
    assert state.cleared
    assert any(t == dashboard_favicon.SUCCESS_MESSAGE for t, _ in msg.sent)
    kb = msg.screen[1]
    expected = asyncio.run(theme_mod.build_miniapp_theme_keyboard())
    assert set(_flat_callback_data(kb)) == set(_flat_callback_data(expected))
    assert f"miniapp_theme_remove_photo:{dashboard_favicon.SETTING_KEY}" in _flat_callback_data(kb)


def test_bot_upload_rejects_photo_with_howto_and_keeps_state(tmp_path):
    _roles_ready(tmp_path)
    state = FakeState(data={"raw_file_key": dashboard_favicon.SETTING_KEY}, state=EditSetting.waiting_for_file)
    msg = FakeAnswerMessage(photo=[FakePhoto("favicon-photo-id")])
    asyncio.run(st.settings_receive_file_photo(msg, state))

    assert asyncio.run(db.get_setting(dashboard_favicon.SETTING_KEY)) is None
    assert not state.cleared
    assert asyncio.run(state.get_data()) == {"raw_file_key": dashboard_favicon.SETTING_KEY}
    assert msg.sent[-1][0] == dashboard_favicon.PHOTO_REJECT_MESSAGE


def test_bot_upload_rejects_oversized_document(tmp_path):
    _roles_ready(tmp_path)
    state = FakeState(data={"raw_file_key": dashboard_favicon.SETTING_KEY}, state=EditSetting.waiting_for_file)
    msg = FakeAnswerMessage(document=_FakeDoc(
        "favicon-doc-id", "image/png", "icon.png", dashboard_favicon.MAX_BYTES + 1,
    ))
    asyncio.run(st.settings_receive_file_doc(msg, state))

    assert asyncio.run(db.get_setting(dashboard_favicon.SETTING_KEY)) is None
    assert not state.cleared
    assert msg.sent[-1][0] == dashboard_favicon.SIZE_REJECT_MESSAGE


def test_bot_upload_rejects_non_image_document(tmp_path):
    _roles_ready(tmp_path)
    state = FakeState(data={"raw_file_key": dashboard_favicon.SETTING_KEY}, state=EditSetting.waiting_for_file)
    msg = FakeAnswerMessage(document=_FakeDoc("doc-id", "application/pdf", "file.pdf", 1024))
    asyncio.run(st.settings_receive_file_doc(msg, state))

    assert asyncio.run(db.get_setting(dashboard_favicon.SETTING_KEY)) is None
    assert not state.cleared
    assert msg.sent[-1][0] == dashboard_favicon.MIME_REJECT_MESSAGE


def test_bot_upload_accepts_ico_by_extension_when_mime_is_generic(tmp_path):
    """Телефоны нередко шлют .ico как application/octet-stream — расширение имени файла
    достаточно (dashboard_favicon.is_acceptable_document)."""
    _roles_ready(tmp_path)
    state = FakeState(data={"raw_file_key": dashboard_favicon.SETTING_KEY}, state=EditSetting.waiting_for_file)
    msg = FakeAnswerMessage(document=_FakeDoc(
        "favicon-ico-id", "application/octet-stream", "favicon.ico", 4096,
    ))
    asyncio.run(st.settings_receive_file_doc(msg, state))

    assert asyncio.run(db.get_setting(dashboard_favicon.SETTING_KEY)) == "favicon-ico-id"


def test_remove_button_clears_setting_and_falls_back(tmp_path):
    _roles_ready(tmp_path)
    asyncio.run(db.set_setting(dashboard_favicon.SETTING_KEY, "favicon-doc-id"))
    cb = FakeCallback(f"miniapp_theme_remove_photo:{dashboard_favicon.SETTING_KEY}")
    asyncio.run(theme_mod.miniapp_theme_remove_photo(cb))

    assert asyncio.run(db.get_setting(dashboard_favicon.SETTING_KEY)) is None
    assert cb.answers[0][0] == f"«{dashboard_favicon.LABEL}» убрана"


def test_consent_pdf_flow_is_unaffected_by_the_new_raw_key_branch(tmp_path):
    """Регресс: раздел quiсk PDF согласия (существующий raw_file_key-путь) не задет новой
    веткой dashboard_favicon — она матчится строго по значению ключа."""
    _roles_ready(tmp_path)
    state = FakeState(data={"raw_file_key": "consent_pdf_offer"}, state=EditSetting.waiting_for_file)
    msg = FakeAnswerMessage(document=_FakeDoc("consent-pdf-id", "application/pdf", "offer.pdf", 1024))
    asyncio.run(st.settings_receive_file_doc(msg, state))

    assert asyncio.run(db.get_setting("consent_pdf_offer")) == "consent-pdf-id"
    assert asyncio.run(db.get_setting(dashboard_favicon.SETTING_KEY)) is None
