"""Quick 260906-8uq (FAQ-01..06): раздел «❓ Частые вопросы» — хранение (`faq_items` +
аксессоры `database/db.py`), чистое правило перекрытия по городу (`services/faq.py`), экран
делегата (бот) и экран менеджера (бот).

pytest-asyncio в проекте нет — каждый async-вызов через `asyncio.run()`; БД — tmp_path,
харнесс `_ready`/`_add_user` в форме `tests/test_sheet_logs_260902.py`.

Задача 1 — блок ниже. Задачи 2/3 дописаны отдельными блоками дальше в этом же файле.
"""
import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from services import faq as faq_service
from handlers import user_actions as ua_mod
from keyboards.builders import get_main_menu_kb
from settings_schema import SETTINGS_SCHEMA


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="faq_260906.db"):
    config.DB_PATH = str(tmp_path / name)
    config.GOOGLE_SHEET_ID = ""
    _run(db.init_db())


async def _seed_cities(rows):
    """rows: list of (code, label, tab_base, sort_order[, enabled]) — форма
    tests/test_cities_registry_260818.py::_seed_cities_db. Нужно ТОЛЬКО тем тестам, что
    резолвят `cities.city_scope("kzn")` — чистое правило `services/faq.py` в резолве города
    не нуждается вовсе."""
    import cities
    for r in rows:
        code, label, tab_base, sort_order = r[0], r[1], r[2], r[3]
        enabled = r[4] if len(r) > 4 else 1
        await db.insert_city(code, label, tab_base, sort_order, enabled)
    await cities.reload_cities()


@pytest.fixture
def _restore_cities_cache():
    """`cities.reload_cities()` мутирует `cities.CITIES` НА МЕСТЕ (см. докстринг в cities.py —
    другие модули держат `from cities import CITIES`, ребинд сломал бы их алиасы), а conftest.py
    этого проекта намеренно не сбрасывает состояние между тестами/файлами. Без восстановления
    тест, дописавший «kzn»/«msk» в реестр, продолжает жить и в следующих тестовых файлах того
    же процесса (в т.ч. tests/test_admin_percity_menu.py, который ждёт СВОЙ набор городов)."""
    import cities
    snapshot = list(cities.CITIES)
    yield
    cities.CITIES.clear()
    cities.CITIES.extend(snapshot)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: normalize_question / apply_city_overrides / city_badge / short (services/faq.py)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_normalize_question_collapses_case_and_whitespace_and_trims_trailing_punct():
    a = faq_service.normalize_question("  Где ПРОХОДИТ форум?? ")
    b = faq_service.normalize_question("где проходит форум")
    assert a == b == "где проходит форум"


def test_normalize_question_empty_and_none_are_safe():
    assert faq_service.normalize_question(None) == ""
    assert faq_service.normalize_question("   ") == ""


def test_apply_city_overrides_city_none_keeps_only_general_rows():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "kzn", "question": "Где проходит форум?", "position": 0},
        {"id": 3, "city": "kzn", "question": "Сколько стоит?", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, None)
    assert [r["id"] for r in result] == [1]


def test_apply_city_overrides_same_question_city_wins_over_general():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "kzn", "question": "где проходит форум??", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, "kzn")
    assert [r["id"] for r in result] == [2]


def test_apply_city_overrides_different_question_both_visible():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "kzn", "question": "Что взять с собой?", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, "kzn")
    assert {r["id"] for r in result} == {1, 2}


