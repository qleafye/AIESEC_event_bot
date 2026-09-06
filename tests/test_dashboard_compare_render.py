"""Phase 26.1 Plan 02 Task 4 (SD-03/SD-04/SD-05/SD-06): динамика несколькими сериями, разрезы
и сторожа экрана `/compare` — рендер по фикстуре из двух ЗАМЕТНО разных баз (числа, даты
старта, включённые блоки, пресет оформления).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

from dashboard import cf_access, compare
from dashboard.cf_access import ACCESS_HEADER
from dashboard.config import DashboardConfig
from dashboard.main import create_app
from dashboard.registry import EventSource

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
COMPARE_HTML = DASHBOARD_DIR / "templates" / "compare.html"

TEAM_DOMAIN = "aiesec"
AUD = "app-aud-tag"
ISSUER = f"https://{TEAM_DOMAIN}.cloudflareaccess.com"
SUPERADMIN_EMAIL = "admin@aiesec.ru"

_HEX_OR_RGB_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")

_PII_VALUES = {
    "full_name": "Иванов Иван Иванович",
    "phone": "+79991234567",
    "email": "ivanov@example.com",
    "resume_url": "https://cloud.example.com/s/XYZSECRET",
}


# ── фикстуры баз событий ─────────────────────────────────────────────────────────────────

def _use_tmp_db(tmp_path, name: str) -> str:
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
        for row in users or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(row.values()))
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


def _make_event_db(tmp_path, name, **seed_kwargs) -> str:
    path = _use_tmp_db(tmp_path, name)
    if seed_kwargs:
        _seed(**seed_kwargs)
    return path


def _users_row(tid, *, registration_date, status="approved", **overrides):
    row = {"telegram_id": tid, "registration_date": registration_date, "status": status}
    row.update(overrides)
    return row


def _cfg(events) -> DashboardConfig:
    return DashboardConfig(
        db_path="data/forum.db",
        public_url="https://stats.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token="123:abc",
        admin_ids=(1,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
        events=events,
        access_team_domain=TEAM_DOMAIN,
        access_aud=AUD,
        superadmin_emails=(SUPERADMIN_EMAIL,),
        access_dev_bypass=False,
    )


def _client(cfg: DashboardConfig, **kwargs) -> TestClient:
    app = create_app(cfg=cfg)
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(app, **kwargs)


def _build_two_event_fixture(tmp_path):
    """A («Юлид'26») — заметно больше заявок, оплата включена, дефолтный пресет (accent
    #037EF3), ранний старт регистрации. B («РилТолк Форум») — меньше заявок, оплата
    выключена (дефолт), разрез источников выключен явно, пресет realtalk (#7552CC), старт
    регистрации позже."""
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_name": "Юлид'26", "event_season": "YL26", "payment_enabled": "on"},
        users=[
            _users_row(
                1, registration_date="2026-08-01 10:00:00", payment_status="paid",
                source="ВК", **_PII_VALUES,
            ),
            _users_row(2, registration_date="2026-08-02 10:00:00", payment_status="paid", source="ВК"),
            _users_row(3, registration_date="2026-08-03 10:00:00", payment_status="not_paid", source="Реф. ссылка"),
            _users_row(4, registration_date="2026-08-04 10:00:00", payment_status="not_paid", source="ВК"),
            _users_row(5, registration_date="2026-08-05 10:00:00", status="pending", source="Реф. ссылка"),
        ],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={
            "event_name": "РилТолк Форум",
            "event_season": "RT26",
            "dashboard_block_sources": "off",
            "miniapp_theme_preset": "realtalk",
        },
        users=[
            _users_row(1, registration_date="2026-08-20 10:00:00"),
        ],
    )
    events = (EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b))
    return _cfg(events)


# ── фикстурный токен Access ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_cache_and_jwks():
    compare.reset_cache()
    cf_access.reset_jwks_cache()
    yield
    compare.reset_cache()
    cf_access.reset_jwks_cache()


def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _install_fixture_jwks(monkeypatch, public_key):
    class _FixtureJWKClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=public_key)

    monkeypatch.setattr(cf_access.jwt, "PyJWKClient", _FixtureJWKClient)


def _access_token(private_key, *, email=SUPERADMIN_EMAIL) -> str:
    now = int(time.time())
    payload = {"email": email, "aud": AUD, "iss": ISSUER, "iat": now - 10, "exp": now + 3600}
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-kid"})


def _get_compare(tmp_path, monkeypatch, cfg, **params):
    # reset_jwks_cache() ОБЯЗАТЕЛЕН перед каждой новой парой ключей: синглтон JWKS-клиента
    # в dashboard.cf_access переживает между вызовами этой функции внутри одного теста —
    # без сброса второй вызов monkeypatch класса на новый ключ не подхватится, старый
    # инстанс синглтона так и будет отдавать ПЕРВЫЙ публичный ключ.
    cf_access.reset_jwks_cache()
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    client = _client(cfg)
    token = _access_token(private_key)
    return client.get("/compare", params=params, headers={ACCESS_HEADER: token})


# ── строка на каждое событие с числами своей базы ────────────────────────────────────────

def test_each_event_row_shows_its_own_numbers(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    resp = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp.status_code == 200
    text = resp.text
    assert "Юлид" in text
    assert "РилТолк" in text
    # Заявок: у "a" 5, у "b" 1 — заметно разные числа рядом в одной таблице.
    table_start = text.find("Событие рядом")
    scoped = text[table_start:]
    m_a = re.search(r"Юлид.*?data-label=\"Заявок\">\s*([0-9]+)", scoped, re.S)
    m_b = re.search(r"РилТолк.*?data-label=\"Заявок\">\s*([0-9]+)", scoped, re.S)
    assert m_a and m_a.group(1) == "5"
    assert m_b and m_b.group(1) == "1"


# ── «Оплатили»: «—» у события без оплаты, число (не 0) у первого ────────────────────────

def test_funnel_paid_stage_dash_for_event_without_payment_number_for_other(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    resp = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp.status_code == 200
    text = resp.text
    funnel_start = text.find("Воронка рядом")
    assert funnel_start != -1
    scoped = text[funnel_start:]
    row_match = re.search(r"<tr>\s*<td data-label=\"Ступень\">Оплатили</td>(.*?)</tr>", scoped, re.S)
    assert row_match, "строка «Оплатили» не найдена в воронке"
    row_html = row_match.group(1)
    cells = re.findall(r'<td class="num"[^>]*>(.*?)</td>', row_html, re.S)
    assert len(cells) == 2
    # Первая ячейка (событие a, оплата включена) — число (не 0, не прочерк), рядом с ним —
    # процент от базовой ступени (subtitle), а не сам прочерк «—».
    number_match = re.match(r"\s*(\d+)", cells[0])
    assert number_match and number_match.group(1) != "0"
    assert "—" not in cells[0]
    # Вторая ячейка (событие b, оплата выключена) — прочерк.
    assert "—" in cells[1]


# ── разрез источников: «—» с причиной у второго, значения у первого, объединение списка ──

def test_cuts_sources_dash_with_reason_for_disabled_event(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    resp = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp.status_code == 200
    text = resp.text
    sources_start = text.find("Источники")
    assert sources_start != -1
    scoped = text[sources_start:2000 + sources_start]
    assert "ВК" in scoped
    assert "Реф. ссылка" in scoped
    assert "у этого события отключён разрез по источникам" in scoped


# ── data-series: валидный JSON, ровно две серии, accent второй = #7552CC ────────────────

def test_data_series_is_valid_json_with_two_series_and_realtalk_accent(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    resp = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp.status_code == 200
    text = resp.text
    m = re.search(r"data-series='(\[.*?\])'", text, re.S)
    assert m, "атрибут data-series не найден"
    series = json.loads(m.group(1))
    assert len(series) == 2
    by_code = {s["code"]: s for s in series}
    assert by_code["b"]["accent"] == "#7552CC"
    assert by_code["a"]["accent"] == "#037EF3"

    m_labels = re.search(r"data-labels='(\[.*?\])'", text, re.S)
    assert m_labels, "атрибут data-labels не найден"
    labels = json.loads(m_labels.group(1))
    assert isinstance(labels, list) and labels


# ── ?axis=calendar меняет метки; при day_n метки целочисленные ───────────────────────────

def test_axis_calendar_changes_labels_day_n_labels_are_integers(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    resp_day_n = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp_day_n.status_code == 200
    labels_day_n = json.loads(re.search(r"data-labels='(\[.*?\])'", resp_day_n.text, re.S).group(1))
    assert all(isinstance(x, int) for x in labels_day_n)

    compare.reset_cache()
    resp_calendar = _get_compare(tmp_path, monkeypatch, cfg, axis="calendar")
    assert resp_calendar.status_code == 200
    labels_calendar = json.loads(re.search(r"data-labels='(\[.*?\])'", resp_calendar.text, re.S).group(1))
    assert all(isinstance(x, str) for x in labels_calendar)
    assert labels_calendar != labels_day_n


# ── недоступное событие -> 200 + предупреждение с именем (кодом) ────────────────────────

def test_unavailable_event_returns_200_with_warning(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    broken_events = cfg.events + (EventSource(code="c", db_path=str(tmp_path / "does-not-exist.db")),)
    cfg = _cfg(broken_events)
    resp = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp.status_code == 200
    assert "Недоступно" in resp.text
    assert "c" in resp.text  # код недоступного события — единственное доступное «имя»
    # Остальные события всё ещё показаны.
    assert "Юлид" in resp.text
    assert "РилТолк" in resp.text


# ── отсутствие ПД; e-mail зрителя только в подписи шапки ─────────────────────────────────

def test_no_pii_in_html_viewer_email_only_in_header(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    resp = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp.status_code == 200
    text = resp.text
    for value in _PII_VALUES.values():
        assert value not in text
    assert text.count(SUPERADMIN_EMAIL) == 1
    assert f"Вы вошли как {SUPERADMIN_EMAIL}" in text


# ── подписи кириллицей, латинских брендов в вёрстке нет ──────────────────────────────────

def test_cyrillic_labels_no_latin_brand_names_in_markup(tmp_path, monkeypatch):
    cfg = _build_two_event_fixture(tmp_path)
    resp = _get_compare(tmp_path, monkeypatch, cfg)
    assert resp.status_code == 200
    text = resp.text
    assert "Сводная статистика" in text
    assert "Событие" in text
    assert "Заявок" in text
    assert "YouLead" not in text
    assert "RealTalk" not in text


# ── повторная проверка: ни одного литерала цвета в исходнике compare.html ────────────────

def test_no_hardcoded_colors_in_compare_html_source():
    text = COMPARE_HTML.read_text(encoding="utf-8")
    matches = _HEX_OR_RGB_COLOR.findall(text)
    assert not matches, f"hardcoded color literal in compare.html: {matches}"
