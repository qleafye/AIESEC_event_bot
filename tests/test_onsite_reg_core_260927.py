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

import domain.cities as cities_mod
from config import config
from database import db
from domain.settings.schema import SETTINGS_SCHEMA
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
    # Отказ менеджера отменяется только явным «вопреки отказу» (ревью 28.09).
    _ready(tmp_path)
    _insert(5022, status="rejected")
    assert _run(db.approve_onsite(5022, by_staff_id=STAFF_ID, season=SEASON)) is False
    assert _row(5022)["status"] == "rejected"
    assert _run(db.approve_onsite(5022, by_staff_id=STAFF_ID, season=SEASON, override_reject=True)) is True
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


# ══════════════════════════════════════════════════════════════════════════════════════════
# services/onsite_reg.py — тумблер, ссылка/QR walk-in, одобрение у стойки
# ══════════════════════════════════════════════════════════════════════════════════════════

from unittest.mock import AsyncMock  # noqa: E402

from services import onsite_reg  # noqa: E402


def _cities_on():
    _run(db.set_setting("event_city_enabled", "on"))


def _enable(city):
    _run(db.set_setting(cities_mod.per_city_key("onsite_reg_enabled", city), "on"))


def _count(sql, *params):
    conn = sqlite3.connect(config.DB_PATH)
    n = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return n


def _decisions(uid):
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM application_decisions WHERE telegram_id = ?", (uid,))]
    conn.close()
    return rows


def _venue(uid):
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM venue_log WHERE telegram_id = ? ORDER BY id", (uid,))]
    conn.close()
    return rows


def _checkins(uid):
    return _count("SELECT COUNT(*) FROM checkins WHERE telegram_id = ? AND point = 'entry'", uid)


def _snapshot(uid):
    return (_row(uid), _decisions(uid), _venue(uid), _checkins(uid))


def _door(uid, *, city="spb", bound="spb"):
    user = _run(db.get_user(uid))
    return _run(onsite_reg.approve_at_door(
        user, city=city, staff_id=STAFF_ID, staff_name="Волонтёр", bound=bound,
    ))


def test_onsite_enabled_per_city(tmp_path):
    _ready(tmp_path)
    _cities_on()
    assert _run(onsite_reg.onsite_enabled("spb")) is False
    _enable("spb")
    assert _run(onsite_reg.onsite_enabled("spb")) is True
    assert _run(onsite_reg.onsite_enabled("msk")) is False


def test_walkin_link(tmp_path):
    _ready(tmp_path)
    assert _run(onsite_reg.walkin_link("MyBot", "spb")) == "https://t.me/MyBot?start=walkin"
    _cities_on()
    assert _run(onsite_reg.walkin_link("MyBot", "spb")) == "https://t.me/MyBot?start=walkin_spb"
    assert _run(onsite_reg.walkin_link("@MyBot", "spb")) == "https://t.me/MyBot?start=walkin_spb"
    assert _run(onsite_reg.walkin_link(None, "spb")) is None
    assert _run(onsite_reg.walkin_link("", "spb")) is None


def test_walkin_qr_png():
    png = onsite_reg.walkin_qr_png("https://t.me/MyBot?start=walkin_spb")
    assert png.startswith(b"\x89PNG")


def test_parse_walkin_arg():
    assert onsite_reg.parse_walkin_arg("walkin") == (True, None)
    assert onsite_reg.parse_walkin_arg("walkin_spb") == (True, "spb")
    assert onsite_reg.parse_walkin_arg("vol_abc") == (False, None)
    assert onsite_reg.parse_walkin_arg("walkinx") == (False, None)
    assert onsite_reg.parse_walkin_arg("walkin_") == (False, None)
    assert onsite_reg.parse_walkin_arg(None) == (False, None)
    assert onsite_reg.parse_walkin_arg("") == (False, None)


