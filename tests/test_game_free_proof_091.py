"""Phase 09.1 Plan 01 (GAME-05, A) — free-form submission: game_submission_parts table +
accessors, `SETTINGS_SCHEMA` "game" group, delegate multi-part accumulate FSM, manager
proof-type checkboxes, moderation card rendering all parts.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run(),
config.DB_PATH points at a tmp_path file (same convention as every other phase-9 test file).
"""
import asyncio

from config import config
from database import db


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_free_proof_091.db")
    asyncio.run(db.init_db())


# ── Task 1: game_submission_parts + accessors ────────────────────────────────────────────

def test_add_and_list_submission_parts_in_order(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "photo,text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "photo", "file_abc", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "photo", "file_abc", None))
    asyncio.run(db.add_submission_part(sub_id, 1, "text", "готово", None))
    parts = asyncio.run(db.list_submission_parts(sub_id))
    assert [p["kind"] for p in parts] == ["photo", "text"]
    assert [p["ord"] for p in parts] == [0, 1]


def test_get_submission_parts_or_legacy_synthesizes_one_part_for_old_row(tmp_path):
    _db_ready(tmp_path)
    with_content = {"id": 1, "content_type": "photo", "content": "file_xyz"}
    parts = asyncio.run(db.get_submission_parts_or_legacy(with_content))
    assert len(parts) == 1
    assert parts[0] == {"ord": 0, "kind": "photo", "content": "file_xyz", "caption": None}


def test_get_submission_parts_or_legacy_maps_pdf_to_document(tmp_path):
    _db_ready(tmp_path)
    row = {"id": 2, "content_type": "pdf", "content": "file_pdf"}
    parts = asyncio.run(db.get_submission_parts_or_legacy(row))
    assert parts[0]["kind"] == "document"


def test_get_submission_parts_or_legacy_ignores_legacy_columns_when_parts_exist(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "text", "legacy content", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "link", "https://example.com", None))
    submission = asyncio.run(db.get_submission(sub_id))
    parts = asyncio.run(db.get_submission_parts_or_legacy(submission))
    assert len(parts) == 1
    assert parts[0]["kind"] == "link"
    assert parts[0]["content"] == "https://example.com"


def test_get_submission_parts_or_legacy_empty_content_returns_empty_list(tmp_path):
    _db_ready(tmp_path)
    row = {"id": 3, "content_type": "text", "content": ""}
    parts = asyncio.run(db.get_submission_parts_or_legacy(row))
    assert parts == []


def test_parse_proof_types_multi():
    assert db.parse_proof_types("photo,text") == ["photo", "text"]


def test_parse_proof_types_single():
    assert db.parse_proof_types("photo") == ["photo"]


def test_parse_proof_types_empty_string():
    assert db.parse_proof_types("") == []


def test_parse_proof_types_none():
    assert db.parse_proof_types(None) == []


def test_parse_proof_types_drops_unknown_codes():
    assert db.parse_proof_types("photo,bogus,text") == ["photo", "text"]


def test_parse_proof_types_preserves_canonical_order_not_input_order():
    assert db.parse_proof_types("text,photo") == ["photo", "text"]


def test_init_db_twice_is_idempotent(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.init_db())  # second call on the same file must not raise


def test_game_settings_schema_has_nine_keys_in_game_group():
    # Phase 14 (GAME-10) added a 10th key, "game_resubmit_limit" -- count bumped from the
    # original 09.1-Wave-1 baseline of 9. Test name kept for git-blame continuity.
    import settings_schema as s
    keys = [k for k, v in s.SETTINGS_SCHEMA.items() if v["group"] == "game"]
    assert len(keys) == 10
    for k in keys:
        assert s.SETTINGS_SCHEMA[k]["default"] not in (None, "")


def test_game_settings_group_registered_in_admin():
    import handlers.admin as a
    assert ("🎮 Геймификация", "game", a._GAME_FIELD_ORDER) in a.SETTINGS_GROUPS


# ── Task 2: delegate — accumulate parts + «✅ Готово» finalize, no timeout ────────────────

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import user_actions as ua_mod
from handlers.states import GameSubmit

