"""Phase 16 (16-01, GAME-UI-01) — делегатские экраны «🎯 Задания» (список + карточка) и «🪙
Баланс», per 16-01-PLAN.md. Handlers called DIRECTLY with Fake message/callback doubles, same
convention as tests/test_game_task_title_photo_260819.py (no pytest-asyncio in this env).
"""
import asyncio

from config import config
from database import db
from handlers import game_labels
from handlers import user_actions as ua_mod
from settings_schema import SETTINGS_SCHEMA, get_setting_typed


ADMIN_ID = 931101
DELEGATE_ID = 931102


def _db_ready(tmp_path, name="test_game_ui16_delegate.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_delegate(uid=DELEGATE_ID):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": f"Delegate {uid}", "registration_date": "2026-08-01",
    }))


def _mk_task(**kwargs):
    defaults = dict(text="Текст задания", category="Light", coins=20,
                     proof_type="photo", deadline_at="2099-01-01 00:00:00", created_by=ADMIN_ID)
    defaults.update(kwargs)
    return asyncio.run(db.create_task(**defaults))


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID, photo=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.photo = photo
        self.answers_sent = []
        self.answer_markups = []
        self.answer_parse_modes = []
        self.answer_photo_calls = []
        self.text_edited = None
        self.edit_markup = None
        self.edit_calls = 0
        self.deleted = False

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_parse_modes.append(parse_mode)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.answer_photo_calls.append((photo, caption, parse_mode, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup
        self.edit_calls += 1

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_kb_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_kb_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ── Task 1: game_labels.category_label / proof_types_label ─────────────────────────────────

def test_category_label_returns_registry_default_for_known_code(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(game_labels.category_label("Light")) == "Лёгкое"
    assert asyncio.run(game_labels.category_label("Medium")) == "Среднее"
    assert asyncio.run(game_labels.category_label("Hard")) == "Сложное"
    assert asyncio.run(game_labels.category_label("Referral")) == "Реферальное"
    assert asyncio.run(game_labels.category_label("Special")) == "Особое"


def test_category_label_unknown_code_fails_soft(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(game_labels.category_label("Nonsense")) == "Nonsense"


def test_proof_types_label_none_is_not_important(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(game_labels.proof_types_label(None)) == (
        SETTINGS_SCHEMA["game_proof_type_unspecified_text"]["default"]
    )


def test_proof_types_label_multi_follows_fixed_order(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(game_labels.proof_types_label("text,photo")) == (
        f'{SETTINGS_SCHEMA["game_proof_type_label_photo"]["default"]} + '
        f'{SETTINGS_SCHEMA["game_proof_type_label_text"]["default"]}'
    )


# ── Task 1: list_coin_entries_for_user / count_coin_entries_for_user ───────────────────────

def test_list_coin_entries_for_user_scoped_and_newest_first(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    other_id = DELEGATE_ID + 1
    _seed_delegate(other_id)
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="один", source="manual"))
    asyncio.run(db.add_coins(DELEGATE_ID, 10, reason="два", source="task"))
    asyncio.run(db.add_coins(other_id, 100, reason="чужое", source="manual"))

    rows = asyncio.run(db.list_coin_entries_for_user(DELEGATE_ID, limit=5, offset=0))
    assert len(rows) == 2
    assert rows[0]["reason"] == "два"
    assert rows[1]["reason"] == "один"
    assert asyncio.run(db.count_coin_entries_for_user(DELEGATE_ID)) == 2
    assert asyncio.run(db.count_coin_entries_for_user(other_id)) == 1


# ── Task 2: delegate list — pagination, RU category, terminal rejected state ───────────────

def test_render_game_task_line_uses_ru_category(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Пост в сторис", category="Medium", coins=15, deadline_at="2099-05-05 12:00:00")
    task = asyncio.run(db.get_task(task_id))
    line, needs_button = asyncio.run(ua_mod._render_game_task_line(1, task, None, DELEGATE_ID))
    assert needs_button is True
    assert "1. 📤 <b>Пост в сторис</b>" in line
    assert "Среднее · 15🪙 · до 05.05" in line


def test_render_game_task_line_overdue_marker(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Просрочка", deadline_at="2020-01-01 10:00:00")
    task = asyncio.run(db.get_task(task_id))
    line, needs_button = asyncio.run(ua_mod._render_game_task_line(1, task, None, DELEGATE_ID))
    assert needs_button is True
    assert "</b> ⏰" in line
    assert "срок вышел, сдать ещё можно" in line


def test_render_game_task_line_pending_and_approved(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="На проверке")
    task = asyncio.run(db.get_task(task_id))
    active_pending = {"status": "pending", "submitted_at": "2026-08-01 10:00:00"}
    line, needs = asyncio.run(ua_mod._render_game_task_line(1, task, active_pending, DELEGATE_ID))
    assert needs is False
    assert "1. ⏳ <b>На проверке</b>" in line
    assert "на проверке (сдано 01.08" in line

    active_approved = {"status": "approved", "coins_awarded": 30}
    line2, needs2 = asyncio.run(ua_mod._render_game_task_line(1, task, active_approved, DELEGATE_ID))
    assert needs2 is False
    assert "1. ✅ <b>На проверке</b>" in line2
    assert "принято (+30🪙)" in line2


def test_render_game_task_line_terminal_rejected_state(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Лимит исчерпан")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "photo", "f1", "2026-08-01 10:00:00"))
    subs = asyncio.run(db.list_all_submissions())
    sub_id = [s["id"] for s in subs if s["task_id"] == task_id][0]
    asyncio.run(db.claim_submission(sub_id, ADMIN_ID, "rejected"))

    from database.db import set_setting
    asyncio.run(set_setting("game_resubmit_limit", "1"))

    task = asyncio.run(db.get_task(task_id))
    active = asyncio.run(db.get_active_submission(task_id, DELEGATE_ID))
    assert active is None  # rejected excluded, per D-05
    line, needs = asyncio.run(ua_mod._render_game_task_line(1, task, active, DELEGATE_ID))
    assert needs is False
    assert "1. ❌ <b>Лимит исчерпан</b>" in line
    assert "отклонено (попытка 1 из 1)" in line


def test_game_task_list_screen_paginates_at_6(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    for i in range(7):
        _mk_task(title=f"Задание {i+1}", deadline_at="2099-01-01 00:00:00")

    text, kb = asyncio.run(ua_mod._game_task_list_screen(DELEGATE_ID, page=0))
    assert "Задание 1" in text and "Задание 6" in text and "Задание 7" not in text
    data = _flat_kb_data(kb)
    assert "gtasks_page:1" in data
    assert not any(d and d.startswith("gtasks_page:-") for d in data if d)
    assert all(not (d and d.startswith("gtasks_page:") and d.endswith(":-1")) for d in data if d)

    text2, kb2 = asyncio.run(ua_mod._game_task_list_screen(DELEGATE_ID, page=1))
    assert "Задание 7" in text2 and "Задание 1" not in text2
    data2 = _flat_kb_data(kb2)
    assert "gtasks_page:0" in data2
    assert "gtasks_page:2" not in data2


def test_game_task_list_screen_button_uses_gtask_open(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Кнопка")
    text, kb = asyncio.run(ua_mod._game_task_list_screen(DELEGATE_ID, page=0))
    assert f"gtask_open:{task_id}" in _flat_kb_data(kb)


def test_game_task_list_screen_empty_uses_registry_text(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    text, kb = asyncio.run(ua_mod._game_task_list_screen(DELEGATE_ID, page=0))
    assert text == asyncio.run(get_setting_typed("game_task_list_empty"))
    assert kb is None


# ── Task 2: card — blockquote, status/attempt, proof hint, gtask_open:/gtasks_back: ────────

def test_mytask_open_no_photo_new_status(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Карточка", text="Описание <опасное>", proof_type="photo,text")
    callback = FakeCallback(f"gtask_open:{task_id}", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.mytask_open(callback))
    assert callback.message.edit_calls == 1
    text = callback.message.text_edited
    assert "<blockquote expandable>" in text
    assert "&lt;опасное&gt;" in text  # escaped BEFORE wrapping (T-16-01-03)
    assert "Статус: новое" in text
    assert (
        "Нужно прислать: "
        f'{SETTINGS_SCHEMA["game_proof_type_label_photo"]["default"]} + '
        f'{SETTINGS_SCHEMA["game_proof_type_label_text"]["default"]}'
    ) in text
    kb_texts = _flat_kb_texts(callback.message.edit_markup)
    assert "📤 Сдать" in kb_texts and "◀️ Назад" in kb_texts
    kb_data = _flat_kb_data(callback.message.edit_markup)
    assert "gtasks_back:0" in kb_data


def test_mytask_open_with_prior_rejections_shows_attempt(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Попытки")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "photo", "f1", "2026-08-01 10:00:00"))
    subs = asyncio.run(db.list_all_submissions())
    sub_id = [s["id"] for s in subs if s["task_id"] == task_id][0]
    asyncio.run(db.claim_submission(sub_id, ADMIN_ID, "rejected"))

    from database.db import set_setting
    asyncio.run(set_setting("game_resubmit_limit", "3"))

    callback = FakeCallback(f"gtask_open:{task_id}", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.mytask_open(callback))
    assert "Статус: новое · попытка 1 из 3" in callback.message.text_edited


def test_mytask_open_stale_pending_no_submit_button(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Уже на проверке")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "photo", "f1", "2026-08-01 10:00:00"))

    callback = FakeCallback(f"gtask_open:{task_id}", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.mytask_open(callback))
    assert "Статус: на проверке" in callback.message.text_edited
    kb_texts = _flat_kb_texts(callback.message.edit_markup)
    assert "📤 Сдать" not in kb_texts
    assert "◀️ Назад" in kb_texts


def test_mytask_back_parses_page_and_rerenders(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    for i in range(7):
        _mk_task(title=f"П{i+1}", deadline_at="2099-01-01 00:00:00")

    text_message = FakeMessage(user_id=DELEGATE_ID, photo=None)
    callback = FakeCallback("gtasks_back:1", user_id=DELEGATE_ID, message=text_message)
    asyncio.run(ua_mod.mytask_back(callback))
    assert text_message.edit_calls == 1
    assert "П7" in text_message.text_edited
    assert "П1" not in text_message.text_edited


def test_mytask_back_bad_page_defaults_to_zero(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    _mk_task(title="Единственное")
    text_message = FakeMessage(user_id=DELEGATE_ID, photo=None)
    callback = FakeCallback("gtasks_back:oops", user_id=DELEGATE_ID, message=text_message)
    asyncio.run(ua_mod.mytask_back(callback))
    assert "Единственное" in text_message.text_edited


# ── Task 3: «🪙 Баланс» screen ───────────────────────────────────────────────────────────────

def test_show_my_coins_empty_history(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.show_my_coins(message))
    text = message.answers_sent[-1]
    assert "🪙 Баланс: 0 монет" in text
    assert "Место в общем рейтинге: — из —" in text
    assert "Пока не было ни одной операции." in text
    kb_texts = _flat_kb_texts(message.answer_markups[-1])
    assert "📜 История" in kb_texts and "🏆 Рейтинг" in kb_texts


def test_show_my_coins_with_history_and_rank(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason=None, source="manual"))
    asyncio.run(db.add_coins(DELEGATE_ID, 10, reason=None, source="task"))
    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.show_my_coins(message))
    text = message.answers_sent[-1]
    assert "🪙 Баланс: 15 монет" in text
    assert "Место в общем рейтинге: 1 из 1" in text
    assert "вручную" in text
    assert "задание" in text


def test_gbal_history_paginates(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    for i in range(12):
        asyncio.run(db.add_coins(DELEGATE_ID, 1, reason=f"op{i}", source="manual"))
    callback = FakeCallback("gbal_history:0", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.gbal_history(callback))
    text = callback.message.text_edited
    assert "Страница 1 из 2" in text
    data = _flat_kb_data(callback.message.edit_markup)
    assert any(d and d.startswith("gbal_history:") and d != "gbal_history:0" for d in data)
    assert "gbal_back" in data


def test_gbal_top_shows_leaderboard(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 5, source="manual"))
    callback = FakeCallback("gbal_top", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.gbal_top(callback))
    # Phase 17.1 (17.1-01): заголовок рейтинга переехал в реестр -- сверяем с дефолтом
    # реестра, а не с литералом (байт-идентичность дефолта сторожит
    # tests/test_delegate_texts_registry_260819.py).
    assert SETTINGS_SCHEMA["leaderboard_header_text"]["default"] in callback.message.text_edited
    assert "gbal_back" in _flat_kb_data(callback.message.edit_markup)


def test_gbal_back_returns_to_summary(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    callback = FakeCallback("gbal_back", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.gbal_back(callback))
    assert "🪙 Баланс:" in callback.message.text_edited
