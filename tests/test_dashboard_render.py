"""Phase 15 Plan 05 (STAT-01/STAT-02): рендер шаблонов дашборда — CSS-токены (Task 2),
состав страницы и тумблеры блоков (Task 3).

Task 2 покрывает каркас (`tokens.css`/`app.css`/`base.html`/`login.html`/`no_access.html`):
только CSS-переменные вместо цветов, единственный внешний скрипт — Login Widget на странице
входа, `data-auth-url` собран из `DASHBOARD_PUBLIC_URL` (D-03: всегда `https://…`, а не из
адреса текущего запроса), текст «нет доступа» дословно по смыслу D-11.

Task 3 покрывает `dashboard.html`/`build_page_context`: семь блоков D-14, тумблеры D-19,
городской скоуп привязанного менеджера (D-10), отсутствие ПД на странице (D-17,
T-15-05-03), человеческие подписи «где бросают» (D-07).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

from dashboard.config import DashboardConfig
from dashboard.main import create_app

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
TOKENS_CSS = DASHBOARD_DIR / "static" / "tokens.css"
APP_CSS = DASHBOARD_DIR / "static" / "app.css"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"

BOT_TOKEN = "123456:ABCDEF-testtoken"
ADMIN_ID = 900001
STATS_MANAGER_ID = 900700
BOUND_MANAGER_ID = 900800

# Литеральный цвет — #rrggbb/#rgb или rgb(/rgba( — вне tokens.css это запрещено (D-15,
# «Дизайн серый и черновой», acceptance_criteria плана).
_HEX_OR_RGB_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")


def _use_tmp_db(tmp_path, name: str = "dashboard_render.db") -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(
    *, cities=None, settings=None, users=None, staff=None, reg_events=None,
    reg_started=None, game_tasks=None, game_submissions=None,
):
    async with bot_db._connect() as conn:
        for code, label, enabled, sort_order in cities or []:
            await conn.execute(
                "INSERT INTO cities (code, label, enabled, sort_order, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, label, enabled, sort_order, "2026-01-01 00:00:00"),
            )
        for key, value in (settings or {}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        for row in users or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        for telegram_id, role, city in staff or []:
            await conn.execute(
                "INSERT INTO staff (telegram_id, role, added_by, added_at, city) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, role, ADMIN_ID, "2026-01-01 00:00:00", city),
            )
        for row in reg_events or []:
            await conn.execute(
                "INSERT INTO reg_events (telegram_id, event, event_city, season, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                row,
            )
        for row in reg_started or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO reg_started ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        for row in game_tasks or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO game_tasks ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        for row in game_submissions or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO game_submissions ({cols}) VALUES ({placeholders})", tuple(row.values())
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


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


def _stats_manager_client(db_path: str, *, city=None, extra_settings=None) -> TestClient:
    """Логинит STATS_MANAGER_ID с правом `stats` (роль reg_manager + расширенный
    `role_caps_reg_manager`) — тот же приём, что в tests/test_dashboard_routes.py."""
    settings = {"role_caps_reg_manager": "moderate_reg;stats"}
    settings.update(extra_settings or {})
    _seed(staff=[(STATS_MANAGER_ID, "reg_manager", city)], settings=settings)
    client = _client(_cfg(db_path))
    _login_as(client, STATS_MANAGER_ID)
    return client


# ── tokens.css ────────────────────────────────────────────────────────────────────────────

def test_tokens_css_has_accent_variable():
    text = TOKENS_CSS.read_text(encoding="utf-8")
    assert text.count("--accent") >= 1


def test_tokens_css_only_defines_root_and_dark_scheme_selectors():
    text = TOKENS_CSS.read_text(encoding="utf-8")
    # Ни одного селектора компонента — только :root и @media (prefers-color-scheme: dark).
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("{"):
            assert stripped == ":root {" or stripped.startswith("@media"), (
                f"unexpected component selector in tokens.css: {stripped!r}"
            )


def test_no_hardcoded_colors_outside_tokens_css():
    for path in [APP_CSS, *TEMPLATES_DIR.glob("*.html")]:
        text = path.read_text(encoding="utf-8")
        matches = _HEX_OR_RGB_COLOR.findall(text)
        assert not matches, f"hardcoded color literal in {path.name}: {matches}"


# ── no external CDNs except Login Widget ────────────────────────────────────────────────

_ALLOWED_EXTERNAL_HOSTS = (
    "telegram.org/js/telegram-widget.js",  # Login Widget — единственный внешний скрипт
    # Шрифты бренда (Lato + Raleway) вшиты локально с Phase 19.1 (D-14) — fonts.googleapis.com
    # и fonts.gstatic.com больше не в allow-list.
)


def test_no_external_cdn_except_telegram_widget_on_login_page():
    for path in TEMPLATES_DIR.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        urls = re.findall(r"https?://[^\s\"'>]+", text)
        for url in urls:
            assert any(host in url for host in _ALLOWED_EXTERNAL_HOSTS), (
                f"unexpected external URL in {path.name}: {url}"
            )


# ── login page ───────────────────────────────────────────────────────────────────────────

def test_login_page_data_auth_url_built_from_public_url_https(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    cfg = _cfg(db_path, public_url="https://yl26.alekseev.info")
    client = _client(cfg)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'data-auth-url="https://yl26.alekseev.info/auth/callback"' in resp.text
    assert "http://" not in resp.text


def test_login_page_uses_tokens_and_no_hardcoded_style_attrs(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.get("/login")
    assert 'href="/static/tokens.css"' in resp.text
    assert 'href="/static/app.css"' in resp.text
    assert not _HEX_OR_RGB_COLOR.search(resp.text)


# ── no_access page (D-11) ────────────────────────────────────────────────────────────────

def test_no_access_html_contains_stats_capability_wording():
    text = (TEMPLATES_DIR / "no_access.html").read_text(encoding="utf-8")
    assert "📊 Статистика" in text
    assert "доступ" in text.lower()


class _FakeTelegramResponse:
    status_code = 200


class _FakeHTTPXClient:
    """Заглушка httpx.Client — без сети, не должна замедлять/ронять рендер-тесты."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return _FakeTelegramResponse()


