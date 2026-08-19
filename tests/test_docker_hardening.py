"""Docker hardening guard: build context must not carry secrets/data, process must not be root.

Static checks on `.dockerignore` / `Dockerfile` (no docker daemon needed in CI)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _lines(name: str) -> list[str]:
    return [
        ln.strip()
        for ln in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


@pytest.mark.parametrize(
    "pattern",
    [".git", ".venv", "venv", "data", "logs", ".env", ".env.*", "google_credentials.json",
     "*.db", "*.pem", "tests", ".planning", ".claude", "__pycache__"],
)
def test_dockerignore_excludes(pattern):
    assert pattern in _lines(".dockerignore"), f"{pattern} must be excluded from build context"


def test_dockerignore_keeps_env_example():
    lines = _lines(".dockerignore")
    assert "!.env.example" in lines
    # negation must come after the pattern it un-ignores, or it has no effect
    assert lines.index("!.env.example") > lines.index(".env.*")


def test_dockerfile_runs_as_non_root_fixed_uid():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    body = [ln for ln in df.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    user_lines = [ln for ln in body if ln.startswith("USER ")]
    assert user_lines == ["USER appuser"]
    assert "--uid 1000" in df and "--gid 1000" in df, "UID must be fixed so host volumes can be chown'ed"
    # USER must come before CMD and after the COPY of sources
    assert body.index("USER appuser") < body.index('CMD ["python", "main.py"]')
    assert any(ln.startswith("COPY --chown=appuser:appuser . .") for ln in body)


def test_dockerfile_has_heartbeat_healthcheck():
    """Quick 260819: HEALTHCHECK по heartbeat-файлу (services/heartbeat.py), не `pgrep`/`true`."""
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    hc = [ln for ln in df.splitlines() if ln.startswith("HEALTHCHECK")]
    assert len(hc) == 1
    assert "python -m services.heartbeat --check" in hc[0]
    assert "HEALTHCHECK NONE" not in df
