"""Раздел «🤝 Амбассадоры» → экран «🙋 Кандидаты и команда» и общая сборка карточки заявки.

- карточка заявки собирается одной функцией (`admin_modcard_render.build_card_text`) для очереди
  «📋 Заявки» и для кнопки «🧾 Анкета» у кандидата;
- список пагинирован, фильтры кнопками, счётчик мест, «привёл / прошли отбор»;
- «✅ Взять» / «⏸ Не сейчас» / «🎁 Пакет выдан» / «🎟 Дать место» / «🚪 Вывести из команды»;
- выгрузка CSV без «@» и формул; права (анкета — moderate_reg).

pytest-asyncio нет — async через `asyncio.run()`; хендлеры зовутся напрямую с фейковыми
Message/CallbackQuery (приём `tests/test_amb_section_34.py`).
"""
from __future__ import annotations

import asyncio
import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from tests._dbtpl import fast_init_db

SEASON = "RT26"
ADMIN_ID = 934101


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_candidates_34.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))
    # Правила отбора и лимита живут только при включённом модуле «🤝 Отбор амбассадоров».
    _run(db.set_setting("amb_team_selection_enabled", "on"))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, *, status="approved", amb_status=None, slot=False, pack=False, reserve=False,
          name=None, username=None, city=None, referrer=None, language=None):
    data = {
        "telegram_id": tid,
        "full_name": name or f"Delegate {tid}",
        "registration_date": "2026-09-01 00:00:00",
        "season": SEASON,
    }
    if username:
        data["username"] = username
    _run(db.add_user(data))
    if status:
        _run(db.set_user_status(tid, status))
    _sql(
        "UPDATE users SET ambassador_status = ?, is_ambassador = ?, ambassador_slot_at = ?, "
        "ambassador_pack_at = ?, ambassador_reserve_at = ?, ambassador_status_at = ?, "
        "event_city = COALESCE(?, event_city), referrer_id = ? WHERE telegram_id = ?",
        (amb_status, 1 if amb_status == "active" else 0,
         "2026-09-02 10:00:00" if slot else None,
         "2026-09-03 10:00:00" if pack else None,
         "2026-09-04 10:00:00" if reserve else None,
         f"2026-09-01 00:{tid % 60:02d}:00", city, referrer, tid),
    )
    if referrer and status == "approved":
        _sql(
            "INSERT OR IGNORE INTO referral_credits (invitee_id, referrer_id, coins, "
            "credited_at, source, season) VALUES (?, ?, 0, '2026-09-01 00:00:00', 'approval', ?)",
            (tid, referrer, SEASON),
        )
    if language:
        _sql("UPDATE users SET language = ? WHERE telegram_id = ?", (language, tid))


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

    async def answer(self, text, parse_mode=None, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))
        return self

    async def answer_document(self, document, caption=None, **kw):
        self.documents.append((document, caption))
        return self


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _buttons(kb):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


# ── общая сборка карточки заявки ────────────────────────────────────────────────────────

def test_build_card_text_matches_queue_card(tmp_path):
    from handlers.applications import admin_moderation
    from handlers.applications.admin_modcard_render import build_card_text
    _ready(tmp_path)
    _seed(500, status="pending", name="Иван <Петров>", username="ivan_p")
    msg = FakeMessage()
    _run(admin_moderation._show_current_card(msg, _new_state()))
    queue_text = msg.answers[-1][0]
    assert "Заявка 1/1" in queue_text

    user = _run(db.get_user(500))
    built = _run(build_card_text(user, position=1, total=1))
    assert built.text == queue_text
    assert built.overflow is False and built.has_history is False


def test_build_card_text_without_position_has_no_counter(tmp_path):
    from handlers.applications.admin_modcard_render import build_card_text
    _ready(tmp_path)
    _seed(501, status="approved", name="Мария")
    user = _run(db.get_user(501))
    text = _run(build_card_text(user)).text
    assert text.startswith("📋 <b>Заявка</b>\n")
    assert text.split("\n", 1)[0] == "📋 <b>Заявка</b>"
    with_pos = _run(build_card_text(user, position=2, total=5)).text
    assert with_pos.startswith("📋 <b>Заявка 2/5</b>\n")
    assert with_pos.split("\n", 1)[1] == text.split("\n", 1)[1]


# ── экран «🙋 Кандидаты и команда» ──────────────────────────────────────────────────────

class FakeMe:
    username = "test_amb_bot"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def get_me(self):
        return FakeMe()

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.sent.append((chat_id, text))


