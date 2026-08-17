"""Phase 14 Plan 01 (GAME-08, GAME-10) — task archive/delete data layer + resubmit limit.

`game_tasks.archived_at` (NULL = active), archive/unarchive/delete/count accessors,
`count_rejected_submissions`, `task_archived_at` exposed on the submission queues, the
delegate submit-button label, and the two server-side gates (archived task, exhausted
resubmit limit) in `mytask_submit_start`.

Phase 14 Plan 03 (GAME-08/GAME-10) appends: the manager-facing «📋 Задания» actions
(«🗄 В архив»/«🗑 Удалить»), the «🗄 Архив» screen + «↩️ Вернуть», the two-step confirm gates,
and the moderation-card/sheet-tab archive markers + «попытка K из N».

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_game_city_tasks_091.py / tests/test_gamification_data_phase9.py.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
import settings_schema
from handlers import user_actions as ua_mod
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability


ADMIN_ID = 940911
DELEGATE_ID = 940912


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_archive_260818.db")
    asyncio.run(db.init_db())


def _new_state(uid: int = DELEGATE_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.markup = None
        self.from_user = FakeUser(user_id)
        self.answers = []
        self.edit_calls = 0

    async def answer(self, text=None, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))
        self.text = text
        self.markup = reply_markup

    async def edit_text(self, text=None, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _mk_task(**overrides):
    kwargs = dict(
        text="Задание", category="Light", coins=10, proof_type="text",
        deadline_at="2099-01-01 00:00:00", created_by=None,
    )
    kwargs.update(overrides)
    return asyncio.run(db.create_task(**kwargs))


def _reject_submission(task_id, user_id):
    """Creates a submission and rejects it, returns the submission id. Since the unique
    index only guards non-rejected rows (idx_game_submissions_active), a fresh call can be
    made repeatedly for the same (task, user) pair."""
    sub_id = asyncio.run(db.create_submission(
        task_id, user_id, "text", "готово", "2026-08-20 10:00:00",
    ))
    asyncio.run(db.claim_submission(sub_id, ADMIN_ID, "rejected", reject_reason="не то"))
    return sub_id


# ── Task 1: archive/delete data layer + submission counters ─────────────────────────────────

def test_archived_at_column_migrated_and_idempotent(tmp_path):
    _db_ready(tmp_path)
    # Idempotency: a second init_db() must not raise (ALTER TABLE guarded by _column_exists).
    asyncio.run(db.init_db())

    async def _columns():
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            async with conn.execute("PRAGMA table_info(game_tasks)") as cursor:
                return [row[1] for row in await cursor.fetchall()]

    columns = asyncio.run(_columns())
    assert "archived_at" in columns


def test_list_active_tasks_excludes_archived_list_all_includes(tmp_path):
    _db_ready(tmp_path)
    active_id = _mk_task(text="active")
    archived_id = _mk_task(text="archived")
    asyncio.run(db.archive_task(archived_id))

    active_texts = {t["text"] for t in asyncio.run(db.list_active_tasks())}
    all_texts = {t["text"] for t in asyncio.run(db.list_all_tasks())}
    assert active_texts == {"active"}
    assert all_texts == {"active", "archived"}


def test_archive_task_and_unarchive_task_toggle(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()

    assert asyncio.run(db.archive_task(task_id)) is True
    assert asyncio.run(db.archive_task(task_id)) is False  # already archived
    task = asyncio.run(db.get_task(task_id))
    assert task["archived_at"] is not None

    assert asyncio.run(db.unarchive_task(task_id)) is True
    assert asyncio.run(db.unarchive_task(task_id)) is False  # already active
    task = asyncio.run(db.get_task(task_id))
    assert task["archived_at"] is None


def test_delete_task_refused_with_submissions_allowed_without(tmp_path):
    _db_ready(tmp_path)
    with_sub = _mk_task(text="has submissions")
    _reject_submission(with_sub, DELEGATE_ID)
    without_sub = _mk_task(text="no submissions")

    assert asyncio.run(db.delete_task(with_sub)) is False
    assert asyncio.run(db.get_task(with_sub)) is not None  # row still there

    assert asyncio.run(db.delete_task(without_sub)) is True
    assert asyncio.run(db.get_task(without_sub)) is None


def test_count_task_submissions_and_count_rejected_submissions(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    other_user = DELEGATE_ID + 1
    _reject_submission(task_id, DELEGATE_ID)
    _reject_submission(task_id, DELEGATE_ID)
    _reject_submission(task_id, other_user)

    assert asyncio.run(db.count_task_submissions(task_id)) == 3
    assert asyncio.run(db.count_rejected_submissions(task_id, DELEGATE_ID)) == 2
    assert asyncio.run(db.count_rejected_submissions(task_id, other_user)) == 1
    assert asyncio.run(db.count_rejected_submissions(task_id, DELEGATE_ID + 999)) == 0


def test_pending_and_all_submissions_expose_task_archived_at(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "готово", "2026-08-20 10:00:00"))
    asyncio.run(db.archive_task(task_id))

    pending = asyncio.run(db.get_pending_submissions(limit=50, offset=0))
    assert pending[0]["task_archived_at"] is not None

    all_subs = asyncio.run(db.list_all_submissions())
    assert all_subs[0]["task_archived_at"] is not None


# ── Task 2: registry key + delegate-side label/gates ─────────────────────────────────────────

def test_submit_button_label_short_and_truncated():
    short = ua_mod._submit_button_label({"text": "Короткое"})
    assert short == "📤 Сдать: Короткое"

    long_name = "Очень длинное название задания, которое точно длиннее сорока символов подряд"
    label = ua_mod._submit_button_label({"text": long_name})
    assert label.startswith("📤 Сдать: ")
    body = label[len("📤 Сдать: "):]
    assert body.endswith("…")
    assert len(body) == 41  # 40 chars + ellipsis


def test_mytask_submit_start_archived_task_blocks_with_alert(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    asyncio.run(db.archive_task(task_id))

    state = _new_state()
    callback = FakeCallback(f"mytask_submit:{task_id}")
    asyncio.run(ua_mod.mytask_submit_start(callback, state))

    assert callback.answers, "expected an alert answer"
    text, show_alert = callback.answers[-1]
    assert show_alert is True
    assert "архив" in text.lower()
    assert asyncio.run(state.get_state()) is None  # submission flow NOT entered


def test_mytask_submit_start_resubmit_limit_blocks_at_limit(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    asyncio.run(db.set_setting("game_resubmit_limit", "2"))
    _reject_submission(task_id, DELEGATE_ID)
    _reject_submission(task_id, DELEGATE_ID)

    state = _new_state()
    callback = FakeCallback(f"mytask_submit:{task_id}")
    asyncio.run(ua_mod.mytask_submit_start(callback, state))

    assert callback.answers
    text, show_alert = callback.answers[-1]
    assert show_alert is True
    assert "лимит" in text.lower()
    assert "2" in text
    assert asyncio.run(state.get_state()) is None


def test_mytask_submit_start_resubmit_limit_allows_below_limit(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    asyncio.run(db.set_setting("game_resubmit_limit", "2"))
    _reject_submission(task_id, DELEGATE_ID)

    state = _new_state()
    callback = FakeCallback(f"mytask_submit:{task_id}")
    asyncio.run(ua_mod.mytask_submit_start(callback, state))

    data = asyncio.run(state.get_data())
    assert data.get("gs_task_id") == task_id  # submission flow WAS entered


def test_mytask_submit_start_default_limit_zero_means_no_limit(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task()
    # game_resubmit_limit not set -> default 0 -> regression: behaves exactly like pre-phase
    for _ in range(5):
        _reject_submission(task_id, DELEGATE_ID)

    state = _new_state()
    callback = FakeCallback(f"mytask_submit:{task_id}")
    asyncio.run(ua_mod.mytask_submit_start(callback, state))

    data = asyncio.run(state.get_data())
    assert data.get("gs_task_id") == task_id


def test_game_resubmit_limit_registered_in_schema_and_admin_field_order():
    from handlers import admin as admin_mod
    assert settings_schema.SETTINGS_SCHEMA["game_resubmit_limit"]["type"] == "int"
    assert settings_schema.SETTINGS_SCHEMA["game_resubmit_limit"]["group"] == "game"
    assert settings_schema.SETTINGS_SCHEMA["game_resubmit_limit"]["default"] == 0
    assert "game_resubmit_limit" in admin_mod._GAME_FIELD_ORDER


# ── Plan 03 Task 1: «📋 Задания» actions + «🗄 Архив» + return-from-archive + caps ────────────

def test_tasks_screen_has_archive_button_and_delete_only_without_submissions(tmp_path):
    _db_ready(tmp_path)
    no_subs = _mk_task(text="no submissions")
    with_subs = _mk_task(text="has submissions")
    _reject_submission(with_subs, DELEGATE_ID)

    text, kb = asyncio.run(admin_mod._game_tasks_screen())
    data = _flat_callback_data(kb)

    assert f"gtarchive:{no_subs}" in data
    assert f"gtdelete:{no_subs}" in data
    assert f"gtarchive:{with_subs}" in data
    assert f"gtdelete:{with_subs}" not in data
    assert "можно только убрать в архив" in text


def test_gtarchive_go_archives_task_and_moves_it_to_archive_screen(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="уйдёт в архив")

    callback = FakeCallback(f"gtarchive_go:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_archive_go(callback))

    task = asyncio.run(db.get_task(task_id))
    assert task["archived_at"] is not None

    tasks_text, _ = asyncio.run(admin_mod._game_tasks_screen())
    assert "уйдёт в архив" not in tasks_text

    archive_text, archive_kb = asyncio.run(admin_mod._game_archive_screen())
    assert "уйдёт в архив" in archive_text
    assert f"gtunarchive:{task_id}" in _flat_callback_data(archive_kb)


def test_gtunarchive_returns_task_to_active_without_confirm_step(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="вернётся")
    asyncio.run(db.archive_task(task_id))

    callback = FakeCallback(f"gtunarchive:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_unarchive(callback))

    task = asyncio.run(db.get_task(task_id))
    assert task["archived_at"] is None

    active_texts = {t["text"] for t in asyncio.run(db.list_active_tasks())}
    assert "вернётся" in active_texts


def test_archive_and_unarchive_trigger_resync(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    task_id = _mk_task()
    calls = []
    monkeypatch.setattr(admin_mod, "_request_game_resync", lambda: calls.append("go"))

    asyncio.run(admin_mod.game_task_archive_go(FakeCallback(f"gtarchive_go:{task_id}", user_id=ADMIN_ID)))
    assert calls == ["go"]

    asyncio.run(admin_mod.game_task_unarchive(FakeCallback(f"gtunarchive:{task_id}", user_id=ADMIN_ID)))
    assert calls == ["go", "go"]


def test_new_game_archive_callbacks_are_capability_mapped_to_moderate_game():
    for cb_data in (
        "admin_game_archive", "gtarchive:5", "gtarchive_go:5", "gtunarchive:5",
        "gtdelete:5", "gtdelete_go:5",
    ):
        assert required_capability(callback_data=cb_data) == "moderate_game"


# ── Plan 03 Task 2: two-step confirm gates for archive/delete ───────────────────────────────

def test_gtarchive_confirm_does_not_touch_db_and_names_consequences(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="Собрать команду")

    callback = FakeCallback(f"gtarchive:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_archive_confirm(callback))

    task = asyncio.run(db.get_task(task_id))
    assert task["archived_at"] is None  # confirm screen -- no DB write yet

    text = callback.message.text
    assert "Собрать команду" in text
    assert "сдачи и начисленные монеты сохранятся" in text
    data = _flat_callback_data(callback.message.markup)
    assert f"gtarchive_go:{task_id}" in data
    assert "admin_game_tasks" in data  # cancel button


def test_gtdelete_confirm_does_not_touch_db_and_says_bezvozvratno(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="Удалить меня")

    callback = FakeCallback(f"gtdelete:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_delete_confirm(callback))

    assert asyncio.run(db.get_task(task_id)) is not None  # still there -- no DB write yet
    text = callback.message.text
    assert "Удалить меня" in text
    assert "безвозвратно" in text
    data = _flat_callback_data(callback.message.markup)
    assert f"gtdelete_go:{task_id}" in data
    assert "admin_game_tasks" in data  # cancel button


def test_gtdelete_go_deletes_task_without_submissions(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="без сдач")

    callback = FakeCallback(f"gtdelete_go:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_delete_go(callback))

    assert asyncio.run(db.get_task(task_id)) is None
    text, show_alert = callback.answers[-1]
    assert text == "Задание удалено"


def test_gtdelete_go_refuses_when_submission_appeared_after_confirm_shown(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="гонка")
    # A submission lands AFTER the (hypothetical) confirm screen was shown, BEFORE the tap.
    _reject_submission(task_id, DELEGATE_ID)

    callback = FakeCallback(f"gtdelete_go:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_delete_go(callback))

    assert asyncio.run(db.get_task(task_id)) is not None  # NOT deleted
    text, show_alert = callback.answers[-1]
    assert show_alert is True
    assert "теперь можно только в архив" in text


def test_gtdelete_confirm_refuses_and_alerts_when_submission_appeared(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="гонка на подтверждении")
    _reject_submission(task_id, DELEGATE_ID)

    callback = FakeCallback(f"gtdelete:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_delete_confirm(callback))

    assert asyncio.run(db.get_task(task_id)) is not None
    text, show_alert = callback.answers[-1]
    assert show_alert is True
    assert "теперь можно только в архив" in text


def test_gtdelete_go_triggers_resync_and_rerenders_tasks_screen(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    task_id = _mk_task(text="уйдёт совсем")
    calls = []
    monkeypatch.setattr(admin_mod, "_request_game_resync", lambda: calls.append("go"))

    callback = FakeCallback(f"gtdelete_go:{task_id}", user_id=ADMIN_ID)
    asyncio.run(admin_mod.game_task_delete_go(callback))

    assert calls == ["go"]
    assert callback.message.edit_calls == 1
    assert "уйдёт совсем" not in callback.message.text


def test_gtarchive_cancel_button_returns_to_tasks_screen_without_db_change():
    # Cancel just points at the already-registered admin_game_tasks callback_data -- a
    # structural check, not a new handler (matches rebuild_sheet_confirm's own cancel button
    # pointing at admin_menu).
    assert required_capability(callback_data="admin_game_tasks") == "moderate_game"


# ── Plan 03 Task 3: archive markers (card/matrix/history) + «попытка K из N» ────────────────

def _card_row(**overrides):
    row = {
        "task_text": "Задание", "task_category": "Light", "task_coins": 20,
        "user_full_name": "Тест", "user_username": None,
        "task_proof_type": "photo", "content_type": "photo", "content": "x",
        "submitted_at": None, "task_deadline_at": None, "task_archived_at": None,
    }
    row.update(overrides)
    return row


def test_render_submission_card_marks_archived_task():
    archived_text = admin_mod._render_submission_card(_card_row(task_archived_at="2026-08-18 10:00:00"), 1, 1)
    assert "🗄 Задание в архиве" in archived_text

    active_text = admin_mod._render_submission_card(_card_row(), 1, 1)
    assert "🗄 Задание в архиве" not in active_text


def test_render_submission_card_attempt_line_only_when_passed():
    with_attempt = admin_mod._render_submission_card(_card_row(), 1, 1, attempt=(3, 3))
    assert "🔁 Попытка 3 из 3" in with_attempt

    without_attempt = admin_mod._render_submission_card(_card_row(), 1, 1)
    assert "Попытка" not in without_attempt


def test_archived_task_pending_submission_stays_in_review_queue(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="архивное, но не решено")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "готово", "2026-08-20 10:00:00"))
    asyncio.run(db.archive_task(task_id))

    pending = asyncio.run(db.get_pending_submissions(limit=50, offset=0))
    assert len(pending) == 1
    assert pending[0]["task_archived_at"] is not None


def test_build_game_matrix_prefixes_archived_task_header_before_truncation():
    long_text = "А" * 60
    tasks = [
        {"id": 1, "text": "Активное", "created_at": "2026-08-01 10:00:00", "archived_at": None},
        {"id": 2, "text": long_text, "created_at": "2026-08-02 10:00:00", "archived_at": "2026-08-10 10:00:00"},
    ]
    headers, _rows = admin_mod._build_game_matrix(tasks, [])
    assert headers[4] == "Активное"
    assert headers[5] == ("🗄 " + long_text)[:40]
    assert headers[5].startswith("🗄 ")


def test_build_game_history_prefixes_archived_submission_task_label():
    submissions = [
        {
            "id": 1, "task_id": 1, "user_id": DELEGATE_ID, "content_type": "text",
            "submitted_at": "2026-08-10 10:00:00", "status": "pending",
            "reviewed_by": None, "reviewed_at": None, "coins_awarded": None, "reject_reason": None,
            "task_text": "Задание", "task_category": "Light",
            "user_full_name": "Тест", "user_username": "t", "user_event_city": None,
            "task_archived_at": "2026-08-15 10:00:00",
        },
    ]
    _headers, rows = admin_mod._build_game_history(submissions)
    assert rows[0][1] == "🗄 Задание"


def test_rebuild_game_sheets_drops_deleted_task_keeps_archived_marked(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    kept_id = _mk_task(text="архивное осталось")
    _reject_submission(kept_id, DELEGATE_ID)
    asyncio.run(db.archive_task(kept_id))

    deleted_id = _mk_task(text="удалённое пропало")
    # delete_task requires zero submissions -- delete right after creation, before rebuild.
    assert asyncio.run(db.delete_task(deleted_id)) is True

    written = {}

    async def _fake_sync(title, headers, rows):
        written[title] = (headers, rows)
        return len(rows)

    monkeypatch.setattr(admin_mod, "sync_named_worksheet", _fake_sync)

    asyncio.run(admin_mod.rebuild_game_sheets())

    matrix_tab = asyncio.run(settings_schema.get_setting_typed("game_matrix_tab"))
    history_tab = asyncio.run(settings_schema.get_setting_typed("game_history_tab"))
    matrix_headers, _matrix_rows = written[matrix_tab]
    _history_headers, history_rows = written[history_tab]

    assert not any("удалённое" in h for h in matrix_headers)
    assert any(h.startswith("🗄 ") and "архивное" in h for h in matrix_headers)
    assert all("удалённое" not in row[1] for row in history_rows)
    assert any(row[1] == "🗄 архивное осталось" for row in history_rows)


def test_attempt_resolved_in_show_current_submission_with_limit(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="лимитное")
    asyncio.run(db.set_setting("game_resubmit_limit", "3"))
    _reject_submission(task_id, DELEGATE_ID)
    _reject_submission(task_id, DELEGATE_ID)
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "финальная попытка", "2026-08-20 10:00:00"))

    state = _new_state(uid=ADMIN_ID)
    message = FakeMessage(user_id=ADMIN_ID)
    asyncio.run(admin_mod._show_current_submission(message, state))

    card_text = message.answers[0][0]
    assert "🔁 Попытка 3 из 3" in card_text


def test_attempt_line_absent_when_limit_is_zero(tmp_path):
    _db_ready(tmp_path)
    task_id = _mk_task(text="без лимита")
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "готово", "2026-08-20 10:00:00"))

    state = _new_state(uid=ADMIN_ID)
    message = FakeMessage(user_id=ADMIN_ID)
    asyncio.run(admin_mod._show_current_submission(message, state))

    card_text = message.answers[0][0]
    assert "Попытка" not in card_text
