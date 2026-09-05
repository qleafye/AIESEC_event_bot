"""Фаза 26, план 01 (RT-01/RT-02): ассеты оформления дашборда на хосте без Mini App рядом.

Сегодня `/theme.css` дашборда отдаёт `--plate-pattern: url("/app/static/pattern/...")` —
адреса, которых на хосте дашборда (rt26., su26.) физически нет: маршрута `/app` там нет
вовсе (сторож `test_no_app_route_and_no_export_or_csv_route`). Этот файл проверяет, что
дашборд отдаёт СВОИ пути (`asset_base=""`) и умеет их обслужить сам: растр орнамента —
статикой, картинку file_id — собственным прокси `GET /api/file/{file_id}` (Задача 3).

Харнесс скопирован из `tests/test_dashboard_routes.py` (`_use_tmp_db`/`_seed`/`_cfg`/
`_client`); пресет пишется тем же приёмом, что `tools/_shoot_common.py::write_preset` —
все ручки пресета разом, плюс ключ самого пресета.
"""
from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

import web_theme
from dashboard.config import DashboardConfig
from dashboard.main import create_app

BOT_TOKEN = "123456:ABCDEF-testtoken"
ADMIN_ID = 900001


def _use_tmp_db(tmp_path, name: str = "dashboard_event_assets.db") -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(*, settings=None, users=None):
    async with bot_db._connect() as conn:
        for key, value in (settings or {}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        for telegram_id, receipt_file_id, resume_file_id in (users or []):
            await conn.execute(
                "INSERT INTO users (telegram_id, receipt_file_id, resume_file_id) VALUES (?, ?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET receipt_file_id = excluded.receipt_file_id, "
                "resume_file_id = excluded.resume_file_id",
                (telegram_id, receipt_file_id, resume_file_id),
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


def _write_preset(preset_name: str) -> dict:
    """Тот же приём, что `tools/_shoot_common.py::write_preset` — пишет ВСЕ ручки пресета
    разом плюс ключ самого пресета. Возвращает словарь `{ключ реестра: значение}` для
    передачи в `_seed(settings=...)`."""
    settings = {web_theme.THEME_KEYS[handle]: value for handle, value in web_theme.PRESETS[preset_name].items()}
    settings[web_theme.THEME_KEYS["preset"]] = preset_name
    return settings


def _cfg(db_path: str, **overrides) -> DashboardConfig:
    base = dict(
        db_path=db_path,
        public_url="https://rt26.example.com",
        session_secret="test-session-secret",
        bot_username="realtalk_forum_bot",
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


# ── Задача 2: /theme.css со своими путями + раздача растра орнамента ────────────────────

def test_theme_css_uses_dashboard_paths_not_miniapp(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(settings=_write_preset("realtalk"))
    client = _client(_cfg(db_path))

    resp = client.get("/theme.css")
    assert resp.status_code == 200
    assert 'url("/static/pattern/realtalk.webp")' in resp.text
    assert "/app/" not in resp.text


def test_pattern_raster_served_by_dashboard(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))

    resp = client.get("/static/pattern/realtalk.webp")
    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "image/webp"
    assert len(resp.content) > 0


def test_static_mount_still_serves_app_css(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))

    resp = client.get("/static/app.css")
    assert resp.status_code == 200


def test_theme_css_pattern_off_has_no_url(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    settings = _write_preset("realtalk")
    settings[web_theme.THEME_KEYS["pattern_enabled"]] = "off"
    _seed(settings=settings)
    client = _client(_cfg(db_path))

    resp = client.get("/theme.css")
    assert resp.status_code == 200
    assert "--plate-pattern: none;" in resp.text
