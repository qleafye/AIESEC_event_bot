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


_NARROW_UPDATE_ONLY = (
    "auto_reject_rule_ids", "auto_rejected_at", "flagged_rule_ids", "auto_rule_note",
    "rejected_at", "lang",
)


async def _seed_user(uid, status="pending", **overrides):
    """`add_user`'s big INSERT only accepts a fixed column list — колонки фазы 31 (и `lang`)
    в него не входят вовсе; для них — узкий `update_user_answers` ПОСЛЕ `add_user`, тот же
    приём, что использует сам `services.reg_finalize`."""
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
    narrow = {k: overrides.pop(k) for k in list(overrides) if k in _NARROW_UPDATE_ONLY}
    row.update(overrides)
    await db.add_user(row)
    await db.set_user_status(uid, status)
    if narrow:
        await db.update_user_answers(uid, narrow, allowed_columns=list(narrow.keys()))
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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: эффекты автоотказа — сообщение делегату, журнал решений, сводка, лист
# ══════════════════════════════════════════════════════════════════════════════════════════

async def _seed_live_journal_entry(uid, rule_ids, reject_texts, triggered_at):
    return await db.upsert_auto_reject_log(
        uid, json.dumps(rule_ids, ensure_ascii=False),
        json.dumps(reject_texts, ensure_ascii=False), triggered_at,
    )


def test_delegate_message_composition_ru(tmp_path, monkeypatch):
    """D-21: сообщение делегату = префикс reject_text плюс склейка текстов ВСЕХ сработавших
    правил через пустую строку."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("reject_text", "К сожалению, твоя заявка отклонена.")
        await _seed_user(
            UID, status="rejected",
            auto_reject_rule_ids=json.dumps([1, 2]), auto_rejected_at="2026-09-20 10:00:00",
            auto_rule_note="🤖 Автоотказ 20.09 (правило: Курс закрыт)",
        )
        await _seed_live_journal_entry(
            UID, [1, 2], ["Курс закрыт.", "Резюме обязательно."], "2026-09-20 10:00:00",
        )
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        return bot

    bot = asyncio.run(go())
    assert len(bot.sent_messages) == 1
    chat_id, text = bot.sent_messages[0]
    assert chat_id == UID
    assert "К сожалению, твоя заявка отклонена." in text
    assert "Курс закрыт." in text
    assert "Резюме обязательно." in text


def test_delegate_message_composition_en_translated_when_available(tmp_path, monkeypatch):
    """D-25: перевод есть -> английский; перевода нет -> русский текст (никогда пустота)."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        from services.i18n import src_hash

        await db.set_setting("delegate_lang_enabled", "on")
        await db.upsert_translation(
            "en", src_hash("Курс закрыт."), "Курс закрыт.", "The course is closed.",
        )
        await _seed_user(
            UID, status="rejected", lang="en",
            auto_reject_rule_ids=json.dumps([1, 2]), auto_rejected_at="2026-09-20 10:00:00",
            auto_rule_note="🤖 Автоотказ 20.09 (правило: Курс закрыт)",
        )
        await _seed_live_journal_entry(
            UID, [1, 2], ["Курс закрыт.", "Резюме обязательно."], "2026-09-20 10:00:00",
        )
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        return bot

    bot = asyncio.run(go())
    _chat_id, text = bot.sent_messages[0]
    assert "The course is closed." in text
    # Второй текст перевода не имеет — русский, не пустота.
    assert "Резюме обязательно." in text
    assert "Курс закрыт." not in text  # русский оригинал ПЕРВОГО текста заменён переводом


def test_retry_of_post_finalize_does_not_send_second_message(tmp_path, monkeypatch):
    """T-31-06-03: повторный вызов хвоста финала (ретрай очереди Mini App) не шлёт делегату
    второе сообщение об отказе и не пишет вторую строку application_decisions."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await _seed_user(
            UID, status="rejected",
            auto_reject_rule_ids=json.dumps([1]), auto_rejected_at="2026-09-20 10:00:00",
            auto_rule_note="🤖 Автоотказ",
        )
        await _seed_live_journal_entry(UID, [1], ["Курс закрыт."], "2026-09-20 10:00:00")
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        await rf.post_finalize(bot, UID, "new")  # ретрай той же волны
        decisions_count = await _count_decisions(UID)
        return bot, decisions_count

    bot, decisions_count = asyncio.run(go())
    assert len(bot.sent_messages) == 1
    assert decisions_count == 1


async def _count_decisions(uid) -> int:
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM application_decisions WHERE telegram_id = ?", (uid,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


def test_application_decisions_row_has_auto_sentinel_and_effects_sent(tmp_path, monkeypatch):
    """T-31-06-05: каждый автоотказ пишет строку application_decisions с сентинелом
    AUTO_DECIDED_BY, effects_sent_at непустым (без него один из двух сметателей отправит
    делегату отказ второй раз)."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await _seed_user(
            UID, status="rejected",
            auto_reject_rule_ids=json.dumps([1]), auto_rejected_at="2026-09-20 10:00:00",
            auto_rule_note="🤖 Автоотказ",
        )
        await _seed_live_journal_entry(UID, [1], ["Курс закрыт."], "2026-09-20 10:00:00")
        await rf.post_finalize(FakeBot(), UID, "new")
        return await db.get_last_application_decision(UID)

    decision = asyncio.run(go())
    from services.reject_journal import AUTO_DECIDED_BY
    assert decision is not None
    assert decision["decision"] == "rejected"
    assert decision["decided_by"] == AUTO_DECIDED_BY
    assert decision["effects_sent_at"]
    assert decision["undone_at"] is None


