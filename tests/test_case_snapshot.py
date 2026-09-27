"""`tools/case_snapshot.py` — read-only снимок цифр кейса (M1–M12) по БД стека.

Скрипт запускается в контейнере бота через stdin (`python - /app/data/forum.db ...`), поэтому
зависит только от stdlib и грузится здесь по пути файла, а не импортом пакета. Фикстура — та же
схема, что у бота (`fast_init_db` = `init_db`), наполненная синхронным sqlite3.
"""
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from config import config
from tests._dbtpl import fast_init_db

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "case_snapshot.py"


def _load():
    spec = importlib.util.spec_from_file_location("case_snapshot_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _insert(conn, table, row):
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))


def _full_db(tmp_path) -> str:
    path = str(tmp_path / "case.db")
    config.DB_PATH = path
    fast_init_db()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO bot_settings (key, value) VALUES ('event_season', 'YL 26/2') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.execute("DELETE FROM cities")
    for code, enabled in (("msk", 1), ("spb", 1), ("tyumen", 0)):
        _insert(conn, "cities", {"code": code, "label": code, "enabled": enabled, "sort_order": 1})
    users = [
        dict(telegram_id=1, status="approved", season="YL 26/2", event_city="msk",
             registration_date="2026-09-01 10:00:00", is_ambassador=1),
        dict(telegram_id=2, status="pending", season="YL 26/2", event_city="spb",
             registration_date="2026-09-02 10:00:00", referrer_id=1),
        dict(telegram_id=3, status="rejected", season="YL 26/2", event_city="msk",
             registration_date="2026-09-03 10:00:00", receipt_file_id="file-3",
             payment_status="pending", referrer_id=1),
        # легаси-строка без сезона: как и на дашборде, считается текущим сезоном
        dict(telegram_id=4, status="approved", season=None, event_city="msk",
             registration_date="2026-08-20 10:00:00"),
        # импорт прошлого сезона: свой сезон, без города мероприятия
        dict(telegram_id=5, status="approved", season="YL 26/1", event_city=None,
             registration_date="2026-03-01 10:00:00"),
        dict(telegram_id=6, status="rejected", season="YL 26/1", event_city=None,
             registration_date=None),
    ]
    for u in users:
        _insert(conn, "users", u)

    decisions = [
        (1, "approved", "2026-09-01 11:00:00", "2026-09-01 11:05:00"),  # отменено — не в счёт
        (1, "approved", "2026-09-01 12:00:00", None),   # 2 ч
        (3, "rejected", "2026-09-03 20:00:00", None),   # 10 ч
        (4, "approved", "2026-08-21 10:00:00", None),   # 24 ч
        (4, "approved", "2026-08-22 10:00:00", None),   # второе решение — берётся первое
        (5, "approved", "2026-03-01 11:00:00", None),   # прошлый сезон — вне скоупа
    ]
    for tid, dec, at, undone in decisions:
        _insert(conn, "application_decisions", dict(
            telegram_id=tid, decision=dec, decided_by=100, decided_at=at,
            effects_due_at=at, undone_at=undone))
    _insert(conn, "auto_reject_log", dict(
        telegram_id=3, rule_ids="[1]", reject_texts="[]", first_triggered_at="2026-09-03 10:00:00",
        last_triggered_at="2026-09-03 10:00:00"))

    events = [
        (1, "start", "2026-08-31 09:00:00"), (1, "form_started", "2026-08-31 09:01:00"),
        (1, "form_completed", "2026-09-01 10:00:00"),
        (2, "start", "2026-09-01 09:00:00"), (2, "form_started", "2026-09-01 09:01:00"),
        (2, "nudged", "2026-09-01 12:00:00"), (2, "form_completed", "2026-09-02 10:00:00"),
        (7, "start", "2026-09-04 09:00:00"), (7, "start", "2026-09-04 09:05:00"),
        (7, "form_started", "2026-09-04 09:06:00"), (7, "nudged", "2026-09-05 12:00:00"),
        (8, "start", "2026-09-06 09:00:00"),
    ]
    for tid, ev, ts in events:
        _insert(conn, "reg_events", dict(telegram_id=tid, event=ev, season="YL 26/2", ts=ts,
                                          event_city=None if ev == "start" else ("spb" if tid == 2 else "msk")))
    _insert(conn, "reg_started", dict(telegram_id=7, started_at="2026-09-04 09:00:00",
                                       nudged_at="2026-09-05 12:00:00", event_city="msk"))
    _insert(conn, "reg_started", dict(telegram_id=9, started_at="2026-09-06 09:00:00", event_city="msk"))

    _insert(conn, "broadcasts", dict(id=1, status="done"))
    for chat, mid in ((1, 10), (1, 11), (2, 12)):
        _insert(conn, "broadcast_deliveries", dict(broadcast_id=1, chat_id=chat, message_id=mid,
                                                   sent_at="2026-09-10 10:00:00"))
    _insert(conn, "scheduled_broadcasts", dict(id=1, scheduled_at="2026-09-05 10:00:00", status="sent"))
    _insert(conn, "scheduled_broadcasts", dict(id=2, scheduled_at="2026-09-10 10:00:00", status="sent",
                                                log_broadcast_id=1))
    for bid, chat, st in ((1, 1, "ok"), (1, 2, "ok"), (1, 3, "failed"), (2, 1, "ok")):
        _insert(conn, "scheduled_broadcast_deliveries", dict(broadcast_id=bid, chat_id=chat, status=st))

    for tid, point in ((1, "entry"), (4, "entry"), (1, "session:5"), (1, "session:6"), (4, "session:5")):
        _insert(conn, "checkins", dict(telegram_id=tid, point=point, scanned_at="2026-10-03 09:00:00",
                                       source="miniapp", created_at="2026-10-03 09:00:00", day="2026-10-03"))

    _insert(conn, "sos_reports", dict(telegram_id=1, city="msk", category="health",
                                      created_at="2026-10-03 10:00:00", claimed_at="2026-10-03 10:10:00",
                                      resolved_at="2026-10-03 10:30:00"))
    _insert(conn, "sos_reports", dict(telegram_id=4, city="msk", category="lost",
                                      created_at="2026-10-03 11:00:00"))

    for invitee in (2, 3):
        _insert(conn, "referral_credits", dict(invitee_id=invitee, referrer_id=1, coins=10,
                                                credited_at="2026-09-05 10:00:00"))
    conn.commit()
    conn.close()
    return path


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_full_snapshot_numbers(tmp_path):
    cs = _load()
    path = _full_db(tmp_path)
    with cs.open_ro(path) as conn:
        data = cs.collect(conn, season="YL 26/2")

    m1 = data["M1"]
    assert m1["total"] == 4
    assert m1["by_status"] == {"approved": 2, "pending": 1, "rejected": 1}
    assert m1["other_seasons"] == {"YL 26/1": 2}

    m2 = data["M2"]
    assert m2["cities_enabled"] == ["msk", "spb"]
    assert m2["by_event_city"] == {"msk": 3, "spb": 1}

    m3 = data["M3"]
    assert (m3["start"], m3["form_started"], m3["form_completed"]) == (4, 3, 2)
    assert m3["since"] == "2026-08-31 09:00:00"
    assert m3["conversion_pct"] == 50.0

    m4 = data["M4"]
    assert m4["nudged"] == 2
    assert m4["completed_after_nudge"] == 1
    assert m4["pending_nudged"] == 1

    m5 = data["M5"]
    assert m5["decided"] == 3
    assert m5["median_hours"] == 10.0
    assert m5["p90_hours"] == 24.0

    m6 = data["M6"]
    assert m6["decisions"] == 4  # не отменённые, в скоупе сезона (4 решения у 1, 3, 4)
    assert m6["by_decision"] == {"approved": 3, "rejected": 1}
    assert m6["auto_reject_people"] == 1

    m7 = data["M7"]
    assert m7["receipts"] == 1
    assert m7["payment_status"]["pending"] == 1

    m8 = data["M8"]
    assert m8["messages"] == 3
    assert m8["recipient_deliveries"] == 2
    assert m8["scheduled_unlogged_ok"] == 2
    assert m8["total_deliveries"] == 4

    m9 = data["M9"]
    assert m9["arrived"] == 2
    assert m9["approved"] == 2
    assert m9["arrived_pct"] == 100.0
    assert m9["by_day"] == {"2026-10-03": 2}
    assert m9["by_city"] == {"msk": 2}

    m10 = data["M10"]
    assert (m10["marks"], m10["delegates"], m10["avg_per_delegate"]) == (3, 2, 1.5)

    m11 = data["M11"]
    assert m11["total"] == 2
    assert m11["claimed"] == 1
    assert m11["median_minutes_to_claim"] == 10.0

    m12 = data["M12"]
    assert m12["ambassadors"] == 1
    assert m12["referral_credits"] == 2
    assert m12["referrers"] == 1


