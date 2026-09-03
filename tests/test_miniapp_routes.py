"""Phase 19 Plan 01 Task 2 (WEBAPP-01, D-05/D-06/D-09): маршруты `miniapp.main` и модель
доступа на каждый запрос — `fastapi.testclient.TestClient`.

Харнесс по образцу `tests/test_dashboard_routes.py`: временная БД через
`bot_config.DB_PATH` + `bot_db.init_db()`, `base_url="https://testserver"` (Secure-cookie
иначе не прилипает). Cookie `yl_dash` выдаёт НАСТОЯЩЕЕ приложение дашборда
(`dashboard.main` `/auth/callback`) — так проверяется допущение A5 из RESEARCH: другой
процесс с тем же secret читает cookie без изменений.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import hmac
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db
from dashboard.config import DashboardConfig
from dashboard.main import create_app as create_dashboard_app

from miniapp.deps import Principal, UploadActor, delegate_gate, require_cap, require_section, upload_actor
from miniapp.main import create_app

from tests.test_miniapp_auth import TOKEN, make_init_data

ADMIN_ID = 900001
DELEGATE_ID = 900100  # одобренный делегат без прав
PENDING_ID = 900101
REJECTED_ID = 900102
UNREGISTERED_ID = 900103
GAME_MANAGER_ID = 900600  # staff game_manager (moderate_game), не делегат
BOUND_MANAGER_ID = 900601  # staff reg_manager, привязан к городу


def _use_tmp_db(tmp_path, name: str = "miniapp_routes.db") -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(*, staff=None, settings=None, users=None):
    async with bot_db._connect() as conn:
        for telegram_id, role, city in staff or []:
            await conn.execute(
                "INSERT INTO staff (telegram_id, role, added_by, added_at, city) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, role, ADMIN_ID, "2026-01-01 00:00:00", city),
            )
        for telegram_id, status in users or []:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status) VALUES (?, ?, ?)",
                (telegram_id, f"User {telegram_id}", status),
            )
        for key, value in (settings or {}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


def _set(key: str, value: str):
    _seed(settings={key: value})


def _cfg(db_path: str, **overrides) -> DashboardConfig:
    base = dict(
        db_path=db_path,
        public_url="https://yl26.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token=TOKEN,
        admin_ids=(ADMIN_ID,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    )
    base.update(overrides)
    return DashboardConfig(**base)


def _client(cfg: DashboardConfig, **kwargs) -> TestClient:
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(create_app(cfg=cfg), **kwargs)


def _hdr(user_id: int, **extra) -> dict:
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id, **extra)}


def _widget_sign(payload: dict) -> dict:
    data = {k: v for k, v in payload.items() if k != "hash"}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    return {**data, "hash": hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()}


def _dashboard_cookie(cfg: DashboardConfig, telegram_id: int) -> str:
    """Cookie `yl_dash`, выданная НАСТОЯЩИМ дашбордом через Login Widget callback."""
    dash = TestClient(create_dashboard_app(cfg=cfg), base_url="https://testserver")
    payload = _widget_sign({
        "id": str(telegram_id), "first_name": "Тест", "auth_date": str(int(time.time()) - 5),
    })
    resp = dash.get("/auth/callback", params=payload, follow_redirects=False)
    assert resp.status_code == 302
    cookie = dash.cookies.get("yl_dash")
    assert cookie
    return cookie


def _cookie_client(cfg: DashboardConfig, telegram_id: int) -> TestClient:
    client = _client(cfg)
    client.cookies.set("yl_dash", _dashboard_cookie(cfg, telegram_id))
    return client


def _standard_seed():
    _seed(
        staff=[(GAME_MANAGER_ID, "game_manager", None), (BOUND_MANAGER_ID, "reg_manager", "spb")],
        users=[
            (DELEGATE_ID, "approved"),
            (PENDING_ID, "pending"),
            (REJECTED_ID, "rejected"),
        ],
        settings={"miniapp_enabled": "on", "event_name": "форума YouLead"},
    )


# Тестовый роутер, чтобы прогнать зависимости, у которых в этом плане ещё нет маршрутов.
_probe = APIRouter()


@_probe.get("/app/api/_probe/delegate")
def _probe_delegate(p: Principal = Depends(delegate_gate)):
    return {"telegram_id": p.telegram_id}


@_probe.post("/app/api/_probe/upload")
def _probe_upload(a: UploadActor = Depends(upload_actor)):
    return {"telegram_id": a.telegram_id, "is_staff_upload": a.is_staff_upload}


@_probe.get("/app/api/_probe/cap")
def _probe_cap(p: Principal = Depends(require_cap("moderate_game"))):
    return {"ok": True}


@_probe.get("/app/api/_probe/section")
def _probe_section(p: Principal = Depends(require_section("coins"))):
    return {"ok": True}


@_probe.post("/app/api/_probe/mutate")
def _probe_mutate(p: Principal = Depends(delegate_gate)):
    return {"ok": True}


@_probe.post("/app/api/_probe/mutate_staff")
def _probe_mutate_staff(p: Principal = Depends(require_cap("moderate_game"))):
    return {"ok": True}


def _probe_client(cfg: DashboardConfig) -> TestClient:
    app = create_app(cfg=cfg)
    app.fastapi_app.include_router(_probe)
    return TestClient(app, base_url="https://testserver")


# ── открытые маршруты / тумблер ──────────────────────────────────────────────────────────

def test_health_open_even_when_disabled(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    assert client.get("/app/health").json() == {"status": "ok"}
    assert client.get("/app/health").status_code == 200


def test_disabled_by_default_returns_503(tmp_path):
    """Дефолт реестра miniapp_enabled = off — новая поверхность включается осознанно."""
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/me", headers=_hdr(ADMIN_ID))
    assert resp.status_code == 503
    assert resp.json() == {"reason": "miniapp_off"}


def test_trailing_slash_redirects_to_shell(tmp_path):
    """Находка 3 приёмки 19-10: `/app/` отдавал JSON 404. Слэш-вариант — 308 на `/app`
    с сохранением query; при выключенном тумблере middleware по-прежнему рисует человеку
    страницу-объяснение (оба пути в SHELL_PATHS), а не редирект в никуда."""
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _client(_cfg(db_path))
    resp = client.get("/app/", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/app"
    resp = client.get("/app/?tgWebAppStartParam=x", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/app?tgWebAppStartParam=x"
    assert client.get("/app/").status_code == 200  # по редиректу — сама оболочка
    _set("miniapp_enabled", "off")
    resp = client.get("/app/", follow_redirects=False)
    assert resp.status_code == 503
    assert "text/html" in resp.headers["content-type"]


def test_toggle_off_between_requests_without_restart(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _client(_cfg(db_path))
    assert client.get("/app/api/me", headers=_hdr(ADMIN_ID)).status_code == 200
    _set("miniapp_enabled", "off")
    assert client.get("/app/api/me", headers=_hdr(ADMIN_ID)).status_code == 503
    assert client.get("/app/health").status_code == 200


def test_openapi_and_docs_disabled(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _client(_cfg(db_path))
    for path in ("/docs", "/redoc", "/openapi.json", "/app/docs", "/app/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_unknown_route_is_json_reason(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/nope")
    assert resp.status_code == 404
    assert resp.json() == {"reason": "not_found"}


# ── ветка initData ───────────────────────────────────────────────────────────────────────

def test_me_without_auth_401_no_auth(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    resp = _client(_cfg(db_path)).get("/app/api/me")
    assert resp.status_code == 401
    assert resp.json() == {"reason": "no_auth"}


def test_me_with_tampered_initdata_401_bad_initdata(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    bad = make_init_data(user_id=DELEGATE_ID).replace(str(DELEGATE_ID), str(ADMIN_ID))
    resp = _client(_cfg(db_path)).get("/app/api/me", headers={"X-Telegram-Init-Data": bad})
    assert resp.status_code == 401
    assert resp.json() == {"reason": "bad_initdata"}


def test_me_with_expired_initdata_401_bad_initdata(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    resp = _client(_cfg(db_path)).get(
        "/app/api/me", headers=_hdr(DELEGATE_ID, auth_date=int(time.time()) - 90000)
    )
    assert resp.status_code == 401
    assert resp.json() == {"reason": "bad_initdata"}


def test_me_delegate_via_initdata(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    resp = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["telegram_id"] == DELEGATE_ID
    assert body["via"] == "initdata"
    assert body["caps"] == [] and body["is_staff"] is False
    assert body["is_delegate"] is True
    assert body["city"] is None
    assert set(body["sections"]) == {
        "tasks", "coins", "leaderboard", "profile", "form", "review",
        # Phase 23-01 (APP-TINDER-01, D-09): раздел «🗂 Отбор заявок».
        "applications",
        "admin_tasks", "stats",
        "settings",
    }
    assert all(body["sections"].values())
    assert body["accent"] == "#037EF3"
    assert body["event_name"] == "форума YouLead"
    assert body["logo_file_id"] is None
    assert body["bot_username"] == "YouLead_test_bot"


def test_me_superadmin_has_all_caps(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    body = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(ADMIN_ID)).json()
    assert "settings" in body["caps"] and "moderate_game" in body["caps"]
    assert body["is_staff"] is True
    assert body["is_delegate"] is False  # без анкеты делегатом не считается


def test_me_bound_manager_city_and_sections_reflect_registry(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    _set("miniapp_section_leaderboard", "off")
    _set("miniapp_accent", "#FF5733")
    body = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["city"] == "spb"
    assert body["caps"] == ["moderate_receipts", "moderate_reg"]
    assert body["sections"]["leaderboard"] is False
    assert body["accent"] == "#FF5733"


def test_me_reports_theme_preset_playful_tone_and_asset_slots(tmp_path):
    """Phase 19.1-02 (D-03/D-04/D-08): новые поля /app/api/me — существующие (accent/
    event_name/logo_file_id/...) НЕ переименованы, это чистое расширение контракта."""
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    body = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(ADMIN_ID)).json()
    assert body["theme_preset"] == "bluebook"
    assert body["playful_tone"] is False
    assert body["cover_file_id"] is None
    assert body["cover_dark_file_id"] is None
    assert body["logo_dark_file_id"] is None
    assert body["sticker_empty_file_id"] is None
    assert body["sticker_success_file_id"] is None
    assert body["sticker_error_file_id"] is None
    assert body["sticker_top1_file_id"] is None
    assert body["coin_icon_file_id"] is None
    assert body["plate_pattern_file_id"] is None
    # старые поля не тронуты
    assert body["accent"] == "#037EF3"
    assert body["logo_file_id"] is None
    # Phase 23.1-03 (UI-REDESIGN-03): герой и шаги привет-экрана — реестр, дефолты нетронутого стенда
    assert body["onboarding_hero"] == "Привет!"
    assert body["onboarding_steps_title"] == "Как это работает"
    assert body["onboarding_steps"].count(";") == 2


def test_me_reflects_youlead_preset_and_cover_asset(tmp_path):
    # D-03: пресет заполняет ВСЕ ручки при выборе (сам процесс выбора — план 19.1-07);
    # здесь миксуем ручки как это сделает та запись — preset + собственные значения ручек.
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    _set("miniapp_theme_preset", "youlead")
    _set("miniapp_theme_playful_tone", "on")
    _set("miniapp_cover", "AgACAgIAAxkBAAIcoverAssetX001")
    body = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(ADMIN_ID)).json()
    assert body["theme_preset"] == "youlead"
    assert body["playful_tone"] is True
    assert body["cover_file_id"] == "AgACAgIAAxkBAAIcoverAssetX001"


# ── плитка «Дашборд» хаба менеджера (quick 260903): адрес — cfg.public_url (деплой,
# D-05/D-19 — никогда не bot_settings-ключ), подпись плитки — реестр ──────────────────────

def test_me_staff_gets_dashboard_url_from_deploy_config(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    body = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["dashboard_url"] == "https://yl26.example.com"
    assert body["dashboard_tile_label"] == "📊 Дашборд"


def test_me_delegate_never_gets_dashboard_url(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    body = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()
    assert body["dashboard_url"] is None


def test_me_dashboard_url_null_when_deploy_config_empty(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    cfg = _cfg(db_path, public_url="")
    body = _client(cfg).get("/app/api/me", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["dashboard_url"] is None


def test_me_dashboard_tile_label_reflects_registry_override(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    _set("miniapp_tile_dashboard_label", "📈 Статистика для всех")
    body = _client(_cfg(db_path)).get("/app/api/me", headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["dashboard_tile_label"] == "📈 Статистика для всех"


# ── ветка cookie (D-05) ──────────────────────────────────────────────────────────────────

def test_cookie_from_dashboard_admits_manager(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    cfg = _cfg(db_path)
    body = _cookie_client(cfg, GAME_MANAGER_ID).get("/app/api/me").json()
    assert body["via"] == "cookie"
    assert body["telegram_id"] == GAME_MANAGER_ID
    assert body["caps"] == ["moderate_game"]
    assert body["is_delegate"] is False


def test_cookie_delegate_without_caps_403_staff_only(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    resp = _cookie_client(_cfg(db_path), DELEGATE_ID).get("/app/api/me")
    assert resp.status_code == 403
    assert resp.json() == {"reason": "staff_only"}


def test_cookie_with_other_secret_is_ignored(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    cookie = _dashboard_cookie(_cfg(db_path, session_secret="another-secret"), GAME_MANAGER_ID)
    client = _client(_cfg(db_path))
    client.cookies.set("yl_dash", cookie)
    resp = client.get("/app/api/me")
    assert resp.status_code == 401
    assert resp.json() == {"reason": "no_auth"}


def test_initdata_wins_over_cookie(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _cookie_client(_cfg(db_path), GAME_MANAGER_ID)
    body = client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()
    assert body["via"] == "initdata" and body["telegram_id"] == DELEGATE_ID


def test_cookie_manager_on_delegate_gate_403_kind_cookie(tmp_path):
    """D-05: делегатские экраны по cookie недоступны в принципе — даже staff."""
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    _seed(users=[(GAME_MANAGER_ID, "approved")])  # одновременно одобренный делегат
    cfg = _cfg(db_path)
    client = _probe_client(cfg)
    client.cookies.set("yl_dash", _dashboard_cookie(cfg, GAME_MANAGER_ID))
    resp = client.get("/app/api/_probe/delegate")
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": "cookie"}
    # …а по initData тот же человек проходит.
    assert client.get("/app/api/_probe/delegate", headers=_hdr(GAME_MANAGER_ID)).status_code == 200


def test_cookie_mutation_without_xrw_403_csrf(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    cfg = _cfg(db_path)
    client = _probe_client(cfg)
    client.cookies.set("yl_dash", _dashboard_cookie(cfg, GAME_MANAGER_ID))
    resp = client.post("/app/api/_probe/mutate_staff")
    assert resp.status_code == 403
    assert resp.json() == {"reason": "csrf"}
    ok = client.post("/app/api/_probe/mutate_staff", headers={"X-Requested-With": "fetch"})
    assert ok.status_code == 200


def test_initdata_mutation_needs_no_xrw(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _probe_client(_cfg(db_path))
    assert client.post("/app/api/_probe/mutate", headers=_hdr(DELEGATE_ID)).status_code == 200


# ── права на каждый запрос (D-09) ────────────────────────────────────────────────────────

def test_cap_revoked_in_bot_settings_takes_effect_next_request(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    cfg = _cfg(db_path)
    client = _probe_client(cfg)
    assert client.get("/app/api/_probe/cap", headers=_hdr(GAME_MANAGER_ID)).status_code == 200
    _set("role_game_manager_enabled", "off")
    resp = client.get("/app/api/_probe/cap", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "no_cap", "cap": "moderate_game"}
    # по cookie — то же: теперь это 403 staff_only, без перелогина
    cookie_client = _probe_client(cfg)
    cookie_client.cookies.set("yl_dash", _dashboard_cookie(cfg, GAME_MANAGER_ID))
    resp = cookie_client.get("/app/api/me")
    assert resp.status_code == 403 and resp.json() == {"reason": "staff_only"}


def test_city_binding_changes_take_effect_next_request(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _client(_cfg(db_path))
    assert client.get("/app/api/me", headers=_hdr(BOUND_MANAGER_ID)).json()["city"] == "spb"

    async def _rebind():
        async with bot_db._connect() as conn:
            await conn.execute("UPDATE staff SET city = ? WHERE telegram_id = ?", ("msk", BOUND_MANAGER_ID))
            await conn.commit()

    asyncio.run(_rebind())
    assert client.get("/app/api/me", headers=_hdr(BOUND_MANAGER_ID)).json()["city"] == "msk"


# ── require_section / delegate_gate / upload_actor ───────────────────────────────────────

def test_require_section_off_403(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _probe_client(_cfg(db_path))
    assert client.get("/app/api/_probe/section", headers=_hdr(DELEGATE_ID)).status_code == 200
    _set("miniapp_section_coins", "off")
    resp = client.get("/app/api/_probe/section", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "coins"}


def test_delegate_gate_pending_rejected_unregistered(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _probe_client(_cfg(db_path))
    assert client.get("/app/api/_probe/delegate", headers=_hdr(DELEGATE_ID)).status_code == 200
    for uid, kind in ((PENDING_ID, "pending"), (REJECTED_ID, "rejected"), (UNREGISTERED_ID, "unregistered")):
        resp = client.get("/app/api/_probe/delegate", headers=_hdr(uid))
        assert resp.status_code == 403, uid
        assert resp.json() == {"reason": "delegate_gate", "kind": kind}


def test_staff_only_mode_blocks_delegate_without_caps(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    _set("miniapp_staff_only", "on")
    client = _probe_client(_cfg(db_path))
    resp = client.get("/app/api/_probe/delegate", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": "staff_only_mode"}
    assert client.get("/app/api/me", headers=_hdr(DELEGATE_ID)).json()["is_delegate"] is False
    # менеджер-делегат в этом режиме проходит
    _seed(users=[(GAME_MANAGER_ID, "approved")])
    assert client.get("/app/api/_probe/delegate", headers=_hdr(GAME_MANAGER_ID)).status_code == 200


def test_upload_actor_admits_delegate_and_staff_with_flag(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    cfg = _cfg(db_path)
    client = _probe_client(cfg)
    d = client.post("/app/api/_probe/upload", headers=_hdr(DELEGATE_ID))
    assert d.status_code == 200 and d.json() == {"telegram_id": DELEGATE_ID, "is_staff_upload": False}
    s = client.post("/app/api/_probe/upload", headers=_hdr(GAME_MANAGER_ID))
    assert s.status_code == 200 and s.json() == {"telegram_id": GAME_MANAGER_ID, "is_staff_upload": True}
    # менеджер по cookie (обложка задания из браузера) — staff-загрузка с CSRF-заголовком
    cookie_client = _probe_client(cfg)
    cookie_client.cookies.set("yl_dash", _dashboard_cookie(cfg, GAME_MANAGER_ID))
    c = cookie_client.post("/app/api/_probe/upload", headers={"X-Requested-With": "fetch"})
    assert c.status_code == 200 and c.json()["is_staff_upload"] is True
    # ни делегат, ни moderate_game — отказ гейта
    p = client.post("/app/api/_probe/upload", headers=_hdr(PENDING_ID))
    assert p.status_code == 403 and p.json() == {"reason": "delegate_gate", "kind": "pending"}
    r = client.post("/app/api/_probe/upload", headers=_hdr(BOUND_MANAGER_ID))
    assert r.status_code == 403 and r.json()["kind"] == "unregistered"


def test_static_and_shell_send_no_cache_header(tmp_path):
    """Вебвью Telegram без Cache-Control кэширует JS-модули эвристикой (у статики только
    ETag/Last-Modified) — после деплоя клиент неделями исполняет старый app.js (находка живой
    приёмки 19-10). Контракт: статика оболочки и сам /app идут с `Cache-Control: no-cache`
    (ревалидация: 304 без изменений, свежий файл после деплоя)."""
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    _set("miniapp_enabled", "on")
    client = _client(_cfg(db_path))
    resp = client.get("/app/static/js/app.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"
    shell = client.get("/app")
    assert shell.headers.get("cache-control") == "no-cache"
    # Кэш-бастинг: оболочка ссылается на версионированный префикс, и он реально смонтирован —
    # иначе вебвью продолжит исполнять старый app.js после деплоя (находка 19-10).
    m = re.search(r"(/app/static/v\d+)/js/app\.js", shell.text)
    assert m, "app.html обязан ссылаться на /app/static/v<версия>/js/app.js"
    versioned = client.get(f"{m.group(1)}/js/app.js")
    assert versioned.status_code == 200
    assert versioned.headers.get("cache-control") == "no-cache"
    assert client.get("/app/api/me").headers.get("cache-control") != "no-cache"


# ── Phase 23.1-03 (UI-REDESIGN-02): GET /app/api/hub — тексты и факты плиты хаба ────────

def test_hub_returns_registry_defaults_for_untouched_stand(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    body = _client(_cfg(db_path)).get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["balance_eyebrow"] == "Твой баланс"
    assert body["balance_unit"] == "монет"
    assert body["next_eyebrow"] == "Следующее"
    assert body["sections_eyebrow"] == "Разделы"
    assert body["event_dates"] is None
    assert body["event_place"] is None
    assert body["days_fact"] is None  # дата отсчёта не задана


def test_hub_tasks_fact_has_real_numbers_not_a_template(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    body = _client(_cfg(db_path)).get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["tasks_fact"] is not None
    assert "{done}" not in body["tasks_fact"] and "{total}" not in body["tasks_fact"]
    assert body["tasks_fact"] == "0 из 0 заданий сдано"  # заданий на стенде ещё нет


def test_hub_days_fact_future_date_gives_number_past_date_gives_none(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    client = _client(_cfg(db_path))
    future = (datetime.now() + timedelta(days=5)).strftime("%d.%m.%Y")
    _set("miniapp_hub_countdown_date", future)
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["days_fact"] is not None and "{days}" not in body["days_fact"]
    past = (datetime.now() - timedelta(days=5)).strftime("%d.%m.%Y")
    _set("miniapp_hub_countdown_date", past)
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["days_fact"] is None


def test_hub_denied_for_non_delegate_403(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _standard_seed()
    resp = _client(_cfg(db_path)).get("/app/api/hub", headers=_hdr(PENDING_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "delegate_gate"