_T2_ADMIN_ID = 940901
_T2_DELEGATE_ID = 940902


def _t2_db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_free_proof_091_t2.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [_T2_ADMIN_ID]


def _t2_seed_delegate(uid=_T2_DELEGATE_ID):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-01",
    }))


def _t2_seed_task(proof_type="photo", deadline_at="2099-01-01 00:00:00"):
    return asyncio.run(db.create_task("t", "Light", 15, proof_type, deadline_at, _T2_ADMIN_ID))


class _T2FakeUser:
    def __init__(self, uid, full_name=None):
        self.id = uid
        self.full_name = full_name


class _T2FakeMessage:
    def __init__(self, text=None, user_id=_T2_DELEGATE_ID, photo=None, document=None,
                 caption=None, media_group_id=None):
        self.text = text
        self.from_user = _T2FakeUser(user_id)
        self.photo = photo
        self.document = document
        self.caption = caption
        self.media_group_id = media_group_id
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class _T2FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class _T2FakeCallback:
    def __init__(self, data, user_id=_T2_DELEGATE_ID, full_name=None):
        self.data = data
        self.from_user = _T2FakeUser(user_id, full_name=full_name)
        self.message = _T2FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _T2FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


def _t2_new_state(uid=_T2_DELEGATE_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _t2_start(task_id, uid=_T2_DELEGATE_ID):
    state = _t2_new_state(uid)
    callback = _T2FakeCallback(f"mytask_submit:{task_id}", user_id=uid)
    asyncio.run(ua_mod.mytask_submit_start(callback, state))
    return state, callback


def test_t2_three_messages_in_a_row_give_three_parts_in_order(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="photo,text")
    state, _cb = _t2_start(task_id)
    bot = _T2FakeBot()

    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(photo=[_T2FakePhotoSize("p1")]), bot, state))
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(document=DummyDoc("d1")), bot, state))
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text="привет"), bot, state))

    data = asyncio.run(state.get_data())
    kinds = [p["kind"] for p in data["gs_parts"]]
    assert kinds == ["photo", "document", "text"]


class DummyDoc:
    def __init__(self, file_id):
        self.file_id = file_id


def test_t2_document_of_any_type_becomes_document_part(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="pdf")
    state, _cb = _t2_start(task_id)
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(document=DummyDoc("f.zip")), _T2FakeBot(), state))
    data = asyncio.run(state.get_data())
    assert data["gs_parts"][0]["kind"] == "document"
    assert data["gs_parts"][0]["content"] == "f.zip"


def test_t2_link_detected_from_http_prefix_else_text(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="link")
    state, _cb = _t2_start(task_id)
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text="https://example.com/proof"), _T2FakeBot(), state))
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text="просто текст"), _T2FakeBot(), state))
    data = asyncio.run(state.get_data())
    assert [p["kind"] for p in data["gs_parts"]] == ["link", "text"]


def test_t2_photo_caption_preserved_in_part(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="photo")
    state, _cb = _t2_start(task_id)
    asyncio.run(ua_mod.receive_proof(
        _T2FakeMessage(photo=[_T2FakePhotoSize("p1")], caption="вот скрин"), _T2FakeBot(), state,
    ))
    data = asyncio.run(state.get_data())
    assert data["gs_parts"][0]["caption"] == "вот скрин"


def test_t2_album_of_n_photos_yields_n_parts_not_one(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="photo")
    state, _cb = _t2_start(task_id)
    bot = _T2FakeBot()
    for i in range(3):
        msg = _T2FakeMessage(photo=[_T2FakePhotoSize(f"album_{i}")], media_group_id="mg1")
        asyncio.run(ua_mod.receive_proof(msg, bot, state))
    data = asyncio.run(state.get_data())
    assert len(data["gs_parts"]) == 3
    assert all(p["kind"] == "photo" for p in data["gs_parts"])
    # ack for the album is debounced (spawned), not sent inline per photo
    assert bot.sent == []