def test_apply_city_overrides_hides_other_citys_row():
    rows = [
        {"id": 1, "city": None, "question": "Где проходит форум?", "position": 0},
        {"id": 2, "city": "spb", "question": "Что взять с собой?", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, "kzn")
    assert [r["id"] for r in result] == [1]


def test_apply_city_overrides_sorts_by_position_then_id_on_ties():
    rows = [
        {"id": 5, "city": None, "question": "Б", "position": 0},
        {"id": 3, "city": None, "question": "А", "position": 0},
        {"id": 4, "city": None, "question": "В", "position": 1},
    ]
    result = faq_service.apply_city_overrides(rows, None)
    assert [r["id"] for r in result] == [3, 5, 4]


def test_city_badge_general_vs_city_label():
    assert faq_service.city_badge(None) == "🌍 все города"
    assert faq_service.city_badge("Казань") == "🏙 Казань"


def test_short_truncates_with_ellipsis():
    assert faq_service.short("Где проходит форум?", 60) == "Где проходит форум?"
    long_text = "А" * 70
    result = faq_service.short(long_text, 10)
    assert result == "А" * 9 + "…"
    assert len(result) == 10


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: аксессоры database/db.py
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_init_db_creates_faq_items_table(tmp_path):
    _ready(tmp_path)

    async def _check():
        async with db._connect() as conn:
            async with conn.execute("PRAGMA table_info(faq_items)") as cursor:
                cols = {row[1] for row in await cursor.fetchall()}
        return cols

    cols = _run(_check())
    assert cols == {
        "id", "city", "question", "answer", "position", "enabled", "created_at", "created_by",
    }


def test_init_db_is_idempotent_and_keeps_existing_faq_rows(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(
        city=None, question="Где проходит форум?", answer="В кампусе.", created_by=1,
    ))
    _run(db.init_db())  # повторный запуск на существующей БД не должен падать/терять строки
    row = _run(db.get_faq_item(item_id))
    assert row is not None
    assert row["question"] == "Где проходит форум?"


def test_create_faq_item_appends_at_end_position(tmp_path):
    _ready(tmp_path)
    first = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    second = _run(db.create_faq_item(city=None, question="B?", answer="b", created_by=1))
    row1 = _run(db.get_faq_item(first))
    row2 = _run(db.get_faq_item(second))
    assert row2["position"] > row1["position"]


def test_reorder_faq_items_writes_sequential_positions(tmp_path):
    _ready(tmp_path)
    a = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    b = _run(db.create_faq_item(city=None, question="B?", answer="b", created_by=1))
    c = _run(db.create_faq_item(city=None, question="C?", answer="c", created_by=1))
    _run(db.reorder_faq_items([c, a, b]))
    rows = {r["id"]: r["position"] for r in _run(db.list_faq_items())}
    assert rows[c] == 0 and rows[a] == 1 and rows[b] == 2


def test_list_faq_items_with_city_scope_includes_city_and_general(tmp_path, _restore_cities_cache):
    _ready(tmp_path)
    _run(_seed_cities([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)]))
    import cities
    general = _run(db.create_faq_item(city=None, question="Общий?", answer="o", created_by=1))
    kzn_item = _run(db.create_faq_item(city="kzn", question="Только Казань?", answer="k", created_by=1))
    spb_like = _run(db.create_faq_item(city="msk", question="Только Москва?", answer="m", created_by=1))
    rows = _run(db.list_faq_items(city_scope=cities.city_scope("kzn")))
    ids = {r["id"] for r in rows}
    assert general in ids and kzn_item in ids
    assert spb_like not in ids


def test_has_faq_for_city_false_until_enabled_item_exists(tmp_path):
    _ready(tmp_path)
    assert _run(db.has_faq_for_city("kzn")) is False
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    _run(db.update_faq_item(item_id, enabled=0))
    assert _run(db.has_faq_for_city("kzn")) is False
    _run(db.update_faq_item(item_id, enabled=1))
    assert _run(db.has_faq_for_city("kzn")) is True


def test_list_faq_for_city_returns_general_and_own_city_enabled_only(tmp_path):
    _ready(tmp_path)
    general = _run(db.create_faq_item(city=None, question="Общий?", answer="o", created_by=1))
    kzn_item = _run(db.create_faq_item(city="kzn", question="Казань?", answer="k", created_by=1))
    other = _run(db.create_faq_item(city="spb", question="Питер?", answer="p", created_by=1))
    disabled = _run(db.create_faq_item(city=None, question="Скрыт?", answer="s", created_by=1))
    _run(db.update_faq_item(disabled, enabled=0))
    rows = _run(db.list_faq_for_city("kzn"))
    ids = {r["id"] for r in rows}
    assert ids == {general, kzn_item}