def test_no_access_page_rendered_over_http_still_uses_https_assets(tmp_path, monkeypatch):
    monkeypatch.setattr("dashboard.notify._last_notified", {})
    monkeypatch.setattr("dashboard.notify.httpx.Client", _FakeHTTPXClient)
    db_path = _use_tmp_db(tmp_path)
    _, resp = _login_and_hit_root_without_stats(tmp_path, db_path)
    assert resp.status_code == 403
    assert "📊 Статистика" in resp.text
    assert not _HEX_OR_RGB_COLOR.search(resp.text)


def _login_and_hit_root_without_stats(tmp_path, db_path):
    cfg = _cfg(db_path)
    client = _client(cfg)
    _login_as(client, 900500)
    return client, client.get("/")


# ═══════════════════════════════════════════════════════════════════════════════════════
# Task 3: состав страницы, тумблеры блоков, городской скоуп, отсутствие ПД
# ═══════════════════════════════════════════════════════════════════════════════════════

_PII_VALUES = {
    "full_name": "Иванов Иван Иванович",
    "phone": "+79991234567",
    "email": "ivanov@example.com",
    "resume_url": "https://cloud.example.com/s/XYZSECRET",
}


def _seed_full_fixture(db_path, *, payment_enabled=True, event_city_enabled=True):
    _seed(
        cities=[("msk", "Москва", 1, 0), ("spb", "СПб", 1, 1)],
        settings={
            "event_name": "YouLead'26",
            "event_season": "YL26",
            "payment_enabled": "on" if payment_enabled else "off",
            "event_city_enabled": "on" if event_city_enabled else "off",
            "dashboard_block_game": "on",
        },
        users=[
            {
                "telegram_id": 1,
                "full_name": _PII_VALUES["full_name"],
                "phone": _PII_VALUES["phone"],
                "email": _PII_VALUES["email"],
                "resume_url": _PII_VALUES["resume_url"],
                "status": "approved", "payment_status": "paid", "payment_option": "Стандарт",
                "source": "ВК", "university": "НИУ ВШЭ", "course": "2 курс",
                "study_field": "Экономика", "participant_type": "full",
                "event_city": "msk", "season": "YL26",
                "registration_date": "2026-08-01 10:00:00",
            },
            {
                "telegram_id": 2,
                "full_name": "Петрова Мария Сергеевна",
                "phone": "+79997654321",
                "email": "petrova@example.com",
                "resume_url": "https://cloud.example.com/s/OTHERSECRET",
                "status": "pending", "payment_status": "not_paid", "payment_option": "Ранняя пташка",
                "source": "Реф. ссылка", "university": "СПбГУ", "course": "1 курс",
                "study_field": "IT", "participant_type": "short",
                "event_city": "spb", "season": "YL26",
                "registration_date": "2026-08-02 11:00:00",
            },
        ],
        reg_events=[
            (1, "start", "msk", "YL26", "2026-08-01 09:00:00"),
            (1, "form_started", "msk", "YL26", "2026-08-01 09:05:00"),
            (1, "form_completed", "msk", "YL26", "2026-08-01 09:10:00"),
            (2, "start", "spb", "YL26", "2026-08-02 10:00:00"),
        ],
        reg_started=[
            {
                "telegram_id": 3, "username": "c", "started_at": "2026-08-01 12:00:00",
                "last_step": "university",
            },
        ],
        game_tasks=[
            {
                "text": "Пост в соцсетях", "category": "Соцсети", "coins": 10,
                "proof_type": "photo", "deadline_at": "2026-09-01 00:00:00",
                "created_by": ADMIN_ID, "created_at": "2026-08-01 00:00:00",
            },
        ],
        game_submissions=[
            {
                "task_id": 1, "user_id": 1, "content_type": "photo", "content": "file_id",
                "submitted_at": "2026-08-01 13:00:00", "status": "approved",
            },
        ],
    )


