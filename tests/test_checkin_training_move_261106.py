"""Сканы точки «🧪 Тренировка» пишутся в журнал и переносятся на сессию дня.

Настоящий QR допущенного делегата своего города — строка `training_scan_log`, не `checkins`.
`TRAIN-*`, отказ и поиск по имени в журнал не попадают. В день форума города точка пропадает
из списка сканера; накануне и без даты форума остаётся. Перенос идёт через
`record_session_checkin` и снимает строки журнала, поэтому второй заход никого не дублирует.
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite

from config import config as bot_config
from database import db as bot_db
from domain.cities import city_label, per_city_key
from handlers.forum.admin_checkin import render_admin_checkin
from handlers.forum.admin_checkin_training_move import (
    training_move_go,
    training_move_open,
    training_move_pick,
)
from services.forum import checkin_training
from tests.test_admin_checkin_260924 import _FakeCallback
from tests.test_miniapp_checkin_260924 import (
    BASE,
    BOUND_MANAGER_ID,
    GAME_MANAGER_ID,
    _freeze_now,
    _grant_checkin_to_bound_manager,
    _grant_checkin_to_game_manager,
    _hdr,
    _insert_user,
    _qr,
    _run,
    client_with,
)
from tests.test_miniapp_routes import _set

ANNA, BORIS, VERA = 961001, 961002, 961003
PENDING, MANUAL, FOREIGN_CITY = 961004, 961005, 961006
DAY = datetime(2026, 10, 3, 10, 0)
LATER = datetime(2026, 10, 3, 10, 40)


def _rows(sql: str, args: tuple = ()):
    async def _q():
        async with bot_db._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(sql, args) as cur:
                return [dict(r) for r in await cur.fetchall()]
    return _run(_q())


def _log():
    return _rows(
        "SELECT telegram_id, day, scanned_at, city, by_staff_id "
        "FROM training_scan_log ORDER BY telegram_id"
    )


def _kb(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _scan(client, uid, *, name, point="training", city="spb"):
    return client.post(
        f"{BASE}/scan",
        json={"payload": _qr(uid, full_name=name), "point": point},
        headers=_hdr(BOUND_MANAGER_ID),
    ).json()


def _ready(tmp_path, monkeypatch, now: datetime):
    # Привязанный менеджер не должен стать суперадмином из .env: город берётся из привязки.
    bot_config.ADMIN_IDS = [1]
    client = client_with(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _grant_checkin_to_bound_manager()
    _grant_checkin_to_game_manager()
    _freeze_now(monkeypatch, now)
    return client


def test_real_qr_is_logged_training_token_is_not(tmp_path, monkeypatch):
    client = _ready(tmp_path, monkeypatch, DAY)
    _run(_insert_user(ANNA, full_name="Анна Анина", city="spb"))
    _run(_insert_user(PENDING, full_name="Ждёт Решения", city="spb", status="pending"))
    _run(_insert_user(MANUAL, full_name="Поиск Поисков", city="spb"))
    _run(_insert_user(FOREIGN_CITY, full_name="Чужой Город", city="msk"))

    body = _scan(client, ANNA, name="Анна Анина")
    assert body["status"] == "new"
    assert _rows("SELECT point FROM checkins") == []
    logged = _log()
    assert len(logged) == 1
    assert logged[0]["telegram_id"] == ANNA
    assert logged[0]["day"] == "2026-10-03"
    assert logged[0]["scanned_at"] == "2026-10-03 10:00:00"
    assert logged[0]["city"] == "spb"
    assert logged[0]["by_staff_id"] == BOUND_MANAGER_ID

    _freeze_now(monkeypatch, LATER)
    again = _scan(client, ANNA, name="Анна Анина")
    assert again["status"] == "new"  # в checkins по-прежнему пусто, плашка не «уже был»
    logged = _log()
    assert len(logged) == 1
    assert logged[0]["scanned_at"] == "2026-10-03 10:00:00"

    train = _run(checkin_training.training_payloads())
    for _kind, payload in train:
        client.post(
            f"{BASE}/scan", json={"payload": payload, "point": "training"},
            headers=_hdr(BOUND_MANAGER_ID),
        )
    assert [r["telegram_id"] for r in _log()] == [ANNA]

    denied = _scan(client, PENDING, name="Ждёт Решения")
    assert denied["status"] == "denied"
    manual = client.post(
        f"{BASE}/manual", json={"telegram_id": MANUAL, "point": "training"},
        headers=_hdr(BOUND_MANAGER_ID),
    ).json()
    assert manual["status"] == "new"
    wrong = _scan(client, FOREIGN_CITY, name="Чужой Город")
    assert wrong["status"] == "wrong_city"
    assert [r["telegram_id"] for r in _log()] == [ANNA]
    assert _rows("SELECT point FROM checkins") == []


def _points(client, when: datetime, monkeypatch):
    _freeze_now(monkeypatch, when)
    return client.get(f"{BASE}/points", headers=_hdr(BOUND_MANAGER_ID)).json()["points"]


def test_training_point_hidden_on_forum_day_visible_day_before(tmp_path, monkeypatch):
    client = _ready(tmp_path, monkeypatch, DAY)
    assert [p["point"] for p in _points(client, DAY, monkeypatch)][:2] == ["entry", "training"]

    _run(bot_db.set_setting(per_city_key("forum_date", "spb"), "03.10.2026"))
    _run(bot_db.set_setting(per_city_key("sos_active_days", "spb"), "1"))
    before = _points(client, datetime(2026, 10, 2, 18, 0), monkeypatch)
    assert before[0]["point"] == "entry"
    assert "training" in [p["point"] for p in before]

    sid = _run(bot_db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    on_day = _points(client, DAY, monkeypatch)
    codes = [p["point"] for p in on_day]
    assert codes[0] == "entry"
    assert "training" not in codes
    assert f"session:{sid}" in codes


def test_move_marks_once_and_second_pass_does_not_duplicate(tmp_path, monkeypatch):
    client = _ready(tmp_path, monkeypatch, DAY)
    _run(_insert_user(ANNA, full_name="Анна Анина", city="spb"))
    _run(_insert_user(BORIS, full_name="Борис Борисов", city="spb"))
    _run(_insert_user(VERA, full_name="Вера Верина", city="spb"))
    small = _run(bot_db.create_program_hall("spb", "Малый зал"))
    big = _run(bot_db.create_program_hall("spb", "Большой зал"))
    target = _run(bot_db.create_program_session(
        "spb", "2026-10-03", "10:00", "11:30", "Открытие", hall_id=small,
    ))
    other = _run(bot_db.create_program_session(
        "spb", "2026-10-03", "11:00", "12:00", "Воркшоп", hall_id=big,
    ))

    manager_kb = _kb(_run(render_admin_checkin(BOUND_MANAGER_ID))[1])
    assert ("Перенести сканы с тренировки", "checkin_training_move") in manager_kb
    game_data = [data for _text, data in _kb(_run(render_admin_checkin(GAME_MANAGER_ID))[1])]
    assert "checkin_training_move" not in game_data

    empty = _FakeCallback("checkin_training_move", BOUND_MANAGER_ID)
    _run(training_move_open(empty))
    assert "Переносить некого" in empty.message.sent[-1][0]
    assert not any(data.startswith("ctm_go:") for _t, data in _kb(empty.message.sent[-1][1]))

    for uid, name in ((ANNA, "Анна Анина"), (BORIS, "Борис Борисов"), (VERA, "Вера Верина")):
        assert _scan(client, uid, name=name)["status"] == "new"
    _run(bot_db.record_session_checkin(
        BORIS, target, [], source="miniapp", scanned_at="2026-10-03 08:00:00",
    ))
    _run(bot_db.record_session_checkin(
        VERA, other, [], source="miniapp", scanned_at="2026-10-03 08:05:00",
    ))

    opened = _FakeCallback("checkin_training_move", BOUND_MANAGER_ID)
    _run(training_move_open(opened))
    text, markup = opened.message.sent[-1]
    assert "3 человека" in text
    for name in ("Анна Анина", "Борис Борисов", "Вера Верина"):
        assert name in text
    for uid in (ANNA, BORIS, VERA):
        assert str(uid) not in text
    assert _run(city_label("spb")) in text
    assert "spb" not in text
    assert "session:" not in text
    buttons = _kb(markup)
    assert any("Малый зал" in label and "10:00" in label and "Открытие" in label for label, _d in buttons)
    assert any("Большой зал" in label and "11:00" in label and "Воркшоп" in label for label, _d in buttons)
    assert not any(data.startswith("ctm_go:") for _t, data in buttons)
    assert all("session:" not in label for label, _d in buttons)

    pick = _FakeCallback(f"ctm_pick:{target}", BOUND_MANAGER_ID)
    _run(training_move_pick(pick))
    confirm, confirm_kb = pick.message.sent[-1]
    assert "будут отмечены" in confirm
    assert "Повторное нажатие их второй раз не отметит" in confirm
    assert "Статус заявки не меняется" in confirm
    assert "Малый зал" in confirm
    assert any(data == f"ctm_go:{target}" for _t, data in _kb(confirm_kb))

    go = _FakeCallback(f"ctm_go:{target}", BOUND_MANAGER_ID)
    _run(training_move_go(go))
    result = go.message.sent[-1][0]
    assert "Новые: 1" in result
    assert "Уже были на этой сессии: 1" in result
    assert "Перенесены с параллельной сессии того же слота: 1" in result

    def _points_of(uid):
        return _rows(
            "SELECT point, source, scanned_at, by_staff_id FROM checkins WHERE telegram_id = ?",
            (uid,),
        )

    anna = _points_of(ANNA)
    assert anna == [{
        "point": f"session:{target}", "source": "training",
        "scanned_at": "2026-10-03 10:00:00", "by_staff_id": BOUND_MANAGER_ID,
    }]
    boris = _points_of(BORIS)
    assert len(boris) == 1 and boris[0]["point"] == f"session:{target}"
    assert boris[0]["scanned_at"] == "2026-10-03 08:00:00"
    vera = _points_of(VERA)
    assert vera == [{
        "point": f"session:{target}", "source": "training",
        "scanned_at": "2026-10-03 10:00:00", "by_staff_id": BOUND_MANAGER_ID,
    }]
    assert _rows("SELECT point FROM checkins WHERE point = 'entry'") == []
    assert _log() == []
    for uid in (ANNA, BORIS, VERA):
        assert _rows("SELECT status FROM users WHERE telegram_id = ?", (uid,))[0]["status"] == "approved"

    again = _FakeCallback(f"ctm_go:{target}", BOUND_MANAGER_ID)
    _run(training_move_go(again))
    assert "Переносить некого" in again.message.sent[-1][0]
    assert len(_rows("SELECT point FROM checkins")) == 3


def test_move_without_one_city_asks_to_pick_header(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch, DAY)
    _set("admin_city__1", "*")
    bot_config.ADMIN_IDS = [1]
    cb = _FakeCallback("checkin_training_move", 1)
    _run(training_move_open(cb))
    assert "один город" in cb.message.sent[-1][0]
    assert not any(data.startswith("ctm_go:") for _t, data in _kb(cb.message.sent[-1][1]))


def _seed_move(client):
    _run(_insert_user(ANNA, full_name="Анна Анина", city="spb"))
    _run(_insert_user(BORIS, full_name="Борис Борисов", city="spb"))
    small = _run(bot_db.create_program_hall("spb", "Малый зал"))
    target = _run(bot_db.create_program_session(
        "spb", "2026-10-03", "10:00", "11:30", "Открытие", hall_id=small,
    ))
    other_day = _run(bot_db.create_program_session(
        "spb", "2026-10-04", "10:00", "11:00", "Завтра", hall_id=small,
    ))
    for uid, name in ((ANNA, "Анна Анина"), (BORIS, "Борис Борисов")):
        assert _scan(client, uid, name=name)["status"] == "new"
    return target, other_day


def test_app_preview_does_not_mark_and_confirm_marks_once(tmp_path, monkeypatch):
    from services.forum.checkin_training_move import EMPTY

    client = _ready(tmp_path, monkeypatch, DAY)
    target, other_day = _seed_move(client)
    headers = _hdr(BOUND_MANAGER_ID)
    path = "/app/api/forum-day/training-move"

    opened = client.get(path, headers=headers)
    assert opened.status_code == 200
    screen = opened.json()
    assert screen["pick_city"] is False
    assert {person["name"] for person in screen["people"]} == {"Анна Анина", "Борис Борисов"}
    assert all("telegram_id" not in person for person in screen["people"])
    assert _run(city_label("spb")) in screen["text"]
    assert "spb" not in screen["text"]
    assert "session:" not in screen["text"]
    labels = [session["label"] for session in screen["sessions"]]
    assert "Малый зал · 10:00–11:30 · Открытие" in labels
    assert all("session:" not in label and "Завтра" not in label for label in labels)

    preview = client.post(path, json={"session_id": target, "confirm": False}, headers=headers)
    assert preview.status_code == 200
    body = preview.json()
    assert body["need_confirm"] is True
    assert "Анна Анина" in body["text"]
    assert "будут отмечены" in body["text"]
    assert "Повторное нажатие их второй раз не отметит" in body["text"]
    assert "Статус заявки не меняется" in body["text"]
    assert _rows("SELECT point FROM checkins") == []
    assert len(_log()) == 2

    foreign = client.post(path, json={"session_id": other_day, "confirm": True}, headers=headers)
    assert foreign.status_code == 200
    assert foreign.json()["ok"] is False
    assert "сегодняшнего дня" in foreign.json()["text"]
    assert _rows("SELECT point FROM checkins") == []
    assert len(_log()) == 2

    done = client.post(path, json={"session_id": target, "confirm": True}, headers=headers)
    assert done.status_code == 200
    result = done.json()
    assert result["ok"] is True
    assert "Новые: 2" in result["text"]
    assert "Уже были на этой сессии: 0" in result["text"]
    assert _log() == []
    marks = _rows("SELECT telegram_id, point, source FROM checkins ORDER BY telegram_id")
    assert marks == [
        {"telegram_id": ANNA, "point": f"session:{target}", "source": "training"},
        {"telegram_id": BORIS, "point": f"session:{target}", "source": "training"},
    ]
    for uid in (ANNA, BORIS):
        assert _rows("SELECT status FROM users WHERE telegram_id = ?", (uid,))[0]["status"] == "approved"

    again = client.post(path, json={"session_id": target, "confirm": True}, headers=headers)
    assert again.json()["ok"] is False
    assert again.json()["text"] == EMPTY
    assert len(_rows("SELECT point FROM checkins")) == 2


def test_app_all_cities_does_not_transfer(tmp_path, monkeypatch):
    from services.forum.checkin_training_move import app_screen, move_decision

    client = _ready(tmp_path, monkeypatch, DAY)
    target, _other = _seed_move(client)
    _set("admin_city__1", "*")
    bot_config.ADMIN_IDS = [1]

    screen = _run(app_screen(1))
    assert screen["pick_city"] is True
    assert screen["people"] == []
    assert screen["sessions"] == []
    assert "один город" in screen["text"]

    decision = _run(move_decision(1, target, confirm=True))
    assert decision["ok"] is False
    assert "один город" in decision["text"]
    assert _rows("SELECT point FROM checkins") == []
    assert len(_log()) == 2


def test_training_move_without_cap_says_what_to_ask(tmp_path, monkeypatch):
    from services.forum.checkin_training_move import NEED_CAP

    client = _ready(tmp_path, monkeypatch, DAY)
    resp = client.get("/app/api/forum-day/training-move", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    body = resp.json()
    assert body == {"reason": "no_cap", "text": NEED_CAP}
    assert "forbidden" not in body["text"].lower()
    assert "moderate_reg" not in body["text"]


def test_chat_move_stays_and_site_only_links():
    from pathlib import Path

    from dashboard.forum_day import TRAINING_MOVE_HREF, training_move_page
    from handlers.access.admin_caps import ADMIN_CAPS
    from services.forum.checkin_training_move import NEED_CAP

    assert training_move_open is not None
    assert training_move_pick is not None
    assert training_move_go is not None
    assert ADMIN_CAPS["checkin_training_move"] == "moderate_reg"
    assert ADMIN_CAPS["ctm_go:*"] == "moderate_reg"
    assert "forbidden" not in NEED_CAP.lower()
    assert "moderate_reg" not in NEED_CAP
    page = training_move_page()
    assert TRAINING_MOVE_HREF == "/app/#/training-move"
    assert TRAINING_MOVE_HREF in page
    source = Path("dashboard/forum_day.py").read_text(encoding="utf-8")
    assert "SELECT" not in source
    assert "transfer_training_scans" not in source
    handler = Path("handlers/forum/admin_checkin_training_move.py").read_text(encoding="utf-8")
    assert 'F.data == "checkin_training_move"' in handler
    assert 'F.data.startswith("ctm_go:")' in handler
    screen = Path("miniapp/static/js/screens/training_move.js").read_text(encoding="utf-8")
    assert "innerHTML" not in screen
    assert "text: session.label" in screen
    assert "<input" not in screen and "textarea" not in screen
