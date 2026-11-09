"""Одно число «схема готова» для health Mini App.

Миграции схемы делает только бот. Константы `*_USER_VERSION = N` в этом каталоге —
их номера. `SCHEMA_READY_USER_VERSION` — максимум: следующая миграция поднимает свою
константу, и число меняется само, без второго литерала в miniapp.
"""
from __future__ import annotations

import re
from pathlib import Path

# Присваивание на уровне модуля: имя кончается на USER_VERSION, справа целое.
# Вызов и f-string не считаются — иначе само это число попало бы в максимум вторым разом.
_ASSIGNED = re.compile(r"^(\w*USER_VERSION)\s*=\s*(\d+)\s*(?:#.*)?$", re.MULTILINE)


def _assigned_user_versions() -> list[int]:
    root = Path(__file__).resolve().parent
    found: list[int] = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        found.extend(int(value) for _name, value in _ASSIGNED.findall(text))
    return found


def _schema_ready_user_version() -> int:
    versions = _assigned_user_versions()
    if not versions:
        raise RuntimeError("в database/ нет констант *_USER_VERSION")
    return max(versions)


SCHEMA_READY_USER_VERSION = _schema_ready_user_version()
