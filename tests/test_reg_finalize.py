"""Phase 21 Plan 08 (FORM-SYNC-02/04): контракт общего финала — `services.reg_finalize.
finalize_data`/`post_finalize` — единого для бота (`handlers/registration.py::
finalize_registration`) и джобы очереди Mini App (`services/miniapp_outbox.py`, kind
`reg_finalized`/`reg_edited`).

pytest-asyncio недоступен — async через asyncio.run(), фикстура временной БД — тот же приём,
что `tests/test_reg_drafts.py::_ready(tmp_path)`. Внешние эффекты (Sheets/Nextcloud/Telegram)
мокаются monkeypatch'ем на модулях, где они реально импортированы и вызываются
(`services.reg_finalize.post_finalize` делает ЛОКАЛЬНЫЕ импорты внутри функции — они
резолвятся заново при каждом вызове, поэтому монкипатч исходного модуля срабатывает).
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg_mod
import reg_engine
from services import reg_finalize as rf
from services import miniapp_outbox
from services import sheets as sheets_service
from miniapp import outbox as mo

UID = 900800100


def _ready(tmp_path, name="reg_finalize.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _seed_user(uid, status="approved", **overrides):
    row = {
        "telegram_id": uid,
        "full_name": "Иван Иванов",
        "username": "@ivan",
        "phone": "+79990000000",
        "university": "СПбГУ",
        "referrer_id": 42,
        "registration_date": "2026-08-01 10:00:00",
        "source": "friend",
        "event_city": None,
        "participant_type": "full",
        "season": "YL'26",
    }
    row.update(overrides)
    await db.add_user(row)
    await db.set_user_status(uid, status)
    return row


def _offline(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")


class FakeBot:
    def __init__(self):
        self.sent_documents = []
        self.sent_messages = []

    async def send_document(self, chat_id, file_id, caption=None):
        self.sent_documents.append((chat_id, file_id, caption))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text))


def _patch_sheet_calls(monkeypatch):
    """Ловит и append (новая заявка), и update_row_by_id (правка) в один общий журнал."""
    calls = []

    async def fake_append(row):
        calls.append(("append", None, list(row)))

    async def fake_append_named(tab, row):
        calls.append(("append", tab, list(row)))

    async def fake_update_row_by_id(tab_name, telegram_id, row):
        calls.append(("update_row_by_id", tab_name, list(row)))
        return True

    monkeypatch.setattr(reg_mod, "append_to_sheet", fake_append)
    monkeypatch.setattr(reg_mod, "append_to_named_sheet", fake_append_named)
    monkeypatch.setattr(sheets_service, "update_row_by_id", fake_update_row_by_id)
    return calls


def _patch_notify(monkeypatch):
    calls = []

    async def fake_notify(bot, capability, text, **kwargs):
        calls.append((capability, text, kwargs.get("city")))

    monkeypatch.setattr(reg_mod, "notify_by_capability", fake_notify)
    return calls


# ── finalize_data: режим new ────────────────────────────────────────────────────────────

def test_finalize_data_new_creates_user_and_status(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        draft = {"telegram_id": UID, "kind": "new", "answers": {"full_name": "Пётр Петров"}}
        result = await rf.finalize_data(UID, "@petr", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = asyncio.run(go())
    assert result["mode"] == "new"
    assert result["status"] == "pending"
    assert user is not None and user["full_name"] == "Пётр Петров"
    assert user["status"] == "pending"


def test_finalize_data_new_clears_draft_and_reg_started(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("full_approval", "auto")
        await db.mark_reg_started(UID, "tester", participant_type="full")
        draft = {"telegram_id": UID, "kind": "new", "answers": {"full_name": "Аня"}}
        await rf.finalize_data(UID, "@a", draft)
        return await db.get_reg_started_track(UID)

    track = asyncio.run(go())
    assert track is None


# ── finalize_data: режим edit ───────────────────────────────────────────────────────────

def test_finalize_data_edit_preserves_attribution_and_writes_changed_columns(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user(UID, status="approved")
        draft = {
            "telegram_id": UID, "kind": "edit",
            "answers": {"phone": "+79991112233"}, "updated_by": "miniapp",
        }
        # Username передан ДРУГИМ (человек мог сменить @username в Telegram между анкетой и
        # правкой) — allowlist (reg_engine.answer_columns()) не содержит "username", узкий
        # UPDATE обязан оставить прежнее значение нетронутым (T-21-19).
        result = await rf.finalize_data(UID, "@changed_handle", draft)
        user = await db.get_user(UID)
        history = await db.get_answer_history(UID, limit=5)
        return result, user, history

    result, user, history = asyncio.run(go())
    assert result["mode"] == "edit"
    assert result["changed_columns"] == ["phone"]
    assert user["phone"] == "+79991112233"
    # Атрибуция/статус НЕ тронуты правкой.
    assert user["registration_date"] == "2026-08-01 10:00:00"
    assert user["referrer_id"] == 42
    assert user["username"] == "@ivan"  # НЕ "@changed_handle" — allowlist исключает username
    assert user["source"] == "friend"
    assert user["status"] == "approved"
    assert user.get("payment_status") in (None, "not_paid")
    assert len(history) == 1
    assert history[0]["changes"] == [{"column": "phone", "old": "+79990000000", "new": "+79991112233"}]
    assert user["edited_at"]
    assert user["edited_source"] == "miniapp"


def test_finalize_data_edit_empty_diff_writes_nothing(tmp_path):
    _ready(tmp_path)

    async def go():
        seeded = await _seed_user(UID, status="approved")
        draft = {
            "telegram_id": UID, "kind": "edit",
            "answers": {"phone": seeded["phone"]}, "updated_by": "miniapp",
        }
        result = await rf.finalize_data(UID, "@ivan", draft)
        user = await db.get_user(UID)
        history = await db.get_answer_history(UID, limit=5)
        return result, user, history

    result, user, history = asyncio.run(go())
    assert result["changed_columns"] == []
    assert history == []
    assert user.get("edited_at") is None


def test_finalize_data_edit_remoderation_toggle_on_sets_pending(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user(UID, status="approved")
        await db.set_setting("toggle_reg_edit_remoderation", "on")
        draft = {"telegram_id": UID, "kind": "edit", "answers": {"phone": "+79995556677"}, "updated_by": "bot"}
        result = await rf.finalize_data(UID, "@ivan", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = asyncio.run(go())
    assert result["remoderated"] is True
    assert result["resubmitted"] is False
    assert result["status"] == "pending"
    assert user["status"] == "pending"


def test_finalize_data_edit_remoderation_toggle_off_keeps_status(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user(UID, status="approved")
        await db.set_setting("toggle_reg_edit_remoderation", "off")
        draft = {"telegram_id": UID, "kind": "edit", "answers": {"phone": "+79995556677"}, "updated_by": "bot"}
        result = await rf.finalize_data(UID, "@ivan", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = asyncio.run(go())
    assert result["remoderated"] is False
    assert result["status"] == "approved"
    assert user["status"] == "approved"


def test_finalize_data_edit_resubmit_rejected_sets_pending_and_marks_history(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user(UID, status="rejected")
        draft = {"telegram_id": UID, "kind": "edit", "answers": {"phone": "+79997778899"}, "updated_by": "miniapp"}
        result = await rf.finalize_data(UID, "@ivan", draft)
        user = await db.get_user(UID)
        history = await db.get_answer_history(UID, limit=5)
        return result, user, history

    result, user, history = asyncio.run(go())
    assert result["resubmitted"] is True
    assert result["status"] == "pending"
    assert user["status"] == "pending"
    markers = [h for h in history if h["changes"] == [{"column": "status", "old": "rejected", "new": "pending"}]]
    assert len(markers) == 1


# ── двойной финал (T-21-02) ──────────────────────────────────────────────────────────────

def test_double_finalize_second_claim_returns_none_and_never_writes(tmp_path, monkeypatch):
    _ready(tmp_path)
    calls = _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("full_approval", "manual")
        await db.upsert_reg_draft(
            UID, kind="new", step="confirm", patch={"full_name": "Клэймд"}, source="bot",
        )
        first = await db.claim_reg_draft(UID)
        second = await db.claim_reg_draft(UID)
        assert second is None
        result = await rf.finalize_data(UID, "@claimed", first)
        return result

    result = asyncio.run(go())
    assert result["mode"] == "new"
    user = asyncio.run(db.get_user(UID))
    assert user is not None
    # Только ОДНА запись — второй вызывающий не получил черновик, значит и не писал.
    assert asyncio.run(db.get_user(UID))["full_name"] == "Клэймд"


# ── сбой внутри finalize_data освобождает claim (T-21-24) ───────────────────────────────

def test_finalize_data_failure_releases_draft_for_retry(tmp_path, monkeypatch):
    _ready(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(reg_engine, "decide_status", boom)

    async def go():
        await db.set_setting("full_approval", "manual")
        await db.upsert_reg_draft(UID, kind="new", step="confirm", patch={"full_name": "X"}, source="bot")
        draft = await db.claim_reg_draft(UID)
        try:
            await rf.finalize_data(UID, "@x", draft)
        except RuntimeError:
            pass
        else:
            raise AssertionError("finalize_data must propagate the exception")
        return await db.claim_reg_draft(UID)  # claimable again -> release worked

    reclaimed = asyncio.run(go())
    assert reclaimed is not None


# ── post_finalize: new -> append, edit -> update_row_by_id, fallback ────────────────────

def test_post_finalize_new_mode_appends_row(tmp_path, monkeypatch):
    _ready(tmp_path)
    _offline(monkeypatch)
    calls = _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await _seed_user(UID, status="approved", event_city=None)
        await rf.post_finalize(FakeBot(), UID, "new")

    asyncio.run(go())
    assert [c for c in calls if c[0] == "append"], "новая анкета обязана идти append-путём"
    assert not any(c[0] == "update_row_by_id" for c in calls)


def test_post_finalize_edit_mode_updates_row_by_id(tmp_path, monkeypatch):
    _ready(tmp_path)
    _offline(monkeypatch)
    calls = _patch_sheet_calls(monkeypatch)

    async def go():
        await _seed_user(UID, status="approved")
        await rf.post_finalize(FakeBot(), UID, "edit", changed_columns=["phone"])

    asyncio.run(go())
    assert [c for c in calls if c[0] == "update_row_by_id"], "правка обязана идти через update_row_by_id"
    assert not any(c[0] == "append" for c in calls)


def test_post_finalize_edit_row_not_found_falls_back_to_append(tmp_path, monkeypatch):
    _ready(tmp_path)
    _offline(monkeypatch)
    calls = []

    async def fake_append(row):
        calls.append(("append", None, list(row)))

    async def fake_update_row_by_id(tab_name, telegram_id, row):
        calls.append(("update_row_by_id", tab_name, list(row)))
        return False  # строка не найдена

    monkeypatch.setattr(reg_mod, "append_to_sheet", fake_append)
    monkeypatch.setattr(sheets_service, "update_row_by_id", fake_update_row_by_id)

    async def go():
        await _seed_user(UID, status="approved")
        await rf.post_finalize(FakeBot(), UID, "edit", changed_columns=["phone"])

    asyncio.run(go())  # не должно упасть
    kinds = [c[0] for c in calls]
    assert kinds == ["update_row_by_id", "append"]


def test_post_finalize_edit_empty_diff_never_touches_sheet(tmp_path, monkeypatch):
    _ready(tmp_path)
    _offline(monkeypatch)
    calls = _patch_sheet_calls(monkeypatch)

    async def go():
        await _seed_user(UID, status="approved")
        await rf.post_finalize(FakeBot(), UID, "edit", changed_columns=[])

    asyncio.run(go())
    assert calls == []


# ── бот напрямую vs через очередь — одинаковый журнал (Task 3 acceptance) ────────────────

def test_direct_call_and_drain_produce_the_same_edit_journal(tmp_path, monkeypatch):
    """T-21-02 / Task 3 acceptance: `post_finalize`, вызванный ботом напрямую, и та же джоба,
    разобранная из очереди по kind `reg_edited` (payload несёт только `telegram_id`,
    `derive_edit_facts` дочитывает недостающее из `reg_answer_history`/`users.status`), дают
    один и тот же журнал вызовов Sheets/уведомлений — с точностью до ID-ячейки строки."""
    _ready(tmp_path)
    _offline(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [777])
    sheet_calls = _patch_sheet_calls(monkeypatch)
    notify_calls = _patch_notify(monkeypatch)

    uid_direct, uid_queue = 900800201, 900800202

    async def go():
        results = {}
        for uid in (uid_direct, uid_queue):
            await _seed_user(uid, status="rejected", full_name="Общее Имя", username="@same")
            draft = {
                "telegram_id": uid, "kind": "edit",
                "answers": {"phone": "+79990001122"}, "updated_by": "miniapp",
            }
            results[uid] = await rf.finalize_data(uid, "@same", draft)

        bot = FakeBot()
        # Прямой вызов бота — вызывающий (в проде: finalize_registration) передаёт то, что
        # вернул сам finalize_data.
        r = results[uid_direct]
        await rf.post_finalize(
            bot, uid_direct, r["mode"],
            changed_columns=r["changed_columns"], remoderated=r["remoderated"],
            resubmitted=r["resubmitted"],
        )
        direct_snapshot = list(sheet_calls), list(notify_calls)
        sheet_calls.clear()
        notify_calls.clear()

        # Тот же путь через очередь (Mini App) — payload несёт только telegram_id.
        await mo.enqueue("reg_edited", {"telegram_id": uid_queue})
        drained = await miniapp_outbox.drain(bot)
        queue_snapshot = list(sheet_calls), list(notify_calls)
        return direct_snapshot, queue_snapshot, drained

    (direct_sheet, direct_notify), (queue_sheet, queue_notify), drained = asyncio.run(go())

    assert drained == 1
    assert [c[0] for c in direct_sheet] == [c[0] for c in queue_sheet] == ["update_row_by_id"]
    # Один и тот же формат строки за вычетом ID-ячейки (первая колонка — ID Telegram).
    assert direct_sheet[0][2][1:] == queue_sheet[0][2][1:]
    assert [n[0] for n in direct_notify] == [n[0] for n in queue_notify] == ["moderate_reg"]


# ── неизвестный kind по-прежнему бросает исключение (закрытый набор) ─────────────────────

def test_unknown_kind_still_raises_via_handle_row(tmp_path):
    _ready(tmp_path)

    async def go():
        try:
            await miniapp_outbox._handle_row(FakeBot(), "reg_totally_unknown", {"telegram_id": UID})
        except ValueError:
            return True
        return False

    assert asyncio.run(go()) is True


# ── Quick 260904-3vm (E2): автоприём читает «заявка принята», не «прошёл отбор» ────────────

def test_post_finalize_new_approved_sends_auto_approve_text(tmp_path, monkeypatch):
    """post_finalize решает `mode == "new" and status == "approved"` -> это ВСЕГДА автоприём
    (модерация дала бы status='pending', одобрение менеджером идёт отдельным путём) — делегат
    должен получить DEFAULT_APPROVE_AUTO_TEXT, а не общий DEFAULT_APPROVE_TEXT."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    _patch_notify(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    from handlers import reg_schema

    async def go():
        await _seed_user(UID, status="approved", event_city=None)
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        return bot

    bot = asyncio.run(go())
    assert len(bot.sent_messages) == 1
    _, text = bot.sent_messages[0]
    assert text == reg_schema.DEFAULT_APPROVE_AUTO_TEXT
    assert text != reg_schema.DEFAULT_APPROVE_TEXT


def test_post_finalize_new_approved_respects_approve_text_auto_override(tmp_path, monkeypatch):
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    _patch_notify(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("approve_text__auto", "Готово! Ты в деле ✅")
        await _seed_user(UID, status="approved", event_city=None)
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        return bot

    bot = asyncio.run(go())
    assert bot.sent_messages[0][1] == "Готово! Ты в деле ✅"


def test_manual_approve_still_uses_default_approve_text(tmp_path, monkeypatch):
    """Ручное одобрение менеджером (handlers.reg_schema.approve_user БЕЗ auto_approved) —
    прежний текст «🎉 После одобрения», регресс не допускается."""
    _ready(tmp_path)
    _offline(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    from handlers import reg_schema

    async def go():
        await _seed_user(UID, status="approved")
        bot = FakeBot()
        await reg_schema.approve_user(bot, UID)
        return bot

    bot = asyncio.run(go())
    assert len(bot.sent_messages) == 1
    assert bot.sent_messages[0][1] == reg_schema.DEFAULT_APPROVE_TEXT