def test_update_faq_item_ignores_keys_outside_whitelist(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    ok = _run(db.update_faq_item(item_id, question="A2?", id=999, created_at="hacked"))
    assert ok is True
    row = _run(db.get_faq_item(item_id))
    assert row["question"] == "A2?"
    assert row["id"] == item_id
    assert row["created_at"] != "hacked"


def test_update_faq_item_with_no_whitelisted_fields_returns_false(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    assert _run(db.update_faq_item(item_id, id=999)) is False


def test_delete_faq_item_removes_row(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=1))
    assert _run(db.delete_faq_item(item_id)) is True
    assert _run(db.get_faq_item(item_id)) is None
    assert _run(db.delete_faq_item(item_id)) is False


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: экран делегата «❓ Частые вопросы» + FAQ перед формой «Задать вопрос»
# ══════════════════════════════════════════════════════════════════════════════════════════

DELEGATE_ID = 8901101


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None
        self.username = None


class _FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers_sent = []
        self.answer_markups = []
        self.text_edited = None
        self.edit_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _seed_delegate(uid=DELEGATE_ID, **extra):
    data = {"telegram_id": uid, "full_name": f"Delegate {uid}", "registration_date": "2026-09-06"}
    data.update(extra)
    _run(db.add_user(data))


def _flat_cb(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


def test_faq_screen_empty_shows_empty_text_and_ask_button(tmp_path):
    _ready(tmp_path)
    text, kb = _run(ua_mod.faq_screen(None))
    assert text == _default("faq_empty_text")
    assert _flat_cb(kb) == ["faq_ask"]


def test_faq_screen_lists_questions_and_ask_button(tmp_path):
    _ready(tmp_path)
    a = _run(db.create_faq_item(city=None, question="Где проходит форум?", answer="В кампусе.", created_by=1))
    b = _run(db.create_faq_item(city=None, question="Сколько стоит участие?", answer="Бесплатно.", created_by=1))
    text, kb = _run(ua_mod.faq_screen(None))
    assert text == _default("faq_intro_text")
    cbs = _flat_cb(kb)
    assert cbs == [f"faq_q:{a}", f"faq_q:{b}", "faq_ask"]


def test_faq_screen_paginates_after_eight_items(tmp_path):
    _ready(tmp_path)
    for i in range(9):
        _run(db.create_faq_item(city=None, question=f"Вопрос {i}?", answer=f"Ответ {i}.", created_by=1))
    text, kb = _run(ua_mod.faq_screen(None, offset=0))
    cbs = _flat_cb(kb)
    assert cbs.count("faq_ask") == 1
    assert any(cb.startswith("faq_list:") for cb in cbs)


def test_faq_screen_city_override_shows_only_city_answer(tmp_path):
    _ready(tmp_path)
    general = _run(db.create_faq_item(city=None, question="Где проходит форум?", answer="Общий ответ.", created_by=1))
    kzn = _run(db.create_faq_item(city="kzn", question="где проходит форум??", answer="Казанский ответ.", created_by=1))
    text, kb = _run(ua_mod.faq_screen("kzn"))
    cbs = _flat_cb(kb)
    assert f"faq_q:{kzn}" in cbs
    assert f"faq_q:{general}" not in cbs


def test_show_faq_message_handler_sends_screen(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_faq_item(city=None, question="Где проходит форум?", answer="В кампусе.", created_by=1))
    message = _FakeMessage(text="❓ Частые вопросы")
    _run(ua_mod.show_faq(message))
    assert message.answers_sent == [_default("faq_intro_text")]


def test_faq_page_callback_paginates(tmp_path):
    _ready(tmp_path)
    for i in range(9):
        _run(db.create_faq_item(city=None, question=f"Вопрос {i}?", answer=f"Ответ {i}.", created_by=1))
    callback = _FakeCallback("faq_list:8")
    _run(ua_mod.faq_page(callback))
    cbs = _flat_cb(callback.message.edit_markup)
    assert any(cb == "faq_list:0" for cb in cbs)  # стрелка «назад» на второй странице


def test_faq_open_answer_shows_question_and_answer_escaped(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_faq_item(
        city=None, question="<b>Где</b> форум?", answer="В кампусе & рядом.", created_by=1,
    ))
    callback = _FakeCallback(f"faq_q:{item_id}")
    _run(ua_mod.faq_open_answer(callback))
    assert "&lt;b&gt;Где&lt;/b&gt;" in callback.message.text_edited
    assert "кампусе &amp; рядом" in callback.message.text_edited
    cbs = _flat_cb(callback.message.edit_markup)
    assert cbs == ["faq_list:0", "faq_ask"]


def test_faq_open_answer_stale_item_falls_back_to_list(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("faq_q:999999")
    _run(ua_mod.faq_open_answer(callback))
    assert callback.answers and callback.answers[0][1] is True  # show_alert=True


def test_faq_ask_callback_opens_question_form(tmp_path):
    _ready(tmp_path)
    state = _new_state(DELEGATE_ID)
    callback = _FakeCallback("faq_ask")
    _run(ua_mod.faq_ask(callback, state))
    assert callback.message.answers_sent == [_default("ask_question_prompt_text")]
    assert _run(state.get_state()) == "Question:waiting_for_question"


def test_ask_organizer_start_shows_faq_first_when_not_empty(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_faq_item(city=None, question="Где проходит форум?", answer="В кампусе.", created_by=1))
    message = _FakeMessage(text="❓ Задать вопрос")
    state = _new_state(DELEGATE_ID)
    _run(ua_mod.ask_organizer_start(message, state))
    assert message.answers_sent == [_default("faq_intro_text")]
    # форма вопроса ещё не открыта — стейт не выставлен
    assert _run(state.get_state()) is None


def test_ask_organizer_start_empty_faq_is_byte_identical_to_before(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    message = _FakeMessage(text="❓ Задать вопрос")
    state = _new_state(DELEGATE_ID)
    _run(ua_mod.ask_organizer_start(message, state))
    assert message.answers_sent == [_default("ask_question_prompt_text")]
    assert _run(state.get_state()) == "Question:waiting_for_question"


def test_get_main_menu_kb_hides_faq_button_when_empty(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    labels = [btn.text for row in kb.keyboard for btn in row]
    assert "❓ Частые вопросы" not in labels


def test_get_main_menu_kb_shows_faq_button_once_item_exists(tmp_path):
    _ready(tmp_path)
    _seed_delegate()
    _run(db.create_faq_item(city=None, question="Где проходит форум?", answer="В кампусе.", created_by=1))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    labels = [btn.text for row in kb.keyboard for btn in row]
    assert "❓ Частые вопросы" in labels


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: экран менеджера «❓ Частые вопросы»
# ══════════════════════════════════════════════════════════════════════════════════════════

from handlers import admin as admin_mod  # noqa: E402 -- канонический порядок импорта хендлеров
from handlers import admin_faq  # noqa: E402
from handlers.admin_caps import required_capability, role_caps_key, role_enabled_key
import cities as cities_mod

ADMIN_ID = 8901201
MANAGER_ID = 8901202


def _admin_ready(tmp_path, name="faq_admin_260906.db"):
    config.DB_PATH = str(tmp_path / name)
    config.GOOGLE_SHEET_ID = ""
    _run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    _run(db.set_setting("event_city_enabled", "on"))


def _bind_manager_to_city(manager_id, city):
    _run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    _run(db.set_staff_city(manager_id, city))
    _run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    _run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))


def test_render_faq_screen_empty_invites_to_add(tmp_path):
    _admin_ready(tmp_path)
    text, kb = _run(admin_faq.render_faq_screen(ADMIN_ID))
    assert "Пока ни одного пункта" in text
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "afaq_new" in cbs


def test_render_faq_screen_lists_items_with_status_badge(tmp_path, _restore_cities_cache):
    _admin_ready(tmp_path)
    a = _run(db.create_faq_item(city=None, question="Где проходит форум?", answer="В кампусе.", created_by=ADMIN_ID))
    b = _run(db.create_faq_item(city=None, question="Сколько стоит?", answer="Бесплатно.", created_by=ADMIN_ID))
    _run(db.update_faq_item(b, enabled=0))
    text, kb = _run(admin_faq.render_faq_screen(ADMIN_ID))
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"afaq_v:{a}" in cbs and f"afaq_v:{b}" in cbs
    row_labels = [btn.text for row in kb.inline_keyboard for btn in row if btn.callback_data == f"afaq_v:{b}"]
    assert row_labels and "🚫" in row_labels[0]


def test_admin_faq_callback_renders_screen(tmp_path):
    _admin_ready(tmp_path)
    callback = _FakeCallback("admin_faq", user_id=ADMIN_ID)
    _run(admin_faq.admin_faq(callback))
    assert "Частые вопросы" in callback.message.text_edited


def test_afaq_view_shows_card_with_actions(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="Где?", answer="Тут.", created_by=ADMIN_ID))
    callback = _FakeCallback(f"afaq_v:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_view(callback))
    cbs = [btn.callback_data for row in callback.message.edit_markup.inline_keyboard for btn in row]
    assert f"afaq_eq:{item_id}" in cbs and f"afaq_ea:{item_id}" in cbs
    assert f"afaq_up:{item_id}" in cbs and f"afaq_dn:{item_id}" in cbs
    assert f"afaq_t:{item_id}" in cbs and f"afaq_d:{item_id}" in cbs
    assert "afaq_p:0" in cbs


def test_afaq_view_stale_item_shows_alert(tmp_path):
    _admin_ready(tmp_path)
    callback = _FakeCallback("afaq_v:999999", user_id=ADMIN_ID)
    _run(admin_faq.afaq_view(callback))
    assert callback.answers and callback.answers[0][1] is True


def test_afaq_move_up_and_down_swap_neighbors(tmp_path):
    _admin_ready(tmp_path)
    a = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    b = _run(db.create_faq_item(city=None, question="B?", answer="b", created_by=ADMIN_ID))
    callback = _FakeCallback(f"afaq_dn:{a}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_move_down(callback))
    rows = {r["id"]: r["position"] for r in _run(db.list_faq_items())}
    assert rows[b] < rows[a]
    callback2 = _FakeCallback(f"afaq_up:{a}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_move_up(callback2))
    rows2 = {r["id"]: r["position"] for r in _run(db.list_faq_items())}
    assert rows2[a] < rows2[b]


def test_afaq_toggle_enabled_flips_status(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    callback = _FakeCallback(f"afaq_t:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_toggle_enabled(callback))
    row = _run(db.get_faq_item(item_id))
    assert row["enabled"] == 0
    callback2 = _FakeCallback(f"afaq_t:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_toggle_enabled(callback2))
    row2 = _run(db.get_faq_item(item_id))
    assert row2["enabled"] == 1


def test_afaq_card_no_city_toggle_when_header_is_all_cities(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))
    text, kb = _run(admin_faq.render_faq_card(ADMIN_ID, item_id))
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"afaq_c:{item_id}" not in cbs
    assert "конкретный город" in text


def test_afaq_toggle_city_switches_general_to_header_city_and_back(tmp_path, _restore_cities_cache):
    _admin_ready(tmp_path)
    _run(_seed_cities([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)]))
    _enable_cities()
    _run(cities_mod.set_admin_city(ADMIN_ID, "kzn"))
    item_id = _run(db.create_faq_item(city=None, question="A?", answer="a", created_by=ADMIN_ID))

    text, kb = _run(admin_faq.render_faq_card(ADMIN_ID, item_id))
    cbs = {btn.callback_data: btn.text for row in kb.inline_keyboard for btn in row}
    assert f"afaq_c:{item_id}" in cbs
    assert "Казань" in cbs[f"afaq_c:{item_id}"]

    callback = _FakeCallback(f"afaq_c:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_toggle_city(callback))
    row = _run(db.get_faq_item(item_id))
    assert row["city"] == "kzn"

    callback2 = _FakeCallback(f"afaq_c:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_toggle_city(callback2))
    row2 = _run(db.get_faq_item(item_id))
    assert row2["city"] is None


def test_afaq_delete_confirm_names_question_then_go_removes_it(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="Удалить меня?", answer="a", created_by=ADMIN_ID))
    confirm_cb = _FakeCallback(f"afaq_d:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_delete_confirm(confirm_cb))
    assert "Удалить меня?" in confirm_cb.message.text_edited
    assert "у всех городов" in confirm_cb.message.text_edited

    go_cb = _FakeCallback(f"afaq_dgo:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_delete_go(go_cb))
    assert _run(db.get_faq_item(item_id)) is None


def test_afaq_new_wizard_creates_item_question_then_answer(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    start_cb = _FakeCallback("afaq_new", user_id=ADMIN_ID)
    _run(admin_faq.afaq_new_start(start_cb, state))
    assert _run(state.get_state()) == "FaqItem:text"

    q_msg = _FakeMessage(text="Где проходит форум?", user_id=ADMIN_ID)
    _run(admin_faq.afaq_text_step(q_msg, state))
    assert _run(state.get_state()) == "FaqItem:text"  # ждём ответ

    a_msg = _FakeMessage(text="В кампусе.", user_id=ADMIN_ID)
    _run(admin_faq.afaq_text_step(a_msg, state))
    assert _run(state.get_state()) is None

    items = _run(db.list_faq_items())
    assert len(items) == 1
    assert items[0]["question"] == "Где проходит форум?"
    assert items[0]["answer"] == "В кампусе."


def test_afaq_new_wizard_cancel_creates_nothing(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    start_cb = _FakeCallback("afaq_new", user_id=ADMIN_ID)
    _run(admin_faq.afaq_new_start(start_cb, state))

    q_msg = _FakeMessage(text="Где проходит форум?", user_id=ADMIN_ID)
    _run(admin_faq.afaq_text_step(q_msg, state))

    cancel_msg = _FakeMessage(text="Отмена", user_id=ADMIN_ID)
    _run(admin_faq.afaq_text_cancel(cancel_msg, state))
    assert _run(state.get_state()) is None
    assert _run(db.list_faq_items()) == []


def test_afaq_edit_question_and_answer_via_wizard(tmp_path):
    _admin_ready(tmp_path)
    item_id = _run(db.create_faq_item(city=None, question="Старый вопрос?", answer="Старый ответ.", created_by=ADMIN_ID))
    state = _new_state(ADMIN_ID)

    eq_cb = _FakeCallback(f"afaq_eq:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_edit_question_start(eq_cb, state))
    q_msg = _FakeMessage(text="Новый вопрос?", user_id=ADMIN_ID)
    _run(admin_faq.afaq_text_step(q_msg, state))
    row = _run(db.get_faq_item(item_id))
    assert row["question"] == "Новый вопрос?"
    assert row["answer"] == "Старый ответ."

    ea_cb = _FakeCallback(f"afaq_ea:{item_id}", user_id=ADMIN_ID)
    _run(admin_faq.afaq_edit_answer_start(ea_cb, state))
    a_msg = _FakeMessage(text="Новый ответ.", user_id=ADMIN_ID)
    _run(admin_faq.afaq_text_step(a_msg, state))
    row2 = _run(db.get_faq_item(item_id))
    assert row2["answer"] == "Новый ответ."


def test_afaq_card_out_of_scope_for_bound_manager_shows_alert(tmp_path, _restore_cities_cache):
    _admin_ready(tmp_path)
    _run(_seed_cities([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)]))
    _enable_cities()
    _bind_manager_to_city(MANAGER_ID, "kzn")
    other_city_item = _run(db.create_faq_item(city="msk", question="Только Москва?", answer="m", created_by=ADMIN_ID))

    callback = _FakeCallback(f"afaq_v:{other_city_item}", user_id=MANAGER_ID)
    _run(admin_faq.afaq_view(callback))
    assert callback.answers and callback.answers[0][1] is True
    assert callback.message.text_edited is None  # карточка не открылась


def test_admin_caps_cover_faq_namespace_exactly_three_records(tmp_path):
    """<verification> квика: три записи, других точек входа нет."""
    assert required_capability(callback_data="admin_faq") == "moderate_reg"
    assert required_capability(callback_data="afaq_new") == "moderate_reg"
    assert required_capability(callback_data="afaq_v:1") == "moderate_reg"
    assert required_capability(raw_state="FaqItem:text") == "moderate_reg"


def test_admin_faq_wired_into_apps_section_and_menu_rows():
    from handlers import admin_sections as sec
    from handlers.admin_core import _ADMIN_MENU_ROWS

    assert ("❓ Частые вопросы", "admin_faq") in _ADMIN_MENU_ROWS
    apps_rows = next(rows for token, _label, rows in sec.SECTIONS if token == "apps")
    assert ("op", "admin_faq") in apps_rows
