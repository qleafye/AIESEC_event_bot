"""Импорт экспорта Telegram: срок хранения, время и имена.

- Срок хранения (chat_rating_retention_days) удалил бы старые строки первым же суточным
  прогоном: пробный прогон предупреждает, сколько таких, а --apply их не пишет.
- Время — только по date_unixtime: поле date — локальное время того, кто выгружал; сообщение
  без date_unixtime пропускается и попадает в отчёт.
- Ников в экспорте нет — тул сохраняет имя (первое слово поля from), чтобы на дашборде не
  было «без ника» у тех, кто писал только до выката; живые ники не трогает.
"""
from datetime import datetime

from tests.test_chat_export_import_260927 import (  # noqa: F401 — фикстура db_path
    CHAT_ID,
    _count,
    _fixture_export,
    _rows,
    _run,
    _setting,
    _write_export,
    db_path,
)
from services import chat_export_import as svc
from tools import chat_export_import as imp


def _freeze(monkeypatch, when: datetime):
    monkeypatch.setattr(svc, "_now", lambda: when)


def test_dry_run_warns_about_rows_older_than_retention(db_path, tmp_path, capsys, monkeypatch):  # noqa: F811
    _freeze(monkeypatch, datetime(2026, 9, 8, 12, 0, 0))
    _setting(db_path, "chat_rating_retention_days", "6")  # граница — 2026-09-02 12:00
    export = _write_export(tmp_path, _fixture_export())
    assert _run([export, "--db", db_path]) == 0
    out = capsys.readouterr().out
    assert "старше срока хранения (6 дн.)" in out
    assert _count(db_path, "chat_messages") == 0


def test_apply_skips_rows_older_than_retention(db_path, tmp_path, capsys, monkeypatch):  # noqa: F811
    _freeze(monkeypatch, datetime(2026, 9, 8, 12, 0, 0))
    _setting(db_path, "chat_rating_retention_days", "6")
    export = _write_export(tmp_path, _fixture_export())
    assert _run([export, "--db", db_path, "--apply"]) == 0
    out = capsys.readouterr().out
    stamps = [r["ts"] for r in _rows(db_path, "SELECT ts FROM chat_messages")]
    assert stamps and min(stamps) >= "2026-09-02 12:00:00"
    assert "не записаны" in out


def test_default_retention_keeps_everything_recent(db_path, tmp_path, monkeypatch):  # noqa: F811
    _freeze(monkeypatch, datetime(2026, 9, 8, 12, 0, 0))
    export = _write_export(tmp_path, _fixture_export())
    assert _run([export, "--db", db_path, "--apply"]) == 0
    stamps = [r["ts"] for r in _rows(db_path, "SELECT ts FROM chat_messages")]
    assert min(stamps).startswith("2026-09-01")


def test_message_without_unixtime_is_skipped_and_reported(db_path, tmp_path, capsys, monkeypatch):  # noqa: F811
    _freeze(monkeypatch, datetime(2026, 9, 8, 12, 0, 0))
    data = _fixture_export()
    target = next(m for m in data["messages"] if m.get("type") == "message" and m["id"] == 12)
    del target["date_unixtime"]
    export = _write_export(tmp_path, data)
    assert _run([export, "--db", db_path, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "без date_unixtime: 1" in out
    ids = {r["message_id"] for r in _rows(db_path, "SELECT message_id FROM chat_messages")}
    assert 12 not in ids and 11 in ids


def test_apply_stores_first_names_without_touching_live_nicks(db_path, tmp_path, capsys, monkeypatch):  # noqa: F811
    import sqlite3
    _freeze(monkeypatch, datetime(2026, 9, 8, 12, 0, 0))
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO chat_usernames (telegram_id, username, first_name) VALUES (1, 'live', 'Живой')")
    conn.commit()
    conn.close()
    export = _write_export(tmp_path, _fixture_export())
    assert _run([export, "--db", db_path, "--apply"]) == 0
    out = capsys.readouterr().out
    names = {r["telegram_id"]: r for r in _rows(db_path, "SELECT * FROM chat_usernames")}
    assert (names[1]["username"], names[1]["first_name"]) == ("live", "Живой")
    assert names[2]["first_name"] == "Человек" and names[2]["username"] is None
    assert "Ников в экспорте нет" in out
