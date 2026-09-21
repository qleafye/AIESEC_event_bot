"""Phase 31 Plan 12 (D-14): реакция на правку настроек анкеты — правило автоотказа, которое
опиралось на выключенный вопрос или пропавший вариант ответа, встаёт на паузу само, а
держателям права «Настройки» уходит одно человеческое сообщение.

Первая часть покрывает сам сервис (`affects_reject_rules`/`on_setting_written`/
`on_settings_written_batch`), вызванный НАПРЯМУЮ, без воронки записи настроек. Вторая часть
покрывает саму воронку (`settings_audit.set_setting_by_admin`/`delete_setting_by_admin`) —
она реально зовёт хук, исключение внутри хука не мешает записи, строка лога с автором не
меняется.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной БД
— тот же приём, что `tests/test_reject_rules_service.py::_ready`.
"""
from __future__ import annotations

import asyncio
import json
import logging

from config import config
from database import db
import reg_presets
import services.reject_rules_notify as rrn
import settings_audit

SUPERADMIN_ID = 900200001


def _ready(tmp_path, name="test_reject_rules_pause_notify.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [SUPERADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


async def _create_course_rule(**overrides):
    fields = dict(
        name=None, city=None, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1", "2"]}]]),
        action="reject", reject_text="текст отказа", enabled=1, created_by=None,
    )
    fields.update(overrides)
    return await db.create_reject_rule(**fields)


# ══════════════════════════════════════════════════════════════════════════════════════════
# affects_reject_rules — чистый предикат
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_affects_reject_rules_true_for_questions_and_option_lists():
    assert rrn.affects_reject_rules("reg_q_course") is True
    assert rrn.affects_reject_rules("reg_q_course__party") is True
    assert rrn.affects_reject_rules("reg_q_course__city__msk") is True
    assert rrn.affects_reject_rules("education_status_options") is True
    assert rrn.affects_reject_rules("university_options") is True
    assert rrn.affects_reject_rules("source_options") is True
    assert rrn.affects_reject_rules("study_field_options") is True  # SELECT_CONFIG


def test_affects_reject_rules_false_for_unrelated_keys():
    assert rrn.affects_reject_rules("reject_text") is False
    assert rrn.affects_reject_rules("event_date") is False
    assert rrn.affects_reject_rules("miniapp_logo") is False


# ══════════════════════════════════════════════════════════════════════════════════════════
# on_setting_written — прямой вызов (без воронки)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_on_setting_written_pauses_rule_and_sends_one_message(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    rule_id = _run(_create_course_rule())
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(db.set_setting("reg_q_course", "off"))
    _run(rrn.on_setting_written("reg_q_course"))

    assert len(bot.sent) == 1
    row = _run(db.get_reject_rule(rule_id))
    assert row["paused_reason"] is not None


def test_on_setting_written_repeat_write_sends_nothing_new(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    _run(_create_course_rule())
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(db.set_setting("reg_q_course", "off"))
    _run(rrn.on_setting_written("reg_q_course"))
    assert len(bot.sent) == 1

    # Повторная запись ТОГО ЖЕ значения — правило уже на паузе, второго сообщения быть не должно.
    _run(db.set_setting("reg_q_course", "off"))
    _run(rrn.on_setting_written("reg_q_course"))
    assert len(bot.sent) == 1


def test_on_setting_written_reenable_unpauses_without_extra_message(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    rule_id = _run(_create_course_rule())
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(db.set_setting("reg_q_course", "off"))
    _run(rrn.on_setting_written("reg_q_course"))
    assert len(bot.sent) == 1

    _run(db.set_setting("reg_q_course", "on"))
    _run(rrn.on_setting_written("reg_q_course"))

    assert len(bot.sent) == 1  # снятие паузы не рассылается
    row = _run(db.get_reject_rule(rule_id))
    assert row["paused_reason"] is None


def test_on_setting_written_disabled_kill_switch_sends_nothing(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "off"))
    _run(db.set_setting("reg_q_course", "on"))
    _run(_create_course_rule())
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(db.set_setting("reg_q_course", "off"))
    _run(rrn.on_setting_written("reg_q_course"))

    assert bot.sent == []


def test_on_setting_written_irrelevant_key_never_touches_db(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    calls = []

    async def _fake_list_reject_rules(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(rrn, "list_reject_rules", _fake_list_reject_rules)
    _run(rrn.on_setting_written("reject_text"))

    assert calls == [], "ключ вне анкеты не должен доходить даже до списка правил"


def test_pause_message_text_has_no_codes(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    _run(_create_course_rule(name="Курс младше выпуска"))
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(db.set_setting("reg_q_course", "off"))
    _run(rrn.on_setting_written("reg_q_course"))

    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    for forbidden in ("reg_q_", "__city__", "step"):
        assert forbidden not in text, f"{forbidden!r} просочился в текст: {text!r}"
    assert "Курс младше выпуска" in text
    assert "📖 Курс" in text  # human label of the broken question


def test_on_setting_written_swallows_notify_exception(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    _run(_create_course_rule())
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    async def _boom(*args, **kwargs):
        raise RuntimeError("сеть легла")

    from handlers import admin_caps
    monkeypatch.setattr(admin_caps, "notify_by_capability", _boom)

    _run(db.set_setting("reg_q_course", "off"))
    # НЕ должно бросить исключение наружу.
    _run(rrn.on_setting_written("reg_q_course"))


def test_on_setting_written_no_bot_yet_logs_and_does_not_raise(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    _run(_create_course_rule())
    monkeypatch.setattr(rrn._sched, "_bot", None)

    _run(db.set_setting("reg_q_course", "off"))
    _run(rrn.on_setting_written("reg_q_course"))  # не должно бросить


# ══════════════════════════════════════════════════════════════════════════════════════════
# on_settings_written_batch — пресет типа события (мимо воронки, см. докстринг
# services/reject_rules_notify.py и .planning FIX-заметку про пресет типа события)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_preset_apply_pauses_rule_with_one_consolidated_message(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    rule_id = _run(_create_course_rule())  # "course" вне списка "on" пресета "conf"
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(reg_presets.apply_reg_preset("conf"))

    assert len(bot.sent) == 1
    row = _run(db.get_reject_rule(rule_id))
    assert row["paused_reason"] is not None

    # Повторный тап того же пресета — правило уже на паузе, второго сообщения нет.
    _run(reg_presets.apply_reg_preset("conf"))
    assert len(bot.sent) == 1


def test_batch_irrelevant_keys_send_nothing(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(rrn.on_settings_written_batch(["reject_text", "event_date"]))

    assert bot.sent == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# Воронка settings_audit.py действительно зовёт хук
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_set_setting_by_admin_calls_hook_exactly_once_with_same_key(tmp_path, monkeypatch):
    _ready(tmp_path)
    calls = []

    async def _fake_on_setting_written(key):
        calls.append(key)

    monkeypatch.setattr(rrn, "on_setting_written", _fake_on_setting_written)
    _run(settings_audit.set_setting_by_admin(777, "reg_q_course", "off"))

    assert calls == ["reg_q_course"]
    assert _run(db.get_setting("reg_q_course")) == "off"


def test_delete_setting_by_admin_calls_hook(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(settings_audit.set_setting_by_admin(777, "reg_q_course", "off"))
    calls = []

    async def _fake_on_setting_written(key):
        calls.append(key)

    monkeypatch.setattr(rrn, "on_setting_written", _fake_on_setting_written)
    _run(settings_audit.delete_setting_by_admin(777, "reg_q_course"))

    assert calls == ["reg_q_course"]
    assert _run(db.get_setting("reg_q_course")) is None


def test_hook_exception_does_not_block_setting_write(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def _boom(key):
        raise RuntimeError("хук упал")

    monkeypatch.setattr(rrn, "on_setting_written", _boom)
    _run(settings_audit.set_setting_by_admin(777, "reg_q_course", "off"))

    assert _run(db.get_setting("reg_q_course")) == "off"


def test_admin_log_line_unchanged(tmp_path, caplog):
    _ready(tmp_path)
    with caplog.at_level(logging.INFO, logger="settings_audit"):
        _run(settings_audit.set_setting_by_admin(777, "test_key", "on"))
    assert any("admin=777 setting test_key" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_end_to_end_through_funnel_pauses_and_notifies(tmp_path, monkeypatch):
    """Полный путь менеджера: settings_audit -> реакция -> рассылка, без единой подмены
    промежуточного звена (кроме бота и получателя — реальная сеть недоступна в тесте)."""
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "on"))
    _run(_create_course_rule())
    bot = _FakeBot()
    monkeypatch.setattr(rrn._sched, "_bot", bot)

    _run(settings_audit.set_setting_by_admin(777, "reg_q_course", "off"))

    assert len(bot.sent) == 1
