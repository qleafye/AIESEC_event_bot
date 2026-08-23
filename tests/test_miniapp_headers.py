"""Phase 19 Plan 01 Task 2 (D-09, T-19-05/T-19-06/T-19-72): security-заголовки Mini App и
сторожа.

- `/app*` несёт `Content-Security-Policy` с `frame-ancestors …telegram.org` и НЕ несёт
  `X-Frame-Options` (Telegram Web встраивает Mini App в iframe — RESEARCH Pitfall 5);
  встречный регресс — дашборд по-прежнему отдаёт `X-Frame-Options: DENY`.
- aiogram-free сторож: `import miniapp.main` в чистом подпроцессе не загружает `aiogram`
  (грепа импортов недостаточно — aiogram приезжает транзитивно через пакетный
  `handlers/__init__.py`, стоит кому-то написать `from handlers.game_labels import …`).
- Сторож на отсутствие слова-триггера кэша прав в `miniapp/deps.py` — по образцу
  `tests/test_dashboard_auth.py`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from starlette.testclient import TestClient

from dashboard.main import create_app as create_dashboard_app
from miniapp.main import CONTENT_SECURITY_POLICY, create_app

from tests.test_miniapp_routes import ADMIN_ID, _cfg, _hdr, _standard_seed, _use_tmp_db

ROOT = Path(__file__).resolve().parent.parent
DEPS_FILE = ROOT / "miniapp" / "deps.py"
ACCESS_FILE = ROOT / "dashboard" / "access.py"


def _miniapp_client(db_path: str) -> TestClient:
    return TestClient(create_app(cfg=_cfg(db_path)), base_url="https://testserver")


# ── CSP / XFO ────────────────────────────────────────────────────────────────────────────

def test_me_has_csp_frame_ancestors_and_no_xfo(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_headers.db")
    _standard_seed()
    resp = _miniapp_client(db_path).get("/app/api/me", headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-ancestors https://web.telegram.org https://*.telegram.org" in csp
    assert "script-src 'self' https://telegram.org" in csp
    assert "X-Frame-Options" not in resp.headers
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_headers_present_on_error_and_health_responses(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_headers2.db")
    client = _miniapp_client(db_path)
    for resp in (client.get("/app/health"), client.get("/app/api/me"), client.get("/app/api/me", headers=_hdr(ADMIN_ID))):
        assert resp.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert "X-Frame-Options" not in resp.headers


def test_dashboard_still_sends_xfo_deny(tmp_path):
    db_path = _use_tmp_db(tmp_path, "dashboard_xfo.db")
    dash = TestClient(create_dashboard_app(cfg=_cfg(db_path)), base_url="https://testserver")
    resp = dash.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" not in resp.headers


# ── сторожа ──────────────────────────────────────────────────────────────────────────────

def test_import_miniapp_main_does_not_load_aiogram():
    code = (
        "import sys\n"
        "import miniapp.main\n"
        "bad = sorted(m for m in sys.modules if m == 'aiogram' or m.startswith('aiogram.') "
        "or m == 'handlers' or m.startswith('handlers.'))\n"
        "print('OK' if not bad else 'LOADED:' + ','.join(bad))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "OK", proc.stdout + proc.stderr


def test_deps_module_has_no_cache_or_proxy_headers():
    text = DEPS_FILE.read_text(encoding="utf-8")
    assert "lru_cache" not in text
    assert "functools.cache" not in text
    assert "CF-Connecting-IP" not in text
    assert "X-Forwarded" not in text
    # модель прав переиспользуется, а не копируется
    assert "from dashboard.access import resolve_capabilities, staff_city" in text


def test_miniapp_does_not_import_handlers_statically():
    for path in (ROOT / "miniapp").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith(("from handlers", "import handlers", "import aiogram", "from aiogram")), (
                f"{path.name}: {stripped}"
            )
