"""Экран «Чат города»: режим словами, чужой город, подтверждение смены ссылки.

pytest-asyncio нет — корутины через asyncio.run. Схема — fast_init_db.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
from config import config
from database import city_chat_db, db
from handlers.access.admin_caps import required_capability
from handlers.chat import admin_city_chat as screen
from handlers.settings.admin_sections import SECTIONS
from handlers.states import CityChatEdit
from services import chat_tracking
from services.city_chat_link import LinkResult
from tests._dbtpl import fast_init_db

ADMIN = 1
BOUND = 77
MSK_CHAT = -100501
SPB_CHAT = -100502
JOIN_PHRASE = (
    "Сейчас в чат пускают по заявке. Одобренный делегат этого города проходит сам, "
    "остальные ждут вас в заявках."
)
OPEN_PHRASE = (
    "Сейчас ссылка пускает сразу. Кто зайдёт и не является одобренным делегатом "
    "этого города, попадёт в сводку. Сам бот никого не выгоняет."
)
FAIL_PHRASE = "Не смог проверить ссылку в чат. Старую ссылку не отправил."
NO_LINK = "Ссылки ещё нет. Выберите, как пускать людей, и бот её создаст."


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, *, header="msk", bind=True):
    config.DB_PATH = str(tmp_path / "city-admin.db")
    config.ADMIN_IDS = [ADMIN]
    fast_init_db()
    _run(db.set_setting("event_city_enabled", "on"))
    if header is not None:
        assert _run(cities.set_admin_city(ADMIN, header))
    if bind:
        _run(chat_tracking.bind_chat(ADMIN, MSK_CHAT, "Московский чат", "msk"))
    return config.DB_PATH


def _fsm(uid):
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=uid, user_id=uid),
    )


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, uid, text="", bot=None):
        self.from_user = _User(uid)
        self.text = text
        self.bot = bot if bot is not None else object()
        self.reply_markup = None
        self.parse_mode = None
        self.sent = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.parse_mode = parse_mode
        self.reply_markup = reply_markup

    async def answer(self, text=None, parse_mode=None, reply_markup=None, show_alert=False):
        self.sent.append(text)
        self.text = text
        self.parse_mode = parse_mode
        self.reply_markup = reply_markup


class _Cb:
    def __init__(self, data, uid=ADMIN, msg=None):
        self.data = data
        self.from_user = _User(uid)
        self.message = msg or _Msg(uid)
        self.bot = self.message.bot
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _labels(markup):
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _stub_describe(phrase):
    async def _describe(client, city):
        return phrase
    return _describe


def _watch_policy():
    calls = []

    async def _set_policy(client, city, *, join_requests, member_limit, lifetime_days):
        calls.append({
            "city": city,
            "join_requests": join_requests,
            "member_limit": member_limit,
            "lifetime_days": lifetime_days,
        })
        return LinkResult(True, None, None, join_requests)

    return calls, _set_policy


def _source():
    return Path("handlers/chat/admin_city_chat.py").read_text(encoding="utf-8")


def test_screen_quotes_describe(tmp_path, monkeypatch):
    _ready(tmp_path)
    seen = []

    async def _describe(client, city):
        seen.append(city)
        return "Живой ответ площадки."

    monkeypatch.setattr(screen, "describe", _describe)
    cb = _Cb("admin_city_chat")
    _run(screen.city_chat_open(cb, _fsm(ADMIN)))
    assert seen == ["msk"]
    assert "Живой ответ площадки." in cb.message.text
    assert "Московский чат" in cb.message.text
    low = cb.message.text.lower()
    assert "creates_join_request" not in low
    assert "delegate_chat_id" not in low
    assert "export" not in low
    src = _source()
    assert "def render_city_chat_screen" in src
    assert "issue(" not in src
    assert "creates_join_request" not in src
    assert "delegate_chat_id" not in src
    assert "export" not in src


def test_screen_probe_failure_is_not_a_mode(tmp_path, monkeypatch):
    _ready(tmp_path)
    notes = []

    async def _notify(bot, city, phrase):
        notes.append(phrase)

    monkeypatch.setattr(screen, "describe", _stub_describe(FAIL_PHRASE))
    monkeypatch.setattr("handlers.chat.city_chat_api.notify_link_failure", _notify)
    cb = _Cb("admin_city_chat")
    _run(screen.city_chat_open(cb, _fsm(ADMIN)))
    assert FAIL_PHRASE in cb.message.text
    assert "Сейчас в чат пускают по заявке" not in cb.message.text
    assert notes == []


def test_no_link_yet_shows_choice(tmp_path, monkeypatch):
    _ready(tmp_path)

    class _Client:
        def __init__(self):
            self.calls = []

        async def edit_invite(self, *args, **kwargs):
            self.calls.append("edit")

        async def create_invite(self, *args, **kwargs):
            self.calls.append("create")

        async def revoke_invite(self, *args, **kwargs):
            self.calls.append("revoke")

    client = _Client()
    monkeypatch.setattr(screen, "client_from_bot", lambda bot: client)
    text, kb = _run(screen.render_city_chat_screen(ADMIN, None, object()))
    assert NO_LINK in text
    labels = _labels(kb)
    assert "Пускать по заявке" in labels
    assert "Пускать сразу" in labels
    assert "Сводка" in labels
    assert client.calls == []
    assert _run(city_chat_db.get_link("msk")) is None


def test_switch_confirms_when_link_exists(tmp_path, monkeypatch):
    _ready(tmp_path)
    calls, _set_policy = _watch_policy()
    monkeypatch.setattr(screen, "set_policy", _set_policy)
    phrases = {"text": NO_LINK}

    async def _describe(client, city):
        return phrases["text"]

    monkeypatch.setattr(screen, "describe", _describe)
    state = _fsm(ADMIN)
    cb = _Cb("cchat:now:msk")
    _run(screen.city_chat_open_mode(cb, state))
    assert calls == [{"city": "msk", "join_requests": False, "member_limit": None, "lifetime_days": None}]
    assert screen._CONFIRM not in (cb.message.text or "")

    calls.clear()
    _run(city_chat_db.save_link(
        "msk", chat_id=MSK_CHAT, invite_link="https://t.me/+old", creates_join_request=1,
    ))
    phrases["text"] = JOIN_PHRASE
    cb = _Cb("cchat:now:msk")
    _run(screen.city_chat_open_mode(cb, state))
    assert calls == []
    assert cb.message.text == screen._CONFIRM
    assert _labels(cb.message.reply_markup) == ["Да, сменить", "Не менять"]

    stay = _Cb("cchat:stay:msk", msg=cb.message)
    _run(screen.city_chat_stay(stay, state))
    assert calls == []
    assert JOIN_PHRASE in stay.message.text

    again = _Cb("cchat:now:msk")
    _run(screen.city_chat_open_mode(again, state))
    assert calls == []
    yes = _Cb("cchat:apply:msk", msg=again.message)
    _run(screen.city_chat_apply(yes, state))
    assert calls == [{"city": "msk", "join_requests": False, "member_limit": None, "lifetime_days": None}]


def test_limit_blocked_for_join_requests(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(city_chat_db.save_link(
        "msk", chat_id=MSK_CHAT, invite_link="https://t.me/+old", creates_join_request=1,
    ))
    calls, _set_policy = _watch_policy()
    monkeypatch.setattr(screen, "set_policy", _set_policy)
    phrases = {"text": JOIN_PHRASE}

    async def _describe(client, city):
        return phrases["text"]

    monkeypatch.setattr(screen, "describe", _describe)
    state = _fsm(ADMIN)
    blocked = _Cb("cchat:lask:msk")
    _run(screen.city_chat_ask_limit(blocked, state))
    assert calls == []
    assert _run(state.get_state()) is None
    assert any("лимит мест вместе с заявками" in (text or "") for text, _alert in blocked.answers)
    assert blocked.message.text != screen._LIMIT_PROMPT

    phrases["text"] = OPEN_PHRASE
    clear = _Cb("cchat:lnone:msk")
    _run(screen.city_chat_no_limit(clear, state))
    assert calls == []
    assert clear.message.text == screen._CONFIRM
    yes = _Cb("cchat:apply:msk", msg=clear.message)
    _run(screen.city_chat_apply(yes, state))
    assert calls == [{"city": "msk", "join_requests": False, "member_limit": None, "lifetime_days": None}]


def test_bad_number_no_api(tmp_path, monkeypatch):
    _ready(tmp_path)
    calls, _set_policy = _watch_policy()
    clients = []
    monkeypatch.setattr(screen, "set_policy", _set_policy)
    monkeypatch.setattr(screen, "client_from_bot", lambda bot: clients.append(bot))
    state = _fsm(ADMIN)
    _run(state.set_state(CityChatEdit.waiting_for_days))
    _run(state.update_data(token="msk", pending="days"))
    for text in ("много", "0"):
        msg = _Msg(ADMIN, text)
        _run(screen.city_chat_days_value(msg, state))
        assert msg.sent == [screen._BAD_NUMBER]
        assert "целое число" in msg.sent[0]
        assert "например" in msg.sent[0]
    _run(state.set_state(CityChatEdit.waiting_for_limit))
    _run(state.update_data(token="msk", pending="limit"))
    msg = _Msg(ADMIN, "много")
    _run(screen.city_chat_limit_value(msg, state))
    assert calls == []
    assert clients == []
    assert msg.sent == [screen._BAD_NUMBER]


def test_bound_manager_foreign_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.add_staff(BOUND, "reg_manager", ADMIN, city="msk"))
    _run(chat_tracking.bind_chat(ADMIN, SPB_CHAT, "Чат Петербурга", "spb"))
    calls, _set_policy = _watch_policy()
    summaries = []

    async def _summary(bot, chat_id, admin_id, *, page=0):
        summaries.append((chat_id, admin_id, page))

    monkeypatch.setattr(screen, "set_policy", _set_policy)
    monkeypatch.setattr(screen, "send_summary", _summary)
    state = _fsm(BOUND)
    mode = _Cb("cchat:now:spb", uid=BOUND)
    _run(screen.city_chat_open_mode(mode, state))
    opened = _Cb("cchat:open:spb", uid=BOUND)
    _run(screen.city_chat_pick(opened, state))
    summary = _Cb("cchat:sum:spb", uid=BOUND)
    _run(screen.city_chat_summary(summary))
    _run(state.set_state(CityChatEdit.waiting_for_limit))
    _run(state.update_data(token="spb", pending="limit"))
    msg = _Msg(BOUND, "200")
    _run(screen.city_chat_limit_value(msg, state))
    assert calls == []
    assert summaries == []
    assert all(
        any(text == screen._FOREIGN for text, _alert in item.answers)
        for item in (mode, opened, summary)
    )
    assert msg.sent == [screen._FOREIGN]


def test_all_cities_is_a_list(tmp_path, monkeypatch):
    _ready(tmp_path, header=cities.ALL_CITIES)
    _run(chat_tracking.bind_chat(ADMIN, SPB_CHAT, "Чат Петербурга", "spb"))
    _run(db.set_setting("city_label__msk", "Москва"))
    _run(db.set_setting("city_label__spb", "Петербург"))
    _run(db.set_setting("city_label__tyumen", "Тюмень"))

    async def _describe(client, city):
        raise AssertionError(city)

    monkeypatch.setattr(screen, "describe", _describe)
    text, kb = _run(screen.render_city_chat_screen(ADMIN))
    assert text == screen._PICK
    labels = _labels(kb)
    assert "Москва" in labels
    assert "Петербург" in labels
    assert "Тюмень" not in labels
    assert all("msk" not in label and "spb" not in label and "tyumen" not in label for label in labels)
    data = _callbacks(kb)
    assert "cchat:open:msk" in data
    assert "cchat:open:spb" in data
    assert not any(item.startswith("cchat:open:tyumen") for item in data)
    assert _run(city_chat_db.get_link("tyumen")) is None
    mode = [label for label in labels if label in ("Пускать по заявке", "Пускать сразу", "Сводка")]
    assert mode == []


def test_summary_button_one_call(tmp_path, monkeypatch):
    _ready(tmp_path)
    summaries = []
    jobs = []

    async def _summary(bot, chat_id, admin_id, *, page=0):
        summaries.append((bot, chat_id, admin_id, page))

    import services.scheduler as sched

    class _Spy:
        def add_job(self, *args, **kwargs):
            jobs.append((args, kwargs))

    previous = sched._scheduler
    sched._scheduler = _Spy()
    monkeypatch.setattr(screen, "send_summary", _summary)
    try:
        bot = object()
        cb = _Cb("cchat:sum:msk", msg=_Msg(ADMIN, bot=bot))
        _run(screen.city_chat_summary(cb))
    finally:
        sched._scheduler = previous
    assert summaries == [(bot, MSK_CHAT, ADMIN, 0)]
    assert jobs == []


def test_section_row_after_digest_pair():
    rows = next(rows for token, _label, rows in SECTIONS if token == "manage")
    callbacks = [row[1] for row in rows]
    assert "toggle_chat_tracking_enabled" not in callbacks
    assert "toggle_daily_digest" not in callbacks
    chat = next(i for i, row in enumerate(rows) if len(row) > 2 and row[1] == "admin_city_chat")
    rating = next(i for i, row in enumerate(rows) if len(row) > 2 and row[1] == "admin_chat_rating")
    assert rows[chat] == ("screen", "admin_city_chat", "💬 Чат города")
    assert rating == chat + 1


def test_caps_cover_screen_and_summary():
    assert required_capability(callback_data="admin_city_chat") == "settings"
    assert required_capability(callback_data="cchat:open:msk") == "settings"
    assert required_capability(callback_data="cchat:yes:1:2") == "settings"
    assert required_capability(callback_data="cchat:keep:-100:3") == "settings"
    assert required_capability(raw_state="CityChatEdit:waiting_for_days") == "settings"


def test_admin_tail_imports_both():
    text = Path("handlers/admin.py").read_text(encoding="utf-8")
    assert "admin_city_chat_summary" in text
    assert "import admin_city_chat\n" in text or "import admin_city_chat " in text
    lines = text.splitlines()
    summary_at = next(i for i, line in enumerate(lines) if "admin_city_chat_summary" in line)
    screen_at = next(
        i for i, line in enumerate(lines)
        if "import admin_city_chat " in line or line.strip().endswith("admin_city_chat")
    )
    find_at = next(i for i, line in enumerate(lines) if "import admin_find" in line)
    assert summary_at < screen_at < find_at
