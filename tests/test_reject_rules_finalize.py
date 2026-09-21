"""Phase 31 (31-06, D-01..D-32): интеграционные тесты — правила автоотказа врезаны в общий
финал анкеты (`services.reg_finalize.finalize_data`/`post_finalize`), общий для чата и Mini
App. Модель — `tests/test_reg_finalize.py`: async через `asyncio.run()` (pytest-asyncio
недоступен в этом окружении), временная БД `_ready(tmp_path)`, внешние эффекты (Sheets/
Nextcloud/Telegram) — monkeypatch на модулях, где они РЕАЛЬНО импортируются и вызываются
(`services.reg_finalize` делает локальные импорты внутри функций — они резолвятся заново при
каждом вызове, поэтому монкипатч исходного модуля срабатывает, тот же приём, что в
`test_reg_finalize.py`).
"""
import asyncio
import json

from config import config
from database import db
import reg_engine
from services import reg_finalize as rf
import services.reject_rules as reject_rules_mod
from handlers import registration as reg_mod
from services import sheets as sheets_service

UID = 910800200


def _ready(tmp_path, name="reject_rules_finalize.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _offline(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.sent_documents = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text))

    async def send_document(self, chat_id, file_id, caption=None):
        self.sent_documents.append((chat_id, file_id, caption))


async def _seed_user(uid, status="pending", **overrides):
    row = {
        "telegram_id": uid,
        "full_name": "Иван Иванов",
        "username": "@ivan",
        "phone": "+79990000000",
        "registration_date": "2026-09-01 10:00:00",
        "event_city": None,
        "participant_type": "full",
        "season": "YL'26",
        "course": "1",
    }
    row.update(overrides)
    await db.add_user(row)
    await db.set_user_status(uid, status)
    return row


async def _enable_reject_rules():
    await db.set_setting("reject_rules_enabled", "on")


async def _seed_course_rule(*, city=None, action="reject", reject_text="Мест на курс уже нет.",
                             enabled=1, values=("1", "2"), name=None):
    return await db.create_reject_rule(
        name=name, city=city, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "course", "op": "in", "values": list(values)}]]),
        action=action, reject_text=reject_text, enabled=enabled, created_by=1,
    )


def _patch_sheet_calls(monkeypatch):
    """Ловит и append (новая заявка), и update_row_by_id (правка) в один общий журнал — тот же
    приём, что `tests/test_reg_finalize.py::_patch_sheet_calls`."""
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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: оценка правил в ветке новой заявки (данные)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reject_rule_matches_new_application_gets_rejected(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await _enable_reject_rules()
        rule_id = await _seed_course_rule()
        draft = {
            "telegram_id": UID, "kind": "new",
            "answers": {"full_name": "Пётр Петров", "course": "1"},
        }
        result = await rf.finalize_data(UID, "@petr", draft)
        user = await db.get_user(UID)
        log_rows = await db.list_auto_reject_log(include_returned=True)
        return result, user, log_rows, rule_id

    result, user, log_rows, rule_id = asyncio.run(go())
    assert result["status"] == "rejected"
    assert result["auto_rejected"] is True
    assert user["status"] == "rejected"
    assert json.loads(user["auto_reject_rule_ids"]) == [rule_id]
    assert user["auto_rejected_at"]
    assert user["rejected_at"]
    assert user["auto_rule_note"]
    assert "🤖" in user["auto_rule_note"]
    assert len(log_rows) == 1
    assert log_rows[0]["telegram_id"] == UID
    assert log_rows[0]["attempt_count"] == 1
    assert json.loads(log_rows[0]["rule_ids"]) == [rule_id]


def test_flag_rule_matches_does_not_change_status(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await _enable_reject_rules()
        rule_id = await _seed_course_rule(action="flag", reject_text=None)
        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "1"}}
        result = await rf.finalize_data(UID, "@petr", draft)
        user = await db.get_user(UID)
        return result, user, rule_id

    result, user, rule_id = asyncio.run(go())
    assert result["status"] == "pending"
    assert result["auto_rejected"] is False
    assert result["flagged_rule_ids"] == [rule_id]
    assert user["status"] == "pending"
    assert json.loads(user["flagged_rule_ids"]) == [rule_id]
    assert user.get("auto_reject_rule_ids") in (None, "null")
    assert "⚠️" in user["auto_rule_note"]