def test_auto_reject_admin_notification_sent_when_admins_configured(tmp_path, monkeypatch):
    """D-17: менеджер видит короткое уведомление об автоотказе (режим «каждую отдельно»)."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [777])

    async def go():
        from handlers import admin_caps
        calls = []

        async def fake_notify(bot, cap, text, **kwargs):
            calls.append((cap, text, kwargs.get("city")))

        monkeypatch.setattr(admin_caps, "notify_by_capability", fake_notify)
        await db.set_setting("pending_notify_mode", "instant")

        await _seed_user(
            UID, status="rejected",
            auto_reject_rule_ids=json.dumps([1]), auto_rejected_at="2026-09-20 10:00:00",
            auto_rule_note="🤖 Автоотказ",
        )
        await _seed_live_journal_entry(UID, [1], ["Курс закрыт совсем."], "2026-09-20 10:00:00")
        await rf.post_finalize(FakeBot(), UID, "new")
        return calls

    calls = asyncio.run(go())
    assert len(calls) == 1
    cap, text, _city = calls[0]
    assert cap == "moderate_reg"
    assert "🤖" in text
    assert "Автоотказ" in text
    assert "Курс закрыт совсем." in text


def test_auto_reject_goes_to_pending_summary_when_applications_are_batched(tmp_path, monkeypatch):
    """Владелец 23.09: заявки приходят сводкой раз в N -> автоотказ отдельно не шлётся, его
    имя попадает в ту же сводку ожидания (services/reminders.py)."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [777])

    async def go():
        from handlers import admin_caps
        calls = []

        async def fake_notify(bot, cap, text, **kwargs):
            calls.append(text)

        monkeypatch.setattr(admin_caps, "notify_by_capability", fake_notify)
        await db.set_setting("pending_notify_mode", "batched")
        await _seed_user(
            UID, status="rejected",
            auto_reject_rule_ids=json.dumps([1]), auto_rejected_at="2026-09-20 10:00:00",
            auto_rule_note="🤖 Автоотказ",
        )
        await _seed_live_journal_entry(UID, [1], ["Курс закрыт совсем."], "2026-09-20 10:00:00")
        await rf.post_finalize(FakeBot(), UID, "new")
        return calls

    assert asyncio.run(go()) == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: правка анкеты снимает правило, и гарантии неприкосновенности очереди
# ══════════════════════════════════════════════════════════════════════════════════════════

async def _count_coins(user_id) -> int:
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM coins WHERE user_id = ?", (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


