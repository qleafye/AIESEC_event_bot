"""Квалифицированная амбассадорка СкиллАп 5: экран «🎓 Ступени амбассадоров» в админке.

- выгрузка CSV по амбассадорам: ровно 10 колонок, сортировка по ТЗ, защита от формул и —
  главное — ни одного поля приглашённых (приёмка 10);
- тумблеры программы и скрытия имён кнопками;
- ручное исключение приглашённого из зачёта и возврат (ступени только вверх);
- права: все callback'и экрана в ADMIN_CAPS с moderate_game.

pytest-asyncio нет — async через `asyncio.run()`; хендлеры зовутся напрямую с фейковыми
Message/CallbackQuery (приём `tests/test_ambassador_waves_crud_32.py`).
"""
from __future__ import annotations

import asyncio
import csv
import io
import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import amb_tiers_db as tdb
from database import db
from services.amb import amb_tiers
from tests._dbtpl import fast_init_db

SEASON = "SU26"
ADMIN_ID = 931001


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_tiers_admin_su5.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed_user(tid, *, referrer_id=None, status="pending", full_name=None, username=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "referrer_id": referrer_id,
        "season": SEASON,
    }))
    _run(db.set_user_status(tid, status))
    if username:
        _sql("UPDATE users SET username = ? WHERE telegram_id = ?", (username, tid))
    if referrer_id and status == "approved":
        # «Прошли отбор» считается из журнала зачётов, а не из users.status.
        _sql(
            "INSERT OR IGNORE INTO referral_credits (invitee_id, referrer_id, coins, "
            "credited_at, source, season) VALUES (?, ?, 0, '2026-09-01 00:00:00', 'approval', ?)",
            (tid, referrer_id, SEASON),
        )


def _make_ambassador(tid, *, username=None, since="2026-09-01 10:00:00"):
    _seed_user(tid, status="approved", full_name=f"Амбассадор {tid}", username=username)
    _run(db.set_ambassador_flag(tid, active=True, at=since))


def _checkin(tid):
    _sql(
        "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
        "VALUES (?, 'entry', '2026-11-21 10:00:00', 'scan', '2026-11-21 10:00:00', '2026-11-21')",
        (tid,),
    )


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.caption = None
        self.from_user = FakeUser(user_id)
        self.answers = []
        self.edits = []
        self.documents = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return self

    async def answer_document(self, document, caption=None):
        self.documents.append((document, caption))
        return self


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _csv_rows():
    from handlers.amb import admin_amb_tiers as h
    cb = FakeCallback("ambt_csv")
    _run(h.amb_tiers_csv(cb))
    assert len(cb.message.documents) == 1
    document, caption = cb.message.documents[0]
    assert document.filename == "ambassadors_tiers.csv"
    raw = document.data.decode("utf-8-sig")
    return raw, list(csv.reader(io.StringIO(raw), delimiter=";"))


# ── CSV ─────────────────────────────────────────────────────────────────────────────────

def test_csv_has_no_invitee_fields_acceptance_10(tmp_path):
    """Приёмка 10: в выгрузке нет ни имён, ни username, ни telegram_id приглашённых."""
    _ready(tmp_path)
    _make_ambassador(100, username="amb_one")
    _make_ambassador(110, username="amb_two")
    _seed_user(770201, referrer_id=100, status="approved", full_name="Иван Уникальный", username="ivan_unique")
    _seed_user(770202, referrer_id=100, full_name="Мария Скрытая", username="maria_hidden")
    _seed_user(770301, referrer_id=110, status="approved", full_name="Пётр Особый", username="petr_special")

    raw, rows = _csv_rows()
    assert rows[0] == [
        "telegram_id", "@username", "Дата вступления", "Всего по ссылке", "На рассмотрении",
        "Прошли отбор", "Дошли", "Текущая ступень", "Дата ступени", "Разбор резюме (O2O)",
    ]
    assert all(len(r) == 10 for r in rows)
    for needle in ("Иван", "Уникальный", "Мария", "Пётр", "Особый",
                   "ivan_unique", "maria_hidden", "petr_special", "770201", "770202", "770301"):
        assert needle not in raw, needle
    by_id = {r[0]: r for r in rows[1:]}
    assert set(by_id) == {"100", "110"}
    assert by_id["100"][1] == "amb_one"
    assert by_id["100"][3:7] == ["2", "1", "1", "0"]


