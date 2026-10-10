"""Тумблеры и мастер настройки шлют тот же `POST /admin/settings/batch`, что и пакетное
сохранение, — значит обязаны показывать `notice` (бот не получил правку), а не зелёное
«сохранено». Проверка по исходнику, без node (приём `test_miniapp_settings_fixes_260915`)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENS = ROOT / "miniapp" / "static" / "js" / "screens"


def _fn_body(text: str, name: str) -> str:
    start = text.index(f"async function {name}(")
    nxt = text.find("\n  async function ", start + 10)
    nxt2 = text.find("\n  function ", start + 10)
    ends = [e for e in (nxt, nxt2) if e != -1]
    return text[start:min(ends) if ends else len(text)]


def test_save_toggle_shows_notice_instead_of_saved():
    body = _fn_body((SCREENS / "settings.js").read_text(encoding="utf-8"), "saveToggle")
    assert re.search(r"if \(resp\.notice\) showToast\(resp\.notice, \"warn\"\)", body)


def test_manager_toggle_shows_notice_instead_of_saved():
    body = _fn_body((SCREENS / "settings.js").read_text(encoding="utf-8"), "saveManagerToggle")
    assert re.search(r"if \(resp\.notice\) showToast\(resp\.notice, \"warn\"\)", body)


def test_setup_wizard_shows_notice():
    text = (SCREENS / "setup.js").read_text(encoding="utf-8")
    assert "if (resp.notice) showToast(resp.notice)" in _fn_body(text, "saveStep")
    assert "resp.notice" in _fn_body(text, "hideTile")


def test_partial_save_clears_pending_of_saved_keys():
    body = _fn_body((SCREENS / "settings.js").read_text(encoding="utf-8"), "submitDiff")
    assert "err.payload.saved" in body and "pending.delete(key)" in body.split("catch (err)")[1]
