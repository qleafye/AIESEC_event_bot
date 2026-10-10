"""Кнопка «По файлу в проекте» есть в меню рассылки только суперадмину и только когда файл с ID
лежит на месте; сам обработчик тоже не пускает менеджера (кнопку можно вызвать старым сообщением)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from config import config
from handlers.comms import admin_broadcasts

SUPER = 777001
MANAGER = 777002


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _with_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "broadcast_target.txt").write_text("1\n", encoding="utf-8")
    monkeypatch.setattr(config, "ADMIN_IDS", [SUPER])


def test_file_button_hidden_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [SUPER])
    cbs = _callbacks(admin_broadcasts.build_broadcast_menu_kb(SUPER))
    assert "broadcast_local" not in cbs
    assert "broadcast_all" in cbs and "broadcast_filter" in cbs


def test_file_button_shown_with_file(tmp_path, monkeypatch):
    _with_file(tmp_path, monkeypatch)
    cbs = _callbacks(admin_broadcasts.build_broadcast_menu_kb(SUPER))
    assert cbs[:2] == ["broadcast_all", "broadcast_local"]


def test_file_button_hidden_for_manager(tmp_path, monkeypatch):
    _with_file(tmp_path, monkeypatch)
    assert "broadcast_local" not in _callbacks(admin_broadcasts.build_broadcast_menu_kb(MANAGER))
    assert "broadcast_local" not in _callbacks(admin_broadcasts.build_broadcast_menu_kb())


def test_file_handler_refuses_manager(tmp_path, monkeypatch):
    _with_file(tmp_path, monkeypatch)
    cb = MagicMock()
    cb.from_user.id = MANAGER
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()
    asyncio.run(admin_broadcasts.process_broadcast_local_file(cb, state))
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()