def test_auto_rejected_referral_earns_nothing(tmp_path):
    """31-CONTEXT.md «Claude's Discretion»: авто-начислений за рефералов сегодня нет вовсе —
    тест фиксирует это инвариантом, чтобы автоотказ не стал лазейкой при будущей геймификации."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await _enable_reject_rules()
        await _seed_course_rule()
        referrer_id = 910800900
        draft = {
            "telegram_id": UID, "kind": "new",
            "answers": {"course": "1", "referrer_id": referrer_id},
        }
        result = await rf.finalize_data(UID, "@x", draft)
        balance = await db.get_balance(referrer_id)
        coins_rows = await _count_coins(referrer_id)
        return result, balance, coins_rows

    result, balance, coins_rows = asyncio.run(go())
    assert result["status"] == "rejected"
    assert balance == 0
    assert coins_rows == 0


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью-находка (дефект 1): правка, снявшая отказ, но НЕ снявшая пометку — заявка обязана уйти
# на ручную модерацию, а не остаться зависшей в "rejected".
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_edit_clears_reject_keeps_flag_goes_to_pending_with_both_badges(tmp_path):
    """До фикса: правка снимает правило-отказ, но правило-пометка ещё срабатывает — код уходил
    в ветку «Пометка» (auto_patch truthy) и НИКОГДА не доходил до `elif was_auto_rejected`,
    поэтому статус навсегда оставался "rejected". После фикса — pending, обнулённые колонки
    автоотказа, СВЕЖИЙ бейдж пометки и отдельный маркер истории «сменил ответ после
    автоотказа»."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await db.set_setting("reg_q_resume", "on")
        await _enable_reject_rules()
        reject_id = await _seed_course_rule(action="reject", reject_text="Курс закрыт.", values=("1",))
        flag_id = await db.create_reject_rule(
            name="Пометка нет резюме", city=None, tracks=json.dumps(["full"]),
            conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
            action="flag", reject_text=None, enabled=1, created_by=1,
        )

        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "1"}}
        result_new = await rf.finalize_data(UID, "@x", draft)
        assert result_new["status"] == "rejected"

        # Курс больше не попадает под правило-отказ — резюме по-прежнему не приложено, поэтому
        # правило-пометка остаётся в силе.
        edit_draft = {
            "telegram_id": UID, "kind": "edit",
            "answers": {"course": "3"}, "updated_by": "miniapp",
        }
        result_edit = await rf.finalize_data(UID, "@x", edit_draft)
        user = await db.get_user(UID)
        history = await db.get_answer_history(UID, limit=5)
        return result_edit, user, history, reject_id, flag_id

    result_edit, user, history, reject_id, flag_id = asyncio.run(go())

    assert result_edit["status"] == "pending"
    assert result_edit["flagged_rule_ids"] == [flag_id]
    assert user["status"] == "pending"
    assert user.get("auto_reject_rule_ids") in (None, "null")
    assert user["auto_rejected_at"] is None
    assert json.loads(user["flagged_rule_ids"]) == [flag_id]
    assert "⚠️" in user["auto_rule_note"]

    cleared_rows = [
        h for h in history
        if any(c.get("auto_reject_cleared") for c in (h.get("changes") or []))
    ]
    assert len(cleared_rows) == 1
    marker = next(c for c in cleared_rows[0]["changes"] if c.get("auto_reject_cleared"))
    assert marker["column"] == "status" and marker["old"] == "rejected" and marker["new"] == "pending"
    assert marker["rule_field"] == "course"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью-находка (дефект 2): взаимодействие с автоодобрением события
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reject_rule_new_application_auto_approval_rejects_without_approve_effects(tmp_path, monkeypatch):
    """(a) Правило-отказ + автоодобрение события ВКЛЮЧЕНО — итог всё равно "rejected", и
    приветственный скрипт автоприёма (`handlers.registration.approve_user`) НЕ зовётся вовсе —
    делегат получает только сообщение об автоотказе."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    approve_calls = []

    async def go():
        async def fake_approve(bot, telegram_id, **kwargs):
            approve_calls.append((telegram_id, kwargs))

        monkeypatch.setattr(reg_mod, "approve_user", fake_approve)

        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "auto")
        await db.set_setting("reject_text", "К сожалению, твоя заявка отклонена.")
        await _enable_reject_rules()
        await _seed_course_rule()

        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "1"}}
        result = await rf.finalize_data(UID, "@x", draft)
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        user = await db.get_user(UID)
        return result, user, bot, approve_calls

    result, user, bot, approve_calls = asyncio.run(go())
    assert result["status"] == "rejected"
    assert user["status"] == "rejected"
    assert approve_calls == []
    assert len(bot.sent_messages) == 1
    assert "отклонена" in bot.sent_messages[0][1]


def test_flag_rule_new_application_auto_approval_stays_pending_no_approve_effects(tmp_path, monkeypatch):
    """(b-1) Правило-пометка (не отказ) + автоодобрение ВКЛЮЧЕНО — заявка обязана уйти на
    ручную модерацию с бейджем, а не проскочить в "approved" молча (D-03: пометка — пробный
    режим перед включением отказа, решает человек, тот же принцип, что у D-23)."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    approve_calls = []

    async def go():
        async def fake_approve(bot, telegram_id, **kwargs):
            approve_calls.append((telegram_id, kwargs))

        monkeypatch.setattr(reg_mod, "approve_user", fake_approve)

        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "auto")
        await db.set_setting("reg_q_resume", "on")
        await _enable_reject_rules()
        flag_id = await db.create_reject_rule(
            name="Пометка нет резюме", city=None, tracks=json.dumps(["full"]),
            conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
            action="flag", reject_text=None, enabled=1, created_by=1,
        )

        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "3"}}
        result = await rf.finalize_data(UID, "@x", draft)
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        user = await db.get_user(UID)
        return result, user, bot, approve_calls, flag_id

    result, user, bot, approve_calls, flag_id = asyncio.run(go())
    assert result["status"] == "pending"
    assert user["status"] == "pending"
    assert json.loads(user["flagged_rule_ids"]) == [flag_id]
    assert "⚠️" in user["auto_rule_note"]
    assert approve_calls == []
    # Ни автоприёма, ни автоотказа — делегат вообще ничего не получает на этом шаге, решение
    # ещё не принято.
    assert bot.sent_messages == []


