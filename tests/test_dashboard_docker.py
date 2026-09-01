"""Статические проверки образа и sidecar'а дашборда (Phase 15-06, D-01/D-04).

Читаем файлы с диска — демон Docker не нужен. Compose парсится через yaml.safe_load,
а не регулярками, чтобы комментарии не давали ложных совпадений."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "dashboard" / "Dockerfile"
REQS = ROOT / "dashboard" / "requirements.txt"
COMPOSE = ROOT / "docker-compose.yml"


def _body(path: Path) -> list[str]:
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# --- dashboard/Dockerfile ---------------------------------------------------------------


def test_dashboard_dockerfile_runs_as_non_root_uid_1000():
    body = _body(DOCKERFILE)
    assert [ln for ln in body if ln.startswith("USER ")] == ["USER appuser"]
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "--uid 1000" in text and "--gid 1000" in text
    cmd = [ln for ln in body if ln.startswith("CMD [")]
    assert len(cmd) == 1 and "uvicorn" in cmd[0] and "dashboard.main:app" in cmd[0]
    assert body.index("USER appuser") < body.index(cmd[0])


def test_dashboard_dockerfile_installs_only_dashboard_requirements():
    body = _body(DOCKERFILE)
    pip_lines = [ln for ln in body if "pip install" in ln]
    assert pip_lines, "образ должен ставить зависимости через pip"
    for ln in pip_lines:
        assert "dashboard/requirements.txt" in ln
    # корневой requirements.txt (aiogram и компания) в образ не попадает
    assert not any(ln.startswith("COPY requirements.txt") for ln in body)
    assert not any("COPY . ." in ln for ln in body)


@pytest.mark.parametrize("forbidden", ["aiogram", "gspread", "apscheduler"])
def test_dashboard_dockerfile_has_no_bot_dependencies(forbidden):
    text = "\n".join(_body(DOCKERFILE))
    assert forbidden not in text


def test_dashboard_dockerfile_copies_whole_package_with_static():
    """Шаблоны и static (tokens/app.css, brand/*.svg, vendor/) живут внутри dashboard/ —
    COPY должен брать каталог целиком."""
    body = _body(DOCKERFILE)
    assert any(
        ln.startswith("COPY --chown=appuser:appuser dashboard/ /app/dashboard/") for ln in body
    )


def test_dashboard_dockerfile_copies_web_theme_module():
    """`dashboard.main` импортирует корневой `web_theme` (19.1) — без COPY образ падает на старте
    `ModuleNotFoundError: web_theme` (прод 31.08). Тест-стенд этого не ловил: стоял на коммите до 19.1."""
    body = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    assert any(ln.startswith("COPY --chown=appuser:appuser web_theme.py /app/web_theme.py") for ln in body)


def test_dashboard_dockerfile_has_healthcheck_without_curl():
    hc = [ln for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines() if ln.startswith("HEALTHCHECK")]
    assert len(hc) == 1
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "/health" in text and "urllib.request" in text
    assert "curl" not in "\n".join(_body(DOCKERFILE))


def test_dashboard_dockerfile_cmd_has_no_proxy_headers_flag():
    """X-Forwarded-* разбирает ProxyHeadersMiddleware из create_app() по подсети."""
    cmd = [ln for ln in _body(DOCKERFILE) if ln.startswith("CMD [")][0]
    assert "--proxy-headers" not in cmd


# --- dashboard/requirements.txt ---------------------------------------------------------


def test_dashboard_requirements_uvicorn_lower_bound():
    lines = [ln for ln in _body(REQS) if ln.startswith("uvicorn")]
    assert len(lines) == 1
    spec = lines[0]
    assert spec.startswith("uvicorn[standard]")
    lower = [p for p in spec.split("]", 1)[1].split(",") if p.startswith(">=")]
    assert lower, "у uvicorn должна быть нижняя граница"
    major, minor = lower[0][2:].split(".")[:2]
    assert (int(major), int(minor)) >= (0, 32), "CIDR в trusted proxies появился в uvicorn 0.32"


@pytest.mark.parametrize("forbidden", ["aiogram", "gspread", "apscheduler"])
def test_dashboard_requirements_have_no_bot_dependencies(forbidden):
    assert not any(ln.startswith(forbidden) for ln in _body(REQS))


# --- docker-compose.yml -------------------------------------------------------------------


def test_compose_has_yl26_dashboard_and_no_plain_dashboard_service():
    services = _compose()["services"]
    assert "yl26-dashboard" in services
    assert "dashboard" not in services, "имя `dashboard` занято чужим проектом на сервере"
    assert services["yl26-dashboard"]["container_name"] == "youlead26-dashboard"


def test_compose_dashboard_builds_from_dashboard_dockerfile():
    build = _compose()["services"]["yl26-dashboard"]["build"]
    assert build["context"] == "."
    assert build["dockerfile"] == "dashboard/Dockerfile"


def test_compose_dashboard_mounts_data_rw_but_db_opens_read_only():
    """Том БД у дашборда БЕЗ `:ro`: WAL-режим (Phase 17) требует у читателя права на `-shm`
    в каталоге, и на read-only томе `mode=ro`-подключение падало «unable to open database
    file» — 500 сразу после входа (стенд 31.08). Контракт «дашборд не пишет» переезжает
    целиком на URI `mode=ro` в `dashboard/db.py` — его и сторожим."""
    vols = _compose()["services"]["yl26-dashboard"]["volumes"]
    assert "./data:/app/data" in vols
    assert not any(v.endswith(":ro") for v in vols), vols
    src = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")
    assert "?mode=ro" in src and "uri=True" in src


def test_compose_dashboard_in_edge_network_and_edge_is_external():
    cfg = _compose()
    nets = cfg["services"]["yl26-dashboard"]["networks"]
    assert "edge" in nets and "default" in nets
    assert cfg["networks"]["edge"]["external"] is True
    assert cfg["networks"]["edge"]["name"] == "edge"


def test_compose_dashboard_uses_stack_env_file():
    assert _compose()["services"]["yl26-dashboard"]["env_file"] == [".env"]


def test_compose_bot_untouched_and_not_in_edge():
    bot = _compose()["services"]["bot"]
    assert "./data:/app/data" in bot["volumes"]
    assert "edge" not in (bot.get("networks") or [])


def test_compose_no_service_publishes_ports():
    for name, svc in _compose()["services"].items():
        assert "ports" not in svc, f"сервис {name} публикует порты — на сервере входящих портов нет (D-01)"
