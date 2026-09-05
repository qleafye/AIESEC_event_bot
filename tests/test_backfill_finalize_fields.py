"""Юнит-тест для `tools/backfill_finalize_fields.py` — ремонт `event_city`/`source`/
`referrer_id`, потерянных багом `finalize_data` (05.09 16:04 UTC — окно между b460826 и
хотфиксом). Временная sqlite-база + фейковый лог бота, без aiosqlite/config — сам инструмент
работает синхронным stdlib sqlite3 напрямую с файлом БД."""
import sqlite3

import pytest

from tools.backfill_finalize_fields import (
    apply_changes,
    main,
    parse_log,
    parse_since,
    plan_changes,
)


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, event_city TEXT, source TEXT, "
        "referrer_id INTEGER, registration_date TEXT, source_from_tag INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE reg_events (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, "
        "event TEXT, event_city TEXT, ts TEXT)"
    )
    conn.execute(
        "CREATE TABLE reg_started (telegram_id INTEGER PRIMARY KEY, event_city TEXT)"
    )
    conn.commit()
    return conn


def _make_log(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def test_parse_since_accepts_common_formats():
    assert parse_since("2026-09-05 16:04").strftime("%Y-%m-%d %H:%M:%S") == "2026-09-05 16:04:00"
    assert parse_since("2026-09-05 16:04:30").second == 30
    assert parse_since("2026-09-05").hour == 0


def test_parse_since_rejects_garbage():
    with pytest.raises(SystemExit):
        parse_since("не дата")


def test_parse_log_takes_last_tag_and_referrer_after_since(tmp_path):
    log_path = tmp_path / "bot.log"
    _make_log(log_path, [
        # ДО since — обязано быть проигнорировано.
        "2026-09-05 15:00:00,000 - handlers.registration - INFO - Saved source_tag=old_tag for user 111",
        # ПОСЛЕ since — последняя строка на пользователя должна победить.
        "2026-09-05 16:10:00,123 - handlers.registration - INFO - Saved source_tag=website_1 for user 111",
        "2026-09-05 16:20:00,456 - handlers.registration - INFO - Saved source_tag=website_2 for user 111",
        "2026-09-05 16:15:00,789 - handlers.registration - INFO - Saved referrer_id=555 for user 222",
        # мусорная строка без метки времени в начале (продолжение traceback) — не должна падать
        "  File \"handlers/registration.py\", line 1330, in _start_registration_flow",
    ])
    since_dt = parse_since("2026-09-05 16:04")

    source_tags, referrers = parse_log(str(log_path), since_dt)

    assert source_tags == {111: "website_2"}
    assert referrers == {222: 555}


def test_plan_changes_covers_city_source_and_referrer(tmp_path):
    db_path = tmp_path / "forum.db"
    conn = _make_db(str(db_path))

    # 100: event_city NULL -> должен подтянуться из reg_events (самое свежее непустое).
    conn.execute(
        "INSERT INTO users (telegram_id, event_city, source, referrer_id, registration_date) "
        "VALUES (100, NULL, 'Реферальная ссылка', 42, '2026-09-05 17:00:00')"
    )
    conn.execute(
        "INSERT INTO reg_events (telegram_id, event, event_city, ts) VALUES "
        "(100, 'form_started', 'msk', '2026-09-05 16:30:00'), "
        "(100, 'form_completed', 'spb', '2026-09-05 17:00:05')"
    )

    # 101: event_city NULL и reg_events пуст -> фоллбэк на reg_started.
    conn.execute(
        "INSERT INTO users (telegram_id, event_city, source, referrer_id, registration_date) "
        "VALUES (101, NULL, 'friend', NULL, '2026-09-05 18:00:00')"
    )
    conn.execute("INSERT INTO reg_started (telegram_id, event_city) VALUES (101, 'ekb')")

    # 102: source == 'Самостоятельно', есть тег в логе -> подмена + source_from_tag.
    conn.execute(
        "INSERT INTO users (telegram_id, event_city, source, referrer_id, registration_date) "
        "VALUES (102, 'msk', 'Самостоятельно', NULL, '2026-09-05 19:00:00')"
    )

    # 103: до since — НЕ должен попасть в выборку вовсе.
    conn.execute(
        "INSERT INTO users (telegram_id, event_city, source, referrer_id, registration_date) "
        "VALUES (103, NULL, 'Самостоятельно', NULL, '2026-09-05 10:00:00')"
    )
    conn.commit()

    source_tags = {102: "website_1"}
    referrers = {}

    changes = plan_changes(conn, "2026-09-05 16:04", source_tags, referrers)
    by_uid = {c["telegram_id"]: c for c in changes}

    assert set(by_uid) == {100, 101, 102}
    assert by_uid[100]["event_city"] == (None, "spb")  # самое свежее по ts, не по event-порядку
    assert by_uid[101]["event_city"] == (None, "ekb")
    assert by_uid[102]["source"] == ("Самостоятельно", "website_1")
    assert by_uid[102]["source_from_tag"] == (None, 1)
    assert "referrer_id" not in by_uid[100]  # уже был непустой — трогать нечего


def test_apply_changes_writes_and_is_one_transaction(tmp_path):
    db_path = tmp_path / "forum.db"
    conn = _make_db(str(db_path))
    conn.execute(
        "INSERT INTO users (telegram_id, event_city, source, referrer_id, registration_date) "
        "VALUES (200, NULL, 'Самостоятельно', NULL, '2026-09-05 20:00:00')"
    )
    conn.commit()

    changes = [{
        "telegram_id": 200,
        "event_city": (None, "spb"),
        "source": ("Самостоятельно", "website_9"),
        "source_from_tag": (None, 1),
    }]
    apply_changes(conn, changes)

    row = conn.execute(
        "SELECT event_city, source, source_from_tag FROM users WHERE telegram_id = 200"
    ).fetchone()
    assert row == ("spb", "website_9", 1)


def test_dry_run_does_not_touch_db_end_to_end(tmp_path, capsys):
    db_path = tmp_path / "forum.db"
    log_path = tmp_path / "bot.log"
    conn = _make_db(str(db_path))
    conn.execute(
        "INSERT INTO users (telegram_id, event_city, source, referrer_id, registration_date) "
        "VALUES (300, NULL, 'Самостоятельно', NULL, '2026-09-05 20:00:00')"
    )
    conn.execute(
        "INSERT INTO reg_events (telegram_id, event, event_city, ts) "
        "VALUES (300, 'form_completed', 'spb', '2026-09-05 20:00:00')"
    )
    conn.commit()
    conn.close()
    _make_log(log_path, [
        "2026-09-05 20:00:00,000 - handlers.registration - INFO - Saved source_tag=website_9 for user 300",
    ])

    main([
        "--db", str(db_path), "--log", str(log_path), "--since", "2026-09-05 16:04",
    ])

    captured = capsys.readouterr()
    assert "БУДЕТ ИЗМЕНЕНО" in captured.out

    check = sqlite3.connect(str(db_path))
    row = check.execute(
        "SELECT event_city, source FROM users WHERE telegram_id = 300"
    ).fetchone()
    check.close()
    assert row == (None, "Самостоятельно"), "dry-run без --apply не должен ничего писать"


def test_apply_flag_end_to_end_writes_through_main(tmp_path, capsys):
    db_path = tmp_path / "forum.db"
    log_path = tmp_path / "bot.log"
    conn = _make_db(str(db_path))
    conn.execute(
        "INSERT INTO users (telegram_id, event_city, source, referrer_id, registration_date) "
        "VALUES (400, NULL, 'Самостоятельно', NULL, '2026-09-05 20:00:00')"
    )
    conn.execute(
        "INSERT INTO reg_events (telegram_id, event, event_city, ts) "
        "VALUES (400, 'form_completed', 'spb', '2026-09-05 20:00:00')"
    )
    conn.commit()
    conn.close()
    _make_log(log_path, [
        "2026-09-05 20:00:00,000 - handlers.registration - INFO - Saved source_tag=website_9 for user 400",
    ])

    main([
        "--db", str(db_path), "--log", str(log_path), "--since", "2026-09-05 16:04", "--apply",
    ])

    captured = capsys.readouterr()
    assert "ПРИМЕНЕНО" in captured.out

    check = sqlite3.connect(str(db_path))
    row = check.execute(
        "SELECT event_city, source, source_from_tag FROM users WHERE telegram_id = 400"
    ).fetchone()
    check.close()
    assert row == ("spb", "website_9", 1)