def test_no_rule_match_new_application_auto_approval_still_approves(tmp_path, monkeypatch):
    """(b-2) Регресс-гвард: правила модуля включены, но НИ ОДНО не подошло — автоодобрение
    работает как раньше, заявка получает "approved" и приветственный текст."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "auto")
        await _enable_reject_rules()
        await _seed_course_rule(values=("5",))  # правило скоупом на курс "5" — не подходит

        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "3"}}
        result = await rf.finalize_data(UID, "@x", draft)
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        return result, bot

    result, bot = asyncio.run(go())
    assert result["status"] == "approved"
    assert len(bot.sent_messages) == 1


def test_reject_rules_module_off_new_application_auto_approval_still_approves(tmp_path, monkeypatch):
    """(b-3) Регресс-гвард: общий рубильник `reject_rules_enabled` выключен (дефолт) —
    автоодобрение работает byte-в-byte как до фазы 31."""
    _ready(tmp_path)
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "auto")
        # reject_rules_enabled НЕ включён — дефолт off.
        await _seed_course_rule()

        draft = {"telegram_id": UID, "kind": "new", "answers": {"course": "1"}}
        result = await rf.finalize_data(UID, "@x", draft)
        bot = FakeBot()
        await rf.post_finalize(bot, UID, "new")
        return result, bot

    result, bot = asyncio.run(go())
    assert result["status"] == "approved"
    assert len(bot.sent_messages) == 1


# ── (c) EDIT-ветка: никогда не отклонённый approved-делегат задевает правило-пометку ────────

def test_edit_flag_rule_matches_approved_delegate_toggle_off_keeps_approved(tmp_path):
    """(c) Делегат НИКОГДА не был автоотклонён, статус "approved"; правка задевает поле, из-за
    которого срабатывает правило-пометка. Тумблер «Изменённая анкета — снова на модерацию»
    ВЫКЛЮЧЕН — статус остаётся существующим переходом (approved), правило добавляет только
    бейдж, ничего не падает."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reject_rules_enabled", "on")
        await db.set_setting("toggle_reg_edit_remoderation", "off")
        flag_id = await _seed_course_rule(action="flag", reject_text=None, values=("1", "2"))
        await _seed_user(UID, status="approved", course="3")

        edit_draft = {
            "telegram_id": UID, "kind": "edit",
            "answers": {"course": "1"}, "updated_by": "miniapp",
        }
        result = await rf.finalize_data(UID, "@x", edit_draft)
        user = await db.get_user(UID)
        return result, user, flag_id

    result, user, flag_id = asyncio.run(go())
    assert result["status"] == "approved"
    assert result["remoderated"] is False
    assert user["status"] == "approved"
    assert json.loads(user["flagged_rule_ids"]) == [flag_id]
    assert "⚠️" in user["auto_rule_note"]


def test_edit_flag_rule_matches_approved_delegate_toggle_on_sets_pending(tmp_path):
    """(c) Тот же сценарий, тумблер ВКЛЮЧЁН — статус уходит на pending СУЩЕСТВУЮЩЕЙ веткой
    ремодерации (не новым переходом ради правила), правило по-прежнему только добавляет
    бейдж."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reject_rules_enabled", "on")
        await db.set_setting("toggle_reg_edit_remoderation", "on")
        flag_id = await _seed_course_rule(action="flag", reject_text=None, values=("1", "2"))
        await _seed_user(UID, status="approved", course="3")

        edit_draft = {
            "telegram_id": UID, "kind": "edit",
            "answers": {"course": "1"}, "updated_by": "miniapp",
        }
        result = await rf.finalize_data(UID, "@x", edit_draft)
        user = await db.get_user(UID)
        return result, user, flag_id

    result, user, flag_id = asyncio.run(go())
    assert result["status"] == "pending"
    assert result["remoderated"] is True
    assert user["status"] == "pending"
    assert json.loads(user["flagged_rule_ids"]) == [flag_id]


def test_no_batch_sweep_functions_exist():
    """Threat register T-31-06-01: пакетного прохода по очереди в проекте нет и не появится —
    структурная проверка отсутствия таких функций (то же, что grep-акцептанс плана)."""
    import re

    for path in ("services/reject_rules.py", "services/reject_journal.py", "services/reg_finalize.py"):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert not re.search(r"def .*(apply_rules_to_queue|apply_to_pending|sweep)", src), path
