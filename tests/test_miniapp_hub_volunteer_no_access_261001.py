"""Хаб Mini App у волонтёра (право только «чек-ин») не рисует «Нет доступа» поверх главной.

Раньше хаб менеджера дёргал `/admin/setup` и `/admin/settings/hints` у всех staff: у волонтёра
без права `settings` оба отвечали 403, и ядро (`api.js` -> authErrorHandler) уводило главную в
экран «✕ Нет доступа». Теперь фоновые запросы идут только при праве `settings` и с `quiet`,
а `quiet` глушит переход в «Нет доступа» на 403.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUB_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "hub.js"
API_JS = ROOT / "miniapp" / "static" / "js" / "api.js"


def _manager_hub_body() -> str:
    text = HUB_JS.read_text(encoding="utf-8")
    start = text.index("async function renderManagerHub(")
    return text[start:start + 6000]


def test_hub_admin_background_requests_gated_by_settings_cap():
    body = _manager_hub_body()
    assert 'me.caps.includes("settings")' in body
    for path in ("/admin/setup", "/admin/settings/hints"):
        m = re.search(r'canSettings \? await api\("' + re.escape(path) + r'", \{ quiet: true \}\)', body)
        assert m, path


def test_api_quiet_skips_no_access_screen_on_403():
    text = API_JS.read_text(encoding="utf-8")
    assert "quiet = false" in text
    assert "response.status === 403 && !quiet" in text
    # ApiError всё равно бросается — вызывающий решает сам (fail-soft в хабе).
    assert "throw new ApiError(response.status, reason, payload);" in text
