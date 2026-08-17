"""Regression for CR-01/CR-02 (`.planning/phases/09.1-.../09.1-REVIEW.md`) — the moderation
queue must not jam on ordinary delegate input:

- CR-01: `receive_proof` had no cap on part count/length, and `_show_current_submission` sent
  the rendered card with a bare `await target.answer(...)` (no try/except).
- CR-02: the photo resend loop collected every consecutive photo part into a single
  `send_media_group` call, which Telegram rejects outright above 10 items -- silently hiding
  the evidence from the moderator.

pytest-asyncio is unavailable in this env -- every async call is driven via asyncio.run(),
same convention as tests/test_game_free_proof_091.py. Fixtures below are copied locally from
that file's T2/T3 sections (this repo does not import fixtures between test files).
"""
import asyncio
import inspect

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
import handlers.admin as admin_mod
from handlers import user_actions as ua_mod
from handlers.states import GameSubmit


# ── shared DB setup ───────────────────────────────────────────────────────────────────────

def _db_ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── T2-style fixtures (delegate side, copied from tests/test_game_free_proof_091.py) ──────

_ADMIN_ID = 260817101
_DELEGATE_ID = 260817102


def _t2_db_ready(tmp_path, name="test_game_review_limits_260817_t2.db"):
    _db_ready(tmp_path, name)
    config.ADMIN_IDS = [_ADMIN_ID]


def _t2_seed_delegate(uid=_DELEGATE_ID):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-01",
    }))


def _t2_seed_task(proof_type="photo,text", deadline_at="2099-01-01 00:00:00"):
    return asyncio.run(db.create_task("t", "Light", 15, proof_type, deadline_at, _ADMIN_ID))


class _T2FakeUser:
    def __init__(self, uid, full_name=None):
        self.id = uid
        self.full_name = full_name


class _T2FakeMessage:
    def __init__(self, text=None, user_id=_DELEGATE_ID, photo=None, document=None,
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
    def __init__(self, data, user_id=_DELEGATE_ID, full_name=None):
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


def _t2_new_state(uid=_DELEGATE_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _t2_start(task_id, uid=_DELEGATE_ID):
    state = _t2_new_state(uid)
    callback = _T2FakeCallback(f"mytask_submit:{task_id}", user_id=uid)
    asyncio.run(ua_mod.mytask_submit_start(callback, state))
    return state, callback


# ── T3-style fixtures (moderator side, copied from tests/test_game_free_proof_091.py) ─────

_MANAGER_ID = 260817201
_T3_DELEGATE_ID = 260817202


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
    def __init__(self, user_id=_MANAGER_ID, bot=None):
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


def _t3_db_ready(tmp_path, name="test_game_review_limits_260817_t3.db"):
    _db_ready(tmp_path, name)
    config.ADMIN_IDS = [_MANAGER_ID]


def _t3_new_state(uid=_MANAGER_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _callback_datas(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _seed_submission(task_id, parts):
    """parts: list of (kind, content, caption) -> creates one pending submission with those
    parts, mirroring test_t3_moderation_card_batches_two_photos_and_shows_text_part's seed
    sequence."""
    first_kind, first_content, _ = parts[0]
    sub_id = asyncio.run(db.create_submission(
        task_id, _T3_DELEGATE_ID, first_kind, first_content, "2026-08-20 10:00:00",
    ))
    for i, (kind, content, caption) in enumerate(parts):
        asyncio.run(db.add_submission_part(sub_id, i, kind, content, caption))
    return sub_id


# ── CR-01: delegate-side part/length ceiling ───────────────────────────────────────────────

def test_receive_proof_21st_part_rejected_state_intact(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="text")
    state, _cb = _t2_start(task_id)
    bot = _T2FakeBot()

    last_message = None
    for i in range(ua_mod.MAX_PARTS + 1):
        last_message = _T2FakeMessage(text=f"часть {i}")
        asyncio.run(ua_mod.receive_proof(last_message, bot, state))

    data = asyncio.run(state.get_data())
    assert len(data["gs_parts"]) == ua_mod.MAX_PARTS
    assert asyncio.run(state.get_state()) == GameSubmit.proof.state
    last_text, _parse_mode, last_markup = last_message.answers[-1]
    assert f"Больше {ua_mod.MAX_PARTS} частей" in last_text
    assert last_markup is not None


def test_receive_proof_long_text_truncated_at_max_text_part(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="text")
    state, _cb = _t2_start(task_id)
    bot = _T2FakeBot()

    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text="x" * 5000), bot, state))
    data = asyncio.run(state.get_data())
    assert len(data["gs_parts"]) == 1
    assert len(data["gs_parts"][0]["content"]) == ua_mod.MAX_TEXT_PART


def test_receive_proof_text_exactly_at_boundary_not_truncated(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="text")
    state, _cb = _t2_start(task_id)
    bot = _T2FakeBot()

    exact = "y" * ua_mod.MAX_TEXT_PART
    asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text=exact), bot, state))
    data = asyncio.run(state.get_data())
    assert data["gs_parts"][0]["content"] == exact


