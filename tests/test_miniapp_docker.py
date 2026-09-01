"""Статические проверки образа и sidecar'а Mini App (Phase 19, 19-09, WEBAPP-01, D-01/D-04/D-08).

Читаем файлы с диска — демон Docker не нужен. Compose парсится через yaml.safe_load,
а не регулярками, чтобы комментарии не давали ложных совпадений."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
REQS = ROOT / "requirements.txt"
COMPOSE = ROOT / "docker-compose.yml"
RUNBOOK = ROOT / "docs" / "DEPLOY-DOMAIN.md"

REQUIRED_PACKAGES = ["fastapi", "uvicorn", "jinja2", "python-multipart", "httpx", "itsdangerous"]


def _body(path: Path) -> list[str]:
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _miniapp() -> dict:
    return _compose()["services"]["miniapp"]


# --- requirements.txt (Task 1) ------------------------------------------------------------


@pytest.mark.parametrize("package", REQUIRED_PACKAGES)
def test_root_requirements_has_miniapp_package(package):
    body = _body(REQS)
    assert any(ln.startswith(package) for ln in body), f"{package} должен быть в requirements.txt"


def test_root_requirements_does_not_pin_starlette_above_1():
    body = _body(REQS)
    starlette_lines = [ln for ln in body if ln.lower().startswith("starlette")]
    assert not starlette_lines, "Starlette не должен явно подниматься до 1.x — @app.middleware() удалён в 1.0"


# --- docker-compose.yml::miniapp (Task 1) --------------------------------------------------


def test_compose_has_miniapp_service():
    services = _compose()["services"]
    assert "miniapp" in services
    assert services["miniapp"]["container_name"] == "youlead26-miniapp"


def test_compose_miniapp_builds_from_root_context():
    build = _miniapp()["build"]
    # build: . — короткая форма, PyYAML отдаёт строку, а не словарь с context/dockerfile
    assert build == "." or (isinstance(build, dict) and build.get("context") == ".")


def test_compose_miniapp_command_is_exec_form_uvicorn_on_8001():
    cmd = _miniapp()["command"]
    assert isinstance(cmd, list), "команда должна быть exec-формой (список), не строкой шелла"
    assert cmd[0] == "uvicorn"
    assert "miniapp.main:app" in cmd
    assert "8001" in cmd


def test_compose_miniapp_mounts_data_read_write():
    vols = _miniapp()["volumes"]
    assert "./data:/app/data" in vols
    assert not any(v == "./data:/app/data:ro" for v in vols), "том БД у miniapp должен быть на запись (второй писатель)"


def test_compose_dashboard_db_module_still_read_only():
    """Регрессия D-04/D-59: второй писатель не должен ослабить контракт дашборда. После снятия
    `:ro` с тома (WAL, quick 260902-38y) контракт живёт в `dashboard/db.py`: подключение
    только `mode=ro`, функций записи в модуле нет."""
    src = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")
    assert "?mode=ro" in src and "uri=True" in src
    assert "INSERT" not in src and "UPDATE" not in src and "DELETE" not in src


def test_compose_miniapp_in_edge_and_default_networks():
    nets = _miniapp()["networks"]
    assert "edge" in nets and "default" in nets


def test_compose_no_service_publishes_ports():
    for name, svc in _compose()["services"].items():
        assert "ports" not in svc, f"сервис {name} публикует порты — на сервере входящих портов нет (D-01, T-19-58)"


def test_compose_miniapp_healthcheck_overridden_to_app_health():
    hc = _miniapp().get("healthcheck")
    assert hc is not None, "healthcheck образа бота проверяет heartbeat, здесь нужен собственный"
    test = hc["test"]
    text = " ".join(test) if isinstance(test, list) else str(test)
    assert "/app/health" in text
    assert "8001" in text


def test_compose_miniapp_uses_stack_env_file():
    assert _miniapp()["env_file"] == [".env"]


def test_compose_miniapp_has_no_tz_override():
    """T-19-64: время miniapp должно совпадать с ботом, а не с локалью хоста запуска."""
    assert "TZ" not in _miniapp()
    env = _miniapp().get("environment") or {}
    if isinstance(env, list):
        assert not any(str(e).startswith("TZ=") for e in env)
    else:
        assert "TZ" not in env


# --- docs/DEPLOY-DOMAIN.md (Task 2) ---------------------------------------------------------


def _runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_documents_app_path_rule():
    text = _runbook_text()
    assert "^/app(/|$)" in text
    assert "miniapp:8001" in text


def test_runbook_states_path_rule_order():
    text = _runbook_text()
    assert "порядок между ними важен" in text or "стоять" in text and "выше" in text


def test_runbook_has_bot_fight_mode_checkpoint():
    text = _runbook_text()
    assert "Bot Fight Mode" in text
    assert "выключен" in text


def test_runbook_has_app_health_and_login_curl_checks():
    text = _runbook_text()
    assert "curl -sI https://yl26.<домен>/app/health" in text
    assert "curl -sI https://yl26.<домен>/login" in text
