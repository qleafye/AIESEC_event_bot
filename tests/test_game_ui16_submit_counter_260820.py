"""Phase 16 Plan 02 (GAME-UI-02) — Экран 3: сдача задания с ОДНИМ редактируемым сообщением-
счётчиком вместо нового «Принял, частей: N» на каждую часть, плюс «🗑 Убрать последнее» и
инлайн «❌ Отмена».

Fake-дублёры скопированы из tests/test_gamification_delegate_phase9.py и расширены (конвенция
«копируй и расширяй», не импорт между тест-файлами): FakeMessage.answer() возвращает объект с
message_id, FakeBot умеет edit_message_text и пишет вызовы в .edits. Хендлеры вызываются
напрямую, asyncio.run + tmp_path-БД, как во всех тестах геймы.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import user_actions as ua_mod
from handlers import game_submit_counter as counter_mod
from handlers.states import GameSubmit


ADMIN_ID = 160201
DELEGATE_ID = 160202


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_ui16_submit_counter.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_delegate(uid=DELEGATE_ID):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-01",
    }))


def _seed_task(text="Пост со скрином", category="Light", coins=30, proof_type="photo,text",
               deadline_at="2026-12-31 23:59:00", created_by=ADMIN_ID):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at, created_by))


def _new_state(uid=DELEGATE_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid, full_name=None):
        self.id = uid
        self.full_name = full_name


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeSent:
    def __init__(self, message_id):
        self.message_id = message_id


_MSG_ID_COUNTER = [1000]


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID, photo=None, document=None,
                 caption=None, media_group_id=None, voice=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.photo = photo
        self.document = document
        self.caption = caption
        self.media_group_id = media_group_id
        self.voice = voice
        self.answers = []
        self.edits = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))
        _MSG_ID_COUNTER[0] += 1
        return FakeSent(_MSG_ID_COUNTER[0])

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID, full_name=None):
        self.data = data
        self.from_user = FakeUser(user_id, full_name=full_name)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))


def _flat_kb_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _flat_kb_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _start(task_id, uid=DELEGATE_ID):
    state = _new_state(uid)
    callback = FakeCallback(f"mytask_submit:{task_id}", user_id=uid)
    asyncio.run(ua_mod.mytask_submit_start(callback, state))
    return state, callback


# ── Task 1: рендер счётчика + клавиатура + ключи реестра ───────────────────────────────────

def test_counter_text_empty_has_zero_and_no_dangling_separator(tmp_path):
    _db_ready(tmp_path)
    text = asyncio.run(ua_mod._game_counter_text([]))
    assert "0" in text
    assert not text.endswith("·") and not text.endswith("· ")
    assert text == "Частей: 0"


def test_counter_text_breakdown_ordered_photo_before_text_zero_kinds_omitted(tmp_path):
    _db_ready(tmp_path)
    parts = [{"kind": "text", "content": "x", "caption": None},
             {"kind": "photo", "content": "p1", "caption": None}]  # текст пришёл ПЕРВЫМ
    text = asyncio.run(ua_mod._game_counter_text(parts))
    assert "📸1" in text and "✍️1" in text
    assert text.index("📸1") < text.index("✍️1")  # порядок фиксированный, не по приходу
    assert "📄" not in text and "🔗" not in text  # нулевые виды не показываются
    assert text == "Частей: 2 · 📸1 ✍️1"


def test_counter_text_survives_broken_template_in_registry(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_proof_collected_template", "Собрано {oops}"))
    text = asyncio.run(ua_mod._game_counter_text([{"kind": "photo", "content": "p"}]))
    assert "1" in text and "📸1" in text  # фолбэк, а не исключение посреди сдачи


def test_counter_kb_empty_has_done_and_cancel_only(tmp_path):
    _db_ready(tmp_path)
    kb = asyncio.run(ua_mod._game_counter_kb([]))
    assert _flat_kb_data(kb) == ["gs_done", "gs_cancel"]
    assert "🗑 Убрать последнее" not in _flat_kb_texts(kb)


def test_counter_kb_with_parts_has_all_three_buttons(tmp_path):
    _db_ready(tmp_path)
    kb = asyncio.run(ua_mod._game_counter_kb([{"kind": "text", "content": "x"}]))
    assert _flat_kb_data(kb) == ["gs_done", "gs_remove_last", "gs_cancel"]
    assert _flat_kb_texts(kb) == ["✅ Готово", "🗑 Убрать последнее", "❌ Отмена"]


def test_counter_button_label_comes_from_registry(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_proof_remove_last_button", "Убрать"))
    kb = asyncio.run(ua_mod._game_counter_kb([{"kind": "text", "content": "x"}]))
    assert "Убрать" in _flat_kb_texts(kb)


def test_registry_has_two_new_game_keys_and_admin_order_lists_them():
    import settings_schema as s
    from handlers import admin as _admin_mod  # noqa: F401 -- ядро первым, иначе circular import
    from handlers import admin_settings
    for key in ("game_proof_collected_template", "game_proof_remove_last_button"):
        assert s.SETTINGS_SCHEMA[key]["group"] == "game"
        assert s.SETTINGS_SCHEMA[key]["default"]
        assert key in admin_settings._GAME_FIELD_ORDER
    assert "{count}" in s.SETTINGS_SCHEMA["game_proof_collected_template"]["default"]
    assert "{breakdown}" in s.SETTINGS_SCHEMA["game_proof_collected_template"]["default"]


def test_counter_helpers_live_in_seam_module_and_are_the_same_objects():
    """Вынесено в handlers/game_submit_counter.py из-за потолка размера user_actions.py --
    ua_mod._game_counter_* обязаны быть теми же объектами, не копиями."""
    assert ua_mod._game_counter_text is counter_mod.game_counter_text
    assert ua_mod._game_counter_kb is counter_mod.game_counter_kb
    assert ua_mod._edit_counter is counter_mod.edit_counter
    assert not hasattr(ua_mod, "_game_done_kb")  # старая клавиатура «только Готово» убрана


# ── Task 2: проводка счётчика в поток сдачи + gs_remove_last / gs_cancel ───────────────────

def test_submit_start_sends_prompt_then_counter_and_persists_counter_ids(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, cb = _start(task_id)

    assert asyncio.run(state.get_state()) == GameSubmit.proof
    assert len(cb.message.answers) == 2  # промпт + счётчик, без третьего «жми Готово»
    prompt, _pm, _rm = cb.message.answers[0]
    assert "Готово" in prompt  # game_proof_done_hint по-прежнему в хвосте промпта
    counter_text, _pm2, kb = cb.message.answers[1]
    assert counter_text == "Частей: 0"
    assert _flat_kb_data(kb) == ["gs_done", "gs_cancel"]

    data = asyncio.run(state.get_data())
    assert data["gs_counter_chat_id"] == DELEGATE_ID
    assert data["gs_counter_msg_id"] == _MSG_ID_COUNTER[0]  # id ВТОРОГО отправленного сообщения
    assert data["gs_parts"] == []


def test_receive_proof_edits_counter_instead_of_new_message(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    counter_id = asyncio.run(state.get_data())["gs_counter_msg_id"]
    bot = FakeBot()

    msg = FakeMessage(text="мой пост")
    asyncio.run(ua_mod.receive_proof(msg, bot, state))

    assert msg.answers == []  # в чат НЕ ушло новое сообщение на эту часть
    assert bot.sent == []
    assert len(bot.edits) == 1
    chat_id, message_id, text, kb = bot.edits[0]
    assert (chat_id, message_id) == (DELEGATE_ID, counter_id)
    assert text == "Частей: 1 · ✍️1"
    assert _flat_kb_data(kb) == ["gs_done", "gs_remove_last", "gs_cancel"]

    asyncio.run(ua_mod.receive_proof(FakeMessage(photo=[FakePhotoSize("p1")]), bot, state))
    assert len(bot.edits) == 2
    assert bot.edits[1][2] == "Частей: 2 · 📸1 ✍️1"


def test_receive_proof_overflow_hint_is_still_a_separate_message(tmp_path):
    """CR-01: подсказка про MAX_PARTS -- отдельное предупреждение, не счётчик; часть НЕ
    добавляется, состояние живо."""
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    parts = [{"kind": "text", "content": str(i), "caption": None} for i in range(ua_mod.MAX_PARTS)]
    asyncio.run(state.update_data(gs_parts=parts))
    bot = FakeBot()

    msg = FakeMessage(text="лишняя")
    asyncio.run(ua_mod.receive_proof(msg, bot, state))

    assert len(msg.answers) == 1 and "Готово" in msg.answers[0][0]
    # у подсказки своя клавиатура «Готово/Отмена» (без «Убрать»: тап по ней правил бы саму
    # подсказку, а не счётчик) -- регресс-гард test_game_review_limits_260817 требует markup
    assert _flat_kb_data(msg.answers[0][2]) == ["gs_done", "gs_cancel"]
    assert bot.edits == []
    assert len(asyncio.run(state.get_data())["gs_parts"]) == ua_mod.MAX_PARTS
    assert asyncio.run(state.get_state()) == GameSubmit.proof


def test_receive_proof_edit_failure_is_fail_soft(tmp_path):
    """Pitfall 1 / T-16-02-02: счётчик удалён/устарел -- часть всё равно добавлена, состояние
    не сломано, исключение не вылетает."""
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)

    class ExplodingBot(FakeBot):
        async def edit_message_text(self, *a, **kw):
            raise RuntimeError("message to edit not found")

    asyncio.run(ua_mod.receive_proof(FakeMessage(text="часть"), ExplodingBot(), state))
    data = asyncio.run(state.get_data())
    assert [p["kind"] for p in data["gs_parts"]] == ["text"]
    assert asyncio.run(state.get_state()) == GameSubmit.proof


def test_ack_album_edits_counter_once_not_send_message(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    counter_id = asyncio.run(state.get_data())["gs_counter_msg_id"]
    bot = FakeBot()
    for i in range(3):
        msg = FakeMessage(photo=[FakePhotoSize(f"a{i}")], media_group_id="mg-16-02")
        asyncio.run(ua_mod.receive_proof(msg, bot, state))
        assert msg.answers == []
    assert bot.edits == []  # per-photo -- ничего; ack один после окна сборки
    ua_mod._gs_pending_albums.pop("mg-16-02", None)

    asyncio.run(ua_mod._ack_album("mg-16-02", bot, DELEGATE_ID, state))
    assert bot.sent == []
    assert len(bot.edits) == 1
    chat_id, message_id, text, kb = bot.edits[0]
    assert (chat_id, message_id) == (DELEGATE_ID, counter_id)
    assert text == "Частей: 3 · 📸3"


def test_gs_remove_last_pops_draft_only_and_edits_own_message(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    bot = FakeBot()
    asyncio.run(ua_mod.receive_proof(FakeMessage(photo=[FakePhotoSize("p1")]), bot, state))
    asyncio.run(ua_mod.receive_proof(FakeMessage(text="подпись"), bot, state))

    cb = FakeCallback("gs_remove_last")
    asyncio.run(ua_mod.gs_remove_last(cb, state))

    data = asyncio.run(state.get_data())
    assert [p["kind"] for p in data["gs_parts"]] == ["photo"]  # ушла ПОСЛЕДНЯЯ (текст)
    assert asyncio.run(state.get_state()) == GameSubmit.proof
    assert cb.answers == [(None, False)]
    assert len(cb.message.edits) == 1
    text, kb = cb.message.edits[0]
    assert text == "Частей: 1 · 📸1"
    assert _flat_kb_data(kb) == ["gs_done", "gs_remove_last", "gs_cancel"]
    assert asyncio.run(db.list_all_submissions()) == []  # БД не тронута

    # второй раз -> черновик пуст: кнопка «Убрать» пропадает из клавиатуры
    cb2 = FakeCallback("gs_remove_last")
    asyncio.run(ua_mod.gs_remove_last(cb2, state))
    text2, kb2 = cb2.message.edits[0]
    assert text2 == "Частей: 0"
    assert _flat_kb_data(kb2) == ["gs_done", "gs_cancel"]


def test_gs_remove_last_on_empty_draft_alerts_and_does_not_edit(tmp_path):
    """T-16-02-01: пустой черновик -> alert, ничего не редактируем, состояние живо."""
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)

    cb = FakeCallback("gs_remove_last")
    asyncio.run(ua_mod.gs_remove_last(cb, state))
    assert cb.answers == [("Уже пусто", True)]
    assert cb.message.edits == []
    assert asyncio.run(state.get_state()) == GameSubmit.proof


def test_gs_cancel_inline_mirrors_text_cancel_and_returns_main_menu(tmp_path):
    from aiogram.types import ReplyKeyboardMarkup
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    asyncio.run(ua_mod.receive_proof(FakeMessage(text="почти"), FakeBot(), state))

    cb = FakeCallback("gs_cancel")
    asyncio.run(ua_mod.gs_cancel(cb, state))

    assert asyncio.run(state.get_state()) is None
    assert asyncio.run(state.get_data()).get("gs_parts") == []
    assert asyncio.run(db.get_active_submission(task_id, DELEGATE_ID)) is None
    assert len(cb.message.edits) == 1 and cb.message.edits[0][1] is None  # счётчик без кнопок
    # UAT-fix c79cd6f: reply-клавиатуру «Отмена» edit'ом не снять -- главное меню отдельным
    # сообщением, как у текстовой «Отмена».
    text, _pm, kb = cb.message.answers[-1]
    assert text == "Действие отменено."
    assert isinstance(kb, ReplyKeyboardMarkup)


def test_text_cancel_still_works_via_shared_helper(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    asyncio.run(ua_mod.receive_proof(FakeMessage(text="почти"), FakeBot(), state))
    msg = FakeMessage(text="Отмена")
    asyncio.run(ua_mod.cancel_game_submit(msg, state))
    assert asyncio.run(state.get_state()) is None
    assert msg.answers[-1][0] == "Действие отменено."


def test_gs_done_empty_leaves_counter_and_state_untouched(tmp_path):
    """09.1 A регресс-гард: пустая «Готово» -> подсказка alert'ом, счётчик не трогаем."""
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    bot = FakeBot()
    cb = FakeCallback("gs_done")
    asyncio.run(ua_mod.finalize_game_submission(cb, bot, state))
    assert cb.answers == [
        ("Сначала пришли хотя бы одну часть — фото, файл, текст или ссылку.", True)
    ]
    assert cb.message.edits == [] and bot.edits == []
    assert asyncio.run(state.get_state()) == GameSubmit.proof


def test_gs_done_after_remove_last_writes_only_remaining_parts(tmp_path):
    """Черновик после «Убрать последнее» -> в БД ровно оставшиеся части, порядок сохранён."""
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    state, _cb = _start(task_id)
    bot = FakeBot()
    asyncio.run(ua_mod.receive_proof(FakeMessage(photo=[FakePhotoSize("p1")]), bot, state))
    asyncio.run(ua_mod.receive_proof(FakeMessage(text="лишний текст"), bot, state))
    asyncio.run(ua_mod.gs_remove_last(FakeCallback("gs_remove_last"), state))

    done = FakeCallback("gs_done")
    asyncio.run(ua_mod.finalize_game_submission(done, bot, state))
    active = asyncio.run(db.get_active_submission(task_id, DELEGATE_ID))
    assert active is not None
    parts = asyncio.run(db.list_submission_parts(active["id"]))
    assert [p["kind"] for p in parts] == ["photo"]
    assert asyncio.run(state.get_state()) is None
