"""Квик 260911-mx6 (UAT-SEED-01..04): скрытая самообслуживаемая команда `/uat` — сеялка
состояний приёмки. Покрывает три слоя:

- Гейт (`_uat_open`/`_tester_ids_ok`/`_gate_ok`): тумблер OFF по умолчанию + пустой список
  тестеров, deny-by-default, молча в обоих отказных случаях, резолв и по id, и по @нику из
  самого апдейта (D-3).
- `database.db.purge_miniapp_outbox_for_user`: хвост, который `purge_user` не трогает.
- Хендлеры (`handlers/uat_seed.py`): два пикера, карточка следа, исполнение (сброс+засев+роль).

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async-вызов
обёрнут в `asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`. Фейки
сообщения/колбэка — по образцу `tests/test_delete_user_260910.py`; `FSMContext` — реальный,
по образцу `tests/test_roles_phase8.py::_fresh_state` (MemoryStorage + StorageKey)."""
import asyncio
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from config import config
from database import db

TESTER_ID = 900920
TESTER_USERNAME = "qleafye"
STRANGER_ID = 900921
OTHER_ID = 900922


def _ready(tmp_path, name="test_uat_seed_260911.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [900999]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _open_gate(testers="@Qleafye; 900920"):
    asyncio.run(db.set_setting("uat_seed_enabled", "on"))
    asyncio.run(db.set_setting("uat_seed_testers", testers))


def _import_handlers():
    from handlers import uat_seed
    return uat_seed


# ── Фейки хендлеров (по образцу tests/test_delete_user_260910.py) ──────────────────────────

class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeEditableMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, parse_mode, reply_markup))


class _FakeMessage:
    def __init__(self, text, user_id, username=None):
        self.text = text
        self.from_user = _FakeUser(user_id, username)
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _FakeEditableMessage()
        self.answer_calls = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


# ── Гейт ─────────────────────────────────────────────────────────────────────────────────────

def test_gate_closed_by_default_tester_gets_no_reply(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("uat_seed_testers", str(TESTER_ID)))  # тумблер НЕ включаем
    uat_seed = _import_handlers()

    message = _FakeMessage("/uat", TESTER_ID)
    asyncio.run(uat_seed.cmd_uat(message))

    assert message.answers == []