def test_t2_done_with_empty_parts_shows_hint_state_kept(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="text")
    state, _cb = _t2_start(task_id)
    done_cb = _T2FakeCallback("gs_done")
    asyncio.run(ua_mod.finalize_game_submission(done_cb, _T2FakeBot(), state))
    assert done_cb.answers == [
        ("Сначала пришли хотя бы одну часть — фото, файл, текст или ссылку.", True)
    ]
    assert asyncio.run(state.get_state()) == GameSubmit.proof
    assert asyncio.run(db.get_active_submission(task_id, _T2_DELEGATE_ID)) is None


def test_t2_done_with_parts_creates_one_submission_and_n_parts_resets_state(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="photo,text")
    state, _cb = _t2_start(task_id)
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(photo=[_T2FakePhotoSize("p1")]), _T2FakeBot(), state))
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text="готово"), _T2FakeBot(), state))

    bot = _T2FakeBot()
    done_cb = _T2FakeCallback("gs_done")
    asyncio.run(ua_mod.finalize_game_submission(done_cb, bot, state))

    assert asyncio.run(state.get_state()) is None
    active = asyncio.run(db.get_active_submission(task_id, _T2_DELEGATE_ID))
    assert active is not None
    parts = asyncio.run(db.list_submission_parts(active["id"]))
    assert len(parts) == 2
    assert "Принято!" in done_cb.message.answers[-1][0]
    assert len(bot.sent) == 1  # notify_by_capability fallback to config.ADMIN_IDS


def test_t2_cancel_at_any_point_resets_state_no_db_rows(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="text")
    state, _cb = _t2_start(task_id)
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text="почти"), _T2FakeBot(), state))

    message = _T2FakeMessage(text="Отмена")
    asyncio.run(ua_mod.cancel_game_submit(message, state))

    assert asyncio.run(state.get_state()) is None
    assert asyncio.run(db.get_active_submission(task_id, _T2_DELEGATE_ID)) is None


def test_t2_no_finalizing_sleep_only_album_ack_sleep():
    """NOT a behavioral test of runtime sleep -- static proof there is exactly one
    asyncio.sleep call in the module and it lives in the album-ack helper, not any
    finalization path (mirrors the plan's own grep acceptance criterion)."""
    import inspect
    src = inspect.getsource(ua_mod._ack_album)
    assert "asyncio.sleep" in src
    finalize_src = inspect.getsource(ua_mod.finalize_game_submission)
    assert "asyncio.sleep" not in finalize_src
    receive_src = inspect.getsource(ua_mod.receive_proof)
    assert "asyncio.sleep" not in receive_src


# ── Task 3: manager checkboxes + moderator sees all parts pooled ─────────────────────────

import handlers.admin as admin_mod
from handlers.states import GameTaskCreate

_T3_MANAGER_ID = 940801
_T3_DELEGATE_ID = 940802


class _T3FakeUser:
    def __init__(self, uid):
        self.id = uid


class _T3FakeBot:
    def __init__(self, media_group_raises=False):
        self.sent = []
        self.media_groups = []
        self._media_group_raises = media_group_raises

    async def send_media_group(self, chat_id, media):
        if self._media_group_raises:
            raise RuntimeError("boom")
        self.media_groups.append((chat_id, media))

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class _T3FakeMessage:
    def __init__(self, user_id=_T3_MANAGER_ID, bot=None):
        self.from_user = _T3FakeUser(user_id)
        self.bot = bot
        self.answers_sent = []
        self.answer_markups = []
        self.photos_sent = []
        self.documents_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def answer_photo(self, photo):
        self.photos_sent.append(photo)

    async def answer_document(self, document):
        self.documents_sent.append(document)


