"""Phase 09.3 Plan 02 (CITY-08): data-экраны в режиме «все города» — очередь заявок, очередь
чеков, выгрузка CSV, гейма-очередь «🎮 Проверка», проверки «карточка из другого города».

Контракт этого файла: `_admin_city_view` теперь возвращает ТРИ различимых состояния
(`(None, None)` — модуль выключен, `(None, ALL_CITIES_LABEL)` — «все города», `(scope, label)` —
выбран город), и КАЖДЫЙ потребитель этого резолвера обязан различать первые два — иначе экран
«все города» молча печатает подпись «модуля нет».

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path; та же конвенция, что в
tests/test_city_admin_phase72.py / tests/test_city_all_mode_093.py.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
import cities


ADMIN_ID = 930301
NON_ADMIN_ID = 930302


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_all_mode_data_093.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _all_cities_mode(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    ok = asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    assert ok is True


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    """Same double as tests/test_city_admin_phase72.py — captures both edit_text (callback
    re-render) and answer (new-message target)."""

    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.answers_sent = []
        self.answer_markups = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.text = text
        self.markup = reply_markup


class FakeExportMessage:
    """Same double as tests/test_city_export_stats_phase72.py — captures answer_document."""

    def __init__(self):
        self.documents = []

    async def answer_document(self, document, caption=None):
        self.documents.append((document, caption))


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeExportMessage()
        self.bot = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _seed_application(telegram_id, event_city, status="pending", full_name=None):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": full_name or f"User {telegram_id}",
        "registration_date": f"2026-01-01 09:{telegram_id % 60:02d}:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))


def _seed_receipt(telegram_id, event_city, full_name=None):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": full_name or f"User {telegram_id}",
        "registration_date": f"2026-01-01 09:{telegram_id % 60:02d}:00",
        "event_city": event_city,
    }))
    asyncio.run(db.update_payment_status(telegram_id, "receipt_sent", receipt_file_id=f"F{telegram_id}"))


def _seed_task(text="Пост со скрином", category="Light", coins=30, proof_type="text",
               deadline_at="2099-01-01 00:00:00", event_city=None):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at, None, event_city=event_city))


def _seed_submission(task_id, user_id, user_city, content="готово", submitted_at="2026-08-14 10:00:00"):
    asyncio.run(db.add_user({
        "telegram_id": user_id,
        "full_name": f"Delegate {user_id}",
        "registration_date": "2026-08-01",
        "event_city": user_city,
    }))
    return asyncio.run(db.create_submission(task_id, user_id, "text", content, submitted_at))


# ── Task 1: _admin_city_view three-state resolver ───────────────────────────────────────────

def test_admin_city_view_module_off_unchanged(tmp_path):
    _admin_ready(tmp_path)
    assert asyncio.run(admin_mod._admin_city_view(ADMIN_ID)) == (None, None)


def test_admin_city_view_all_cities_scope_none_label_set(tmp_path):
    _all_cities_mode(tmp_path)
    scope, label = asyncio.run(admin_mod._admin_city_view(ADMIN_ID))
    assert scope is None
    assert label == cities.ALL_CITIES_LABEL == "🌍 Все города"


def test_admin_city_view_real_city_unchanged(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    scope, label = asyncio.run(admin_mod._admin_city_view(ADMIN_ID))
    assert scope == ("spb", ())
    assert label == asyncio.run(cities.city_label("spb"))


def test_admin_city_view_three_states_all_distinct(tmp_path):
    """The single invariant this whole plan protects: three READS, three DIFFERENT results."""
    _admin_ready(tmp_path)
    off = asyncio.run(admin_mod._admin_city_view(ADMIN_ID))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    everywhere = asyncio.run(admin_mod._admin_city_view(ADMIN_ID))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    scoped = asyncio.run(admin_mod._admin_city_view(ADMIN_ID))
    assert off == (None, None)
    assert everywhere[0] is None and everywhere[1] is not None
    assert scoped[0] is not None and scoped[1] is not None
    assert off != everywhere != scoped != off


# ── Task 1: _card_out_of_scope / _submission_out_of_scope in ALL_CITIES mode ───────────────

def test_card_out_of_scope_all_cities_never_rejects_any_city(tmp_path):
    _all_cities_mode(tmp_path)
    _seed_application(1, "msk")
    _seed_application(2, "spb")
    _seed_application(3, None)  # no city at all
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 1)) is False
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 2)) is False
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 3)) is False


def test_submission_out_of_scope_all_cities_never_rejects_any_city(tmp_path):
    _all_cities_mode(tmp_path)
    task_id = _seed_task()
    sid_msk = _seed_submission(task_id, 101, "msk")
    sid_spb = _seed_submission(task_id, 102, "spb")
    submission_msk = asyncio.run(db.get_submission(sid_msk))
    submission_spb = asyncio.run(db.get_submission(sid_spb))
    assert asyncio.run(admin_mod._submission_out_of_scope(ADMIN_ID, submission_msk)) is False
    assert asyncio.run(admin_mod._submission_out_of_scope(ADMIN_ID, submission_spb)) is False


# ── Task 1: show_admin_export — CSV caption/filename in ALL_CITIES mode ────────────────────

def test_show_admin_export_all_cities_filename_unchanged_caption_names_mode(tmp_path):
    _all_cities_mode(tmp_path)
    _seed_application(1, "msk")
    _seed_application(2, "spb")
    cb = FakeCallback("admin_export_csv")
    asyncio.run(admin_mod.show_admin_export(cb))
    assert len(cb.message.documents) == 1
    document, caption = cb.message.documents[0]
    assert document.filename == "users.csv"
    assert caption == "База данных пользователей — 🌍 Все города"


def test_show_admin_export_all_cities_rows_are_unfiltered(tmp_path):
    _all_cities_mode(tmp_path)
    _seed_application(1, "msk")
    _seed_application(2, "spb")
    _seed_application(3, None)
    cb = FakeCallback("admin_export_csv")
    asyncio.run(admin_mod.show_admin_export(cb))
    document, _caption = cb.message.documents[0]
    body = document.data.decode("utf-8-sig")
    # All three seeded telegram_ids show up somewhere in the CSV body (no city filter applied).
    for tid in ("1", "2", "3"):
        assert tid in body


def test_show_admin_export_module_off_caption_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    _seed_application(1, "spb")
    cb = FakeCallback("admin_export_csv")
    asyncio.run(admin_mod.show_admin_export(cb))
    document, caption = cb.message.documents[0]
    assert document.filename == "users.csv"
    assert caption == "База данных пользователей"


# ── Task 2: applications queue — card names the DELEGATE's own city in ALL_CITIES mode ─────

def test_show_current_card_all_cities_each_card_names_its_own_delegate_city(tmp_path):
    _all_cities_mode(tmp_path)
    _seed_application(1, "msk", full_name="Moscow Delegate")
    _seed_application(2, "spb", full_name="Piter Delegate")
    state = _new_state(ADMIN_ID)
    target1 = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target1, state))
    assert "· 🏙 Москва" in target1.text
    assert cities.ALL_CITIES_LABEL not in target1.text
    # skip the first card, the second (other city) must be named correctly too
    asyncio.run(state.update_data(appr_skipped=[1]))
    target2 = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target2, state))
    assert "· 🏙 Санкт-Петербург" in target2.text
    assert cities.ALL_CITIES_LABEL not in target2.text


def test_show_current_card_all_cities_null_city_uses_default(tmp_path):
    _all_cities_mode(tmp_path)
    _seed_application(1, None)
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    default_label = asyncio.run(cities.city_label(cities.default_city_code()))
    assert f"· 🏙 {default_label}" in target.text


def test_show_current_card_selected_city_still_uses_header_label(tmp_path):
    """Regression: a real selected city must still pass `label` straight through unchanged."""
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    _seed_application(1, "spb", full_name="Piter Delegate")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "· 🏙 Санкт-Петербург" in target.text


def test_show_current_card_module_off_header_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    _seed_application(1, "spb")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "🏙" not in target.text.split("\n")[0]


def test_show_current_card_all_cities_empty_queue_names_mode(tmp_path):
    _all_cities_mode(tmp_path)
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert target.answers_sent == ["✅ Заявок нет — «🌍 Все города»."]


# ── Task 2: receipts queue — same contract as applications queue ───────────────────────────

def test_show_current_receipt_card_all_cities_each_card_names_its_own_delegate_city(tmp_path):
    _all_cities_mode(tmp_path)
    _seed_receipt(1, "msk", full_name="Moscow Delegate")
    _seed_receipt(2, "spb", full_name="Piter Delegate")
    state = _new_state(ADMIN_ID)
    target1 = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target1, state))
    assert "· 🏙 Москва" in target1.text
    assert cities.ALL_CITIES_LABEL not in target1.text
    asyncio.run(state.update_data(rcpt_skipped=[1]))
    target2 = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target2, state))
    assert "· 🏙 Санкт-Петербург" in target2.text
    assert cities.ALL_CITIES_LABEL not in target2.text


def test_show_current_receipt_card_selected_city_still_uses_header_label(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    _seed_receipt(1, "spb", full_name="Piter Delegate")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert "· 🏙 Санкт-Петербург" in target.text


def test_show_current_receipt_card_module_off_header_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    _seed_receipt(1, "spb")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert "🏙" not in target.text.split("\n")[0]


def test_show_current_receipt_card_all_cities_empty_queue_names_mode(tmp_path):
    _all_cities_mode(tmp_path)
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert target.answers_sent == ["✅ Чеков на проверке нет — «🌍 Все города»."]


# ── Task 2: gamification review queue («🎮 Проверка») — unfiltered in ALL_CITIES mode ───────

def test_show_current_submission_all_cities_shows_every_city(tmp_path):
    _all_cities_mode(tmp_path)
    task_id = _seed_task()
    sid_msk = _seed_submission(task_id, 201, "msk", submitted_at="2026-08-14 10:00:00")
    _seed_submission(task_id, 202, "spb", submitted_at="2026-08-14 10:01:00")
    state = _new_state(ADMIN_ID)

    target1 = FakeMessage()
    asyncio.run(admin_mod._show_current_submission(target1, state))
    assert "Delegate 201" in target1.answers_sent[0]

    asyncio.run(state.update_data(grev_skipped=[sid_msk]))
    target2 = FakeMessage()
    asyncio.run(admin_mod._show_current_submission(target2, state))
    assert "Delegate 202" in target2.answers_sent[0]
