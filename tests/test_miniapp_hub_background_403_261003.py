"""Фоновые запросы главной Mini App не уводят экран в «Нет доступа» ни у одной роли.

Приёмка 03.10: счётчик плитки «Статистика» звал `/stats/game` без нужного права и без quiet,
ядро рисовало «Нет доступа» поверх всей главной. Дом менеджера больше не зовёт эти счётчики:
в фоне остаются только тихие подсказки настройки, и только при праве settings.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import re

from tests.test_miniapp_frontend import _nav_from_app_js

ROOT = REPO_ROOT
HUB_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "hub.js"

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


def _manager_hub_body() -> str:
    text = HUB_JS.read_text(encoding="utf-8")
    start = text.index("async function renderManagerHub(")
    rest = text[start + 1:]
    ends = [m.start() for m in re.finditer(r"^(async )?function ", rest, re.M)]
    return rest[:ends[0]] if ends else rest


def test_manager_hub_background_calls_are_only_quiet_settings():
    """Счётчики старых плиток сняты: дом больше не зовёт чужие ручки в фоне. Остаются
    только тихие подсказки настройки, и только при праве settings."""
    text = HUB_JS.read_text(encoding="utf-8")
    assert "MANAGER_FETCHERS" not in text
    body = _manager_hub_body()
    calls = re.findall(r'api\("([^"]+)"', body)
    assert calls == ["/admin/setup", "/admin/settings/hints"]
    assert 'me.caps.includes("settings")' in body
    assert "{ quiet: true }" in body
    for forbidden in ("/stats/game", "/review/next", "/admin/tasks", "/admin/coins", "/questions?"):
        assert forbidden not in body, forbidden


def test_reg_manager_without_game_has_no_stats_tile():
    caps, _ = ROLES["reg_manager"]
    assert "#/stats" not in {i["hash"] for i in _visible(caps, False)}
    assert "#/stats" in {i["hash"] for i in _visible(ALL_CAPS, False)}


def test_manager_rows_are_cap_gated_without_a_counter_loop():
    """Строка без права не рисуется. Фонового цикла счётчиков, который звал чужую ручку, нет."""
    body = _manager_hub_body()
    assert "hasCap(item.cap)" in body
    assert "fetcher.cap" not in body
    assert "api(fetcher.path" not in body
    assert "Promise.all(items.map" not in body


def test_delegate_hub_section_counters_are_quiet():
    text = HUB_JS.read_text(encoding="utf-8")
    body = text[text.index("async function renderDelegateHub("):text.index("async function renderTilesOnlyHub(")]
    for path in ("/coins/balance", "/coins/history?offset=0&limit=1", "/profile", "/tasks?offset=0&limit=2"):
        assert f'api("{path}", {{ quiet: true }})' in body, path
