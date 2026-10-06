"""Кнопка «По файлу в проекте» есть в меню рассылки только когда файл с ID лежит на месте."""
from handlers import admin_broadcasts


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_file_button_hidden_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cbs = _callbacks(admin_broadcasts.build_broadcast_menu_kb())
    assert "broadcast_local" not in cbs
    assert "broadcast_all" in cbs and "broadcast_filter" in cbs


def test_file_button_shown_with_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "broadcast_target.txt").write_text("1\n", encoding="utf-8")
    cbs = _callbacks(admin_broadcasts.build_broadcast_menu_kb())
    assert cbs[:2] == ["broadcast_all", "broadcast_local"]
