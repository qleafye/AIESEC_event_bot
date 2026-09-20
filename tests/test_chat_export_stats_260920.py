"""Тул `tools/chat_export_stats.py`: рейтинг активности по экспорту чата Telegram Desktop.

Экспорт собирается в тесте словарём и пишется в tmp_path — настоящих переписок в репозитории
нет и быть не должно. Тексты сообщений здесь нарочно уникальные строки-маркеры: тест приватности
ищет их в stdout и CSV и обязан не найти ни одной.
"""
import csv
import json
import sqlite3

import pytest

import tools.chat_export_stats as tool

ALICE, BOB, CAROL, STAFF = 101, 102, 103, 900

SECRET_SHORT = "маркер-короткий-текст"
SECRET_LONG = "маркер-длинный-текст " * 12
SECRET_PIECE = "маркер-кусок-ссылки"


def _msg(mid, author, when, text="", *, name=None, reply_to=None, **extra):
    names = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol", STAFF: "Орг"}
    raw = {
        "id": mid, "type": "message", "date": when, "from": name or names.get(author, "?"),
        "from_id": f"user{author}", "text": text,
    }
    if reply_to is not None:
        raw["reply_to_message_id"] = reply_to
    raw.update(extra)
    return raw


def _topic_root(mid, when):
    return {"id": mid, "type": "service", "date": when, "action": "topic_created",
            "actor": "Орг", "actor_id": f"user{STAFF}", "title": "Флудилка"}


def _write(tmp_path, messages, name="result.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"name": "Тест-чат", "type": "private_supergroup",
                                "id": 1, "messages": messages}, ensure_ascii=False),
                    encoding="utf-8")
    return str(path)


def _stats(messages, **kwargs):
    parsed, index, _ = tool.parse_messages(messages)
    aggs = tool.aggregate(parsed, index, **kwargs)
    return aggs, tool.score_authors(aggs)


def test_text_as_string_and_as_entity_list_give_same_length():
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "привет мир"),
        _msg(2, BOB, "2026-09-01T10:00:00",
             ["привет ", {"type": "bold", "text": "мир"}]),
    ]
    aggs, _ = _stats(messages)
    assert aggs[ALICE].chars_total == aggs[BOB].chars_total == len("привет мир")


def test_short_and_long_split_by_threshold():
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "а" * 120),
        _msg(2, ALICE, "2026-09-01T11:00:00", "а" * 121),
        _msg(3, ALICE, "2026-09-01T12:00:00", "", photo="photo.jpg"),
    ]
    aggs, _ = _stats(messages)
    assert (aggs[ALICE].short, aggs[ALICE].long, aggs[ALICE].media) == (1, 1, 1)


def test_bursts_glue_own_messages_within_gap_only():
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "раз"),
        _msg(2, ALICE, "2026-09-01T10:01:30", "два"),      # +90 с — та же реплика
        _msg(3, BOB, "2026-09-01T10:01:40", "вклинился"),  # чужое сообщение склейку не рвёт
        _msg(4, ALICE, "2026-09-01T10:03:00", "три"),      # +90 с от предыдущего своего
        _msg(5, ALICE, "2026-09-01T10:10:00", "четыре"),   # +7 мин — новая реплика
    ]
    aggs, _ = _stats(messages)
    assert aggs[ALICE].messages == 4
    assert [b.length for b in aggs[ALICE].bursts] == [9, 6]


def test_reply_to_topic_root_and_to_self_is_not_a_reply():
    messages = [
        _topic_root(1, "2026-09-01T09:00:00"),
        _msg(2, ALICE, "2026-09-01T10:00:00", "вопрос", reply_to=1),   # корень топика
        _msg(3, ALICE, "2026-09-01T11:00:00", "уточню", reply_to=2),   # самому себе
        _msg(4, BOB, "2026-09-01T12:00:00", "ответ", reply_to=2),      # настоящий ответ
        _msg(5, BOB, "2026-09-01T13:00:00", "в пустоту", reply_to=999),  # цели нет в экспорте
    ]
    aggs, _ = _stats(messages)
    assert aggs[ALICE].replies_given == 0
    assert aggs[ALICE].replies_received == 1
    assert aggs[BOB].replies_given == 1
    assert aggs[BOB].replies_received == 0


