"""Quick 260819-gtl — task title + cover photo (CONTEXT.md decisions 1-9).

Covers: `task_title()` fallback (decision 2), title/photo DB round-trip (decisions 1/4),
the wizard's new title-first-step / photo-step (decisions 1/4), the point-edit screen
(«✏️ Название» / «📷 Заменить фото» / «🗑 Убрать фото», decisions 1/4), the delegate task
CARD open/back navigation with and without a photo (decision 5), and that the manager
list/archive/review-card/sheet builders show the title, not a raw text preview
(decisions 3/6/8). ADMIN_CAPS coverage for the new callbacks (decision 8's "moderate_game").

Handlers called DIRECTLY with Fake message/callback doubles -- same convention as every
other phase-9/14 gamification test file (pytest-asyncio unavailable in this env).
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_gamification
from handlers import user_actions as ua_mod
from handlers.admin_caps import required_capability
from handlers.states import GameTaskCreate, GameTaskEdit


ADMIN_ID = 931001
DELEGATE_ID = 931002


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_title_photo_260819.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_delegate(uid=DELEGATE_ID):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": f"Delegate {uid}", "registration_date": "2026-08-01",
    }))


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


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


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _mk_task(tmp_path=None, **kwargs):
    defaults = dict(text="Пост со скрином #знакомство", category="Light", coins=20,
                     proof_type="photo", deadline_at="2099-01-01 00:00:00", created_by=ADMIN_ID)
    defaults.update(kwargs)
    return asyncio.run(db.create_task(**defaults))


# ── task_title() fallback (CONTEXT.md decision 2) ───────────────────────────────────────────

def test_task_title_uses_title_when_set():
    assert db.task_title({"title": "Название", "text": "Другой текст"}) == "Название"


def test_task_title_falls_back_to_first_line_when_short():
    assert db.task_title({"title": None, "text": "Короткая строка\nвторая строка"}) == "Короткая строка"


def test_task_title_falls_back_truncates_long_first_line_at_40():
    long_line = "A" * 60
    assert db.task_title({"title": None, "text": long_line}) == "A" * 40 + "…"


def test_task_title_falls_back_on_empty_title_string():
    assert db.task_title({"title": "", "text": "Текст"}) == "Текст"


# ── DB round-trip (CONTEXT.md decisions 1/4) ────────────────────────────────────────────────

def test_create_task_with_title_and_photo_round_trips(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Моё задание", photo_file_id="AgAC123")
    task = asyncio.run(db.get_task(task_id))
    assert task["title"] == "Моё задание"
    assert task["photo_file_id"] == "AgAC123"


def test_create_task_without_title_photo_stays_null(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    task = asyncio.run(db.get_task(task_id))
    assert task["title"] is None
    assert task["photo_file_id"] is None


def test_update_task_title_and_photo_round_trip(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    assert asyncio.run(db.update_task_title(task_id, "Новое имя")) is True
    assert asyncio.run(db.update_task_photo(task_id, "file1")) is True
    task = asyncio.run(db.get_task(task_id))
    assert task["title"] == "Новое имя"
    assert task["photo_file_id"] == "file1"
    assert asyncio.run(db.update_task_photo(task_id, None)) is True
    task = asyncio.run(db.get_task(task_id))
    assert task["photo_file_id"] is None


def test_update_task_title_and_photo_missing_task_returns_false(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(db.update_task_title(99999, "x")) is False
    assert asyncio.run(db.update_task_photo(99999, "y")) is False


# ── wizard: title-first step (CONTEXT.md decision 1) ────────────────────────────────────────

def test_game_task_new_prompts_title_and_sets_state(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("gtnew")
    state = _new_state()
    asyncio.run(admin_gamification.game_task_new(callback, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.title
    assert callback.message.answers_sent[-1] == "Название задания (коротко, до 60 символов):"


def test_game_task_title_step_rejects_empty_and_advances(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.title))

    empty_message = FakeMessage(text="   ")
    asyncio.run(admin_gamification.game_task_title_step(empty_message, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.title
    assert "пуст" in empty_message.answers_sent[-1].lower()

    ok_message = FakeMessage(text="  Моё\nзадание  ")
    asyncio.run(admin_gamification.game_task_title_step(ok_message, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.text
    # newline collapsed to a single space ("перенос -> пробел", CONTEXT.md decision 1)
    assert asyncio.run(state.get_data())["gt_title"] == "Моё задание"


def test_game_task_title_step_truncates_over_60_chars(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.title))
    long_title = "Б" * 90
    message = FakeMessage(text=long_title)
    asyncio.run(admin_gamification.game_task_title_step(message, state))
    assert asyncio.run(state.get_data())["gt_title"] == "Б" * 60


# ── wizard: photo step right after description (CONTEXT.md decision 4) ─────────────────────

def test_game_task_text_step_advances_to_photo_not_category(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    message = FakeMessage(text="Текст задания")
    asyncio.run(admin_gamification.game_task_text_step(message, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.photo


def test_game_task_photo_step_sets_file_id_and_advances_to_category(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.photo))
    message = FakeMessage(photo=[FakePhotoSize("small"), FakePhotoSize("large")])
    asyncio.run(admin_gamification.game_task_photo_step(message, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.category
    assert asyncio.run(state.get_data())["gt_photo_file_id"] == "large"


def test_game_task_photo_skip_clears_file_id_and_advances(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.photo))
    callback = FakeCallback("gtphoto_skip")
    asyncio.run(admin_gamification.game_task_photo_skip(callback, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.category
    assert asyncio.run(state.get_data())["gt_photo_file_id"] is None


def test_game_task_photo_step_invalid_content_reprompts(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.photo))
    message = FakeMessage(text="не фото")
    asyncio.run(admin_gamification.game_task_photo_step_invalid(message, state))
    assert "фото" in message.answers_sent[-1].lower()
    # state untouched -- still waiting for a photo or the skip button
    assert asyncio.run(state.get_state()) == GameTaskCreate.photo


def test_full_wizard_creates_task_with_title_and_photo(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(admin_gamification.game_task_new(FakeCallback("gtnew"), state))
    asyncio.run(admin_gamification.game_task_title_step(FakeMessage(text="Итоговое задание"), state))
    asyncio.run(admin_gamification.game_task_text_step(FakeMessage(text="Описание задания"), state))
    asyncio.run(admin_gamification.game_task_photo_step(
        FakeMessage(photo=[FakePhotoSize("cover1")]), state,
    ))
    asyncio.run(admin_gamification.game_task_category_step(FakeCallback("gtcat:Light"), state))
    asyncio.run(admin_gamification.game_task_coins_step(FakeMessage(text="10"), state))
    asyncio.run(admin_gamification.game_task_proof_step(FakeCallback("gtproof:text"), state))

    confirm_msg = FakeMessage()
    asyncio.run(admin_gamification.game_task_proof_done(FakeCallback("gtproof_done", message=confirm_msg), state))
    deadline_msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(deadline_msg, state))
    # preview step shows the card WITH the photo (CONTEXT.md decision 7)
    assert deadline_msg.answer_photo_calls, "confirm card should be sent as a photo when gt_photo_file_id is set"
    assert deadline_msg.answer_photo_calls[0][0] == "cover1"
    assert "Итоговое задание" in deadline_msg.answer_photo_calls[0][1]

    confirm_cb = FakeCallback("gtconfirm")
    asyncio.run(admin_gamification.game_task_confirm(confirm_cb, state))

    tasks = asyncio.run(db.list_all_tasks())
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Итоговое задание"
    assert tasks[0]["photo_file_id"] == "cover1"


# ── point-edit: title / photo replace / photo remove (CONTEXT.md decisions 1/4) ────────────

def test_game_task_edit_screen_shows_add_photo_when_none(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Задание для правки")
    callback = FakeCallback(f"gtedit:{task_id}")
    asyncio.run(admin_gamification.game_task_edit_screen(callback, _new_state()))
    assert "Задание для правки" in callback.message.text_edited
    texts = _flat_texts(callback.message.edit_markup)
    assert "✏️ Название" in texts
    assert "📷 Добавить фото" in texts
    assert "🗑 Убрать фото" not in texts


def test_game_task_edit_screen_shows_replace_and_remove_when_photo_set(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="С фото", photo_file_id="cover9")
    callback = FakeCallback(f"gtedit:{task_id}")
    asyncio.run(admin_gamification.game_task_edit_screen(callback, _new_state()))
    texts = _flat_texts(callback.message.edit_markup)
    assert "📷 Заменить фото" in texts
    assert "🗑 Убрать фото" in texts
    assert "📷 Добавить фото" not in texts


def test_gtedittitle_flow_updates_title(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Старое имя")
    state = _new_state()
    asyncio.run(admin_gamification.game_task_edittitle_start(FakeCallback(f"gtedittitle:{task_id}"), state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.title
    assert asyncio.run(state.get_data())["gte_task_id"] == task_id

    message = FakeMessage(text="Новое имя")
    asyncio.run(admin_gamification.game_task_edittitle_step(message, state))
    assert asyncio.run(state.get_state()) is None
    task = asyncio.run(db.get_task(task_id))
    assert task["title"] == "Новое имя"


def test_gteditphoto_flow_replaces_photo(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Фото-задание", photo_file_id="old_cover")
    state = _new_state()
    asyncio.run(admin_gamification.game_task_editphoto_start(FakeCallback(f"gteditphoto:{task_id}"), state))
    assert asyncio.run(state.get_state()) == GameTaskEdit.photo

    message = FakeMessage(photo=[FakePhotoSize("new_cover")])
    asyncio.run(admin_gamification.game_task_editphoto_step(message, state))
    assert asyncio.run(state.get_state()) is None
    task = asyncio.run(db.get_task(task_id))
    assert task["photo_file_id"] == "new_cover"


def test_gtremovephoto_clears_photo_no_confirm_needed(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(title="Убрать фото", photo_file_id="cover_to_remove")
    callback = FakeCallback(f"gtremovephoto:{task_id}")
    asyncio.run(admin_gamification.game_task_removephoto(callback))
    task = asyncio.run(db.get_task(task_id))
    assert task["photo_file_id"] is None
    # re-rendered edit screen now offers "add", not "replace/remove"
    texts = _flat_texts(callback.message.edit_markup)
    assert "📷 Добавить фото" in texts


# ── ADMIN_CAPS coverage (CONTEXT.md decision 8) ─────────────────────────────────────────────

def test_new_admin_callbacks_require_moderate_game():
    assert required_capability(callback_data="gtedit:1") == "moderate_game"
    assert required_capability(callback_data="gtedittitle:1") == "moderate_game"
    assert required_capability(callback_data="gteditphoto:1") == "moderate_game"
    assert required_capability(callback_data="gtremovephoto:1") == "moderate_game"
    assert required_capability(callback_data="gtphoto_skip") == "moderate_game"
    assert required_capability(raw_state="GameTaskEdit:title") == "moderate_game"
    assert required_capability(raw_state="GameTaskEdit:photo") == "moderate_game"


# ── manager list/archive/review-card/sheet use title (CONTEXT.md decisions 3/6/8) ──────────

def test_game_tasks_screen_shows_title_not_raw_text(tmp_path):
    _db_ready(tmp_path)
    _mk_task(text="Очень длинный служебный текст задания про хэштег", title="Короткое имя")
    text, kb = asyncio.run(admin_gamification._game_tasks_screen())
    assert "Короткое имя" in text
    assert "Очень длинный служебный текст" not in text


def test_matrix_and_history_column_use_title(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(text="Полный текст задания подробно", title="Заголовок", proof_type="text")
    sub_id = asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "42", "2026-08-01 10:00:00"))
    assert sub_id is not None

    tasks = asyncio.run(db.list_all_tasks())
    submissions = asyncio.run(db.list_all_submissions())
    headers, _rows = admin_gamification._build_game_matrix(tasks, submissions)
    assert "Заголовок" in headers
    assert not any("Полный текст задания подробно" in h for h in headers)

    _h_headers, h_rows = admin_gamification._build_game_history(submissions)
    assert h_rows[0][1] == "Заголовок"


def test_submission_card_shows_task_line_with_title(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Карточка-имя", proof_type="text")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "ответ", "2026-08-01 10:00:00"))
    rows = asyncio.run(db.get_pending_submissions(limit=10, offset=0))
    card = admin_gamification._render_submission_card(rows[0], 1, 1)
    assert "Задание: Карточка-имя" in card


# ── delegate: list line/button + card (CONTEXT.md decisions 3/5) ───────────────────────────

def test_delegate_list_line_uses_title_and_numbering(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    _mk_task(text="Текст без названия, который довольно длинный на самом деле совсем")
    _mk_task(title="Второе задание", text="short")
    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.show_game_tasks(message))
    text, parse_mode, markup = message.answers_sent[-1], message.answer_parse_modes[-1], message.answer_markups[-1]
    assert text.startswith("1. 📤")
    assert "2. 📤 Второе задание" in text
    assert parse_mode == "HTML"


def test_mytask_open_no_photo_edits_list_message_into_card(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Карточка без фото", text="Полное описание задания без фото")
    callback = FakeCallback(f"mytask:{task_id}", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.mytask_open(callback))
    assert callback.message.edit_calls == 1
    assert "Карточка без фото" in callback.message.text_edited
    assert "Полное описание задания без фото" in callback.message.text_edited
    texts = _flat_texts(callback.message.edit_markup)
    assert "📤 Сдать" in texts
    assert "◀️ Назад" in texts
    assert not callback.message.answer_photo_calls


def test_mytask_open_with_photo_sends_separate_photo_message(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Карточка с фото", photo_file_id="cover_x")
    callback = FakeCallback(f"mytask:{task_id}", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.mytask_open(callback))
    assert callback.message.edit_calls == 0  # list message left untouched
    assert len(callback.message.answer_photo_calls) == 1
    photo, caption, parse_mode, kb = callback.message.answer_photo_calls[0]
    assert photo == "cover_x"
    assert "Карточка с фото" in caption
    assert parse_mode == "HTML"
    assert "📤 Сдать" in _flat_texts(kb)


def test_mytask_open_archived_task_alerts(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _mk_task(title="Скоро в архиве")
    asyncio.run(db.archive_task(task_id))
    callback = FakeCallback(f"mytask:{task_id}", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.mytask_open(callback))
    assert callback.answers[0][1] is True  # show_alert=True
    assert "архив" in callback.answers[0][0]


def test_mytask_back_no_photo_rerenders_list_via_edit(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    _mk_task(title="Осталось активным")
    text_message = FakeMessage(user_id=DELEGATE_ID, photo=None)
    callback = FakeCallback("mytask_back", user_id=DELEGATE_ID, message=text_message)
    asyncio.run(ua_mod.mytask_back(callback))
    assert text_message.edit_calls == 1
    assert "Осталось активным" in text_message.text_edited
    assert not text_message.deleted


def test_mytask_back_with_photo_deletes_card_message(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    photo_message = FakeMessage(user_id=DELEGATE_ID, photo=[FakePhotoSize("x")])
    callback = FakeCallback("mytask_back", user_id=DELEGATE_ID, message=photo_message)
    asyncio.run(ua_mod.mytask_back(callback))
    assert photo_message.deleted is True
    assert photo_message.edit_calls == 0