def test_city_scope(tmp_path):
    cs = _load()
    path = _full_db(tmp_path)
    with cs.open_ro(path) as conn:
        data = cs.collect(conn, season="YL 26/2", city="spb")
    assert data["M1"]["total"] == 1
    assert data["M1"]["by_status"] == {"pending": 1}
    assert data["M3"]["form_completed"] == 1


def test_default_season_comes_from_settings(tmp_path):
    cs = _load()
    path = _full_db(tmp_path)
    with cs.open_ro(path) as conn:
        data = cs.collect(conn)
    assert data["meta"]["season"] == "YL 26/2"
    assert data["M1"]["total"] == 4


def test_no_payment_reported_humanly(tmp_path):
    cs = _load()
    path = _full_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE users SET receipt_file_id = NULL, payment_status = 'not_paid'")
    conn.commit()
    conn.close()
    with cs.open_ro(path) as conn:
        data = cs.collect(conn, season="YL 26/2")
    assert data["M7"]["receipts"] == 0
    assert "нет оплаты" in cs.render_md(data)


def test_missing_tables_and_columns_tolerated(tmp_path):
    cs = _load()
    path = str(tmp_path / "bare.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'approved'), (2, 'pending')")
    conn.commit()
    conn.close()

    with cs.open_ro(path) as conn:
        data = cs.collect(conn, season="YL 26/2")
    assert data["M1"]["total"] == 2  # колонки season нет — берутся все, с пометкой
    assert data["M1"]["note"]
    for key in ("M3", "M4", "M5", "M6", "M8", "M9", "M10", "M11"):
        assert data[key]["available"] is False, key
    md = cs.render_md(data)
    assert "M9" in md