def test_day_cap_limits_flood():
    flood = [
        _msg(i, ALICE, f"2026-09-01T{8 + i // 6:02d}:{(i % 6) * 10:02d}:00", "ок")
        for i in range(1, 61)  # 60 реплик за день, каждая по ~1 очку
    ]
    _, scores = _stats(flood)
    assert scores[ALICE]["volume"] == tool.WEIGHTS["day_cap"]


def test_forwarded_text_has_zero_length_and_no_volume():
    messages = [_msg(1, ALICE, "2026-09-01T10:00:00", SECRET_LONG, forwarded_from="Канал")]
    aggs, scores = _stats(messages)
    assert aggs[ALICE].messages == 1
    assert aggs[ALICE].chars_total == 0
    assert scores[ALICE]["volume"] == 0


def test_sticker_only_burst_scores_half_and_media_burst_scores_one():
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "", media_type="sticker", file="s.webp"),
        _msg(2, BOB, "2026-09-01T10:00:00", "", photo="p.jpg"),
    ]
    _, scores = _stats(messages)
    assert scores[ALICE]["volume"] == tool.WEIGHTS["burst_sticker_score"]
    assert scores[BOB]["volume"] == tool.WEIGHTS["burst_media_score"]


def test_reactions_received_capped_per_message_and_given_from_recent():
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "пост", reactions=[
            {"type": "emoji", "emoji": "❤", "count": 14,
             "recent": [{"from": "Bob", "from_id": f"user{BOB}", "date": "2026-09-01T10:05:00"}]},
            {"type": "emoji", "emoji": "🔥", "count": 3, "recent": []},
        ]),
    ]
    aggs, scores = _stats(messages)
    assert aggs[ALICE].reactions_received == 17
    assert scores[ALICE]["resonance"] == tool.WEIGHTS["resonance_reaction"] * 10
    assert aggs[BOB].reactions_given == 1
    assert scores[BOB]["giving"] == tool.WEIGHTS["giving_per_reaction"]


def test_weights_override_changes_caps_in_the_calculation():
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "пост",
             reactions=[{"type": "emoji", "emoji": "❤", "count": 14}]),
    ]
    parsed, index, _ = tool.parse_messages(messages)
    aggs = tool.aggregate(parsed, index)
    scores = tool.score_authors(aggs, {"resonance_reaction_cap": 4})
    assert scores[ALICE]["resonance"] == tool.WEIGHTS["resonance_reaction"] * 4


def test_export_without_reactions_is_reported(tmp_path, capsys):
    path = _write(tmp_path, [_msg(1, ALICE, "2026-09-01T10:00:00", "привет")])
    assert tool.main([path]) == 0
    assert "нет блока реакций" in capsys.readouterr().out


def test_since_until_window_keeps_reply_target_outside_window():
    messages = [
        _msg(1, ALICE, "2026-08-31T23:00:00", "до окна"),
        _msg(2, BOB, "2026-09-01T10:00:00", "ответ", reply_to=1),
        _msg(3, BOB, "2026-09-03T10:00:00", "после окна"),
    ]
    parsed, index, _ = tool.parse_messages(messages)
    aggs = tool.aggregate(parsed, index,
                          since=tool._parse_date_arg("2026-09-01"),
                          until=tool._parse_date_arg("2026-09-02"))
    assert aggs[BOB].messages == 1
    assert aggs[ALICE].messages == 0
    assert aggs[ALICE].replies_received == 1  # цель ответа лежит до окна, отклик засчитан


def test_excluded_author_is_out_of_rating_but_their_reply_counts(tmp_path, capsys):
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "вопрос"),
        _msg(2, STAFF, "2026-09-01T10:05:00", "ответ орга", reply_to=1),
    ]
    out_csv = tmp_path / "rating.csv"
    assert tool.main([_write(tmp_path, messages), "--exclude-ids", str(STAFF),
                      "--csv", str(out_csv)]) == 0
    rows = list(csv.DictReader(out_csv.open(encoding="utf-8-sig")))
    assert [r["telegram_id"] for r in rows] == [str(ALICE)]
    assert rows[0]["replies_received"] == "1"
    assert "ручной список: 1" in capsys.readouterr().out