class _T3FakeCallback:
    def __init__(self, data, user_id=_T3_MANAGER_ID, bot=None):
        self.data = data
        self.from_user = _T3FakeUser(user_id)
        self.bot = bot if bot is not None else _T3FakeBot()
        self.message = _T3FakeMessage(user_id=user_id, bot=self.bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _t3_db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_free_proof_091_t3.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [_T3_MANAGER_ID]


def _t3_new_state(uid=_T3_MANAGER_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def test_t3_proof_types_label_helper():
    assert admin_mod._proof_types_label("photo,text") == "📷 Скриншот/фото + ✍️ Текст"
    assert admin_mod._proof_types_label("") == "не важно"
    assert admin_mod._proof_types_label(None) == "не важно"


def test_t3_task_created_with_two_types_stores_comma_joined_and_roundtrips(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "t", "Light", 20, "photo,text", "2099-01-01 00:00:00", _T3_MANAGER_ID,
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["proof_type"] == "photo,text"
    assert db.parse_proof_types(task["proof_type"]) == ["photo", "text"]


def test_t3_render_submission_card_default_parts_none_unchanged():
    """Existing call sites/tests that never pass `parts` keep the pre-09.1 rendering."""
    row = {
        "task_text": "Задание", "task_category": "Light", "task_coins": 20,
        "user_full_name": "Тест", "user_username": None,
        "task_proof_type": "photo", "content_type": "photo", "content": "x",
        "submitted_at": None, "task_deadline_at": None,
    }
    text = admin_mod._render_submission_card(row, 1, 1)
    assert "см. файл ниже" in text


def test_t3_moderation_card_batches_two_photos_and_shows_text_part(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "photo,text", "2099-01-01 00:00:00", _T3_MANAGER_ID,
    ))
    sub_id = asyncio.run(db.create_submission(task_id, _T3_DELEGATE_ID, "photo", "p1", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "photo", "p1", None))
    asyncio.run(db.add_submission_part(sub_id, 1, "photo", "p2", "второе фото"))
    asyncio.run(db.add_submission_part(sub_id, 2, "text", "готово, всё сделал", None))

    bot = _T3FakeBot()
    message = _T3FakeMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    card_text = message.answers_sent[-1]
    assert "готово, всё сделал" in card_text
    assert card_text.count("см. файл ниже") == 2  # one line per photo part

    assert len(bot.media_groups) == 1
    _chat_id, media = bot.media_groups[0]
    assert len(media) == 2
    assert message.photos_sent == []  # both photos went through the batched media group
    assert message.documents_sent == []


def test_t3_moderation_card_single_photo_still_uses_answer_photo(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "photo", "2099-01-01 00:00:00", _T3_MANAGER_ID,
    ))
    sub_id = asyncio.run(db.create_submission(task_id, _T3_DELEGATE_ID, "photo", "only_one", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "photo", "only_one", None))

    bot = _T3FakeBot()
    message = _T3FakeMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    assert message.photos_sent == ["only_one"]
    assert bot.media_groups == []


def test_t3_moderation_card_document_part_resent_via_answer_document(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "pdf", "2099-01-01 00:00:00", _T3_MANAGER_ID,
    ))
    sub_id = asyncio.run(db.create_submission(task_id, _T3_DELEGATE_ID, "pdf", "doc1", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "document", "doc1", None))

    bot = _T3FakeBot()
    message = _T3FakeMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    assert message.documents_sent == ["doc1"]


def test_t3_moderation_card_survives_broken_file_id_logs_and_continues(tmp_path):
    """T-behavior: a stale file_id on resend must not crash the queue -- exception caught,
    logged, the card text itself (already sent before the resend attempt) stays delivered."""
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "photo,text", "2099-01-01 00:00:00", _T3_MANAGER_ID,
    ))
    sub_id = asyncio.run(db.create_submission(task_id, _T3_DELEGATE_ID, "photo", "bad_id", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "photo", "bad_id", None))
    asyncio.run(db.add_submission_part(sub_id, 1, "text", "текст всё равно есть", None))

    class _BoomMessage(_T3FakeMessage):
        async def answer_photo(self, photo):
            raise RuntimeError("stale file_id")

    bot = _T3FakeBot()
    message = _BoomMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))  # must not raise

    assert message.answers_sent  # the card text itself was sent


def test_t3_gtproof_done_registered_under_moderate_game():
    from handlers.admin_caps import required_capability
    assert required_capability(callback_data="gtproof_done") == "moderate_game"