def test_read_only_no_writes(tmp_path):
    cs = _load()
    path = _full_db(tmp_path)
    before = _digest(path)
    with cs.open_ro(path) as conn:
        cs.collect(conn, season="YL 26/2")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO users (telegram_id) VALUES (999)")
    assert _digest(path) == before


def test_runs_from_stdin_like_docker_exec(tmp_path):
    path = _full_db(tmp_path)
    before = _digest(path)
    src = SCRIPT.read_text(encoding="utf-8")
    for fmt in ("json", "md"):
        proc = subprocess.run(
            [sys.executable, "-", path, "--season", "YL 26/2", "--label", "S0", "--format", fmt],
            input=src, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        if fmt == "json":
            data = json.loads(proc.stdout)
            assert data["meta"]["label"] == "S0"
            assert data["M1"]["total"] == 4
        else:
            assert "Заявок подано" in proc.stdout
    assert _digest(path) == before


def test_script_is_stdlib_only():
    src = SCRIPT.read_text(encoding="utf-8")
    allowed = {"argparse", "json", "sqlite3", "sys", "math", "statistics", "datetime",
               "pathlib", "contextlib", "collections", "__future__", "urllib", "os"}
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("import ") or s.startswith("from "):
            mod = s.split()[1].split(".")[0]
            assert mod in allowed, line