def test_approve_at_door_pending_full_path(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _enable("spb")
    _create(6001)
    res = _door(6001)
    assert res["status"] == "new"
    assert res["onsite_approved"] is True
    # событие для бота сервис ставит сам, сразу после флипа (ревью 28.09)
    assert _count("SELECT COUNT(*) FROM miniapp_outbox WHERE kind = 'onsite_approved'") == 1
    assert "log_id" not in res
    assert res.get("first_entry")  # вызывающий Mini App переносит его в outbox
    row = _row(6001)
    assert row["status"] == "approved" and row["onsite_by"] == STAFF_ID
    decisions = _decisions(6001)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "approved" and decisions[0]["decided_by"] == STAFF_ID
    assert decisions[0]["effects_sent_at"]  # эффекты не ждут окна отмены
    onsite_rows = [v for v in _venue(6001) if v["action"] == "onsite_approve"]
    assert len(onsite_rows) == 1
    assert onsite_rows[0]["staff_id"] == STAFF_ID and onsite_rows[0]["city"] == "spb"
    assert _checkins(6001) == 1


def test_approve_at_door_toggle_off_changes_nothing(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _create(6002)
    before = _snapshot(6002)
    res = _door(6002)
    assert res["status"] == "onsite_off"
    assert res["reason_text"] == SETTINGS_SCHEMA["onsite_off_text"]["default"]
    assert _snapshot(6002) == before


def test_approve_at_door_wrong_city_current_season(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _enable("spb")
    _insert(6003, status="pending", city="msk")
    before = _snapshot(6003)
    res = _door(6003)
    assert res["status"] == "wrong_city"
    assert res["reason_text"]
    assert _snapshot(6003) == before


def test_approve_at_door_wrong_city_approved_current_season_not_checked_in(tmp_path):
    """D-26 и для уже одобренного: волонтёр СПб не отмечает делегата Москвы у своей стойки."""
    _ready(tmp_path)
    _cities_on()
    _enable("spb")
    _insert(6008, status="approved", city="msk")
    before = _snapshot(6008)
    assert _door(6008)["status"] == "wrong_city"
    assert _snapshot(6008) == before


def test_approve_at_door_past_season_other_city_moves_to_stand(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _enable("spb")
    _insert(6004, status="approved", season=PAST, city="msk")
    res = _door(6004)
    assert res["status"] == "new"
    assert res["onsite_approved"] is True
    row = _row(6004)
    assert row["status"] == "approved"
    assert row["season"] == SEASON and row["prev_season"] == PAST
    assert row["event_city"] == "spb"
    assert _checkins(6004) == 1


def test_approve_at_door_already_approved_just_checks_in(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _enable("spb")
    _insert(6005, status="approved")
    res = _door(6005)
    assert res["status"] == "new"
    assert "onsite_approved" not in res and "outbox" not in res
    assert _decisions(6005) == []
    assert _row(6005)["onsite_kind"] is None
    assert _checkins(6005) == 1


def test_approve_at_door_second_call_no_second_decision(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _enable("spb")
    _insert(6006, status="pending")
    first = _door(6006)
    assert first["onsite_approved"] is True
    second = _door(6006)
    assert second["status"] == "duplicate"
    assert "outbox" not in second
    assert len(_decisions(6006)) == 1


def test_approve_at_door_unbound_uses_stand_city(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _enable("msk")
    _insert(6007, status="pending", city="msk")
    res = _door(6007, city="msk", bound=None)
    assert res["status"] == "new" and res["onsite_approved"] is True


# ── after_onsite_approved: лист, текст, QR — в процессе бота ────────────────────────────

def _fake_bot():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    return bot


def _patch_sheets(monkeypatch, calls, *, fail=False):
    from services import reg_finalize, sheets

    async def fake_row(tid, full, mode):
        calls.append(("row", tid, mode))
        if fail:
            raise RuntimeError("лист упал")

    async def fake_status(tid, label):
        calls.append(("status", tid, label))
        if fail:
            raise RuntimeError("лист упал")
        return True

    monkeypatch.setattr(reg_finalize, "write_sheet_row", fake_row)
    monkeypatch.setattr(sheets, "update_status_in_sheet", fake_status)


def test_after_onsite_approved_sheet_text_and_qr(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _create(6010)
    _run(db.approve_onsite(6010, by_staff_id=STAFF_ID, season=SEASON))
    calls = []
    _patch_sheets(monkeypatch, calls)
    bot = _fake_bot()
    _run(onsite_reg.after_onsite_approved(bot, 6010))
    assert ("row", 6010, "new") in calls
    assert ("status", 6010, onsite_reg.ONSITE_SHEET_LABEL) in calls
    assert bot.send_message.await_count == 1
    sent_text = bot.send_message.await_args.args[1]
    assert sent_text == SETTINGS_SCHEMA["onsite_reg_approved_text"]["default"]
    assert bot.send_photo.await_count == 1
    assert bot.send_photo.await_args.args[0] == 6010


def test_after_onsite_approved_no_qr_when_disabled(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("checkin_qr_enabled", "off"))
    _create(6011)
    _run(db.approve_onsite(6011, by_staff_id=STAFF_ID, season=SEASON))
    _patch_sheets(monkeypatch, [])
    bot = _fake_bot()
    _run(onsite_reg.after_onsite_approved(bot, 6011))
    assert bot.send_message.await_count == 1
    assert bot.send_photo.await_count == 0


def test_after_onsite_approved_fail_soft(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _create(6012)
    _run(db.approve_onsite(6012, by_staff_id=STAFF_ID, season=SEASON))
    _patch_sheets(monkeypatch, [], fail=True)
    bot = _fake_bot()
    bot.send_message.side_effect = RuntimeError("бот заблокирован")
    bot.send_photo.side_effect = RuntimeError("бот заблокирован")
    _run(onsite_reg.after_onsite_approved(bot, 6012))  # не падает


def test_after_onsite_approved_unknown_user_no_crash(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sheets(monkeypatch, [])
    bot = _fake_bot()
    _run(onsite_reg.after_onsite_approved(bot, 6099))
    assert bot.send_message.await_count == 0


def test_outbox_routes_onsite_approved(monkeypatch):
    from services import miniapp_outbox

    seen = []

    async def fake_after(bot, tid):
        seen.append(tid)

    monkeypatch.setattr(onsite_reg, "after_onsite_approved", fake_after)
    _run(miniapp_outbox._handle_row(object(), "onsite_approved", {"telegram_id": 6020}))
    assert seen == [6020]


def test_venue_log_action_label():
    from services import venue_log
    assert venue_log.ACTION_ONSITE_APPROVE == "onsite_approve"
    assert venue_log.ACTION_LABELS[venue_log.ACTION_ONSITE_APPROVE] == "📝 одобрил(а) на месте"


def test_onsite_reg_module_does_not_import_sheets_or_aiogram_at_top():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(onsite_reg.__file__).read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module)
    assert not any(n.startswith(("services.sheets", "gspread", "aiogram")) for n in top), top