def test_gate_open_but_stranger_outside_list_gets_no_reply(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    message = _FakeMessage("/uat", STRANGER_ID)
    asyncio.run(uat_seed.cmd_uat(message))

    assert message.answers == []


def test_gate_open_tester_by_numeric_id_passes(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    message = _FakeMessage("/uat", TESTER_ID)
    asyncio.run(uat_seed.cmd_uat(message))

    assert len(message.answers) == 1


def test_gate_open_tester_by_username_no_users_row_passes(tmp_path):
    """В списке «@Qleafye», у отправителя username «qleafye», строки `users` нет вовсе."""
    _ready(tmp_path)
    _open_gate(testers="@Qleafye")
    uat_seed = _import_handlers()
    assert asyncio.run(db.get_user(TESTER_ID)) is None

    message = _FakeMessage("/uat", TESTER_ID, username=TESTER_USERNAME)
    asyncio.run(uat_seed.cmd_uat(message))

    assert len(message.answers) == 1


# ── purge_miniapp_outbox_for_user ───────────────────────────────────────────────────────────

def test_purge_outbox_empty_queue_returns_zero(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(db.purge_miniapp_outbox_for_user(TESTER_ID)) == 0


def test_purge_outbox_removes_only_unprocessed_events_for_this_user(tmp_path):
    _ready(tmp_path)
    now = _now()
    mine = asyncio.run(db.enqueue_miniapp_outbox("submit", {"user_id": TESTER_ID}, now))
    mine_by_tid = asyncio.run(db.enqueue_miniapp_outbox("submit", {"telegram_id": TESTER_ID}, now))
    other = asyncio.run(db.enqueue_miniapp_outbox("submit", {"user_id": OTHER_ID}, now))
    processed_mine = asyncio.run(db.enqueue_miniapp_outbox("submit", {"user_id": TESTER_ID}, now))
    asyncio.run(db.mark_miniapp_outbox_processed([processed_mine], now))

    removed = asyncio.run(db.purge_miniapp_outbox_for_user(TESTER_ID))

    assert removed == 2
    remaining_ids = {
        row["id"] for row in asyncio.run(db.list_unprocessed_miniapp_outbox(limit=50))
    }
    assert mine not in remaining_ids
    assert mine_by_tid not in remaining_ids
    assert other in remaining_ids  # чужое не тронуто

    async def _processed_still_there():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM miniapp_outbox WHERE id = ?", (processed_mine,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0]

    assert asyncio.run(_processed_still_there()) == 1  # уже обработанное не трогаем


def test_purge_outbox_ignores_broken_json_payload(tmp_path):
    _ready(tmp_path)

    async def _seed_broken():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO miniapp_outbox (kind, payload, created_at) VALUES (?, ?, ?)",
                ("submit", "{not json", _now()),
            )
            await conn.commit()

    asyncio.run(_seed_broken())
    removed = asyncio.run(db.purge_miniapp_outbox_for_user(TESTER_ID))
    assert removed == 0


# ── Экран 1: /uat → шесть состояний + отмена ────────────────────────────────────────────────

def test_uat_command_shows_six_states_and_cancel(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    message = _FakeMessage("/uat", TESTER_ID)
    asyncio.run(uat_seed.cmd_uat(message))

    text, parse_mode, kb = message.answers[0]
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert len(buttons) == 7  # шесть состояний + отмена
    for code, _label in uat_seed._STATES:
        assert f"uat_st:{code}" in buttons
    assert "uat_no" in buttons


# ── Экран 2: выбор состояния → четыре роли + строка про админа ─────────────────────────────

def test_pick_state_shows_four_roles_and_admin_note(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    callback = _FakeCallback("uat_st:pending", TESTER_ID)
    asyncio.run(uat_seed.uat_pick_state(callback))

    text, parse_mode, kb = callback.message.edits[0]
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert len(buttons) == 5  # четыре роли + отмена
    for code, _label in uat_seed._ROLES_PICK:
        assert f"uat_role:pending:{code}" in buttons
    assert "админ" in text.lower()


# ── Экран 3: карточка следа ──────────────────────────────────────────────────────────────────

def test_pick_role_shows_footprint_card(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()
    asyncio.run(db.add_user({
        "telegram_id": TESTER_ID, "full_name": "Т", "registration_date": _now(),
    }))

    callback = _FakeCallback("uat_role:pending:both", TESTER_ID)
    asyncio.run(uat_seed.uat_pick_role(callback))

    text = callback.message.edits[0][0]
    assert "заявка" in text  # непустая группа следа, человеческая подпись
    assert "Пропадёт" in text
    assert "Вернуть нельзя" in text
    assert "Менеджер регистраций" in text or "Менеджер геймификации" in text


def test_pick_role_gate_recheck_stranger_no_reply(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    callback = _FakeCallback("uat_role:pending:none", STRANGER_ID)
    asyncio.run(uat_seed.uat_pick_role(callback))

    assert callback.message.edits == []


# ── Экран 4: исполнение (сброс + засев + роль) ──────────────────────────────────────────────

def _go(uat_seed, state_code, role_code, user_id=TESTER_ID, username=None):
    callback = _FakeCallback(f"uat_go:{state_code}:{role_code}", user_id, username)
    state = _fresh_state(user_id)
    asyncio.run(uat_seed.uat_execute(callback, state))
    return callback


def test_go_gate_recheck_disabled_toggle_erases_nothing(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()
    asyncio.run(db.add_user({
        "telegram_id": TESTER_ID, "full_name": "Т", "registration_date": _now(),
    }))
    asyncio.run(db.set_setting("uat_seed_enabled", "off"))  # выключили посреди сценария

    callback = _go(uat_seed, "fresh", "none")

    assert callback.message.edits == []
    assert asyncio.run(db.get_user(TESTER_ID)) is not None  # ничего не стёрто


def test_go_fresh_wipes_all_delegate_rows_and_outbox(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()
    asyncio.run(db.add_user({
        "telegram_id": TESTER_ID, "full_name": "Т", "registration_date": _now(),
    }))
    asyncio.run(db.mark_reg_started(TESTER_ID, "@qleafye"))
    asyncio.run(db.upsert_reg_draft(TESTER_ID, kind="new", source="bot"))
    asyncio.run(db.enqueue_miniapp_outbox("submit", {"user_id": TESTER_ID}, _now()))

    callback = _go(uat_seed, "fresh", "none")

    assert "Готово" in callback.message.edits[0][0]
    assert asyncio.run(db.get_user(TESTER_ID)) is None
    assert asyncio.run(db.get_reg_draft(TESTER_ID)) is None

    async def _reg_started_left():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM reg_started WHERE telegram_id = ?", (TESTER_ID,)
            ) as cursor:
                return (await cursor.fetchone())[0]

    assert asyncio.run(_reg_started_left()) == 0
    assert asyncio.run(db.list_unprocessed_miniapp_outbox(limit=50)) == []


def test_go_draft_leaves_reg_started_and_draft_at_resume_step_empty(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    _go(uat_seed, "draft", "none")

    assert asyncio.run(db.get_user(TESTER_ID)) is None

    async def _reg_started_present():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM reg_started WHERE telegram_id = ?", (TESTER_ID,)
            ) as cursor:
                return (await cursor.fetchone())[0]

    assert asyncio.run(_reg_started_present()) == 1
    draft = asyncio.run(db.get_reg_draft(TESTER_ID))
    assert draft is not None
    assert draft["step"] == "resume"
    from reg_engine import columns_for_step
    for col in columns_for_step("resume"):
        assert not draft["answers"].get(col)


def test_go_pending_fills_status_and_season_and_answers(tmp_path):
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_season", "YL 26/2 test"))
    uat_seed = _import_handlers()

    _go(uat_seed, "pending", "none")

    user = asyncio.run(db.get_user(TESTER_ID))
    assert user["status"] == "pending"
    assert user["season"] == "YL 26/2 test"
    assert user["city"] == uat_seed._SEED_ANSWERS["city"]


def test_go_approved_and_rejected_set_status(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    _go(uat_seed, "approved", "none")
    assert asyncio.run(db.get_user(TESTER_ID))["status"] == "approved"

    _go(uat_seed, "rejected", "none")
    assert asyncio.run(db.get_user(TESTER_ID))["status"] == "rejected"


def test_go_short_track_sets_participant_type_and_pending(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    _go(uat_seed, "short", "none")

    user = asyncio.run(db.get_user(TESTER_ID))
    assert user["participant_type"] == "short"
    assert user["status"] == "pending"


def test_go_role_none_leaves_no_staff_rows(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()
    asyncio.run(db.add_staff(TESTER_ID, "reg_manager", TESTER_ID))

    _go(uat_seed, "fresh", "none")

    assert asyncio.run(db.get_staff_roles(TESTER_ID)) == []


def test_go_role_both_grants_both_roles(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    _go(uat_seed, "fresh", "both")

    assert set(asyncio.run(db.get_staff_roles(TESTER_ID))) == {"reg_manager", "game_manager"}


def test_go_role_switch_from_both_to_reg_drops_game_manager(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    _go(uat_seed, "fresh", "both")
    _go(uat_seed, "fresh", "reg")

    assert asyncio.run(db.get_staff_roles(TESTER_ID)) == ["reg_manager"]


# ── Отмена ───────────────────────────────────────────────────────────────────────────────────

def test_cancel_erases_nothing(tmp_path):
    _ready(tmp_path)
    uat_seed = _import_handlers()
    asyncio.run(db.add_user({
        "telegram_id": TESTER_ID, "full_name": "Т", "registration_date": _now(),
    }))

    callback = _FakeCallback("uat_no", TESTER_ID)
    asyncio.run(uat_seed.uat_cancel(callback))

    assert callback.message.edits[0][0].startswith("Отменено")
    assert asyncio.run(db.get_user(TESTER_ID)) is not None


# ── Инвариант _SEED_ANSWERS ⊆ answer_columns() ──────────────────────────────────────────────

def test_seed_answers_keys_are_all_real_answer_columns():
    from reg_engine import answer_columns
    from handlers import uat_seed
    assert set(uat_seed._SEED_ANSWERS) <= set(answer_columns())