def _make_db(tmp_path, *, with_staff=True):
    db_path = tmp_path / "forum.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (telegram_id INTEGER, username TEXT, full_name TEXT, status TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", [
        (ALICE, "alice_nick", "Алиса Иванова", "approved"),
        (BOB, "-", "Борис Петров", "pending"),
    ])
    if with_staff:
        conn.execute("CREATE TABLE staff (telegram_id INTEGER, role TEXT)")
        # Две роли у одного сотрудника и сотрудник, которого в чате нет вовсе.
        conn.executemany("INSERT INTO staff VALUES (?, ?)",
                         [(STAFF, "admin"), (STAFF, "manager"), (777, "manager")])
    conn.commit()
    conn.close()
    return str(db_path)


def test_db_gives_username_and_excludes_staff_present_in_chat(tmp_path, capsys):
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", "раз"),
        _msg(2, BOB, "2026-09-01T10:00:00", "два"),
        _msg(3, STAFF, "2026-09-01T10:00:00", "орг пишет"),
    ]
    out_csv = tmp_path / "rating.csv"
    assert tool.main([_write(tmp_path, messages), "--db", _make_db(tmp_path),
                      "--csv", str(out_csv)]) == 0
    rows = {r["telegram_id"]: r for r in csv.DictReader(out_csv.open(encoding="utf-8-sig"))}
    assert set(rows) == {str(ALICE), str(BOB)}
    assert rows[str(ALICE)]["username"] == "@alice_nick"
    assert rows[str(ALICE)]["full_name"] == "Алиса Иванова"
    assert rows[str(BOB)]["username"] == ""  # плейсхолдер «-» — это отсутствие ника
    assert "сотрудники: 1" in capsys.readouterr().out  # 777 в чате не было — не считается


def test_db_without_staff_table_is_a_warning_not_an_error(tmp_path, capsys):
    path = _write(tmp_path, [_msg(1, ALICE, "2026-09-01T10:00:00", "привет")])
    assert tool.main([path, "--db", _make_db(tmp_path, with_staff=False)]) == 0
    assert "нет таблицы staff" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["{не json", "[]", '{"name": "чат"}'])
def test_broken_export_exits_with_code_1_and_howto(tmp_path, capsys, content):
    path = tmp_path / "result.json"
    path.write_text(content, encoding="utf-8")
    assert tool.main([str(path)]) == 1
    assert "Экспорт истории чата" in capsys.readouterr().err


def test_weights_file_with_unknown_key_is_rejected(tmp_path, capsys):
    path = _write(tmp_path, [_msg(1, ALICE, "2026-09-01T10:00:00", "привет")])
    weights = tmp_path / "w.json"
    weights.write_text('{"day_kap": 30}', encoding="utf-8")
    assert tool.main([path, "--weights", str(weights)]) == 1
    assert "day_kap" in capsys.readouterr().err


def test_message_without_date_does_not_create_phantom_day():
    broken = _msg(1, ALICE, "не дата", "текст")
    aggs, _ = _stats([broken, _msg(2, ALICE, "2026-09-01T10:00:00", "норм")])
    assert aggs[ALICE].messages == 1
    assert aggs[ALICE].active_days == 1


def test_no_message_text_leaks_to_stdout_or_csv(tmp_path, capsys):
    messages = [
        _msg(1, ALICE, "2026-09-01T10:00:00", SECRET_SHORT),
        _msg(2, BOB, "2026-09-01T10:01:00", SECRET_LONG, reply_to=1),
        _msg(3, CAROL, "2026-09-01T10:02:00",
             ["смотри ", {"type": "link", "text": SECRET_PIECE}]),
    ]
    out_csv = tmp_path / "rating.csv"
    assert tool.main([_write(tmp_path, messages), "--csv", str(out_csv)]) == 0
    haystack = capsys.readouterr().out + out_csv.read_text(encoding="utf-8-sig")
    for secret in (SECRET_SHORT, SECRET_LONG.strip(), SECRET_PIECE, "смотри"):
        assert secret not in haystack
