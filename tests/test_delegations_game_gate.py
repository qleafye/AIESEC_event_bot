"""Делегации вузов (D-08): геймификация для делегатов вузов по умолчанию выключена.

Две половины одного правила в боте (половина Mini App — отдельный план):
- клавиатура (`keyboards/builders.py::get_main_menu_kb`): у делегата вуза при выключенном
  `delegation_game_enabled` нет кнопок «🪙 Мои монеты» / «🎯 Задания»;
- хендлеры (`handlers/user_actions.py::ensure_game_allowed`): гейт на КАЖДОМ входе в монеты и
  задания — старая клавиатура у делегата всё ещё шлёт тексты и колбэки, ответ —
  `delegation_game_off_text`.

Не-делегаты не затронуты в любом положении тумблера; рейтинг чата этот гейт не трогает.
Харнес — как в `tests/test_gamification_delegate_phase9.py` (pytest-asyncio нет, `asyncio.run()`).
"""
import asyncio
import re
import sqlite3
from pathlib import Path

from config import config
from database import db
from handlers import user_actions as ua_mod
from keyboards.builders import get_main_menu_kb, MENU_BUTTONS
from domain.settings.schema import get_setting_typed
from tests._dbtpl import fast_init_db


DELEGATE_ID = 936001
REGULAR_ID = 936002
COINS_TEXT = dict(MENU_BUTTONS)["menu_coins"]
TASKS_TEXT = dict(MENU_BUTTONS)["menu_game_tasks"]


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_delegations_game_gate.db")
    fast_init_db()


def _seed(uid, *, delegation=None):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"User {uid}",
        "registration_date": "2026-10-01",
    }))
    if delegation is not None:
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("UPDATE users SET delegation = ? WHERE telegram_id = ?", (delegation, uid))
        conn.commit()
        conn.close()


def _game_on():
    asyncio.run(db.set_setting("delegation_game_enabled", "on"))


def _off_text():
    return asyncio.run(get_setting_typed("delegation_game_off_text"))


def _kb_texts(kb):
    return [b.text for row in kb.keyboard for b in row]


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers = []
        self.edits = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, parse_mode, reply_markup))


class FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


# ── клавиатура ──────────────────────────────────────────────────────────────────────────────

