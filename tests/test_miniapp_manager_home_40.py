"""Phase 40: цифры регистраций и отметки площадки в Mini App — только агрегаты.

Харнесс — `tests/test_miniapp_routes.py`. Боевую базу не открываем.
`GET /app/api/stats/game` остаётся статистикой геймы.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from datetime import datetime
from pathlib import Path

from database import db as bot_db
from database import ext_forms_db
from domain.cities import city_scope
from services.forum import checkin_arrival
from services.sheets.sheet_target import sheet_id

from tests.test_miniapp_routes import (
    ADMIN_ID,
    DELEGATE_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _use_tmp_db,
)

STATS_CITY_ID = 900701  # stats_manager, привязан к spb
FLOOR_SPB_ID = 900702  # volunteer, привязан к spb
FLOOR_MSK_ID = 900703  # volunteer, привязан к msk
REG_PATH = "/app/api/stats/registrations"
GAME_PATH = "/app/api/stats/game"
FLOOR_PATH = "/app/api/checkin/floor"
SEASON = "YL26"
FLOOR_NOW = datetime(2026, 11, 4, 15, 0, 0)

# Ключи, которых в агрегатах быть не должно (T-40-01): люди, супердашборд, код города.
FORBIDDEN_KEYS = {
    "username", "phone", "telegram_id", "full_name", "dashboard", "sheet_id",
    "code", "city", "email", "user_id", "first_name", "last_name",
}
_SECRET_TEXT = (
    "Секретная Фамилия", "leak_nick", "+79990001122", "Прошлый Делегат",
    str(910001), str(910010),
)
_CITY_CODES = ("msk", "spb", "tyumen")


def _run(coro):
    return asyncio.run(coro)


def _all_keys(value, acc=None) -> set:
    acc = set() if acc is None else acc
    if isinstance(value, dict):
        for key, item in value.items():
            acc.add(key)
            _all_keys(item, acc)
    elif isinstance(value, list):
        for item in value:
            _all_keys(item, acc)
    return acc


def _strings(value, acc=None) -> list:
    acc = [] if acc is None else acc
    if isinstance(value, str):
        acc.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, acc)
    elif isinstance(value, list):
        for item in value:
            _strings(item, acc)
    return acc


async def _insert_user(
    telegram_id, *, city, status="approved", university=None, season=SEASON,
    prev_season=None, username=None, phone=None, full_name=None,
):
    async with bot_db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, phone, status, season, "
            "prev_season, event_city, university, registration_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                telegram_id, full_name, username, phone, status, season, prev_season,
                city, university, "2026-01-01 00:00:00",
            ),
        )
        await conn.commit()


def _seed_people():
    """Текущий сезон: МГУ 4 (включая NULL-город → Москва), ВШЭ 2, ИТМО 2, ОмГУ 1.
    Один повторный в Москве, один человек прошлого сезона в СПб."""
    rows = [
        (910001, "msk", "approved", "МГУ", SEASON, "YL25", "leak_nick", "+79990001122", "Секретная Фамилия"),
        (910002, "msk", "approved", "МГУ", SEASON, None, None, None, None),
        (910003, "msk", "pending", "МГУ", SEASON, None, None, None, None),
        (910004, "msk", "approved", "ВШЭ", SEASON, None, None, None, None),
        (910005, "msk", "pending", "ВШЭ", SEASON, None, None, None, None),
        (910006, None, "approved", "МГУ", SEASON, None, None, None, None),
        (910007, "spb", "approved", "ИТМО", SEASON, None, None, None, None),
        (910008, "spb", "approved", "ИТМО", SEASON, None, None, None, None),
        (910009, "tyumen", "approved", "ОмГУ", SEASON, None, None, None, None),
        (910010, "spb", "approved", "Архив", "OLD", None, None, None, "Прошлый Делегат"),
    ]
    for tid, city, status, uni, season, prev, username, phone, full_name in rows:
        _run(_insert_user(
            tid, city=city, status=status, university=uni, season=season,
            prev_season=prev, username=username, phone=phone, full_name=full_name,
        ))


def _ready(tmp_path, *, people=True):
    db_path = _use_tmp_db(tmp_path, "miniapp_manager_home_40.db")
    _seed(
        staff=[
            (GAME_MANAGER_ID, "game_manager", None),
            (STATS_CITY_ID, "stats_manager", "spb"),
            (FLOOR_SPB_ID, "volunteer", "spb"),
            (FLOOR_MSK_ID, "volunteer", "msk"),
            # Суперадмин с привязкой: admin_ids всё равно не сужает цифры и отметки.
            (ADMIN_ID, "stats_manager", "spb"),
        ],
        settings={
            "miniapp_enabled": "on",
            "event_city_enabled": "on",
            "event_season": SEASON,
            "registration_mode": "full",
            "city_label__msk": "Москва",
            "city_label__spb": "Санкт-Петербург",
            "city_label__tyumen": "Тюмень",
        },
    )
    if people:
        _seed_people()
    return _client(_cfg(db_path))


def _no_people(body: dict):
    assert not (_all_keys(body) & FORBIDDEN_KEYS)
    text = json.dumps(body, ensure_ascii=False)
    for secret in _SECRET_TEXT:
        assert secret not in text
    assert not (set(_strings(body)) & set(_CITY_CODES))


def test_game_stats_stays_game_and_registrations_are_a_different_path(tmp_path):
    client = _ready(tmp_path)
    game = client.get(GAME_PATH, headers=_hdr(ADMIN_ID))
    assert game.status_code == 200, game.text
    game_body = game.json()
    assert "participants" in game_body
    assert "universities" not in game_body and "returning" not in game_body
    assert "past_seasons" not in game_body and "cities" not in game_body
    regs = client.get(REG_PATH, headers=_hdr(GAME_MANAGER_ID))
    assert regs.status_code == 403
    assert regs.json() == {"reason": "no_cap", "cap": "stats"}


def test_registration_numbers_for_superadmin_are_aggregates(tmp_path):
    client = _ready(tmp_path)
    resp = client.get(REG_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    total, top = _run(bot_db.get_stats(city_scope=None, season=SEASON))
    assert body["total"] == total == 9
    assert body["returning"] == _run(bot_db.get_returning_count(city_scope=None, season=SEASON)) == 1
    assert body["past_seasons"] == _run(bot_db.count_past_season_users(SEASON, city_scope=None)) == 1
    assert {row["name"]: row["count"] for row in body["universities"]} == {
        "МГУ": 4, "ВШЭ": 2, "ИТМО": 2,
    }
    assert [(row["name"], row["count"]) for row in body["universities"]] == [
        (str(name), int(count)) for name, count in top
    ]
    assert len(body["universities"]) <= 3
    assert "ОмГУ" not in {row["name"] for row in body["universities"]}
    assert body["city_label"] is None
    assert body["cities"] == [
        {"city_label": "Москва", "total": 6, "pending": 2, "approved": 4},
        {"city_label": "Санкт-Петербург", "total": 2, "pending": 0, "approved": 2},
        {"city_label": "Тюмень", "total": 1, "pending": 0, "approved": 1},
    ]
    _no_people(body)
    # Параметр города клиента не сужает суперадмина.
    narrowed = client.get(REG_PATH, params={"city": "spb"}, headers=_hdr(ADMIN_ID))
    assert narrowed.status_code == 200
    assert narrowed.json() == body


def test_city_manager_sees_only_bound_city(tmp_path):
    client = _ready(tmp_path)
    scope = city_scope("spb")
    resp = client.get(REG_PATH, params={"city": "msk"}, headers=_hdr(STATS_CITY_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    total, top = _run(bot_db.get_stats(city_scope=scope, season=SEASON))
    assert body["total"] == total == 2
    assert body["returning"] == _run(bot_db.get_returning_count(city_scope=scope, season=SEASON)) == 0
    assert body["past_seasons"] == _run(bot_db.count_past_season_users(SEASON, city_scope=scope)) == 1
    assert body["universities"] == [{"name": name, "count": int(count)} for name, count in top]
    assert body["city_label"] == "Санкт-Петербург"
    assert body["cities"] == [
        {"city_label": "Санкт-Петербург", "total": 2, "pending": 0, "approved": 2},
    ]
    text = json.dumps(body, ensure_ascii=False)
    assert "Москва" not in text and "Тюмень" not in text
    _no_people(body)


def test_zero_registrations_are_numbers_not_an_empty_object(tmp_path):
    client = _ready(tmp_path, people=False)
    resp = client.get(REG_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["returning"] == 0
    assert body["universities"] == []
    assert "past_seasons" not in body
    assert body["cities"]
    for row in body["cities"]:
        assert row["total"] == 0 and row["pending"] == 0 and row["approved"] == 0
        assert row["city_label"]
    assert body != {}


def test_registrations_without_stats_cap_is_403(tmp_path):
    client = _ready(tmp_path, people=False)
    for user in (GAME_MANAGER_ID, DELEGATE_ID):
        resp = client.get(REG_PATH, headers=_hdr(user))
        assert resp.status_code == 403
        assert resp.json() == {"reason": "no_cap", "cap": "stats"}


def test_registrations_section_off_is_403(tmp_path):
    client = _ready(tmp_path, people=False)
    _set("miniapp_section_stats", "off")
    resp = client.get(REG_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "stats"}


def _freeze_floor(monkeypatch):
    monkeypatch.setattr("services.infra.timeutil.msk_now", lambda: FLOOR_NOW)


async def _insert_entry(telegram_id, stamp="2026-11-04 15:00:00"):
    async with bot_db._connect() as conn:
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (?, 'entry', ?, 'miniapp', ?, ?)",
            (telegram_id, stamp, stamp, stamp[:10]),
        )
        await conn.commit()


def _seed_floor():
    """СПб: двое одобренных, один вход сегодня. Москва: один одобренный со входом.
    Неодобренный в СПб в «одобрено» не входит. Идёт одна сессия, утренняя — нет."""
    _run(_insert_user(920001, city="spb", full_name="На Площадке", username="floor_nick", phone="+79990002233"))
    _run(_insert_user(920002, city="msk", full_name="Московский Гость"))
    _run(_insert_user(920004, city="spb"))
    _run(_insert_user(920003, city="spb", status="pending", full_name="Ещё Не Одобрен"))
    _run(_insert_entry(920001))
    _run(_insert_entry(920002))
    _run(bot_db.create_program_session("spb", "2026-11-04", "14:00", "16:00", "Пленарка"))
    _run(bot_db.create_program_session("spb", "2026-11-04", "09:00", "10:00", "Утренняя"))


def _floor_direct(city: str | None) -> dict:
    scope = city_scope(city) if city is not None else None
    floor = _run(checkin_arrival.floor_report(scope, city, FLOOR_NOW))
    day = _run(checkin_arrival.counter_day())
    arrived, approved = _run(checkin_arrival.arrived_counts(scope, day))
    return {
        "present": int(floor["present"]),
        "arrived": int(arrived),
        "approved": int(approved),
    }


def test_floor_three_numbers_and_live_session_titles(tmp_path, monkeypatch):
    _freeze_floor(monkeypatch)
    client = _ready(tmp_path, people=False)
    _seed_floor()
    resp = client.get(FLOOR_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    direct = _floor_direct(None)
    assert body["present"] == direct["present"] == 2
    assert body["arrived"] == direct["arrived"] == 2
    assert body["approved"] == direct["approved"] == 3
    assert body["sessions"] == ["Пленарка"]
    assert all(isinstance(title, str) for title in body["sessions"])
    _no_people(body)
    text = json.dumps(body, ensure_ascii=False)
    assert "На Площадке" not in text and "floor_nick" not in text
    assert "+79990002233" not in text and "Ещё Не Одобрен" not in text
    assert "csv" not in text.lower() and "broadcast" not in _all_keys(body)
    same = client.get(FLOOR_PATH, params={"city": "spb"}, headers=_hdr(ADMIN_ID))
    assert same.json() == body


def test_floor_city_manager_does_not_see_another_city(tmp_path, monkeypatch):
    _freeze_floor(monkeypatch)
    client = _ready(tmp_path, people=False)
    _seed_floor()
    resp = client.get(FLOOR_PATH, params={"city": "msk"}, headers=_hdr(FLOOR_SPB_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    direct = _floor_direct("spb")
    assert body["present"] == direct["present"] == 1
    assert body["arrived"] == direct["arrived"] == 1
    assert body["approved"] == direct["approved"] == 2
    assert body["sessions"] == ["Пленарка"]
    _no_people(body)
    msk = client.get(FLOOR_PATH, headers=_hdr(FLOOR_MSK_ID))
    assert msk.status_code == 200, msk.text
    msk_body = msk.json()
    assert msk_body["present"] == _floor_direct("msk")["present"] == 1
    assert msk_body["arrived"] == 1 and msk_body["approved"] == 1
    assert msk_body["sessions"] == []
    assert "Пленарка" not in json.dumps(msk_body, ensure_ascii=False)


def test_floor_zero_stays_zero(tmp_path, monkeypatch):
    _freeze_floor(monkeypatch)
    client = _ready(tmp_path, people=False)
    resp = client.get(FLOOR_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["present"] == 0
    assert body["arrived"] == 0
    assert body["approved"] == 0
    assert body["sessions"] == []
    assert body != {}


def test_floor_without_checkin_cap_is_403_and_scan_route_remains(tmp_path):
    client = _ready(tmp_path, people=False)
    for user in (GAME_MANAGER_ID, STATS_CITY_ID, DELEGATE_ID):
        resp = client.get(FLOOR_PATH, headers=_hdr(user))
        assert resp.status_code == 403
        assert resp.json() == {"reason": "no_cap", "cap": "checkin"}
    scan = client.post("/app/api/checkin/scan", json={"payload": "x"}, headers=_hdr(GAME_MANAGER_ID))
    assert scan.status_code == 403
    assert scan.json()["reason"] == "no_cap"


def test_floor_section_off_is_403(tmp_path):
    client = _ready(tmp_path, people=False)
    _set("miniapp_section_checkin", "off")
    resp = client.get(FLOOR_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "checkin"}


# ── Таблица и формы: карточка, слот, вопросы, включение ──────────────────────────────────

SHEETS_PATH = "/app/api/admin/sheets-forms"
SLOT_PATH = SHEETS_PATH + "/slot"
QUESTION_PATH = SHEETS_PATH + "/question"
ARM_PATH = SHEETS_PATH + "/delegations"
CITY_WRITER_ID = 900811
_LEAK_SECRET = "ya-token-SHOULD-NOT-LEAK"
_LEAK_EXTERNAL = "ext-SHOULD-NOT-LEAK"
_LEAK_QKEY = "qkey-SHOULD-NOT-LEAK"
_CARD_FORBIDDEN_KEYS = {
    "form_id", "qkey", "external_id", "sheet_url", "sheet_id", "token", "secret",
    "access_token", "oauth_token", "id", "count",
}
_KEYS_MISSING = (
    "Сначала выберите вопросы формы: имя, вуз и курс. Без этого делегации не включаются."
)


def _sheets_client(tmp_path, name="sheets_forms.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _seed(
        users=[(DELEGATE_ID, "approved")],
        settings={"miniapp_enabled": "on", "registration_mode": "full"},
    )
    return _client(_cfg(db_path))


def _raw_setting(key):
    return _run(bot_db.get_setting(key))


def _card_is_closed(body: dict, *planted: str):
    assert not (_all_keys(body) & _CARD_FORBIDDEN_KEYS)
    text = json.dumps(body, ensure_ascii=False)
    for secret in planted:
        assert secret not in text
    sid = sheet_id()
    assert body["book_connected"] is bool(sid)
    if sid:
        assert sid not in text


async def _make_form(title, *, secret, external_id, columns=()):
    fid = await ext_forms_db.create_form(
        platform="yandex", external_id=external_id, title=title, secret=secret,
    )
    if columns:
        await ext_forms_db.upsert_columns(fid, list(columns))
    return fid


def test_sheets_forms_card_hides_ids_and_reads_tabs(tmp_path):
    client = _sheets_client(tmp_path)
    fid = _run(_make_form(
        "Анкета регистрации",
        secret=_LEAK_SECRET,
        external_id=_LEAK_EXTERNAL,
        columns=[(_LEAK_QKEY, "ФИО человека"), ("q-age-hidden", "Возраст")],
    ))
    _set("delegation_form_id", str(fid))
    _set("delegation_q_fullname", _LEAK_QKEY)
    _set("main_sheet_tab", "Регистрации")
    _set("incomplete_sheet_tab", "")
    resp = client.get(SHEETS_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _card_is_closed(body, _LEAK_SECRET, _LEAK_EXTERNAL, _LEAK_QKEY, "q-age-hidden", str(fid))
    names = {tab["key"]: tab["name"] for tab in body["tabs"]}
    assert [tab["key"] for tab in body["tabs"]] == ["main", "short", "party", "incomplete"]
    assert names == {
        "main": "Регистрации",
        "short": "Краткая",
        "party": "Party",
        "incomplete": None,
    }
    assert [slot["slot"] for slot in body["slots"]] == [
        "registration", "delegation", "ambassador",
    ]
    titles = {slot["slot"]: slot["title"] for slot in body["slots"]}
    assert titles["registration"] is None
    assert titles["delegation"] == "Анкета регистрации"
    assert titles["ambassador"] is None
    assert body["delegations_on"] is False
    assert body["questions_ready"] is False
    assert [q["label"] for q in body["questions"]] == ["Имя", "Вуз", "Курс"]
    assert body["questions"][0]["text"] == "ФИО человека"
    assert body["questions"][1]["text"] is None and body["questions"][2]["text"] is None
    assert body["forms"] == ["Анкета регистрации"]
    assert body["question_choices"] == ["ФИО человека", "Возраст"]
    assert "count" not in body


def test_sheets_forms_slot_writes_only_a_listed_form(tmp_path):
    client = _sheets_client(tmp_path, "sheets_slot.db")
    first = _run(_make_form(
        "Первая форма", secret=_LEAK_SECRET, external_id=_LEAK_EXTERNAL,
    ))
    second = _run(_make_form(
        "Вторая форма", secret="other-secret-SHOULD-NOT-LEAK", external_id="ext-other",
    ))
    missed = client.post(
        SLOT_PATH,
        json={"slot": "registration", "form_id": 999999, "index": 0},
        headers=_hdr(ADMIN_ID),
    )
    assert missed.status_code == 400, missed.text
    assert missed.json()["reason"] == "unknown_form"
    assert _raw_setting("registration_form_id") in (None, "")
    assert _LEAK_SECRET not in missed.text
    saved = client.post(
        SLOT_PATH, json={"slot": "registration", "index": 0}, headers=_hdr(ADMIN_ID),
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    _card_is_closed(body, _LEAK_SECRET, _LEAK_EXTERNAL, str(first), str(second))
    assert _raw_setting("registration_form_id") == str(first)
    assert body["slots"][0]["title"] == "Первая форма"
    other = client.post(
        SLOT_PATH, json={"slot": "ambassador", "index": 1}, headers=_hdr(ADMIN_ID),
    )
    assert other.status_code == 200, other.text
    assert _raw_setting("ambassador_form_id") == str(second)
    assert _raw_setting("google_sheet_id") in (None, "")


def test_sheets_forms_question_uses_column_index_not_client_qkey(tmp_path):
    client = _sheets_client(tmp_path, "sheets_question.db")
    columns = [
        ("col-fullname", "ФИО человека"),
        ("col-age", "Возраст"),
        ("col-course", "Курс обучения"),
    ]
    fid = _run(_make_form(
        "Делегации вузов", secret=_LEAK_SECRET, external_id=_LEAK_EXTERNAL, columns=columns,
    ))
    _set("delegation_form_id", str(fid))
    _set("delegation_armed_form_id", str(fid))
    refused = client.post(
        QUESTION_PATH,
        json={"which": "course", "index": 2, "qkey": _LEAK_QKEY},
        headers=_hdr(ADMIN_ID),
    )
    assert refused.status_code == 200, refused.text
    body = refused.json()
    _card_is_closed(body, _LEAK_SECRET, _LEAK_EXTERNAL, _LEAK_QKEY, "col-course", str(fid))
    assert _raw_setting("delegation_q_course") == "col-course"
    assert body["questions"][2]["text"] == "Курс обучения"
    assert body["delegations_on"] is True
    assert _raw_setting("delegation_armed_form_id") == str(fid)
    no_form = _sheets_client(tmp_path, "sheets_question_empty.db")
    bad = no_form.post(
        QUESTION_PATH,
        json={"which": "course", "index": 0, "qkey": _LEAK_QKEY},
        headers=_hdr(ADMIN_ID),
    )
    assert bad.status_code == 400, bad.text
    assert _raw_setting("delegation_q_course") in (None, "")
    assert _LEAK_QKEY not in bad.text


def test_sheets_forms_city_manager_cannot_write_common(tmp_path, monkeypatch):
    from config import config

    monkeypatch.setattr(config, "ADMIN_IDS", [1])
    client = _sheets_client(tmp_path, "sheets_city.db")
    fid = _run(_make_form(
        "Общая форма", secret=_LEAK_SECRET, external_id=_LEAK_EXTERNAL,
    ))
    _seed(staff=[(CITY_WRITER_ID, "reg_manager", "spb")])
    _set("role_caps_reg_manager", "settings")
    _set("event_city_enabled", "on")
    _set("delegation_form_id", str(fid))
    _set("delegation_q_fullname", "q1")
    _set("delegation_q_university", "q4")
    _set("delegation_q_course", "q6")
    read = client.get(SHEETS_PATH, headers=_hdr(CITY_WRITER_ID))
    assert read.status_code == 200, read.text
    resp = client.post(
        SLOT_PATH, json={"slot": "registration", "index": 0}, headers=_hdr(CITY_WRITER_ID),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"reason": "common_locked"}
    assert _raw_setting("registration_form_id") in (None, "")
    arm = client.post(ARM_PATH, json={"action": "on"}, headers=_hdr(CITY_WRITER_ID))
    assert arm.status_code == 403, arm.text
    assert arm.json()["reason"] == "common_locked"
    assert _raw_setting("delegation_armed_form_id") in (None, "")


def test_sheets_forms_without_settings_cap_is_403(tmp_path):
    client = _sheets_client(tmp_path, "sheets_cap.db")
    resp = client.get(SHEETS_PATH, headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "no_cap", "cap": "settings"}
    _set("miniapp_section_settings", "off")
    off = client.get(SHEETS_PATH, headers=_hdr(ADMIN_ID))
    assert off.status_code == 403
    assert off.json() == {"reason": "section_off", "section": "settings"}


def _delegation_http(tmp_path):
    from tests.test_delegations_core import _delegation_form, _env

    _env(tmp_path, "sheets_arm.db")
    fid = _delegation_form()
    _set("delegation_armed_form_id", "")
    _set("miniapp_enabled", "on")
    from config import config

    return _client(_cfg(config.DB_PATH)), fid


def test_sheets_form_change_resets_delegation_questions(tmp_path, monkeypatch):
    client = _sheets_client(tmp_path, "sheets_form_change.db")
    old = _run(_make_form(
        "Старая форма", secret="secret-old", external_id="ext-old",
        columns=[("old-name", "ФИО"), ("old-uni", "Вуз"), ("old-course", "Курс")],
    ))
    new = _run(_make_form(
        "Новая форма", secret="secret-new", external_id="ext-new",
        columns=[("new-name", "Имя"), ("new-uni", "Университет"), ("new-course", "Курс обучения")],
    ))
    _set("delegation_form_id", str(old))
    _set("delegation_armed_form_id", str(old))
    _set("delegation_q_fullname", "old-name")
    _set("delegation_q_university", "old-uni")
    _set("delegation_q_course", "old-course")
    _set("delegation_q_email", "old-mail")

    changed = client.post(
        SLOT_PATH, json={"slot": "delegation", "form_id": new}, headers=_hdr(ADMIN_ID),
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["questions_ready"] is False
    assert [q["text"] for q in body["questions"]] == [None, None, None]
    assert [slot["slot"] for slot in body["slots"]] == [
        "registration", "delegation", "ambassador",
    ]
    assert "game" not in body and "sessions" not in body
    assert _raw_setting("delegation_q_fullname") == ""
    assert _raw_setting("delegation_q_university") == ""
    assert _raw_setting("delegation_q_course") == ""
    assert _raw_setting("delegation_q_email") == "old-mail"
    assert _raw_setting("delegation_armed_form_id") == ""

    # Непустые ключи старой формы всё равно не считаются выбранными.
    _set("delegation_q_fullname", "old-name")
    _set("delegation_q_university", "old-uni")
    _set("delegation_q_course", "old-course")
    stale = client.get(SHEETS_PATH, headers=_hdr(ADMIN_ID))
    assert stale.status_code == 200, stale.text
    assert stale.json()["questions_ready"] is False

    swept: list = []

    async def _no_sweep(*_a, **_k):
        swept.append(1)
        return {"evaluated": 0, "linked": 0, "rechecked": 0}

    import miniapp.routers.sheets_forms as sheets_forms
    monkeypatch.setattr(sheets_forms.delegations, "sweep_pending", _no_sweep)
    blocked = client.post(ARM_PATH, json={"action": "on"}, headers=_hdr(ADMIN_ID))
    assert blocked.status_code == 200, blocked.text
    assert blocked.json() == {"ok": False, "text": _KEYS_MISSING}
    assert _raw_setting("delegation_armed_form_id") == ""
    assert swept == []

    for which, index in (("fullname", 0), ("university", 1), ("course", 2)):
        picked = client.post(
            QUESTION_PATH, json={"which": which, "index": index}, headers=_hdr(ADMIN_ID),
        )
        assert picked.status_code == 200, picked.text
    assert picked.json()["questions_ready"] is True
    assert _raw_setting("delegation_q_fullname") == "new-name"
    assert _raw_setting("delegation_q_university") == "new-uni"
    assert _raw_setting("delegation_q_course") == "new-course"
    turned = client.post(ARM_PATH, json={"action": "on"}, headers=_hdr(ADMIN_ID))
    assert turned.status_code == 200, turned.text
    assert turned.json()["ok"] is True and turned.json()["delegations_on"] is True
    assert _raw_setting("delegation_armed_form_id") == str(new)
    assert swept == [1]


def test_sheets_forms_on_without_questions_does_not_arm_or_sweep(tmp_path):
    from database import delegations_db as ddb
    from tests.test_delegations_core import _answer_from_fixture

    client, fid = _delegation_http(tmp_path)
    _answer_from_fixture(fid, "a1", answered_at="2026-09-01 12:00:00")
    _set("delegation_q_course", "")
    resp = client.post(ARM_PATH, json={"action": "on"}, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": False, "text": _KEYS_MISSING}
    assert _raw_setting("delegation_armed_form_id") == ""
    assert _run(ddb.get_by_answer(fid, "a1")) is None


def test_sheets_forms_preview_then_on_converts_one_and_off_keeps_key(tmp_path):
    from database import delegations_db as ddb
    from tests.test_delegations_core import _answer_from_fixture, _reg_started

    client, fid = _delegation_http(tmp_path)
    _answer_from_fixture(fid, "a1", answered_at="2026-09-01 12:00:00")
    _reg_started(501)
    card = client.get(SHEETS_PATH, headers=_hdr(ADMIN_ID))
    assert card.status_code == 200, card.text
    assert "count" not in card.json()
    assert "Тест Делегат Тестович" not in card.text
    assert _run(ddb.get_by_answer(fid, "a1")) is None
    preview = client.post(ARM_PATH, json={"action": "preview"}, headers=_hdr(ADMIN_ID))
    assert preview.status_code == 200, preview.text
    assert preview.json() == {"ok": True, "count": 1}
    assert _raw_setting("delegation_armed_form_id") == ""
    assert _run(ddb.get_by_answer(fid, "a1")) is None
    turned = client.post(ARM_PATH, json={"action": "on"}, headers=_hdr(ADMIN_ID))
    assert turned.status_code == 200, turned.text
    assert turned.json()["ok"] is True
    assert turned.json()["delegations_on"] is True
    assert _raw_setting("delegation_armed_form_id") == str(fid)
    assert _run(ddb.get_by_answer(fid, "a1"))["linked_telegram_id"] == 501
    off = client.post(ARM_PATH, json={"action": "off"}, headers=_hdr(ADMIN_ID))
    assert off.status_code == 200, off.text
    assert off.json() == {"ok": True, "delegations_on": False}
    assert _raw_setting("delegation_armed_form_id") == ""


def test_sheets_forms_router_does_not_close_the_season():
    text = (
        Path(__file__).resolve().parents[1] / "miniapp" / "routers" / "sheets_forms.py"
    ).read_text(encoding="utf-8")
    assert "mark_season_ended" not in text
    assert "season_reset_apply" not in text
    assert "initData" not in text and "init_data" not in text
    assert "YANDEX_OAUTH" not in text
    assert "google_sheet_id" not in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith((
            "from handlers", "import handlers", "import aiogram", "from aiogram",
        ))


# ── Итог сезона: четыре числа после окна форума города ──────────────────────────────────

RECAP_PATH = "/app/api/stats/season-recap"
DISMISS_PATH = RECAP_PATH + "/dismiss"
_RECAP_NOW = datetime(2026, 11, 4, 12, 0, 0)
_NUMBER_KEYS = ("applications", "arrived", "participants", "questions")
COMMON_WRITER_ID = 900704


def _freeze_recap(monkeypatch):
    monkeypatch.setattr("miniapp.routers.stats.msk_now", lambda: _RECAP_NOW)


def _user_count() -> int:
    async def _count():
        async with bot_db._connect() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM users")
            row = await cursor.fetchone()
            return int(row[0])

    return _run(_count())


def _ended_city_forum():
    """Своя дата СПб уже позади. Общая и московская даты впереди и не должны подставиться."""
    _set("forum_date__city__spb", "01.01.2020")
    _set("forum_date", "01.01.2099")
    _set("forum_date__city__msk", "01.01.2099")


def _plant_recap_counts():
    """Один вход в СПб, вопрос СПб и вопрос Москвы, сдача игрока из Москвы."""
    _run(_insert_entry(910007, "2026-10-01 12:00:00"))
    _run(bot_db.create_question(910007, "Вопрос Петербурга"))
    _run(bot_db.create_question(910001, "Вопрос Москвы"))
    task_id = _run(bot_db.create_task(
        "задание", "Light", 1, "text", "2026-12-01 00:00:00", None, event_city="msk",
    ))
    _run(bot_db.create_submission(task_id, 910001, "text", "ответ", "2026-11-01 00:00:00"))


def test_season_recap_players_are_unfiltered_game_stats(tmp_path, monkeypatch):
    from miniapp.routers import stats as stats_router

    src = inspect.getsource(stats_router.season_recap)
    dismiss_src = inspect.getsource(stats_router.season_recap_dismiss)
    assert re.search(r"get_game_stats\(\s*\)", src)
    assert not re.search(r"get_game_stats\(\s*[^)\s]", src)
    file_src = Path(stats_router.__file__).read_text(encoding="utf-8")
    assert "mark_season_ended" not in file_src
    assert "season_reset_apply" not in file_src
    assert "forum_end_date_iso" not in file_src
    assert "inherit_common" not in file_src
    assert "initData" not in src and "init_data" not in src
    assert "initData" not in dismiss_src and "init_data" not in dismiss_src

    client = _ready(tmp_path)
    _freeze_recap(monkeypatch)
    _ended_city_forum()
    _plant_recap_counts()
    resp = client.get(RECAP_PATH, params={"city": "msk"}, headers=_hdr(STATS_CITY_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    scope = city_scope("spb")
    total, _top = _run(bot_db.get_stats(city_scope=scope, season=SEASON))
    arrived, _approved = _run(checkin_arrival.arrived_counts(scope))
    buckets = _run(bot_db.count_questions_by_status(city_scope=scope))
    questions = int(buckets["new"]) + int(buckets["in_work"]) + int(buckets["answered"])
    participants = _run(bot_db.get_game_stats())["participants"]
    assert participants >= 1
    assert body == {
        "show": True,
        "applications": total,
        "arrived": arrived,
        "participants": participants,
        "questions": questions,
    }
    assert body["applications"] == 2
    assert body["arrived"] == 1
    assert body["questions"] == 1
    assert "по всем городам" not in resp.text
    _no_people(body)
    same = client.get(RECAP_PATH, headers=_hdr(ADMIN_ID))
    assert same.status_code == 200, same.text
    assert same.json()["applications"] == body["applications"]
    assert same.json()["participants"] == participants


def test_season_recap_without_own_window_stays_hidden(tmp_path, monkeypatch):
    client = _ready(tmp_path)
    _freeze_recap(monkeypatch)
    _set("forum_date", "01.01.2020")
    _set("forum_date__city__msk", "01.01.2020")
    hidden = client.get(RECAP_PATH, headers=_hdr(STATS_CITY_ID))
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"show": False}
    assert not (set(hidden.json()) & set(_NUMBER_KEYS))

    _set("forum_date__city__spb", "03.11.2026")
    on_last_day = client.get(RECAP_PATH, headers=_hdr(STATS_CITY_ID))
    assert on_last_day.json() == {"show": False}

    _set("forum_date__city__spb", "10.12.2026")
    before = client.get(RECAP_PATH, headers=_hdr(STATS_CITY_ID))
    assert before.json() == {"show": False}


def test_season_recap_dismiss_hides_card_and_keeps_users(tmp_path, monkeypatch):
    client = _ready(tmp_path)
    _freeze_recap(monkeypatch)
    _ended_city_forum()
    before = _user_count()
    names = _run(bot_db.get_setting("season_recap_dismissed"))
    posted = client.post(DISMISS_PATH, headers=_hdr(STATS_CITY_ID))
    assert posted.status_code == 200, posted.text
    assert posted.json() == {"ok": True}
    assert _run(bot_db.get_setting("season_recap_dismissed__city__spb")) == "on"
    assert _run(bot_db.get_setting("season_recap_dismissed")) == names
    assert _user_count() == before
    again = client.get(RECAP_PATH, headers=_hdr(STATS_CITY_ID))
    assert again.status_code == 200
    assert again.json() == {"show": False}


def test_season_recap_common_writer_sets_registry_flag(tmp_path, monkeypatch):
    client = _ready(tmp_path)
    _freeze_recap(monkeypatch)
    _ended_city_forum()
    _seed(staff=[(COMMON_WRITER_ID, "stats_manager", None)])
    before = _user_count()
    posted = client.post(DISMISS_PATH, headers=_hdr(COMMON_WRITER_ID))
    assert posted.status_code == 200, posted.text
    assert _run(bot_db.get_setting("season_recap_dismissed")) == "on"
    assert _user_count() == before
    again = client.get(RECAP_PATH, headers=_hdr(STATS_CITY_ID))
    assert again.json() == {"show": False}


def test_season_recap_requires_stats_cap(tmp_path):
    client = _ready(tmp_path, people=False)
    for user in (GAME_MANAGER_ID, DELEGATE_ID):
        got = client.get(RECAP_PATH, headers=_hdr(user))
        assert got.status_code == 403
        assert got.json() == {"reason": "no_cap", "cap": "stats"}
        posted = client.post(DISMISS_PATH, headers=_hdr(user))
        assert posted.status_code == 403
        assert posted.json() == {"reason": "no_cap", "cap": "stats"}


def test_season_recap_section_off_is_403(tmp_path):
    client = _ready(tmp_path, people=False)
    _set("miniapp_section_stats", "off")
    resp = client.get(RECAP_PATH, headers=_hdr(STATS_CITY_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "stats"}


def test_unbound_superadmin_has_no_season_card(tmp_path, monkeypatch):
    db_path = _use_tmp_db(tmp_path, "season_recap_unbound.db")
    _seed(
        staff=[(ADMIN_ID, "stats_manager", None)],
        settings={
            "miniapp_enabled": "on",
            "event_city_enabled": "on",
            "event_season": SEASON,
            "forum_date": "01.01.2020",
            "forum_date__city__spb": "01.01.2020",
        },
    )
    _seed_people()
    _freeze_recap(monkeypatch)
    client = _client(_cfg(db_path))
    resp = client.get(RECAP_PATH, params={"city": "spb"}, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"show": False}


def test_season_recap_flag_is_not_writable_by_batch(tmp_path):
    client = _ready(tmp_path, people=False)
    resp = client.post(
        "/app/api/admin/settings/batch",
        json={
            "changes": [{"key": "season_recap_dismissed", "value": "on"}],
            "base": {},
            "confirm": [],
        },
        headers=_hdr(ADMIN_ID),
    )
    assert resp.status_code == 403
    assert resp.json() == {"reason": "not_editable", "key": "season_recap_dismissed"}
    assert _run(bot_db.get_setting("season_recap_dismissed")) is None