def test_csv_sort_arrived_then_qualified_then_earlier_tier(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_qualified_program", "on"))
    # A: 3 прошли, 2 дошли. B: 7 прошли, 0 дошли. C и D: по 1 прошедшему, C получил ступень раньше.
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100, status="approved")
    _checkin(201)
    _checkin(202)
    _make_ambassador(110)
    for tid in range(301, 308):
        _seed_user(tid, referrer_id=110, status="approved")
    _make_ambassador(120)
    _seed_user(401, referrer_id=120, status="approved")
    _make_ambassador(130)
    _seed_user(501, referrer_id=130, status="approved")
    _run(tdb.claim_new_tiers(130, [1], "2026-10-02 10:00:00", 15))
    _run(tdb.claim_new_tiers(120, [1], "2026-10-01 10:00:00", 15))

    _, rows = _csv_rows()
    order = [r[0] for r in rows[1:]]
    assert order == ["100", "110", "120", "130"]
    row_a = rows[1]
    assert row_a[5] == "3" and row_a[6] == "2"
    assert rows[3][7] == "1" and rows[3][8] == "2026-10-01 10:00:00"


def test_csv_o2o_labels_and_tier_columns(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    _make_ambassador(110)
    _run(tdb.claim_new_tiers(100, [1, 2], "2026-10-01 10:00:00", 1))
    _run(tdb.claim_new_tiers(110, [1, 2], "2026-10-02 10:00:00", 1))
    _, rows = _csv_rows()
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["100"][7] == "2" and by_id["100"][9] == "выдан"
    assert by_id["110"][9] == "лист ожидания"


def test_csv_formula_injection_escaped(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    _sql("UPDATE users SET ambassador_since = '=HYPERLINK(1)' WHERE telegram_id = 100")
    _, rows = _csv_rows()
    assert rows[1][2] == "'=HYPERLINK(1)"


# ── тумблеры ────────────────────────────────────────────────────────────────────────────

def test_program_toggle_off_on_off(tmp_path):
    from handlers.amb import admin_amb_tiers as h
    _ready(tmp_path)
    assert not _run(amb_tiers.program_on())
    cb = FakeCallback("ambt_toggle:program")
    _run(h.amb_tiers_toggle(cb))
    assert _run(amb_tiers.program_on())
    text, show_alert = cb.answers[-1]
    assert show_alert and "Пересчитать ступени" in text and len(text) <= 200
    screen = cb.message.edits[-1][0]
    assert "включена" in screen and "amb_qualified_program" not in screen

    cb2 = FakeCallback("ambt_toggle:program")
    _run(h.amb_tiers_toggle(cb2))
    assert not _run(amb_tiers.program_on())


def test_hide_names_toggle(tmp_path):
    from handlers.amb import admin_amb_tiers as h
    _ready(tmp_path)
    cb = FakeCallback("ambt_toggle:hide")
    _run(h.amb_tiers_toggle(cb))
    assert _run(db.get_setting("amb_hide_invitee_names")) == "on"
    assert len(cb.answers[-1][0]) <= 200


def test_screen_off_says_disabled_and_has_buttons(tmp_path):
    from handlers.amb import admin_amb_tiers as h
    _ready(tmp_path)
    cb = FakeCallback("admin_amb_tiers")
    _run(h.show_amb_tiers(cb, _new_state()))
    text, kb = cb.message.edits[-1]
    assert "выключена" in text
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    for expected in ("ambt_toggle:program", "ambt_toggle:hide", "ambt_csv", "ambt_excl",
                     "ambt_excl_list:0", "ambl:main", "admin_sec:game"):
        assert expected in callbacks, expected
    # подписи кнопок — человеческие, без кодовых имён ключей
    labels = " ".join(b.text for row in kb.inline_keyboard for b in row)
    assert "amb_" not in labels and "amb_" not in text


# ── исключение и возврат ─────────────────────────────────────────────────────────────────

def _exclude_flow(invitee_input: str, reason="накрутка — аккаунт создан в день регистрации"):
    from handlers.amb import admin_amb_tiers as h
    state = _new_state()
    _run(h.amb_exclude_start(FakeCallback("ambt_excl"), state))
    msg = FakeMessage(invitee_input)
    _run(h.amb_exclude_person_step(msg, state))
    msg2 = FakeMessage(reason)
    _run(h.amb_exclude_reason_step(msg2, state))
    confirm_text = msg2.answers[-1][0]
    cb = FakeCallback("ambt_excl_go")
    _run(h.amb_exclude_go(cb, state))
    return msg, confirm_text, cb, state


def test_exclude_invitee_removes_from_counts_keeps_tiers(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_qualified_program", "on"))
    _make_ambassador(100)
    _seed_user(201, referrer_id=100, status="approved", username="cheater")
    _seed_user(202, referrer_id=100, status="approved")
    _run(amb_tiers.check_tiers([100]))
    assert [r["tier"] for r in _run(tdb.list_tiers(100))] == [1]

    msg, confirm_text, cb, state = _exclude_flow("@cheater")
    assert "Пригласил" in msg.answers[-1][0] and "Амбассадор 100" in msg.answers[-1][0]
    assert "Снять ступень" in confirm_text

    counts = _run(tdb.referral_counts(100, SEASON))
    assert counts["total"] == 1 and counts["qualified"] == 1
    row = _run(tdb.get_exclusion(201))
    assert row["reason"].startswith("накрутка") and row["excluded_by"] == ADMIN_ID
    assert [r["tier"] for r in _run(tdb.list_tiers(100))] == [1]
    assert _run(state.get_state()) is None


def test_person_without_referrer_keeps_waiting(tmp_path):
    from handlers.amb import admin_amb_tiers as h
    _ready(tmp_path)
    _seed_user(201)
    state = _new_state()
    _run(h.amb_exclude_start(FakeCallback("ambt_excl"), state))
    msg = FakeMessage("201")
    _run(h.amb_exclude_person_step(msg, state))
    assert "не по чьей-то ссылке" in msg.answers[-1][0]
    assert _run(state.get_state()) == "AmbExclude:waiting_for_person"

    msg2 = FakeMessage("@nobody_here")
    _run(h.amb_exclude_person_step(msg2, state))
    assert "Не нашёл такого человека" in msg2.answers[-1][0]
    msg3 = FakeMessage("какой-то текст")
    _run(h.amb_exclude_person_step(msg3, state))
    assert "@username" in msg3.answers[-1][0]
    assert _run(state.get_state()) == "AmbExclude:waiting_for_person"


def test_unexclude_rechecks_tiers_upwards(tmp_path):
    from handlers.amb import admin_amb_tiers as h
    _ready(tmp_path)
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100, status="approved")
    _exclude_flow("203")
    _run(db.set_setting("amb_qualified_program", "on"))
    _run(amb_tiers.check_tiers([100]))
    assert [r["tier"] for r in _run(tdb.list_tiers(100))] == [1]

    list_cb = FakeCallback("ambt_excl_list:0")
    _run(h.amb_exclusions_list(list_cb))
    callbacks = [b.callback_data for row in list_cb.message.edits[-1][1].inline_keyboard for b in row]
    assert "ambt_unexcl:203" in callbacks

    confirm_cb = FakeCallback("ambt_unexcl:203")
    _run(h.amb_unexclude_confirm(confirm_cb))
    assert "ambt_unexcl_go:203" in [
        b.callback_data for row in confirm_cb.message.edits[-1][1].inline_keyboard for b in row
    ]
    _run(h.amb_unexclude_go(FakeCallback("ambt_unexcl_go:203")))
    assert _run(tdb.get_exclusion(203)) is None
    assert [r["tier"] for r in _run(tdb.list_tiers(100))] == [1, 2]

    again = FakeCallback("ambt_unexcl_go:203")
    _run(h.amb_unexclude_go(again))
    assert "уже вернули" in again.answers[-1][0]


def test_exclude_with_program_off_creates_no_tiers(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    for tid in (201, 202, 203, 204):
        _seed_user(tid, referrer_id=100, status="approved")
    _exclude_flow("204")
    from handlers.amb import admin_amb_tiers as h
    _run(h.amb_unexclude_go(FakeCallback("ambt_unexcl_go:204")))
    assert _run(tdb.list_tiers(100)) == []
    assert _sql("SELECT COUNT(*) FROM miniapp_outbox")[0][0] == 0


def test_stale_confirm_button(tmp_path):
    from handlers.amb import admin_amb_tiers as h
    _ready(tmp_path)
    cb = FakeCallback("ambt_excl_go")
    _run(h.amb_exclude_go(cb, _new_state()))
    assert cb.answers[-1][1] is True and "устарела" in cb.answers[-1][0]


# ── права ───────────────────────────────────────────────────────────────────────────────

def test_all_amb_tiers_callbacks_require_moderate_game():
    from handlers.access.admin_caps import required_capability
    for data in ("admin_amb_tiers", "ambt_toggle:program", "ambt_toggle:hide", "ambt_csv",
                 "ambt_excl", "ambt_excl_go", "ambt_excl_cancel", "ambt_excl_list:10",
                 "ambt_unexcl:5", "ambt_unexcl_go:5"):
        assert required_capability(callback_data=data) == "moderate_game", data
