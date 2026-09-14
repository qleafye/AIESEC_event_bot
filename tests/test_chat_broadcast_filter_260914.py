"""Квик 260914-rgr (RGR-01..07), задача 3: фильтр рассылки «Чат делегатов» — «в чате» /
«не в чате».

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
import json

from config import config
from database import db
from handlers import admin_broadcasts
from handlers import admin_chat
from handlers.states import Broadcast
from services import chat_tracking
from tests.test_roles_phase8 import FakeCallback, FakeMessage, _fresh_state

ADMIN_ID = 900901
MSK_CHAT_ID = -1001111111111
SPB_CHAT_ID = -1002222222222


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "chat_filter.db")
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())


async def _add_user(tid, status="approved", city=None):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, event_city) VALUES (?, ?, ?, ?)",
            (tid, f"Делегат {tid}", status, city),
        )
        await conn.commit()


def _chats_msk_spb():
    """Полная карта двух чатов — msk (город по умолчанию, exclude spb/tyumen) и spb."""
    return [
        {"city": "msk", "chat_id": MSK_CHAT_ID, "exclude": ["spb", "tyumen"]},
        {"city": "spb", "chat_id": SPB_CHAT_ID, "exclude": []},
    ]


# ── D-19: двойная регистрация ────────────────────────────────────────────────────────────

def test_double_registration_delegate_chat():
    assert "delegate_chat" in db._FILTER_COLUMNS
    assert "delegate_chat" in admin_broadcasts._PICKER_FIELDS
    assert "delegate_chat" in db._FILTER_VIRTUAL_FIELDS


def test_get_distinct_filter_values_does_not_crash_on_virtual_field(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(db.get_distinct_filter_values("delegate_chat")) == []


# ── SQL-слой: _build_filter_clause / count_and_list_filtered ────────────────────────────

def test_empty_chats_map_gives_empty_audience_not_everyone(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(1, status="approved", city="msk"))
    ids = asyncio.run(db.count_and_list_filtered(
        [{"field": "delegate_chat", "value": db.CHAT_OUT, "chats": []}]
    ))
    assert ids == []


def test_unknown_value_gives_empty_audience(tmp_path):
    _ready(tmp_path)
    asyncio.run(_add_user(1, status="approved", city="msk"))
    ids = asyncio.run(db.count_and_list_filtered(
        [{"field": "delegate_chat", "value": "garbage", "chats": _chats_msk_spb()}]
    ))
    assert ids == []


def test_in_chat_and_not_in_chat_split_correctly_per_city_d6_d8(tmp_path):
    _ready(tmp_path)
    # msk (город по умолчанию) — в чате / left / restricted (D-8: restricted = «не в чате»).
    asyncio.run(_add_user(101, status="approved", city="msk"))
    asyncio.run(_add_user(102, status="approved", city="msk"))
    asyncio.run(_add_user(103, status="approved", city="msk"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 101, "member", source="test"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 102, "left", source="test"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 103, "restricted", source="test"))
    # легаси-делегат без event_city (NULL) — тоже Москва по построению `_city_clause`.
    asyncio.run(_add_user(104, status="approved", city=None))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 104, "member", source="test"))

    # spb — один в чате.
    asyncio.run(_add_user(201, status="approved", city="spb"))
    asyncio.run(db.upsert_chat_member(SPB_CHAT_ID, 201, "member", source="test"))

    # tyumen — чат НЕ привязан вовсе (D-6): делегат не должен попасть НИ в одну выборку.
    asyncio.run(_add_user(301, status="approved", city="tyumen"))

    chats = _chats_msk_spb()
    in_ids = set(asyncio.run(db.count_and_list_filtered(
        [{"field": "delegate_chat", "value": db.CHAT_IN, "chats": chats}]
    )))
    out_ids = set(asyncio.run(db.count_and_list_filtered(
        [{"field": "delegate_chat", "value": db.CHAT_OUT, "chats": chats}]
    )))

    assert in_ids == {101, 104, 201}
    assert out_ids == {102, 103}
    assert 301 not in in_ids and 301 not in out_ids


def test_filter_spec_survives_json_roundtrip(tmp_path):
    _ready(tmp_path)
    spec = [{
        "field": "delegate_chat", "value": db.CHAT_OUT, "label": "не в чате",
        "chats": _chats_msk_spb(),
    }]
    restored = json.loads(json.dumps(spec))
    assert restored == spec

    asyncio.run(_add_user(1, status="approved", city="msk"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 1, "left", source="test"))
    ids = asyncio.run(db.count_and_list_filtered(restored))
    assert ids == [1]


def test_get_chat_filter_options_thresholds_on_both_sides_present(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(db.get_chat_filter_options([])) == []

    chats = _chats_msk_spb()
    asyncio.run(_add_user(1, status="approved", city="msk"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 1, "member", source="test"))
    # Пока все «в чате» — фильтровать не по чему, опция ровно одна.
    assert asyncio.run(db.get_chat_filter_options(chats)) == [db.CHAT_IN]

    asyncio.run(_add_user(2, status="approved", city="msk"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 2, "left", source="test"))
    options = asyncio.run(db.get_chat_filter_options(chats))
    assert set(options) == {db.CHAT_IN, db.CHAT_OUT}


# ── UI-слой: меню/пикер/мастер ───────────────────────────────────────────────────────────

def test_chat_button_hidden_when_no_chats_bound(tmp_path):
    _ready(tmp_path)
    msg = FakeMessage()
    asyncio.run(admin_broadcasts._render_filter_menu(msg, [], edit=False))
    kb = msg.answers[-1][2]
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "filter_f_delegate_chat" not in flat


def test_chat_button_shown_when_both_sides_present(tmp_path):
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, MSK_CHAT_ID, "Общий чат", None))
    asyncio.run(_add_user(1, status="approved"))
    asyncio.run(_add_user(2, status="approved"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 1, "member", source="test"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 2, "left", source="test"))

    msg = FakeMessage()
    asyncio.run(admin_broadcasts._render_filter_menu(msg, [], edit=False))
    kb = msg.answers[-1][2]
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "filter_f_delegate_chat" in flat


def test_picker_alerts_when_chat_not_bound(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("filter_f_delegate_chat", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)

    asyncio.run(admin_broadcasts._show_value_picker(cb, state, "delegate_chat", "Выберите значение:"))

    assert cb.answers and cb.answers[0][1] is True  # show_alert
    assert "не подключён" in (cb.answers[0][0] or "")


def test_picker_alerts_when_all_delegates_on_one_side(tmp_path):
    """Гейт живёт В ХЭНДЛЕРЕ (WR-04) — даже если кнопка когда-то нарисовалась, пикер сам
    перепроверяет порог на каждый тап (эмулирует «вчерашнее меню с кнопкой живо сегодня»)."""
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, MSK_CHAT_ID, "Общий чат", None))
    asyncio.run(_add_user(1, status="approved"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 1, "member", source="test"))

    cb = FakeCallback("filter_f_delegate_chat", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_broadcasts._show_value_picker(cb, state, "delegate_chat", "Выберите значение:"))

    assert cb.answers and cb.answers[0][1] is True


def test_picker_shows_human_labels_not_codes(tmp_path):
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, MSK_CHAT_ID, "Общий чат", None))
    asyncio.run(_add_user(1, status="approved"))
    asyncio.run(_add_user(2, status="approved"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 1, "member", source="test"))
    asyncio.run(db.upsert_chat_member(MSK_CHAT_ID, 2, "left", source="test"))

    cb = FakeCallback("filter_f_delegate_chat", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_broadcasts._show_value_picker(cb, state, "delegate_chat", "Выберите значение:"))

    data = asyncio.run(state.get_data())
    labels = data.get("filter_option_labels") or {}
    assert labels.get(db.CHAT_IN) == "в чате"
    assert labels.get(db.CHAT_OUT) == "не в чате"


# ── Экран «💬 Чат» -> мастер рассылки с проставленным фильтром ───────────────────────────

def test_chat_broadcast_out_sets_filter_and_state(tmp_path):
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, MSK_CHAT_ID, "Общий чат", None))

    cb = FakeCallback("chat_broadcast_out:global", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    asyncio.run(admin_chat.chat_broadcast_out(cb, state))

    data = asyncio.run(state.get_data())
    assert data["filters"][0]["field"] == "delegate_chat"
    assert data["filters"][0]["value"] == db.CHAT_OUT
    assert data["filters"][0]["chats"][0]["chat_id"] == MSK_CHAT_ID
    assert asyncio.run(state.get_state()) == Broadcast.filter_field.state


def test_chat_broadcast_out_unknown_chat_refuses(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("chat_broadcast_out:spb", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)

    asyncio.run(admin_chat.chat_broadcast_out(cb, state))

    assert cb.answers and cb.answers[0][1] is True
