"""Регистрация на месте (FORUM-CHECKIN.md D-41): ядро — слой данных, настройки и сервис
`services/onsite_reg.py`, который используют и чат бота, и сканер Mini App.

Что проверяем:
- миграция `users.onsite_kind/onsite_at/onsite_by` аддитивна (старые строки — NULL);
- `create_onsite_user` никогда не перезаписывает существующую анкету;
- `approve_onsite` одобряет ровно одного человека и не флипает дважды;
- walk-in в статусе pending не виден очереди менеджера и «Принять всех» (урок инцидента
  06.09 — никаких тихих массовых одобрений);
- `list_onsite_pending` — только walk-in этого дня и города;
- реестр: тумблер per_city дефолт off, у каждого нового текста есть английский дефолт.

pytest-asyncio в проекте нет — async через `asyncio.run()` (конвенция проекта)."""
from __future__ import annotations

import asyncio
import sqlite3

import cities as cities_mod
from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

SEASON = "YL'26"
PAST = "YL'26/1"
STAFF_ID = 927001


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="onsite_reg_core.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))


def _insert(uid, *, status="pending", season=SEASON, city="spb", full_name="Иванов Иван",
            onsite_kind=None, registration_date="2026-09-20 10:00:00"):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO users (telegram_id, full_name, status, season, event_city, registration_date, "
        "onsite_kind, email, phone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, full_name, status, season, city, registration_date, onsite_kind, "a@b.c", "+79990000000"),
    )
    conn.commit()
    conn.close()


