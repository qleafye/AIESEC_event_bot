"""Плашка сканера «не день форума» и подтверждение «Отметить всё равно» — без рода: делегатом
бывает и девушка, «у него форум»/«не его день» читаются как ошибка бота."""
from __future__ import annotations
from tests._paths import REPO_ROOT

import re
from pathlib import Path

ROOT = REPO_ROOT
_GENDERED = re.compile(r"у него|не его день|он пришёл|пришёл не")


def test_forum_day_denial_text_is_gender_neutral():
    text = (ROOT / "services" / "checkin_forum_day.py").read_text(encoding="utf-8")
    body = text[text.index("async def _entry_day_denial("):text.index("async def city_emphasis(")]
    assert not _GENDERED.search(body)
    assert "у делегата форум" in body


def test_scanner_force_confirm_is_gender_neutral():
    text = (ROOT / "miniapp" / "static" / "js" / "screens" / "scanner.js").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "askConfirm(`Отметить вход" in ln)
    assert not _GENDERED.search(line)
