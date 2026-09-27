"""Импорт экспорта Telegram Desktop в историю живого рейтинга чата (tools/chat_export_import.py).

По умолчанию — пробный прогон (ничего не пишет); --apply пишет INSERT OR IGNORE, повторный
запуск ничего не добавляет, живые строки не трогает. Текста в БД нет — только длины.
"""
import calendar
import json
import sqlite3
from datetime import datetime

import pytest

from config import config
from tests._dbtpl import fast_init_db
from tests.test_chat_rating_parity_260927 import (
    ROOT_ID,
    TEAM,
    _rounded,
    export_dict,
    export_scores,
    live_scores,
)
from tools import chat_export_import as imp

EXPORT_ID = 3333333333
CHAT_ID = -1003333333333
CHANNEL_ID = 1777777777


def _unix_from_msk(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    return calendar.timegm(dt.timetuple()) - 3 * 3600


def _with_unixtime(data: dict) -> dict:
    """Настоящий экспорт кладёт date_unixtime рядом с локальным date; здесь date = Москва."""
    for msg in data["messages"]:
        msg["date_unixtime"] = str(_unix_from_msk(msg["date"]))
    return data


def _fixture_export(extra_messages=()) -> dict:
    data = export_dict()
    data["id"] = EXPORT_ID
    data["messages"].extend(extra_messages)
    return _with_unixtime(data)


CHANNEL_POST = {
    "id": 30, "type": "message", "date": "2026-09-03T10:00:00",
    "from": "Канал", "from_id": f"channel{CHANNEL_ID}", "text": "y" * 250,
}
COMMENT_TO_CHANNEL = {
    "id": 31, "type": "message", "date": "2026-09-03T10:05:00",
    "from": "Человек 2", "from_id": "user2", "text": "z" * 40, "reply_to_message_id": 30,
}
ANON_ADMIN = {
    "id": 32, "type": "message", "date": "2026-09-03T10:06:00",
    "from": "Чат", "from_id": f"channel{EXPORT_ID}", "text": "w" * 10,
}
MANY_REACTIONS = {
    "id": 33, "type": "message", "date": "2026-09-03T11:00:00",
    "from": "Человек 3", "from_id": "user3", "text": "q" * 20,
    "reactions": [
        {"type": "emoji", "emoji": "👍", "count": 5,
         "recent": [{"from_id": "user1", "date": "2026-09-03T11:01:00"},
                    {"from_id": "user2", "date": "2026-09-03T11:02:00"}]},
        {"type": "custom_emoji", "document_id": "54321", "count": 1,
         "recent": [{"from_id": "user4", "date": "2026-09-03T11:03:00"}]},
        # реальный экспорт СПб: кастомные эмодзи с пустым document_id — не слипаются
        {"type": "custom_emoji", "document_id": "", "count": 1,
         "recent": [{"from_id": "user4", "date": "2026-09-03T11:04:00"}]},
        {"type": "custom_emoji", "document_id": "", "count": 1,
         "recent": [{"from_id": "user4", "date": "2026-09-03T11:05:00"}]},
    ],
}


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    # Срок хранения считается от «сейчас»: без заморозки фикстура сентября 2026 через полгода
    # оказалась бы старше срока и тесты начали бы падать сами.
    monkeypatch.setattr(imp, "_now", lambda: datetime(2026, 9, 27, 12, 0, 0))


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "import.db")
    config.DB_PATH = path
    fast_init_db()
    _setting(path, "delegate_chat_id__city__spb", str(CHAT_ID))
    return path


def _setting(path, key, value):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
    conn.close()


def _write_export(tmp_path, data) -> str:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _count(path, table) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _run(argv):
    return imp.main(argv)


# ── пробный прогон ───────────────────────────────────────────────────────────────────────

def test_dry_run_prints_counts_and_city_and_writes_nothing(db_path, tmp_path, capsys):
    export = _write_export(tmp_path, _fixture_export([CHANNEL_POST, COMMENT_TO_CHANNEL]))
    code = _run([export, "--db", db_path])
    out = capsys.readouterr().out
    assert code == 0
    assert str(CHAT_ID) in out
    assert "spb" in out
    assert "Пробный прогон" in out
    assert "--apply" in out
    assert "2026-09-01" in out and "2026-09-03" in out
    assert _count(db_path, "chat_messages") == 0
    assert _count(db_path, "chat_reactions") == 0


# ── запись ───────────────────────────────────────────────────────────────────────────────

