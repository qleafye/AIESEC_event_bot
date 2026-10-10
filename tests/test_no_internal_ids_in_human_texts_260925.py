"""Сторож: служебные коды планов и решений (`D-30`, `F19`, «квик», «п.8») не уходят людям.

Менеджер увидел в хабе «🎪 Форум: функции» строку «всегда доступна (весь сезон, D-30)» — код
решения из `.planning/`, которого у него нет. Такие ссылки живут в комментариях и докстрингах,
в текстах для людей им не место.

Проверяется:
1. реестр настроек — `label`/`prompt`/`default` каждого ключа (их видит менеджер в боте и в
   Mini App, дефолты текстов видит делегат);
2. строковые литералы `handlers/`, `keyboards/`, `services/`, `miniapp/` (Python) — кроме
   докстрингов, вызовов логгера и `raise` (это не уходит в Telegram);
3. отрисованные экраны хаба форума, SOS и рассылки QR — текст и подписи кнопок.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import ast
import asyncio
import re
from pathlib import Path

import pytest

from config import config
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

ROOT = REPO_ROOT
# `(?<!#)` — HEX-цвет вроде #F48924 в подсказке про цвет оформления кодом не является.
INTERNAL_ID_RE = re.compile(r"\bD-\d+\b|(?<!#)\bF\d+\b|квик|\bп\.\d+", re.IGNORECASE)
SCANNED_DIRS = ("handlers", "keyboards", "services", "miniapp")
SUPERADMIN_ID = 900925401


def _texts_of(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def test_settings_registry_texts_have_no_internal_ids():
    bad = []
    for key, spec in SETTINGS_SCHEMA.items():
        for field in ("label", "prompt", "default", "default_free"):
            for text in _texts_of(spec.get(field)):
                m = INTERNAL_ID_RE.search(text)
                if m:
                    bad.append(f"{key}.{field}: {m.group(0)!r}")
    assert not bad, "служебные коды в текстах реестра настроек: " + "; ".join(bad)


def _skipped_nodes(tree: ast.AST) -> set[int]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            owner_name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
            if owner_name in ("logger", "logging", "log", "_log"):
                skip.update(id(sub) for sub in ast.walk(node))
        elif isinstance(node, ast.Raise):
            skip.update(id(sub) for sub in ast.walk(node))
    return skip


def _python_files() -> list[Path]:
    files = []
    for name in SCANNED_DIRS:
        files.extend(p for p in (ROOT / name).rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def test_code_string_literals_have_no_internal_ids():
    bad = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _skipped_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                m = INTERNAL_ID_RE.search(node.value)
                if m:
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: {m.group(0)!r}")
    assert not bad, "служебные коды в строках, которые уходят людям: " + "; ".join(bad)


def _markup_texts(kb) -> list[str]:
    rows = getattr(kb, "inline_keyboard", None) or getattr(kb, "keyboard", None) or []
    return [btn.text for row in rows for btn in row]


@pytest.fixture
def db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "no_internal_ids.db")
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


def test_rendered_forum_screens_have_no_internal_ids(db_ready):
    from cities import default_city_code
    from handlers import admin_checkin, admin_forum_functions, admin_sos

    code = default_city_code()

    async def _render_all():
        return [
            await admin_forum_functions._render_hub(SUPERADMIN_ID, code),
            await admin_sos.render_sos_screen(SUPERADMIN_ID),
            await admin_sos.render_sos_settings_screen(SUPERADMIN_ID),
            await admin_checkin._qr_cfg_text_kb(code),
        ]

    bad = []
    for text, kb in asyncio.run(_render_all()):
        for piece in [text, *_markup_texts(kb)]:
            m = INTERNAL_ID_RE.search(piece)
            if m:
                bad.append(f"{m.group(0)!r} в {piece[:80]!r}")
    assert not bad, "служебные коды на экранах форума: " + "; ".join(bad)