def test_receive_proof_overflow_hint_within_album_sent_once(tmp_path):
    _t2_db_ready(tmp_path)
    _t2_seed_delegate()
    task_id = _t2_seed_task(proof_type="text")
    state, _cb = _t2_start(task_id)
    bot = _T2FakeBot()

    for i in range(ua_mod.MAX_PARTS):
        asyncio.run(ua_mod.receive_proof(_T2FakeMessage(text=f"часть {i}"), bot, state))

    messages = [
        _T2FakeMessage(photo=[_T2FakePhotoSize(f"overflow_{i}")], media_group_id="mg_overflow")
        for i in range(3)
    ]
    for msg in messages:
        asyncio.run(ua_mod.receive_proof(msg, bot, state))

    total_hints = sum(
        1 for msg in messages for (text, _pm, _rm) in msg.answers
        if f"Больше {ua_mod.MAX_PARTS} частей" in text
    )
    assert total_hints == 1


def test_receive_proof_max_parts_structural_anchor():
    """The overflow guard must actually reference MAX_PARTS (not a hardcoded 20) so a future
    constant edit can't silently desync the flow from its own comment."""
    src = inspect.getsource(ua_mod.receive_proof)
    code_lines = [line for line in src.splitlines() if not line.lstrip().startswith("#")]
    assert "MAX_PARTS" in "\n".join(code_lines)


# ── CR-01: moderator-side card ceiling + fail-soft send ────────────────────────────────────

def test_show_current_submission_two_long_text_parts_fit_card_limit(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "text", "2099-01-01 00:00:00", _MANAGER_ID,
    ))
    _seed_submission(task_id, [
        ("text", "a" * 4000, None),
        ("text", "b" * 4000, None),
    ])

    message = _T3FakeMessage(bot=_T3FakeBot())
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    card_text = message.answers_sent[0]
    assert len(card_text) <= 4096
    for line in card_text.splitlines():
        assert len(line) <= admin_mod._CARD_PART_MAX + 3  # +1 for the '• ', +1 for '…', margin
    assert "…" in card_text
    assert "grev_approve:" in _callback_datas(message.answer_markups[0])[0]


def test_show_current_submission_twenty_parts_hits_card_ceiling(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "text", "2099-01-01 00:00:00", _MANAGER_ID,
    ))
    _seed_submission(task_id, [("text", "z" * 500, None) for _ in range(20)])

    message = _T3FakeMessage(bot=_T3FakeBot())
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    card_text = message.answers_sent[0]
    assert len(card_text) <= 4096
    assert "…(обрезано)" in card_text


def test_show_current_submission_card_send_failure_falls_back_with_keyboard(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "text", "2099-01-01 00:00:00", _MANAGER_ID,
    ))
    _seed_submission(task_id, [("text", "обычный текст", None)])

    class _FailingHtmlMessage(_T3FakeMessage):
        async def answer(self, text, parse_mode=None, reply_markup=None):
            if parse_mode == "HTML":
                raise RuntimeError("simulated sendMessage failure")
            self.answers_sent.append(text)
            self.answer_markups.append(reply_markup)

    message = _FailingHtmlMessage(bot=_T3FakeBot())
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))  # must not raise

    assert len(message.answers_sent) == 1  # only the fallback text made it through
    datas = _callback_datas(message.answer_markups[-1])
    assert any("grev_approve:" in d for d in datas)
    assert any("grev_skip:" in d for d in datas)


# ── CR-02: moderator-side media group chunking + visible resend failures ──────────────────

def test_show_current_submission_twelve_photos_chunk_into_ten_and_two(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "photo", "2099-01-01 00:00:00", _MANAGER_ID,
    ))
    _seed_submission(task_id, [("photo", f"p{i}", None) for i in range(12)])

    bot = _T3FakeBot()
    message = _T3FakeMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    assert len(bot.media_groups) == 2
    assert [len(m) for _c, m in bot.media_groups] == [10, 2]
    assert message.photos_sent == []


def test_show_current_submission_eleven_photos_tail_uses_answer_photo(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "photo", "2099-01-01 00:00:00", _MANAGER_ID,
    ))
    _seed_submission(task_id, [("photo", f"p{i}", None) for i in range(11)])

    bot = _T3FakeBot()
    message = _T3FakeMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    assert len(bot.media_groups) == 1
    assert len(bot.media_groups[0][1]) == 10
    assert message.photos_sent == ["p10"]  # 11th photo, one-element tail chunk


def test_show_current_submission_long_caption_truncated(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "photo", "2099-01-01 00:00:00", _MANAGER_ID,
    ))
    _seed_submission(task_id, [
        ("photo", "p0", None),
        ("photo", "p1", "c" * 3000),
    ])

    bot = _T3FakeBot()
    message = _T3FakeMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))

    _chat_id, media = bot.media_groups[0]
    for item in media:
        if item.caption is not None:
            assert len(item.caption) <= admin_mod._MEDIA_CAPTION_MAX


def test_show_current_submission_resend_failure_visible_to_moderator(tmp_path):
    _t3_db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "Задание", "Light", 20, "photo", "2099-01-01 00:00:00", _MANAGER_ID,
    ))
    _seed_submission(task_id, [("photo", f"p{i}", None) for i in range(12)])

    bot = _T3FakeBot(media_group_raises=True)
    message = _T3FakeMessage(bot=bot)
    state = _t3_new_state()
    asyncio.run(admin_mod._show_current_submission(message, state))  # must not raise

    # Card is sent FIRST regardless of what happens to attachments afterward.
    assert "Задание" in message.answers_sent[0] or "Сдача" in message.answers_sent[0]
    warnings = [t for t in message.answers_sent if "Часть вложений показать не удалось" in t]
    assert len(warnings) == 1