def _row(uid) -> dict | None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (uid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def _create(uid, **kw):
    args = dict(username="masha", full_name="Иванова Мария", phone="+79991234567",
                university="СПбГУ", event_city="spb", season=SEASON)
    args.update(kw)
    return _run(db.create_onsite_user(uid, **args))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Миграция
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_fresh_db_has_onsite_columns(tmp_path):
    _ready(tmp_path)
    conn = sqlite3.connect(config.DB_PATH)
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert cols["onsite_kind"] == "TEXT"
    assert cols["onsite_at"] == "TEXT"
    assert cols["onsite_by"] == "INTEGER"


def test_old_db_migrates_additively_existing_rows_null(tmp_path):
    config.DB_PATH = str(tmp_path / "old.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, "
        "email TEXT, registration_date TEXT)"
    )
    conn.execute(
        "INSERT INTO users (telegram_id, full_name, registration_date) VALUES (1, 'Старый', '2025-01-01')"
    )
    conn.commit()
    conn.close()
    _run(db.init_db())
    row = _row(1)
    assert row["full_name"] == "Старый"
    assert row["onsite_kind"] is None and row["onsite_at"] is None and row["onsite_by"] is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# create_onsite_user
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_create_onsite_user_new_row(tmp_path):
    _ready(tmp_path)
    assert _create(5001) is True
    row = _row(5001)
    assert row["status"] == "pending"
    assert row["onsite_kind"] == "walkin"
    assert row["participant_type"] == "full"
    assert row["source"] == "На месте"
    assert row["username"] == "@masha"
    assert row["full_name"] == "Иванова Мария"
    assert row["phone"] == "+79991234567"
    assert row["university"] == "СПбГУ"
    assert row["event_city"] == "spb"
    assert row["season"] == SEASON
    assert row["email"] == "-"
    assert row["registration_date"] and len(row["registration_date"]) == 19
    assert row["onsite_at"] is None and row["onsite_by"] is None


def test_create_onsite_user_university_optional(tmp_path):
    _ready(tmp_path)
    assert _create(5002, university=None, username=None) is True
    row = _row(5002)
    assert row["university"] is None
    assert row["username"] is None


def test_create_onsite_user_never_overwrites_existing(tmp_path):
    _ready(tmp_path)
    for uid, status in ((5010, "approved"), (5011, "rejected"), (5012, "pending")):
        _insert(uid, status=status, full_name="Прежняя Анкета", city="msk")
        before = _row(uid)
        assert _create(uid) is False
        assert _row(uid) == before


# ══════════════════════════════════════════════════════════════════════════════════════════
# approve_onsite
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_approve_onsite_pending_door(tmp_path):
    _ready(tmp_path)
    _insert(5020, status="pending")
    assert _run(db.approve_onsite(5020, by_staff_id=STAFF_ID, season=SEASON)) is True
    row = _row(5020)
    assert row["status"] == "approved"
    assert row["approved_at"] and row["onsite_at"]
    assert row["onsite_by"] == STAFF_ID
    assert row["onsite_kind"] == "door"
    assert row["season"] == SEASON
    assert row["event_city"] == "spb"


def test_approve_onsite_walkin_keeps_kind(tmp_path):
    _ready(tmp_path)
    _create(5021)
    assert _run(db.approve_onsite(5021, by_staff_id=STAFF_ID, season=SEASON)) is True
    row = _row(5021)
    assert row["status"] == "approved"
    assert row["onsite_kind"] == "walkin"


def test_approve_onsite_rejected(tmp_path):
    _ready(tmp_path)
    _insert(5022, status="rejected")
    assert _run(db.approve_onsite(5022, by_staff_id=STAFF_ID, season=SEASON)) is True
    assert _row(5022)["status"] == "approved"


def test_approve_onsite_already_approved_current_season_noop(tmp_path):
    _ready(tmp_path)
    _insert(5023, status="approved")
    before = _row(5023)
    assert _run(db.approve_onsite(5023, by_staff_id=STAFF_ID, season=SEASON)) is False
    assert _row(5023) == before


def test_approve_onsite_past_season_moves_to_current(tmp_path):
    _ready(tmp_path)
    _insert(5024, status="approved", season=PAST, city="msk")
    assert _run(db.approve_onsite(5024, by_staff_id=STAFF_ID, season=SEASON, event_city="spb")) is True
    row = _row(5024)
    assert row["status"] == "approved"
    assert row["season"] == SEASON
    assert row["prev_season"] == PAST
    assert row["event_city"] == "spb"
    assert row["onsite_kind"] == "door"


def test_approve_onsite_twice_second_is_false(tmp_path):
    _ready(tmp_path)
    _insert(5025, status="pending")
    assert _run(db.approve_onsite(5025, by_staff_id=STAFF_ID, season=SEASON)) is True
    first = _row(5025)
    assert _run(db.approve_onsite(5025, by_staff_id=STAFF_ID + 1, season=SEASON)) is False
    assert _row(5025) == first


def test_approve_onsite_unknown_id_false(tmp_path):
    _ready(tmp_path)
    assert _run(db.approve_onsite(5099, by_staff_id=STAFF_ID, season=SEASON)) is False


# ══════════════════════════════════════════════════════════════════════════════════════════
# Очередь менеджера не видит walk-in
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_pending_queue_excludes_walkin(tmp_path):
    _ready(tmp_path)
    _insert(5030, status="pending")                        # обычная заявка
    _insert(5031, status="pending", onsite_kind="door")    # не бывает pending door, но не walkin
    _create(5032)                                          # walk-in
    ids = [u["telegram_id"] for u in _run(db.get_pending_users(limit=50))]
    assert 5030 in ids and 5031 in ids
    assert 5032 not in ids
    assert _run(db.get_pending_count()) == 2


def test_approve_all_pending_skips_walkin(tmp_path):
    _ready(tmp_path)
    _insert(5040, status="pending")
    _create(5041)
    flipped = _run(db.approve_all_pending())
    assert flipped == [5040]
    assert _row(5041)["status"] == "pending"


def test_approve_all_pending_scoped_skips_walkin(tmp_path):
    _ready(tmp_path)
    _insert(5042, status="pending", city="spb")
    _create(5043, event_city="spb")
    flipped = _run(db.approve_all_pending(city_scope=cities_mod.city_scope("spb")))
    assert flipped == [5042]
    assert _row(5043)["status"] == "pending"


# ══════════════════════════════════════════════════════════════════════════════════════════
# list_onsite_pending
# ══════════════════════════════════════════════════════════════════════════════════════════

def _set_regdate(uid, value):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET registration_date = ? WHERE telegram_id = ?", (value, uid))
    conn.commit()
    conn.close()


def test_list_onsite_pending_day_city_order(tmp_path):
    _ready(tmp_path)
    _create(5050, full_name="Первая Анна", event_city="spb")
    _set_regdate(5050, "2026-10-03 09:00:00")
    _create(5051, full_name="Вторая Белла", event_city="spb")
    _set_regdate(5051, "2026-10-03 11:00:00")
    _create(5052, full_name="Москва Вера", event_city="msk")
    _set_regdate(5052, "2026-10-03 10:00:00")
    _create(5053, full_name="Вчера Галя", event_city="spb")
    _set_regdate(5053, "2026-10-02 10:00:00")
    _create(5054, full_name="Одобрена Даша", event_city="spb")
    _set_regdate(5054, "2026-10-03 10:30:00")
    _run(db.approve_onsite(5054, by_staff_id=STAFF_ID, season=SEASON))
    _insert(5055, status="pending", city="spb", registration_date="2026-10-03 10:40:00")  # не walk-in

    items = _run(db.list_onsite_pending(city_scope=cities_mod.city_scope("spb"), day="2026-10-03"))
    assert [i["telegram_id"] for i in items] == [5051, 5050]
    first = items[0]
    for key in ("telegram_id", "full_name", "username", "university", "event_city", "phone"):
        assert key in first
    assert first["full_name"] == "Вторая Белла"

    all_cities = _run(db.list_onsite_pending(day="2026-10-03"))
    assert [i["telegram_id"] for i in all_cities] == [5051, 5052, 5050]


def test_list_onsite_pending_limit(tmp_path):
    _ready(tmp_path)
    for n in range(5):
        _create(5060 + n)
        _set_regdate(5060 + n, f"2026-10-03 09:0{n}:00")
    assert len(_run(db.list_onsite_pending(day="2026-10-03", limit=3))) == 3


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реестр настроек
# ══════════════════════════════════════════════════════════════════════════════════════════

ONSITE_DELEGATE_TEXTS = (
    "onsite_reg_intro_text", "onsite_reg_consent_button_text", "onsite_reg_name_prompt_text",
    "onsite_reg_phone_prompt_text", "onsite_reg_university_prompt_text",
    "onsite_reg_skip_button_text", "onsite_reg_done_text", "onsite_reg_closed_text",
    "onsite_reg_already_approved_text", "onsite_reg_existing_text", "onsite_reg_approved_text",
    "onsite_reg_bad_name_text", "onsite_reg_bad_phone_text",
)
ONSITE_STAFF_TEXTS = (
    "onsite_approve_button_text", "onsite_approve_confirm_text", "onsite_register_button_text",
    "onsite_register_hint_text", "onsite_pending_title_text", "onsite_off_text",
)


def test_registry_toggle_per_city_default_off():
    meta = SETTINGS_SCHEMA["onsite_reg_enabled"]
    assert meta["type"] == "enum"
    assert meta["options"] == ["on", "off"]
    assert meta["default"] == "off"
    assert meta["per_city"] is True
    assert meta["group"] == "toggles"


def test_registry_texts_exist_with_english_defaults():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    staff_group = SETTINGS_SCHEMA["checkin_undo_button_text"]["group"]
    for key in ONSITE_DELEGATE_TEXTS:
        meta = SETTINGS_SCHEMA[key]
        assert meta["type"] == "text" and meta["group"] == "reg", key
    for key in ONSITE_STAFF_TEXTS:
        meta = SETTINGS_SCHEMA[key]
        assert meta["type"] == "text" and meta["group"] == staff_group, key
    for key in ONSITE_DELEGATE_TEXTS + ONSITE_STAFF_TEXTS:
        default = SETTINGS_SCHEMA[key]["default"]
        assert default and default.strip(), key
        assert default in FORM_DEFAULT_EN, f"{key}: нет английского дефолта"
        # Бренды — только кириллицей (закон РФ): латинских «YouLead»/«AIESEC» в текстах нет.
        assert "YouLead" not in default and "AIESEC" not in default, key


def test_confirm_and_done_placeholders():
    assert "{name}" in SETTINGS_SCHEMA["onsite_approve_confirm_text"]["default"]
    assert "{name}" in SETTINGS_SCHEMA["onsite_reg_done_text"]["default"]