def test_keyboard_delegate_off_hides_game_buttons(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    texts = _kb_texts(asyncio.run(get_main_menu_kb(DELEGATE_ID)))
    assert COINS_TEXT not in texts
    assert TASKS_TEXT not in texts


def test_keyboard_delegate_on_shows_game_buttons(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    _game_on()
    texts = _kb_texts(asyncio.run(get_main_menu_kb(DELEGATE_ID)))
    assert COINS_TEXT in texts
    assert TASKS_TEXT in texts


def test_keyboard_regular_user_unaffected_by_toggle(tmp_path):
    _db_ready(tmp_path)
    _seed(REGULAR_ID)
    texts_off = _kb_texts(asyncio.run(get_main_menu_kb(REGULAR_ID)))
    assert COINS_TEXT in texts_off and TASKS_TEXT in texts_off
    _game_on()
    assert _kb_texts(asyncio.run(get_main_menu_kb(REGULAR_ID))) == texts_off


def test_keyboard_legacy_call_without_id_unchanged(tmp_path):
    _db_ready(tmp_path)
    texts = _kb_texts(asyncio.run(get_main_menu_kb()))
    assert COINS_TEXT in texts and TASKS_TEXT in texts


def test_keyboard_gate_removes_only_the_two_game_buttons(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    _seed(REGULAR_ID)
    delegate = _kb_texts(asyncio.run(get_main_menu_kb(DELEGATE_ID)))
    regular = _kb_texts(asyncio.run(get_main_menu_kb(REGULAR_ID)))
    assert delegate == [t for t in regular if t not in (COINS_TEXT, TASKS_TEXT)]


# ── ensure_game_allowed ─────────────────────────────────────────────────────────────────────

def test_ensure_game_allowed_regular_user(tmp_path):
    _db_ready(tmp_path)
    _seed(REGULAR_ID)
    msg = FakeMessage(user_id=REGULAR_ID)
    assert asyncio.run(ua_mod.ensure_game_allowed(msg)) is True
    assert msg.answers == []


def test_ensure_game_allowed_delegate_off_replies_off_text(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    msg = FakeMessage()
    assert asyncio.run(ua_mod.ensure_game_allowed(msg)) is False
    assert len(msg.answers) == 1
    assert msg.answers[0][0] == _off_text()


def test_ensure_game_allowed_delegate_on(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    _game_on()
    msg = FakeMessage()
    assert asyncio.run(ua_mod.ensure_game_allowed(msg)) is True
    assert msg.answers == []


def test_ensure_game_allowed_callback_answers_alert(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    cb = FakeCallback("gbal_top")
    assert asyncio.run(ua_mod.ensure_game_allowed(cb)) is False
    assert cb.answers == [(_off_text(), True)]
    assert cb.message.answers == [] and cb.message.edits == []


# ── хендлеры ────────────────────────────────────────────────────────────────────────────────

def test_show_my_coins_delegate_off(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    msg = FakeMessage(text=COINS_TEXT)
    asyncio.run(ua_mod.show_my_coins(msg))
    assert len(msg.answers) == 1
    assert msg.answers[0][0] == _off_text()
    assert msg.answers[0][2] is None  # no balance keyboard


def test_show_game_tasks_delegate_off(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    msg = FakeMessage(text=TASKS_TEXT)
    asyncio.run(ua_mod.show_game_tasks(msg))
    assert [a[0] for a in msg.answers] == [_off_text()]


def test_show_my_coins_delegate_on_renders_balance(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    _game_on()
    msg = FakeMessage(text=COINS_TEXT)
    asyncio.run(ua_mod.show_my_coins(msg))
    assert len(msg.answers) == 1
    assert msg.answers[0][0] != _off_text()
    assert msg.answers[0][2] is not None


def test_gbal_top_callback_delegate_off(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    cb = FakeCallback("gbal_top")
    asyncio.run(ua_mod.gbal_top(cb))
    assert cb.answers == [(_off_text(), True)]
    assert cb.message.edits == []


def test_gtask_open_callback_delegate_off(tmp_path):
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    task_id = asyncio.run(db.create_task("Пост", "Light", 10, "photo", "2026-12-31 23:59:00", 1))
    cb = FakeCallback(f"gtask_open:{task_id}")
    asyncio.run(ua_mod.mytask_open(cb))
    assert cb.answers == [(_off_text(), True)]
    assert cb.message.edits == [] and cb.message.answers == []


def test_every_game_entry_point_is_gated():
    """Сторож на уровне исходника: все входы в монеты/задания зовут гейт."""
    src = Path("handlers/user_actions.py").read_text(encoding="utf-8")
    assert src.count("async def ensure_game_allowed") == 1
    assert src.count("ensure_game_allowed(") >= 7
    for handler in ("show_my_coins", "show_game_tasks", "gbal_history", "gbal_top", "gbal_back",
                    "mytask_open", "mytask_submit_start", "mytask_back", "gtasks_page"):
        m = re.search(rf"async def {handler}\(.*?\n(.*?)(?=\n@router\.|\nasync def |\Z)", src, re.S)
        assert m, handler
        assert "ensure_game_allowed(" in m.group(1), f"{handler} is not gated"


def test_chat_rating_untouched():
    for p in Path("services").rglob("chat_*.py"):
        src = p.read_text(encoding="utf-8")
        assert "ensure_game_allowed" not in src and "delegation_game" not in src, p


def test_leaderboard_command_is_gated_for_delegate(tmp_path):
    """`/рейтинг` — игровой вход, как монеты и задания: делегату при выключенной геме — текст
    «геймы нет», а не таблица рейтинга."""
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    msg = FakeMessage("/рейтинг")
    asyncio.run(ua_mod.show_leaderboard(msg))
    assert len(msg.answers) == 1 and msg.answers[0][0] == _off_text()
    _seed(REGULAR_ID)
    reg = FakeMessage("/рейтинг", user_id=REGULAR_ID)
    asyncio.run(ua_mod.show_leaderboard(reg))
    assert len(reg.answers) == 1 and reg.answers[0][0] != _off_text()


def test_long_off_text_goes_as_message_not_alert(tmp_path):
    """Всплывающее окно Telegram держит 200 символов; текст правит менеджер (до 3500)."""
    _db_ready(tmp_path)
    _seed(DELEGATE_ID, delegation="МГУ")
    long_text = "Игра выключена. " * 40
    asyncio.run(db.set_setting("delegation_game_off_text", long_text))
    cb = FakeCallback("gbal_top")
    assert asyncio.run(ua_mod.ensure_game_allowed(cb)) is False
    assert cb.answers == [(None, False)]
    assert [a[0] for a in cb.message.answers] == [long_text]
