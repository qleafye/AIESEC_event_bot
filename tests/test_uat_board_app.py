"""Доска приёмки `tools/uat_board/app.py`: отметки шагов и сброс с бэкапом.

Сервер на stdlib, без фреймворка: проверяем функции состояния напрямую, HTTP-слой тонкий.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("UAT_DATA", str(tmp_path / "data" / "state.json"))
    monkeypatch.syspath_prepend(str(ROOT / "tools" / "uat_board"))
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    yield mod
    sys.modules.pop("app", None)


def test_mark_sets_and_clears_step(board):
    state = board._load()
    assert board._mark(state, {"key": "s1.scan.4", "s": "bad", "note": "плашка не появилась", "who": "Тимлид"})
    rec = state["steps"]["s1.scan.4"]
    assert rec["s"] == "bad" and rec["who"] == "Тимлид" and rec["at"]
    assert state["v"] == 1
    # Пустой статус без заметки — запись убирается, версия растёт.
    assert board._mark(state, {"key": "s1.scan.4", "s": "", "note": ""})
    assert "s1.scan.4" not in state["steps"] and state["v"] == 2


def test_mark_rejects_bad_payload(board):
    state = board._load()
    assert not board._mark(state, {"key": "", "s": "ok"})
    assert not board._mark(state, {"key": "s1.qr.1", "s": "maybe"})
    assert state["steps"] == {} and state["v"] == 0


def test_reset_backs_up_previous_state(board):
    state = board._load()
    board._mark(state, {"key": "s1.qr.1", "s": "ok", "note": "", "who": "Кристина"})
    board._store(state)
    data_dir = Path(board.DATA).parent

    fresh = board._reset()

    assert fresh == {"v": 2, "steps": {}}
    assert json.loads(Path(board.DATA).read_text(encoding="utf-8")) == fresh
    backups = sorted(p for p in data_dir.iterdir() if p.name.startswith("state.json.bak-"))
    assert len(backups) == 1
    saved = json.loads(backups[0].read_text(encoding="utf-8"))
    assert saved["steps"]["s1.qr.1"]["who"] == "Кристина"


def test_reset_without_file_creates_empty_state(board):
    assert not os.path.exists(board.DATA)
    assert board._reset() == {"v": 1, "steps": {}}
    assert not [p for p in Path(board.DATA).parent.iterdir() if ".bak-" in p.name]