def test_all_seven_blocks_present_when_toggles_on(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.text
    assert "<h1>YouLead" in text
    assert 'class="kpi-grid"' in text
    assert "Воронка регистрации" in text
    assert "Динамика регистраций" in text
    assert "Разрезы" in text
    assert "Где бросают" in text
    assert "Геймификация" in text


def test_daily_chart_data_attributes_are_parseable_json(tmp_path):
    """tojson не экранирует двойные кавычки — в атрибуте с `"` строки-даты рвали JSON
    и график молча не рисовался. Атрибуты в одинарных кавычках, внутри — валидный JSON."""
    import json as _json
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path)
    text = client.get("/").text
    m = re.search(r"data-labels='([^']*)'\s+data-values='([^']*)'", text)
    assert m, "canvas data-labels/data-values not found in single-quoted form"
    labels = _json.loads(m.group(1))
    values = _json.loads(m.group(2))
    assert labels and len(labels) == len(values)
    assert labels[0] == "2026-08-01"


@pytest.mark.parametrize(
    "toggle_key, marker",
    [
        ("dashboard_block_funnel", "Воронка регистрации"),
        ("dashboard_block_dynamics", "Динамика регистраций"),
        ("dashboard_block_sources", '<h3 class="cut-title">Источник</h3>'),
        ("dashboard_block_universities", '<h3 class="cut-title">ВУЗ</h3>'),
        ("dashboard_block_courses", '<h3 class="cut-title">Курс</h3>'),
        ("dashboard_block_study_fields", '<h3 class="cut-title">Направление обучения</h3>'),
        ("dashboard_block_dropout", "Где бросают"),
        ("dashboard_block_game", "Геймификация"),
    ],
)
def test_disabled_toggle_removes_block_entirely(tmp_path, toggle_key, marker):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path, extra_settings={toggle_key: "off"})
    resp = client.get("/")
    assert resp.status_code == 200
    assert marker not in resp.text


def test_payment_disabled_removes_payment_stage_and_tariff_cut(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path, payment_enabled=False)
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert "Оплатили" not in resp.text
    assert "Тариф" not in resp.text


def test_payment_enabled_shows_payment_stage_and_tariff_cut(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path, payment_enabled=True)
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert "Оплатили" in resp.text
    assert "Тариф" in resp.text


def test_empty_game_and_disabled_toggle_both_hide_game_block(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    # Тумблер включён, но game_submissions пуст — блока быть не должно (D-12).
    _seed(settings={"dashboard_block_game": "on"})
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert "Геймификация" not in resp.text

    # Тумблер выключен, даже если данные появятся — тоже не должно быть блока.
    _seed_full_fixture(db_path)
    _seed(settings={"dashboard_block_game": "off"})
    resp2 = client.get("/")
    assert "Геймификация" not in resp2.text


def test_bound_manager_has_no_city_switcher_and_no_foreign_city_data(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path, city="msk")
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="switchers"' not in resp.text or "Все города" not in resp.text
    assert "СПб" not in resp.text  # чужой город не виден вообще


def test_unbound_viewer_sees_all_cities_switcher(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert "Все города" in resp.text
    assert "Москва" in resp.text
    assert "СПб" in resp.text


def test_past_season_query_changes_kpi_numbers(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    _seed(users=[
        {
            "telegram_id": 4, "full_name": "Старый Сезон", "status": "approved",
            "season": "RusCo25", "registration_date": "2025-01-01 10:00:00",
        },
    ])
    client = _stats_manager_client(db_path)
    current = client.get("/")
    past = client.get("/", params={"season": "RusCo25"})
    assert current.status_code == past.status_code == 200
    assert current.text != past.text


def test_unknown_city_query_falls_back_without_crash(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path)
    resp = client.get("/", params={"city": "not-a-real-city"})
    assert resp.status_code == 200


def test_no_pii_values_anywhere_in_rendered_html(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    text = resp.text
    for field, value in _PII_VALUES.items():
        assert value not in text, f"PII leak ({field}): {value!r} found in rendered HTML"
    assert "Петрова Мария Сергеевна" not in text
    assert "+79997654321" not in text
    assert "petrova@example.com" not in text


def test_dropout_step_labels_are_human_not_raw_codes(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed_full_fixture(db_path)
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert "ВУЗ" in resp.text
    assert ">university<" not in resp.text


def test_conversion_shows_dash_until_events_tracked(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(settings={"role_caps_reg_manager": "moderate_reg;stats"})
    client = _stats_manager_client(db_path)
    resp = client.get("/")
    assert "—" in resp.text