def _cb(data, bot=None, uid=ADMIN_ID):
    cb = FakeCallback(data, uid)
    cb.bot = bot or FakeBot()
    return cb


def _screen(cb):
    return (cb.message.edits or cb.message.answers)[-1]


def _limit(n):
    _run(db.set_setting("amb_join_mode", "selection"))
    _run(db.set_setting("amb_slots_limit", str(n)))


def _amb_row(tid, cols="ambassador_status"):
    return _sql(f"SELECT {cols} FROM users WHERE telegram_id = ?", (tid,))[0]


def test_list_paginates_with_counter_and_filters(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _limit(17)
    for tid in range(100, 112):
        _seed(tid, amb_status="active", slot=True)
    for tid in range(1000, 1023):
        _seed(tid, amb_status="candidate")
    cb = _cb("ambc:candidates:0")
    _run(h.candidates_page(cb))
    text, kb = _screen(cb)
    assert "Занято мест: <b>12 из 17</b>" in text
    assert "Стр. 1 из 3" in text
    assert text.count("привёл 0 / прошли 0") == 10
    buttons = _buttons(kb)
    assert ("Дальше ▶️", "ambc:candidates:10") in buttons
    assert not any(t == "◀️ Назад" for t, _ in buttons)
    assert ("• 🙋 Кандидаты", "ambc:candidates:0") in buttons
    assert ("🤝 В команде", "ambc:team:0") in buttons
    assert ("📭 Без пакета", "ambc:no_pack:0") in buttons
    assert ("🚫 Отказано", "ambc:declined:0") in buttons
    assert sum(1 for _, d in buttons if d.startswith("ambp:")) == 10
    assert "candidate" not in text and "approved" not in text

    last = _cb("ambc:candidates:20")
    _run(h.candidates_page(last))
    text3, kb3 = _screen(last)
    assert "Стр. 3 из 3" in text3 and text3.count("привёл") == 3
    assert ("◀️ Назад", "ambc:candidates:10") in _buttons(kb3)


def test_list_empty_filter_explains(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    cb = _cb("admin_amb_candidates")
    _run(h.show_candidates(cb))
    text, _kb = _screen(cb)
    assert ("Кандидатов пока нет. Они появятся, когда делегаты нажмут «Хочу свою "
            "ссылку» или ответят «да» в анкете.") in text


def test_list_row_format_with_referrals_and_reserve(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _seed(10, amb_status="candidate", reserve=True, name="Иван Петров", username="ivan_p")
    for i in range(5):
        _seed(20 + i, status="approved" if i < 3 else "pending", referrer=10)
    cb = _cb("ambc:candidates:0")
    _run(h.candidates_page(cb))
    text, _ = _screen(cb)
    assert "1. Иван Петров · ivan_p · " in text
    assert "заявка: одобрена · привёл 5 / прошли 3 · ⏸ в запасе" in text


def test_team_list_marks_no_pack(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _seed(10, amb_status="active", slot=True, name="С местом")
    _seed(11, amb_status="active", name="Без места", status="pending")
    cb = _cb("ambc:team:0")
    _run(h.candidates_page(cb))
    text, _ = _screen(cb)
    lines = {ln.split(" · ")[0]: ln for ln in text.split("\n") if ln[:1].isdigit()}
    assert "без пакета" not in lines["1. С местом"]
    assert "без пакета" in lines["2. Без места"]


def test_person_card_buttons_by_status(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _limit(17)
    _seed(10, amb_status="candidate")
    _seed(11, amb_status="declined")
    _seed(12, amb_status="left")
    _seed(13, amb_status="active")               # одобрен, без места, место есть
    _seed(14, amb_status="active", status="pending")

    def card(tid):
        cb = _cb(f"ambp:{tid}:candidates:0")
        _run(h.person_card(cb))
        return _screen(cb)

    b10 = _buttons(card(10)[1])
    assert ("✅ Взять", "ambc_take:10:candidates:0") in b10
    assert ("⏸ Не сейчас", "ambc_later:10:candidates:0") in b10
    assert ("🧾 Анкета", "ambc_card:10") in b10
    assert ("← К списку", "ambc:candidates:0") in b10
    for tid in (11, 12):
        b = _buttons(card(tid)[1])
        assert ("✅ Взять", f"ambc_take:{tid}:candidates:0") in b
        assert not any(d.startswith("ambc_later:") for _, d in b)
    b13 = _buttons(card(13)[1])
    assert ("🎁 Пакет выдан: нет", "ambc_pack:13:candidates:0") in b13
    assert ("🎟 Дать место", "ambc_slot:13:candidates:0") in b13
    assert ("🚪 Вывести из команды", "ambc_rm:13:candidates:0") in b13
    assert not any(d.startswith("ambc_take:") for _, d in b13)
    b14 = _buttons(card(14)[1])
    assert not any(d.startswith("ambc_slot:") for _, d in b14)  # заявка не одобрена


def test_slot_button_hidden_when_full(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _limit(1)
    _seed(10, amb_status="active", slot=True)
    _seed(11, amb_status="active")
    cb = _cb("ambp:11:team:0")
    _run(h.person_card(cb))
    assert not any(d.startswith("ambc_slot:") for _, d in _buttons(_screen(cb)[1]))


def test_stale_person_alerts(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    for handler, data in ((h.person_card, "ambp:999:candidates:0"), (h.take_person, "ambc_take:999"),
                          (h.later_person, "ambc_later:999"), (h.toggle_pack, "ambc_pack:999"),
                          (h.give_slot, "ambc_slot:999"), (h.remove_confirm, "ambc_rm:999"),
                          (h.remove_apply, "ambc_rm_go:999"), (h.show_form_card, "ambc_card:999"),
                          (h.person_card, "ambp:abc")):
        cb = _cb(data)
        _run(handler(cb))
        alert, show = cb.answers[-1]
        assert show and "обновите список" in alert, data


def test_take_with_slot_sends_link_once(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _limit(17)
    for tid in range(100, 112):
        _seed(tid, amb_status="active", slot=True)
    _seed(10, amb_status="candidate")
    bot = FakeBot()
    cb = _cb("ambc_take:10:candidates:0", bot)
    _run(h.take_person(cb))
    alert, show = cb.answers[-1]
    assert show and alert == "Взят. Место за ним — 13 из 17."
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 10 and "https://t.me/test_amb_bot?start=amb_10" in bot.sent[0][1]
    assert "{link}" not in bot.sent[0][1]
    status, slot_at, by = _amb_row(10, "ambassador_status, ambassador_slot_at, ambassador_status_by")
    assert status == "active" and slot_at and by == ADMIN_ID
    # карточка перерисована уже как у человека из команды
    assert any(d.startswith("ambc_rm:") for _, d in _buttons(_screen(cb)[1]))

    again = _cb("ambc_take:10:candidates:0", bot)
    _run(h.take_person(again))
    assert again.answers[-1] == ("Уже в команде", True)
    assert len(bot.sent) == 1


def test_take_without_slot_explains_why(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _limit(1)
    _seed(10, amb_status="candidate", status="pending")
    cb = _cb("ambc_take:10")
    _run(h.take_person(cb))
    assert cb.answers[-1][0] == "Взят без пакета: заявка на форум ещё не одобрена."
    _seed(11, amb_status="active", slot=True)
    _seed(12, amb_status="declined")
    cb2 = _cb("ambc_take:12:declined:0")
    _run(h.take_person(cb2))
    assert cb2.answers[-1][0] == "Взят без пакета: все 1 мест заняты."
    _seed(13, amb_status="left")
    cb3 = _cb("ambc_take:13")
    _run(h.take_person(cb3))
    assert cb3.answers[-1][0].startswith("Взят без пакета")
    assert _amb_row(13)[0] == "active"


def test_take_deferred_by_quiet_hours(tmp_path, monkeypatch):
    from handlers.amb import admin_amb_candidates as h
    from services import quiet_hours
    _ready(tmp_path)
    _seed(10, amb_status="candidate")
    calls = []

    async def fake_queue(now, user_id, text, *, sender, **kw):
        calls.append((user_id, text))
        return False

    monkeypatch.setattr(quiet_hours, "send_or_queue_text", fake_queue)
    bot = FakeBot()
    cb = _cb("ambc_take:10", bot)
    _run(h.take_person(cb))
    assert calls and calls[0][0] == 10 and not bot.sent
    assert "сообщение придёт утром" in cb.answers[-1][0].lower()
    assert len(cb.answers[-1][0]) <= 200


def test_take_message_in_delegate_language(tmp_path, monkeypatch):
    from handlers.amb import admin_amb_candidates as h
    from services import i18n
    from domain.settings.schema import SETTINGS_SCHEMA
    _ready(tmp_path)
    _seed(10, amb_status="candidate")
    ru = SETTINGS_SCHEMA["amb_taken_text"]["default"]
    en_map = {i18n.src_hash(ru): "You are in! {link}"}

    async def fake_context(tid, language_code=None):
        return "en", en_map

    monkeypatch.setattr(i18n, "context", fake_context)
    bot = FakeBot()
    _run(h.take_person(_cb("ambc_take:10", bot)))
    link = "https://t.me/test_amb_bot?start=amb_10"
    expected = i18n.tr(ru, "en", en_map).replace("{link}", link)
    assert bot.sent[0][1] == expected
    assert expected != ru.replace("{link}", link)


def test_later_keeps_candidate_and_sends_nothing(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _seed(10, amb_status="candidate")
    bot = FakeBot()
    cb = _cb("ambc_later:10:candidates:0", bot)
    _run(h.later_person(cb))
    assert cb.answers[-1] == ("Оставлен в запасе — ему ничего не придёт.", True)
    assert not bot.sent
    status, reserve = _amb_row(10, "ambassador_status, ambassador_reserve_at")
    assert status == "candidate" and reserve
    assert "⏸ в запасе" in _screen(cb)[0]


def test_pack_toggle(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _seed(10, amb_status="active", slot=True)
    cb = _cb("ambc_pack:10:team:0")
    _run(h.toggle_pack(cb))
    assert _amb_row(10, "ambassador_pack_at")[0]
    assert ("🎁 Пакет выдан: да", "ambc_pack:10:team:0") in _buttons(_screen(cb)[1])
    cb2 = _cb("ambc_pack:10:team:0")
    _run(h.toggle_pack(cb2))
    assert _amb_row(10, "ambassador_pack_at")[0] is None


def test_give_slot_and_refusals(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _limit(2)
    _seed(9, amb_status="active", slot=True)
    _seed(10, amb_status="active")
    cb = _cb("ambc_slot:10:team:0")
    _run(h.give_slot(cb))
    assert cb.answers[-1] == ("Место выдано — 2 из 2.", True)
    _seed(11, amb_status="active")
    cb2 = _cb("ambc_slot:11")
    _run(h.give_slot(cb2))
    assert cb2.answers[-1][0].startswith("Нельзя: все места заняты")
    _seed(12, amb_status="active", status="pending")
    _run(db.set_setting("amb_slots_limit", "0"))
    cb3 = _cb("ambc_slot:12")
    _run(h.give_slot(cb3))
    assert cb3.answers[-1][0] == "Нельзя: заявка на форум не одобрена."


def test_remove_confirm_texts(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    from domain.settings.schema import SETTINGS_SCHEMA
    _ready(tmp_path)
    _seed(10, amb_status="active", slot=True, name="Иван Петров")
    _seed(11, amb_status="active", slot=True, pack=True, name="Мария")
    cb = _cb("ambc_rm:10:team:0")
    _run(h.remove_confirm(cb))
    text, kb = _screen(cb)
    assert "Вывести Иван Петров из команды?" in text
    assert "Место освободится — можно взять следующего из запаса." in text
    assert "Ему придёт сообщение: «" + SETTINGS_SCHEMA["amb_removed_text"]["default"][:30] in text
    assert ("🚪 Да, вывести из команды", "ambc_rm_go:10:team:0") in _buttons(kb)
    assert _amb_row(10)[0] == "active"
    cb2 = _cb("ambc_rm:11:team:0")
    _run(h.remove_confirm(cb2))
    assert "Пакет уже выдан — место останется за ним и не освободится." in _screen(cb2)[0]


def test_remove_apply_frees_slot_and_notifies_once(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _limit(17)
    for tid in range(100, 111):
        _seed(tid, amb_status="active", slot=True)
    _seed(10, amb_status="active", slot=True)
    bot = FakeBot()
    cb = _cb("ambc_rm_go:10:team:0", bot)
    _run(h.remove_apply(cb))
    assert cb.answers[-1] == ("Выведен из команды. Занято мест: 11 из 17.", True)
    assert len(bot.sent) == 1 and bot.sent[0][0] == 10
    status, slot_at, by = _amb_row(10, "ambassador_status, ambassador_slot_at, ambassador_status_by")
    assert status == "left" and slot_at is None and by == ADMIN_ID
    again = _cb("ambc_rm_go:10:team:0", bot)
    _run(h.remove_apply(again))
    assert again.answers[-1] == ("Он уже не в команде", True)
    assert len(bot.sent) == 1


def test_form_card_and_button_needs_moderate_reg(tmp_path, monkeypatch):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _seed(10, amb_status="candidate", name="Иван")
    cb = _cb("ambc_card:10")
    _run(h.show_form_card(cb))
    text, _ = cb.message.answers[-1]
    assert text.startswith("📋 <b>Заявка</b>") and "Иван" in text

    async def no_reg(uid, cap):
        return cap != "moderate_reg"

    monkeypatch.setattr(h, "has_capability", no_reg)
    card = _cb("ambp:10:candidates:0")
    _run(h.person_card(card))
    assert not any(d.startswith("ambc_card:") for _, d in _buttons(_screen(card)[1]))


# ── выгрузка, раздел, права ─────────────────────────────────────────────────────────────

def test_csv_export_no_at_and_formula_safe(tmp_path):
    import csv as csv_mod
    import io as io_mod
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    _seed(10, amb_status="active", slot=True, pack=True, name="=1+1", username="@ivan_p")
    _seed(11, amb_status="candidate", name="Мария", username="maria")
    _seed(12, name="Не амбассадор")
    _seed(20, referrer=10)
    cb = _cb("ambc_csv")
    _run(h.candidates_csv(cb))
    document, caption = cb.message.documents[-1]
    assert caption == "Амбассадоры и кандидаты: 2 человек"
    assert document.filename == "ambassadors_team.csv"
    raw = document.data
    assert raw.startswith(b"\xef\xbb\xbf")
    body = raw.decode("utf-8-sig")
    assert "@" not in body
    rows = list(csv_mod.reader(io_mod.StringIO(body), delimiter=";"))
    assert rows[0] == h.CSV_HEADERS
    by_id = {r[-1]: r for r in rows[1:]}
    assert set(by_id) == {"10", "11"}
    ivan = by_id["10"]
    assert ivan[0] == "'=1+1" and ivan[1] == "ivan_p"
    assert ivan[3] == "одобрена" and ivan[4] == "в команде" and ivan[5] == "да"
    assert ivan[6].startswith("2026-09-03") and ivan[7] == "1" and ivan[8] == "1"
    assert by_id["11"][4] == "кандидат" and by_id["11"][5] == "нет" and by_id["11"][6] == ""


def test_list_has_csv_button_and_section_row():
    from handlers.settings import admin_sections as sec
    rows = sec.section_rows("amb")
    assert rows.index(("screen", "admin_amb_candidates", "🙋 Кандидаты и команда")) == \
        rows.index(("screen", "admin_amb_entry", "🚪 Вход и лимит")) + 1
    assert sec.back_button("admin_amb_candidates").callback_data == "admin_sec:amb"


def test_list_screen_has_csv_button(tmp_path):
    from handlers.amb import admin_amb_candidates as h
    _ready(tmp_path)
    cb = _cb("admin_amb_candidates")
    _run(h.show_candidates(cb))
    buttons = _buttons(_screen(cb)[1])
    assert ("📥 Выгрузить в таблицу (CSV)", "ambc_csv") in buttons
    assert ("← Назад", "admin_sec:amb") in buttons


def test_entry_screen_links_to_candidates(tmp_path):
    from handlers.amb import admin_amb_section as s
    _ready(tmp_path)
    _seed(10, amb_status="candidate")
    _seed(11, amb_status="candidate")
    _text, kb = _run(s.render_entry_screen(ADMIN_ID))
    assert ("🙋 Кандидаты: 2", "admin_amb_candidates") in _buttons(kb)


def test_every_callback_resolves_to_its_right():
    from handlers.access.admin_caps import required_capability
    game = ("admin_amb_candidates", "ambc:candidates:0", "ambc:team:10", "ambp:10:team:0",
            "ambc_take:10:candidates:0", "ambc_later:10", "ambc_pack:10:team:0",
            "ambc_slot:10", "ambc_rm:10:team:0", "ambc_rm_go:10:team:0", "ambc_csv")
    for data in game:
        assert required_capability(callback_data=data) == "moderate_game", data
    assert required_capability(callback_data="ambc_card:10") == "moderate_reg"


def test_seam_is_registered_on_admin_router():
    import handlers.forum.admin_onsite_reg  # noqa: F401 — хвост admin.router подключает шов
    from handlers.admin import router
    names = [h.callback.__name__ for h in router.callback_query.handlers]
    expected = ["show_candidates", "candidates_page", "person_card", "take_person", "later_person",
                "toggle_pack", "give_slot", "remove_confirm", "remove_apply", "candidates_csv",
                "show_form_card"]
    start = names.index("amb_texts_menu") + 1
    assert names[start:start + len(expected)] == expected
