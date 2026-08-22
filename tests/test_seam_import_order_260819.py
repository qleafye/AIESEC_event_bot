"""Швы Phase 13 REFAC устойчивы к порядку импорта (quick 260819).

Швы (`handlers/admin_*.py`, `handlers/reg_flow.py`, `handlers/reg_steps.py`) регистрируют
хендлеры на общий `router` владельца (`handlers.admin` / `handlers.registration`) и импортируются
из ТЕЛА владельца в фиксированной позиции — так воспроизводится порядок регистрации (first-match)
исходного god-file. До фикса порядок зависел от того, какой модуль пакета `handlers` импортирован
в процессе ПЕРВЫМ:

- шов с именными импортами у владельца (`admin_settings`, `admin_broadcasts`, `admin_moderation`,
  `admin_roles`) первым -> `ImportError: cannot import name ... from partially initialized module`;
- шов без именных импортов (`admin_cities`, `admin_reg_config`, `reg_flow`) первым -> его хендлеры
  регистрируются в ХВОСТ роутера (first-match меняется, golden-снапшот падает).

Фикс: `handlers/__init__.py` импортирует владельцев в каноническом порядке `main.py`
(`registration, user_actions, admin, payment`) — пакет инициализируется одинаково при импорте
ЛЮБОГО `handlers.x`. Этот тест гоняет каждый шов ПЕРВЫМ в отдельном subprocess (`tests/conftest.py`
там не действует — он сам маскирует баг для pytest-прогонов) и сверяет отпечаток порядка хендлеров
всех четырёх роутеров с каноническим (импорт в порядке `main.py`).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SEAMS = [
    "admin_settings",
    "admin_cities",
    "admin_broadcasts",
    "admin_reg_config",
    "admin_moderation",
    "admin_roles",
    "admin_gamification",
    "admin_game_tasks",
    "reg_flow",
    "reg_steps",
    # quick 260822: согласия -- версия/пересогласие (шов admin_settings) и делегатский
    # пересбор (шов registration)
    "admin_consent",
    "reg_consent",
]

# Отпечаток: имя колбэка каждого хендлера каждого observer'а всех четырёх роутеров,
# в порядке включения main.py. Только порядок и имена -- ни путей модулей, ни строк.
_FINGERPRINT_SNIPPET = """
from handlers import registration, user_actions, admin, payment
lines = []
for mod in (registration, user_actions, admin, payment):
    for obs_name in ("message", "callback_query"):
        obs = getattr(mod.router, obs_name)
        for h in obs.handlers:
            lines.append(f"{mod.__name__}.router.{obs_name}:{h.callback.__name__}")
print("\\n".join(lines))
"""


def _run(first_import: str) -> tuple[int, str, str]:
    code = (first_import + "\n" if first_import else "") + _FINGERPRINT_SNIPPET
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture(scope="module")
def canonical_fingerprint() -> list[str]:
    rc, out, err = _run("")
    assert rc == 0, f"канонический импорт (порядок main.py) упал:\n{err[-3000:]}"
    lines = out.strip().splitlines()
    assert len(lines) > 100, f"подозрительно короткий отпечаток: {len(lines)} строк"
    return lines


@pytest.mark.parametrize("seam", SEAMS)
def test_seam_imported_first_keeps_canonical_handler_order(seam, canonical_fingerprint):
    rc, out, err = _run(f"import handlers.{seam}")
    assert rc == 0, (
        f"`import handlers.{seam}` ПЕРВЫМ в процессе падает "
        f"(цикл шов <-> владелец роутера):\n{err[-3000:]}"
    )
    actual = out.strip().splitlines()
    assert actual == canonical_fingerprint, (
        f"`import handlers.{seam}` ПЕРВЫМ меняет порядок регистрации хендлеров "
        f"(first-match) относительно порядка main.py"
    )