def test_apply_writes_rows_like_live_capture(db_path, tmp_path):
    export = _write_export(tmp_path, _fixture_export(
        [CHANNEL_POST, COMMENT_TO_CHANNEL, ANON_ADMIN, MANY_REACTIONS]))
    assert _run([export, "--db", db_path, "--apply"]) == 0
    rows = {r["message_id"]: r for r in _rows(
        db_path, "SELECT * FROM chat_messages WHERE chat_id = ?", (CHAT_ID,))}
    assert ROOT_ID not in rows          # сервисное сообщение не журналируется
    assert 32 not in rows               # анонимный админ — как в живом учёте, пропуск
    assert all(r["source"] == "export" for r in rows.values())
    # корень темы — reply-поля пустые; ответ человеку — оба поля
    assert rows[10]["reply_to_message_id"] is None and rows[10]["reply_to_author_id"] is None
    assert (rows[12]["reply_to_message_id"], rows[12]["reply_to_author_id"]) == (11, 1)
    # пост канала: отрицательный id канала, настоящая длина, флаг
    assert rows[30]["is_channel_post"] == 1
    assert rows[30]["telegram_id"] == int(f"-100{CHANNEL_ID}")
    assert rows[30]["text_len"] == 250
    # ответ на пост канала: цель есть, автора-человека нет
    assert (rows[31]["reply_to_message_id"], rows[31]["reply_to_author_id"]) == (30, None)
    # виды и длины: стикер, фото с подписью, пересланное (своя длина 0)
    assert rows[16]["kind"] == "sticker" and rows[16]["text_len"] == 0
    assert rows[17]["kind"] == "media" and rows[17]["text_len"] == 90
    assert rows[18]["kind"] == "text" and rows[18]["text_len"] == 0
    assert rows[28]["kind"] == "sticker"
    # время — Москва по date_unixtime
    assert rows[10]["ts"] == "2026-09-01 10:00:00"
    # в БД нет ни одного текстового поля
    cols = {c["name"] for c in _rows(db_path, "PRAGMA table_info(chat_messages)")}
    assert not {"text", "caption"} & cols


def test_reactions_recent_become_rows_and_rest_goes_to_extra(db_path, tmp_path):
    export = _write_export(tmp_path, _fixture_export([MANY_REACTIONS]))
    assert _run([export, "--db", db_path, "--apply"]) == 0
    reactions = _rows(db_path, "SELECT telegram_id, reaction FROM chat_reactions "
                               "WHERE chat_id = ? AND message_id = 33", (CHAT_ID,))
    assert sorted((r["telegram_id"], r["reaction"]) for r in reactions) == [
        (1, "👍"), (2, "👍"), (4, "custom:54321"), (4, "export:2"), (4, "export:3"),
    ]
    msg = _rows(db_path, "SELECT reactions_extra FROM chat_messages WHERE message_id = 33")[0]
    assert msg["reactions_extra"] == 3


def test_second_apply_adds_nothing_and_live_row_untouched(db_path, tmp_path, capsys):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, text_len, source) "
        "VALUES (?, 19, 1, '2026-09-01 13:00:00', 'text', 7, 'live')", (CHAT_ID,),
    )
    conn.commit()
    conn.close()
    export = _write_export(tmp_path, _fixture_export())
    assert _run([export, "--db", db_path, "--apply"]) == 0
    first_msgs, first_reacts = _count(db_path, "chat_messages"), _count(db_path, "chat_reactions")
    live = _rows(db_path, "SELECT * FROM chat_messages WHERE message_id = 19")[0]
    assert live["source"] == "live" and live["text_len"] == 7 and live["reactions_extra"] == 0
    assert _rows(db_path, "SELECT COUNT(*) AS n FROM chat_reactions WHERE message_id = 19")[0]["n"] == 0
    capsys.readouterr()
    assert _run([export, "--db", db_path, "--apply"]) == 0
    assert _count(db_path, "chat_messages") == first_msgs
    assert _count(db_path, "chat_reactions") == first_reacts
    assert "Новых сообщений: 0" in capsys.readouterr().out


# ── чат и привязка ───────────────────────────────────────────────────────────────────────

def test_chat_id_override(db_path, tmp_path):
    other = -1009999999999
    _setting(db_path, "delegate_chat_id__city__msk", str(other))
    export = _write_export(tmp_path, _fixture_export())
    assert _run([export, "--db", db_path, "--chat-id", str(other), "--apply"]) == 0
    assert _rows(db_path, "SELECT DISTINCT chat_id FROM chat_messages") == [{"chat_id": other}]


def test_unbound_chat_refused_without_force(db_path, tmp_path, capsys):
    export = _write_export(tmp_path, _fixture_export())
    code = _run([export, "--db", db_path, "--chat-id", "-1008888888888", "--apply"])
    err = capsys.readouterr().err
    assert code == 1
    assert "не привязан" in err and "--force" in err
    assert _count(db_path, "chat_messages") == 0
    assert _run([export, "--db", db_path, "--chat-id", "-1008888888888", "--apply", "--force"]) == 0
    assert _count(db_path, "chat_messages") > 0


def test_bad_export_exit_1(db_path, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    assert _run([str(bad), "--db", db_path]) == 1
    assert "экспорт" in capsys.readouterr().err.lower()


def test_db_without_rating_tables_exit_1(tmp_path, capsys):
    db = tmp_path / "old.db"
    sqlite3.connect(db).close()
    export = _write_export(tmp_path, _fixture_export())
    assert _run([export, "--db", str(db), "--force"]) == 1
    assert "chat_messages" in capsys.readouterr().err


# ── паритет: импорт -> дашборд == тул по экспорту ────────────────────────────────────────

@pytest.mark.parametrize("since", [None, datetime(2026, 9, 2).date()])
def test_scores_after_import_equal_export_tool(db_path, tmp_path, since):
    data = _fixture_export([CHANNEL_POST, COMMENT_TO_CHANNEL, MANY_REACTIONS])
    export = _write_export(tmp_path, data)
    assert _run([export, "--db", db_path, "--apply"]) == 0
    _aggs, exp = export_scores(data, since=since)
    _laggs, live = live_scores(db_path, since=since, chat_id=CHAT_ID)

    def keep(pid):
        return pid not in TEAM

    assert _rounded(exp, keep) == _rounded(live, keep)
    assert _rounded(exp, keep)
