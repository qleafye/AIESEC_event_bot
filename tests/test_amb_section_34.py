"""Раздел «🤝 Амбассадоры» → экран «🚪 Вход и лимит» в админке бота.

- экран: способ входа человеческой подписью, лимит, «Занято мест: N из M», счётчики списков;
- переключение способа входа через подтверждение с тем, что изменится у делегатов;
- ввод лимита числом: пример формата, «0 — без лимита», ошибки с объяснением, отмена;
- подменю шести текстов делегату — только держателю «⚙️ Настройки».

pytest-asyncio нет — async через `asyncio.run()`; хендлеры зовутся напрямую с фейковыми
Message/CallbackQuery (приём `tests/test_amb_tiers_admin_su5.py`).
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
ADMIN_ID = 934001
MANAGER_ID = 934002


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_section_34.db"):
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


def _seed(tid, *, amb_status=None, slot=False):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": "2026-09-01 00:00:00",
        "season": SEASON,
    }))
    _run(db.set_user_status(tid, "approved"))
    if amb_status:
        _sql(
            "UPDATE users SET ambassador_status = ?, is_ambassador = ?, ambassador_slot_at = ? "
            "WHERE telegram_id = ?",
            (amb_status, 1 if amb_status == "active" else 0,
             "2026-09-02 10:00:00" if slot else None, tid),
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

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
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


def _setting(key):
    rows = _sql("SELECT value FROM bot_settings WHERE key = ?", (key,))
    return rows[0][0] if rows else None


def _open_screen(uid=ADMIN_ID):
    from handlers.amb import admin_amb_section as h
    cb = FakeCallback("admin_amb_entry", uid)
    _run(h.show_amb_entry(cb, _new_state(uid)))
    return cb.message.edits[-1]


# ── экран ───────────────────────────────────────────────────────────────────────────────

def test_screen_defaults_instant_no_limit(tmp_path):
    _ready(tmp_path)
    text, kb = _open_screen()
    assert "⚡ Сразу по кнопке" in text
    assert "Мест в команде: <b>без лимита</b>" in text
    assert "instant" not in text and "amb_" not in text
    buttons = _buttons(kb)
    assert ("Способ входа: ⚡ Сразу по кнопке", "ambs_mode") in buttons
    assert ("🎁 Мест в команде: без лимита", "ambs_limit") in buttons
    assert ("✏️ Тексты для делегатов", "ambs_texts") in buttons


def test_screen_counters(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_join_mode", "selection"))
    _run(db.set_setting("amb_slots_limit", "17"))
    for tid in range(100, 112):
        _seed(tid, amb_status="active", slot=True)
    _seed(200, amb_status="active")
    _seed(201, amb_status="active")
    for tid in range(300, 303):
        _seed(tid, amb_status="candidate")
    _seed(400, amb_status="declined")
    text, kb = _open_screen()
    assert "🗳 Отбор менеджером" in text
    assert "Занято мест: <b>12 из 17</b>" in text
    assert "Кандидатов: 3 · В команде: 14 (без пакета: 2) · Отказано: 1" in text
    assert ("🎁 Мест в команде: 17", "ambs_limit") in _buttons(kb)


# ── способ входа ────────────────────────────────────────────────────────────────────────

def test_mode_confirm_explains_and_switches(tmp_path):
    from handlers.amb import admin_amb_section as h
    _ready(tmp_path)
    cb = FakeCallback("ambs_mode")
    _run(h.amb_mode_confirm(cb))
    text, kb = cb.message.edits[-1]
    assert "кандидатом" in text and "«Взять»" in text
    assert "ничего не произойдёт" in text
    assert ("✅ Переключить: 🗳 Отбор менеджером", "ambs_mode_go:selection") in _buttons(kb)
    assert _setting("amb_join_mode") is None  # подтверждение само ничего не пишет

    go = FakeCallback("ambs_mode_go:selection")
    _run(h.amb_mode_apply(go))
    assert _setting("amb_join_mode") == "selection"
    alert, show = go.answers[-1]
    assert show and len(alert) <= 200 and "кандидатом" in alert
    assert "🗳 Отбор менеджером" in go.message.edits[-1][0]

    back = FakeCallback("ambs_mode")
    _run(h.amb_mode_confirm(back))
    assert ("✅ Переключить: ⚡ Сразу по кнопке", "ambs_mode_go:instant") in _buttons(back.message.edits[-1][1])
    go2 = FakeCallback("ambs_mode_go:instant")
    _run(h.amb_mode_apply(go2))
    assert _setting("amb_join_mode") == "instant"
    assert len(go2.answers[-1][0]) <= 200


def test_mode_stale_value_rejected(tmp_path):
    from handlers.amb import admin_amb_section as h
    _ready(tmp_path)
    cb = FakeCallback("ambs_mode_go:weird")
    _run(h.amb_mode_apply(cb))
    assert _setting("amb_join_mode") is None
    assert "устарела" in cb.answers[-1][0]


# ── лимит мест ──────────────────────────────────────────────────────────────────────────

def _limit_input(text, state):
    from handlers.amb import admin_amb_section as h
    msg = FakeMessage(text)
    _run(h.amb_limit_value(msg, state))
    return msg


def test_limit_prompt_and_save(tmp_path):
    from handlers.amb import admin_amb_section as h
    from handlers.states import AmbSlotsEdit
    _ready(tmp_path)
    state = _new_state()
    cb = FakeCallback("ambs_limit")
    _run(h.amb_limit_start(cb, state))
    prompt = cb.message.answers[-1][0]
    assert "например <code>17</code>" in prompt and "без лимита" in prompt
    assert _run(state.get_state()) == AmbSlotsEdit.waiting_for_limit.state

    msg = _limit_input("17", state)
    assert _setting("amb_slots_limit") == "17"
    assert _run(state.get_state()) is None
    assert "✅ Сохранено" in msg.answers[-1][0] and "Мест в команде: <b>17</b>" in msg.answers[-1][0]


def test_limit_not_a_number(tmp_path):
    from handlers.states import AmbSlotsEdit
    _ready(tmp_path)
    state = _new_state()
    _run(state.set_state(AmbSlotsEdit.waiting_for_limit))
    for bad in ("abc", "-3", "17.5", "１７"):
        msg = _limit_input(bad, state)
        assert msg.answers[-1][0] == (
            "Не понял. Пришлите целое число, например 17, или 0, чтобы снять лимит."
        ), bad
    assert _setting("amb_slots_limit") is None
    assert _run(state.get_state()) == AmbSlotsEdit.waiting_for_limit.state


def test_limit_below_taken_rejected_zero_allowed(tmp_path):
    from handlers.states import AmbSlotsEdit
    _ready(tmp_path)
    for tid in range(100, 112):
        _seed(tid, amb_status="active", slot=True)
    state = _new_state()
    _run(state.set_state(AmbSlotsEdit.waiting_for_limit))
    msg = _limit_input("10", state)
    assert msg.answers[-1][0] == (
        "Уже занято 12 мест — лимит не может быть меньше. Освободите места в списке команды "
        "или введите 12 и больше."
    )
    assert _setting("amb_slots_limit") is None
    _limit_input("12", state)
    assert _setting("amb_slots_limit") == "12"
    _run(state.set_state(AmbSlotsEdit.waiting_for_limit))
    _limit_input("0", state)
    assert _setting("amb_slots_limit") == "0"


def test_limit_cancel_changes_nothing(tmp_path):
    from handlers.amb import admin_amb_section as h
    from handlers.states import AmbSlotsEdit
    _ready(tmp_path)
    _run(db.set_setting("amb_slots_limit", "17"))
    state = _new_state()
    _run(state.set_state(AmbSlotsEdit.waiting_for_limit))
    cb = FakeCallback("ambs_limit_cancel")
    _run(h.amb_limit_cancel(cb, state))
    assert _run(state.get_state()) is None
    assert _setting("amb_slots_limit") == "17"
    assert "не менялся" in cb.message.answers[0][0]

    _run(state.set_state(AmbSlotsEdit.waiting_for_limit))
    msg = _limit_input("❌ Отмена", state)
    assert _run(state.get_state()) is None
    assert _setting("amb_slots_limit") == "17"
    assert "не менялся" in msg.answers[-1][0]


# ── тексты для делегатов ────────────────────────────────────────────────────────────────

def test_texts_menu_for_settings_holder(tmp_path):
    from handlers.amb import admin_amb_section as h
    from domain.settings.schema import SETTINGS_SCHEMA
    _ready(tmp_path)
    cb = FakeCallback("ambs_texts")
    _run(h.amb_texts_menu(cb))
    text, kb = cb.message.edits[-1]
    buttons = _buttons(kb)
    keys = [
        "amb_candidate_ack_text", "amb_status_candidate_text", "amb_slots_full_text",
        "amb_taken_text", "amb_removed_text", "amb_decline_all_text",
    ]
    for key in keys:
        assert (SETTINGS_SCHEMA[key]["label"], f"settings_edit:{key}") in buttons
    assert len([b for b in buttons if b[1].startswith("settings_edit:")]) == 6
    assert ("← Назад", "admin_amb_entry") in buttons
    assert "право «⚙️ Настройки»" not in text


def test_texts_menu_without_settings_right(tmp_path):
    from handlers.amb import admin_amb_section as h
    from handlers.access.admin_caps import role_caps_key
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    _run(db.set_setting(role_caps_key("reg_manager"), "moderate_game"))
    cb = FakeCallback("ambs_texts", MANAGER_ID)
    _run(h.amb_texts_menu(cb))
    text, kb = cb.message.edits[-1]
    assert "Тексты меняет тот, у кого есть право «⚙️ Настройки»." in text
    assert not [b for b in _buttons(kb) if b[1].startswith("settings_edit:")]


# ── раздел и права ──────────────────────────────────────────────────────────────────────

def test_section_after_game_with_entry_screen():
    from handlers import admin_sections as sec
    tokens = [t for t, _, _ in sec.SECTIONS]
    assert tokens.index("amb") == tokens.index("game") + 1
    label = dict((t, lbl) for t, lbl, _ in sec.SECTIONS)["amb"]
    assert label == "🤝 Амбассадоры"
    assert ("screen", "admin_amb_entry", "🚪 Вход и лимит") in sec.section_rows("amb")
    assert sec.back_button("admin_amb_entry").callback_data == "admin_sec:amb"


def test_section_visible_to_moderate_game_only():
    from handlers import admin_sections as sec
    assert "amb" in [t for t, _ in sec.visible_sections({"moderate_game"}, False)]
    assert "amb" not in [t for t, _ in sec.visible_sections({"moderate_reg"}, False)]
    # держатель «⚙️ Настройки» видит раздел ради одной строки «Тексты и настройки» — как в «🎮 Геймификации»
    assert "amb" in [t for t, _ in sec.visible_sections({"settings"}, False)]
    assert sec.visible_rows("amb", {"settings"}, False) == [("group", "amb")]


def test_every_callback_and_state_resolves_to_moderate_game():
    from handlers.access.admin_caps import required_capability
    for data in ("admin_amb_entry", "ambs_mode", "ambs_mode_go:selection", "ambs_mode_go:instant",
                 "ambs_limit", "ambs_limit_cancel", "ambs_texts"):
        assert required_capability(callback_data=data) == "moderate_game", data
    assert required_capability(raw_state="AmbSlotsEdit:waiting_for_limit") == "moderate_game"


def test_seam_is_registered_on_admin_router():
    import handlers.forum.admin_onsite_reg  # noqa: F401 — хвост admin.router подключает шов
    from handlers.admin import router
    names = {h.callback.__name__ for h in router.callback_query.handlers}
    assert {"show_amb_entry", "amb_mode_confirm", "amb_mode_apply", "amb_limit_start",
            "amb_limit_cancel", "amb_texts_menu"} <= names
