"""Статические проверки стека Cloudflare Tunnel (Phase 15-06, D-01/D-03).

Сторож против возврата старой схемы: на сервере не открывается ни один входящий порт,
обратного прокси с сертификатами (caddy) нет, токен туннеля живёт только в tunnel/.env."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TUNNEL_COMPOSE = ROOT / "tunnel" / "docker-compose.yml"
TUNNEL_ENV_EXAMPLE = ROOT / "tunnel" / ".env.example"
STACK_COMPOSE = ROOT / "docker-compose.yml"
ALL_COMPOSES = [STACK_COMPOSE, TUNNEL_COMPOSE]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _cloudflared():
    services = _load(TUNNEL_COMPOSE)["services"]
    matches = [s for s in services.values() if str(s.get("image", "")).startswith("cloudflare/cloudflared")]
    assert len(matches) == 1, "ровно один сервис на образе cloudflare/cloudflared"
    return matches[0]


def test_cloudflared_runs_tunnel_without_autoupdate():
    cmd = _cloudflared()["command"]
    cmd = cmd if isinstance(cmd, str) else " ".join(cmd)
    assert "tunnel" in cmd and "run" in cmd
    assert "--no-autoupdate" in cmd


def test_cloudflared_has_no_ports_volumes_or_user():
    svc = _cloudflared()
    for key in ("ports", "volumes", "user"):
        assert key not in svc, f"{key} не нужен: remotely-managed туннель, образ уже не от root"


def test_cloudflared_in_both_origin_networks_declared_external():
    cfg = _load(TUNNEL_COMPOSE)
    nets = _cloudflared()["networks"]
    assert set(nets) == {"edge", "nextcloud_internal"}
    for name in ("edge", "nextcloud_internal"):
        assert cfg["networks"][name]["external"] is True
        assert cfg["networks"][name]["name"] == name


def test_tunnel_token_only_via_env_file():
    assert "TUNNEL_TOKEN" not in TUNNEL_COMPOSE.read_text(encoding="utf-8")
    assert _cloudflared()["env_file"] == [".env"]


def test_tunnel_env_example_has_empty_token():
    lines = [
        ln.strip()
        for ln in TUNNEL_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines == ["TUNNEL_TOKEN="]


def test_tunnel_dir_excluded_from_bot_build_context():
    lines = [
        ln.strip()
        for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "tunnel" in lines


@pytest.mark.parametrize("compose", ALL_COMPOSES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_compose_publishes_web_ports(compose: Path):
    for name, svc in _load(compose)["services"].items():
        for spec in svc.get("ports", []) or []:
            text = str(spec)
            for port in ("80", "443", "8443"):
                assert not text.startswith(port + ":") and f":{port}:" not in text and text != port, (
                    f"{compose.name}::{name} публикует порт {port} — схема D-03 без открытых портов"
                )


@pytest.mark.parametrize("compose", ALL_COMPOSES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_compose_has_caddy_service(compose: Path):
    for name, svc in _load(compose)["services"].items():
        assert "caddy" not in name.lower()
        assert "caddy" not in str(svc.get("image", "")).lower()
