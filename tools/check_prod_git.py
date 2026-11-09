"""Сверка запущенных образов с HEAD. Ничего не собирает и не перезапускает.

Из каталога чекаута на сервере (`/home/leaf/YouLead26`):

    python tools/check_prod_git.py

Код 0 — дерево чистое и у bot, miniapp, yl26-dashboard метка
org.opencontainers.image.revision совпала с HEAD. Иначе код 1.
Секреты и .env не печатаются.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping

SERVICES = ("bot", "miniapp", "yl26-dashboard")
REVISION_LABEL = "org.opencontainers.image.revision"
_NO_LABEL = frozenset(("", "unknown"))
_TIMEOUT_S = 30

_REBUILD_LINE = (
    "Пересоберите из чистого дерева: "
    "GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build"
)


class ProbeError(Exception):
    """Не удалось прочитать git или docker. Текст уже без секретов."""


def compare_prod_git(
    head: str,
    dirty: bool,
    revisions: Mapping[str, str | None],
) -> tuple[bool, list[str]]:
    """Сверяет коммит и флаг грязи с метками сервисов.

    revisions: имя сервиса → метка. None — контейнер не запущен. Пусто и «unknown» —
    образ собран без GIT_SHA. Ключа нет — тоже «не запущен». Лишние ключи игнорируются.
    Диск, сеть и docker не трогает.
    """
    commit = (head or "").strip()
    lines = [
        f"Коммит на диске: {commit or '(пусто)'}",
        "Дерево грязное" if dirty else "Дерево чистое",
    ]
    ok = bool(commit) and not dirty
    for name in SERVICES:
        status = _status(commit, revisions.get(name))
        lines.append(f"{name}: {status}")
        if status != "совпадает":
            ok = False
    if not ok:
        lines.append(_REBUILD_LINE)
    return ok, lines


def _status(commit: str, revision: str | None) -> str:
    if revision is None:
        return "контейнер не запущен"
    label = revision.strip()
    if label in _NO_LABEL:
        return "метки нет, образ собран без GIT_SHA"
    if label == commit:
        return "совпадает"
    return "в образе другой коммит"


def git_head_argv() -> list[str]:
    return ["git", "rev-parse", "HEAD"]


def git_dirty_argv() -> list[str]:
    return ["git", "diff-index", "--quiet", "HEAD"]


def compose_ps_argv(service: str) -> list[str]:
    return ["docker", "compose", "ps", "-q", service]


def inspect_labels_argv(container_id: str) -> list[str]:
    return ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT_S,
        shell=False,
    )


def _git_head() -> str:
    proc = _run(git_head_argv())
    if proc.returncode != 0:
        raise ProbeError("Не удалось прочитать коммит (git rev-parse HEAD).")
    head = proc.stdout.strip()
    if not head:
        raise ProbeError("Не удалось прочитать коммит (git rev-parse HEAD).")
    return head


def _git_dirty() -> bool:
    """Только отслеживаемые правки. Неотслеживаемые и gitignore сюда не входят."""
    proc = _run(git_dirty_argv())
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise ProbeError("Не удалось проверить дерево (git diff-index).")


def _container_ids(service: str) -> list[str]:
    proc = _run(compose_ps_argv(service))
    if proc.returncode != 0:
        raise ProbeError(f"{service}: не удалось спросить docker compose.")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _image_revision(service: str, container_id: str) -> str:
    proc = _run(inspect_labels_argv(container_id))
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProbeError(f"{service}: не удалось прочитать метку контейнера.")
    try:
        labels = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{service}: не удалось прочитать метку контейнера.") from exc
    if not isinstance(labels, dict):
        return ""
    value = labels.get(REVISION_LABEL)
    if value is None:
        return ""
    return str(value).strip()


def collect_revisions() -> dict[str, str | None]:
    revisions: dict[str, str | None] = {}
    for service in SERVICES:
        ids = _container_ids(service)
        if not ids:
            revisions[service] = None
            continue
        labels = [_image_revision(service, container_id) for container_id in ids]
        revisions[service] = labels[0] if len(set(labels)) == 1 else "разные метки"
    return revisions


def main() -> int:
    try:
        head = _git_head()
        dirty = _git_dirty()
        revisions = collect_revisions()
    except ProbeError as exc:
        print(str(exc))
        return 1
    except subprocess.TimeoutExpired:
        print("Команда проверки не ответила вовремя.")
        return 1
    ok, lines = compare_prod_git(head, dirty, revisions)
    for line in lines:
        print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