def test_reject_wins_over_flag_when_both_fire(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        # reg_q_resume дефолт "off" (D-14: правило по выключенному вопросу встаёт на паузу) —
        # включаем вопрос, иначе правило-пометка молча не сработает.
        await db.set_setting("reg_q_resume", "on")
        await _enable_reject_rules()
        reject_id = await _seed_course_rule(action="reject", reject_text="Курс закрыт.", values=("1",))
        flag_id = await db.create_reject_rule(
            name="Пометка нет резюме", city=None, tracks=json.dumps(["full"]),
            conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
            action="flag", reject_text=None, enabled=1, created_by=1,
        )
        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "1"}}
        result = await rf.finalize_data(UID, "@x", draft)
        user = await db.get_user(UID)
        return result, user, reject_id, flag_id

    result, user, reject_id, flag_id = asyncio.run(go())
    assert result["status"] == "rejected"
    assert json.loads(user["auto_reject_rule_ids"]) == [reject_id]
    assert json.loads(user["flagged_rule_ids"]) == [flag_id]


def test_age_rule_uses_forum_date_not_today(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await db.set_setting("forum_date", "01.11.2026")
        # reg_q_birth_date дефолт "off" (D-14) — включаем, иначе правило на паузе.
        await db.set_setting("reg_q_birth_date", "on")
        await _enable_reject_rules()
        await db.create_reject_rule(
            name="Младше 18", city=None, tracks=json.dumps(["full"]),
            conditions=json.dumps([[{"step": "birth_date", "op": "age_on_forum_lt", "values": [18]}]]),
            action="reject", reject_text="На дату форума тебе нет 18.", enabled=1, created_by=1,
        )
        # 15.11.2008 -> на 01.11.2026 (форум) человеку 17 лет (день рождения ещё не наступил),
        # хотя «сегодня» (дата теста) возраст может быть другим — оценщик обязан считать от
        # forum_date, а не от msk_now().
        draft = {
            "telegram_id": UID, "kind": "new",
            "answers": {"course": "3", "birth_date": "15.11.2008"},
        }
        result = await rf.finalize_data(UID, "@x", draft)
        return result

    result = asyncio.run(go())
    assert result["status"] == "rejected"


def test_missing_birth_date_or_forum_date_takes_normal_path(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await _enable_reject_rules()
        # forum_date НЕ настроен (D-31: нет данных -> условие не выполнено).
        await db.create_reject_rule(
            name="Младше 18", city=None, tracks=json.dumps(["full"]),
            conditions=json.dumps([[{"step": "birth_date", "op": "age_on_forum_lt", "values": [18]}]]),
            action="reject", reject_text="Нет 18.", enabled=1, created_by=1,
        )
        draft = {"telegram_id": UID, "kind": "new", "answers": {"birth_date": "15.11.2008"}}
        result = await rf.finalize_data(UID, "@x", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = asyncio.run(go())
    assert result["status"] == "pending"
    assert user.get("auto_reject_rule_ids") in (None, "null")


def test_reject_rules_disabled_switch_makes_zero_db_calls(tmp_path, monkeypatch):
    _ready(tmp_path)
    calls = []
    real_list = db.list_reject_rules

    async def counting_list(*a, **kw):
        calls.append(1)
        return await real_list(*a, **kw)

    monkeypatch.setattr(reject_rules_mod, "list_reject_rules", counting_list)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        # reject_rules_enabled НЕ включён — дефолт off (D-15).
        await _seed_course_rule()
        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "1"}}
        result = await rf.finalize_data(UID, "@x", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = asyncio.run(go())
    assert result["status"] == "pending"
    assert user["status"] == "pending"
    assert user.get("auto_reject_rule_ids") in (None, "null")
    assert calls == []


def test_evaluator_failure_preserves_application_without_auto_reject(tmp_path, monkeypatch):
    """T-31-06-02: сбой оценщика/загрузчика правил не имеет права потерять заявку —
    `users.status` остаётся ровно тем, что вернул бы `decide_status` без автоотказа."""
    _ready(tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(reject_rules_mod, "active_rules", boom)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await _enable_reject_rules()
        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "1"}}
        result = await rf.finalize_data(UID, "@x", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = asyncio.run(go())
    assert result["status"] == "pending"
    assert user is not None
    assert user["status"] == "pending"
    assert user.get("auto_reject_rule_ids") in (None, "null")


def test_no_active_rules_for_city_takes_normal_path(tmp_path):
    """Правило существует, но для другого города — не подходит по скоупу, заявка обычным
    путём."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await _enable_reject_rules()
        await _seed_course_rule(city="spb")
        draft = {
            "telegram_id": UID, "kind": "new",
            "answers": {"course": "1", "event_city": "msk"},
        }
        result = await rf.finalize_data(UID, "@x", draft)
        user = await db.get_user(UID)
        return result, user

    result, user = asyncio.run(go())
    assert result["status"] == "pending"
    assert user.get("auto_reject_rule_ids") in (None, "null")
