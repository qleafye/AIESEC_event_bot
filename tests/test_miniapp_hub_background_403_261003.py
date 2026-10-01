"""Фоновые запросы главной Mini App не уводят экран в «Нет доступа» ни у одной роли.

Приёмка 03.10: менеджер заявок без геймы (роли «🆘 Дежурный SOS», «🎪 Менеджер форума»)
видел плитку «Статистика» по праву `stats`, её счётчик звал `/stats/game` (маршрут требует
`moderate_game`) -> 403 без quiet -> ядро рисовало «Нет доступа» поверх всей главной.

Сторож по ролям: для каждой роли считаем видимые плитки (та же логика, что `visibleNav`),
фоновые счётчики, которые хаб для них позовёт, и право, которое проверяет сам маршрут
(читаем из `miniapp/routers/*.py`). Ни один позванный счётчик и ни одна видимая плитка не
должны упираться в право, которого у роли нет.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import _nav_from_app_js

ROOT = Path(__file__).resolve().parent.parent
HUB_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "hub.js"
ROUTERS = ROOT / "miniapp" / "routers"

ALL_CAPS = {
    "moderate_reg", "moderate_receipts", "moderate_game", "broadcast",
    "settings", "stats", "checkin", "checkin_approve",
}

# Роль -> (caps, is_delegate). Менеджер заявок — как на стенде (с правом статистики, без геймы).
ROLES = {
    "volunteer": ({"checkin"}, False),
    "reg_manager": ({"moderate_reg", "moderate_receipts", "stats"}, False),
    "game_manager": ({"moderate_game"}, False),
    "superadmin": (ALL_CAPS, False),
    "delegate": (set(), True),
}

# Экран плитки -> маршрут, который экран зовёт первым (право маршрута = право экрана).
SCREEN_ROUTES = {"#/stats": "/stats/game"}


def _visible(caps: set[str], is_delegate: bool) -> list[dict]:
    out = []
    for item in _nav_from_app_js():
        if item.get("delegate"):
            if is_delegate:
                out.append(item)
            continue
        if item.get("staffOnly") and is_delegate:
            continue
        if item["cap"] in caps and (not item.get("alsoCap") or item["alsoCap"] in caps):
            out.append(item)
    return out


def _fetchers() -> dict[str, tuple[str, str]]:
    text = HUB_JS.read_text(encoding="utf-8")
    block = text[text.index("const MANAGER_FETCHERS = {"):]
    block = block[:block.index("\n};")]
    return {
        m.group(1): (m.group(2), m.group(3))
        for m in re.finditer(r'"(#/[\w-]+)": \{ cap: "(\w+)", path: "([^"]+)" \}', block)
    }


def _route_cap(path: str) -> str | None:
    route = "/app/api" + path.split("?", 1)[0]
    for py in ROUTERS.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        idx = text.find(f'@router.get("{route}")')
        if idx < 0:
            continue
        head = text[idx:idx + 600]
        m = re.search(r'require_cap\("(\w+)"\)', head)
        return m.group(1) if m else None
    raise AssertionError(f"маршрут {route} не найден")


def test_fetchers_parsed():
    fetchers = _fetchers()
    assert {"#/review", "#/admin-tasks", "#/admin-coins", "#/questions", "#/stats", "#/settings"} <= set(fetchers)
    for hash_, (cap, path) in fetchers.items():
        assert _route_cap(path) == cap, (hash_, path)


@pytest.mark.parametrize("role", sorted(ROLES))
def test_role_hub_never_hits_forbidden_route(role):
    caps, is_delegate = ROLES[role]
    fetchers = _fetchers()
    for item in _visible(caps, is_delegate):
        fetcher = fetchers.get(item["hash"])
        if fetcher and fetcher[0] in caps:
            need = _route_cap(fetcher[1])
            assert need is None or need in caps, (role, item["hash"], need)
        screen = SCREEN_ROUTES.get(item["hash"])
        if screen:
            need = _route_cap(screen)
            assert need is None or need in caps, (role, item["hash"], need)


def test_reg_manager_without_game_has_no_stats_tile():
    caps, _ = ROLES["reg_manager"]
    assert "#/stats" not in {i["hash"] for i in _visible(caps, False)}
    assert "#/stats" in {i["hash"] for i in _visible(ALL_CAPS, False)}


def test_manager_counters_are_quiet_and_cap_gated():
    text = HUB_JS.read_text(encoding="utf-8")
    body = text[text.index("async function renderManagerHub("):]
    assert "me.caps.includes(fetcher.cap)" in body
    assert "api(fetcher.path, { quiet: true })" in body


def test_delegate_hub_section_counters_are_quiet():
    text = HUB_JS.read_text(encoding="utf-8")
    body = text[text.index("async function renderDelegateHub("):text.index("async function renderTilesOnlyHub(")]
    for path in ("/coins/balance", "/coins/history?offset=0&limit=1", "/profile", "/tasks?offset=0&limit=2"):
        assert f'api("{path}", {{ quiet: true }})' in body, path
