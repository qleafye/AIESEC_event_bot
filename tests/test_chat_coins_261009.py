"""«+N» ответом гейм-менеджера в чате и разовый перенос баллов из таблицы (09.10)."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

import pytest
from aiogram.types import Chat, Message, User

from config import config
from database import chat_coins_db, db
from handlers.chat import group_chat
from handlers.access.admin_caps import required_capability
from services.chat import chat_coins
from services import chat_tracking
from services.game import coins_transfer
from services.comms import quiet_hours
from tests._dbtpl import fast_init_db

MANAGER = 900100901
DELEGATE = 900100902
STRANGER = 900100903
CHAT = -1009100901
BOT_ID = 777100901


def _run(coro):
    return asyncio.run(coro)


class _Bot:
    id = BOT_ID

    def __init__(self, reaction_fail: set[str] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.reactions: list[tuple[int, int, str]] = []
        self.reaction_fail = reaction_fail or set()

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        self.sent.append((chat_id, text))

    async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
        emoji = reaction[0].emoji
        if emoji in self.reaction_fail:
            raise RuntimeError("REACTION_INVALID")
        self.reactions.append((chat_id, message_id, emoji))


def _user(uid, username=None, is_bot=False):
    return User(id=uid, is_bot=is_bot, first_name="Имя", username=username)


def _msg(mid, uid, text, reply_to=None, username=None):
    return Message(
        message_id=mid, date=datetime.now(), chat=Chat(id=CHAT, type="supergroup", title="Делегаты"),
        from_user=_user(uid, username), text=text, reply_to_message=reply_to,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "chat_coins.db")
    config.ADMIN_IDS = [MANAGER]
    fast_init_db()
    _run(chat_tracking.bind_chat(MANAGER, CHAT, "Делегаты", None))
    with sqlite3.connect(config.DB_PATH) as conn:
        for tid, name in ((DELEGATE, "@delegate_one"), (MANAGER, "@manager")):
            conn.execute(
                "INSERT INTO users (telegram_id, username, full_name, status) VALUES (?, ?, ?, 'approved')",
                (tid, name, "Тест"),
            )

    async def _send_now(now, user_id, text, sender):
        await sender()
        return True

    monkeypatch.setattr(quiet_hours, "send_or_queue_text", _send_now)
    monkeypatch.setattr("services.game.game_sync.request_resync", lambda *a, **k: None)


def _coins():
    with sqlite3.connect(config.DB_PATH) as conn:
        return conn.execute("SELECT user_id, delta, reason, changed_by, source FROM coins").fetchall()


# ── разбор текста ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("+5 за мем", (5, "за мем")),
    ("+ 10", (10, "")),
    ("  +15, за помощь\nв чате", (15, "за помощь в чате")),
    ("+3 — за ответ", (3, "за ответ")),
])
def test_parse_award(text, expected):
    assert chat_coins.parse_award(text) == expected


@pytest.mark.parametrize("text", ["5 за мем", "+79991234567", "спасибо +5", "+", "++5", None])
def test_parse_award_rejects(text):
    assert chat_coins.parse_award(text) is None


def test_award_reason():
    assert chat_coins.award_reason("за мем") == "за мем — в чате"
    assert chat_coins.award_reason("") == "в чате"


# ── «+N» в чате ─────────────────────────────────────────────────────────────────────────

def test_manager_reply_awards_coins_reacts_and_notifies(env):
    bot = _Bot(reaction_fail={"👌"})
    target = _msg(10, DELEGATE, "мой мем", username="delegate_one")
    _run(group_chat.on_group_message(_msg(11, MANAGER, "+5 за мем", reply_to=target), bot))

    assert _coins() == [(DELEGATE, 5, "за мем — в чате", MANAGER, "chat")]
    assert bot.reactions == [(CHAT, 11, "👍")]  # первая реакция запрещена в чате — взяли следующую
    dm = [t for cid, t in bot.sent if cid == DELEGATE]
    assert dm and "+5" in dm[0] and "за мем — в чате" in dm[0] and "5" in dm[0]
    assert all(cid != CHAT for cid, _ in bot.sent)  # в группу бот не пишет


def test_second_award_on_same_message_is_refused_quietly(env):
    bot = _Bot()
    target = _msg(10, DELEGATE, "мем")
    _run(group_chat.on_group_message(_msg(11, MANAGER, "+5", reply_to=target), bot))
    _run(group_chat.on_group_message(_msg(12, MANAGER, "+7 ещё", reply_to=target), bot))

    assert [r[1] for r in _coins()] == [5]
    to_manager = [t for cid, t in bot.sent if cid == MANAGER]
    assert to_manager and "уже начислено +5" in to_manager[-1]


def test_non_manager_plus_is_ordinary_message(env):
    bot = _Bot()
    target = _msg(10, DELEGATE, "мем")
    _run(group_chat.on_group_message(_msg(11, STRANGER, "+5 за мем", reply_to=target), bot))
    assert _coins() == []
    assert bot.sent == [] and bot.reactions == []


@pytest.mark.parametrize("make, needle", [
    (lambda: _msg(11, MANAGER, "+5 за мем"), "ответьте этим сообщением"),
    (lambda: _msg(11, MANAGER, "+5", reply_to=_msg(10, MANAGER, "я")), "себе"),
    (lambda: _msg(11, MANAGER, "+5000", reply_to=_msg(10, DELEGATE, "x")), "от 1 до 1000"),
    (lambda: _msg(11, MANAGER, "+5", reply_to=_msg(10, STRANGER, "x")), "анкету"),
])
def test_refusals_explained_to_manager_in_dm(env, make, needle):
    bot = _Bot()
    _run(group_chat.on_group_message(make(), bot))
    assert _coins() == []
    to_manager = [t for cid, t in bot.sent if cid == MANAGER]
    assert to_manager and needle in to_manager[0]


def test_unbound_chat_ignored(env):
    _run(chat_tracking.unbind_chat(MANAGER, None))
    bot = _Bot()
    _run(group_chat.on_group_message(_msg(11, MANAGER, "+5", reply_to=_msg(10, DELEGATE, "x")), bot))
    assert _coins() == []


def test_chat_and_transfer_rows_show_in_manager_journal(env):
    _run(db.add_coins(DELEGATE, 3, reason="руками", changed_by=MANAGER, source="manual"))
    _run(chat_coins_db.claim_chat_award(chat_id=CHAT, message_id=1, user_id=DELEGATE, amount=5,
                                        reason="в чате", awarded_by=MANAGER, award_message_id=2))
    _run(chat_coins_db.record_transfer([(DELEGATE, 40, "перенос")], MANAGER))
    _run(db.add_coins(DELEGATE, 9, reason="задание", source="task"))
    assert _run(db.count_manual_coin_entries()) == 3
    assert {r["source"] for r in _run(db.list_manual_coin_entries())} == {"manual", "chat", "transfer"}


# ── перенос из таблицы ──────────────────────────────────────────────────────────────────

SHEET = [
    ["Ник", "Задание 1 (#rolemodel)", "Задание 2 (#единомышленник)", "Задание 3 (#работамечты)"],
    ["@delegate_one", "10", "", "15"],
    ["@Delegate_One", "", "25"],
    ["＠chat_nick", "9"],
    ["@nobody_here", "10"],
    ["без ника", "5"],
    ["@empty_row"],
]


def test_parse_values_sums_duplicates_and_labels_by_hashtag():
    parsed = coins_transfer.parse_values(SHEET)
    people = {p.nick.lower(): p for p in parsed.people}
    assert people["delegate_one"].total == 50
    assert people["delegate_one"].parts == {"#rolemodel": 10, "#работамечты": 15, "#единомышленник": 25}
    assert people["chat_nick"].total == 9
    assert "empty_row" not in people
    assert parsed.bad_rows == 1


def test_nick_column_found_without_header():
    values = [["", "a"], ["5", "@someone_x"], ["7", "@other_y"]]
    assert coins_transfer.find_nick_column(values) == 1


def test_transfer_plan_apply_and_rerun_is_idempotent(env):
    chat_user = 900100904
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("INSERT INTO users (telegram_id, username, full_name, status) "
                     "VALUES (?, '@old_nick', 'Тест', 'approved')", (chat_user,))
    _run(db.upsert_chat_username(chat_user, "chat_nick", "Имя"))

    parsed = coins_transfer.parse_values(SHEET)
    plan = _run(coins_transfer.build_plan(parsed))
    assert {tid for _, tid in plan.matched} == {DELEGATE, chat_user}
    assert [p.nick for p in plan.unknown] == ["nobody_here"]
    assert plan.total == 59

    done = _run(coins_transfer.apply_plan(plan, "Гейма чат ", MANAGER))
    assert len(done) == 2
    rows = {r[0]: r for r in _coins()}
    assert rows[DELEGATE][1] == 50 and rows[DELEGATE][4] == "transfer"
    assert rows[DELEGATE][2].startswith("перенос из таблицы «Гейма чат»: #rolemodel 10")

    again = _run(coins_transfer.build_plan(coins_transfer.parse_values(SHEET)))
    assert again.matched == [] and len(again.already) == 2
    assert _run(coins_transfer.apply_plan(plan, "Гейма чат", MANAGER)) == []
    assert len(_coins()) == 2


def test_transfer_callbacks_need_game_right():
    for data in ("admin_coins_transfer", "cointr_go:notify", "cointr_tab:5", "cointr_cancel"):
        assert required_capability(callback_data=data) == "moderate_game"
