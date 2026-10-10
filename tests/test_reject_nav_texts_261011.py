"""11.10.2026 — автоотказ: «Назад» из журнала и отчётности, текст удаления правила, имя
правила в уведомлении менеджеру об автоотказе.

- Журнал «🤖 Автоотказы» и «📊 Отчётность» открываются только с экрана правил, а «← Назад»
  вело в корень /admin и в раздел «📋 Заявки» мимо него.
- Подтверждение удаления говорило «Уже отклонило заявок» и про правило «⚠️ Помечать», которое
  никого не отклоняет.
- Уведомление «🤖 Автоотказ: … — правило «…»» брало первое предложение текста отказа, а не имя,
  которое менеджер дал правилу.
"""
from __future__ import annotations

import asyncio
import json

from handlers.applications import admin_reject_journal, admin_reject_reports, admin_reject_rules
from services.registration import reg_finalize
from tests.test_reject_rules_editor import SUPERADMIN_ID, _FakeCallback, _create_rule, _ready


def _run(coro):
    return asyncio.run(coro)


def _last_button(kb):
    return kb.inline_keyboard[-1][-1]


def test_journal_and_reports_back_to_rules(tmp_path):
    _ready(tmp_path)
    for render in (admin_reject_journal.render_journal_screen, admin_reject_reports.render_reports_screen):
        _text, kb = _run(render(SUPERADMIN_ID))
        back = _last_button(kb)
        assert (back.text, back.callback_data) == ("← К правилам автоотказа", "admin_reject_rules")


def _delete_screen(rule_id):
    callback = _FakeCallback(f"arr_d:{rule_id}")
    _run(admin_reject_rules.arr_delete_confirm(callback))
    return callback.message.text_edited


def test_delete_text_reject_rule(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(name="Младше 18"))
    text = _delete_screen(rule_id)
    assert "🗑 <b>Удалить правило «Младше 18»?</b>" in text
    assert "Правило пропадёт из списка. Уже отклонённые им заявки и журнал останутся как есть." in text
    assert "Вернуть правило нельзя — если сомневаетесь, лучше выключите его." in text
    assert "Уже отклонило заявок" not in text and "Пока не отклонило" not in text


def test_delete_text_flag_rule_does_not_say_rejected(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(name="Без резюме", action="flag"))
    text = _delete_screen(rule_id)
    assert "Уже помеченные им заявки останутся с пометкой" in text
    assert "отклон" not in text


def test_admin_notice_uses_rule_name():
    full = {"full_name": "Иван", "username": "@ivan"}
    texts = ["Мы рады, что ты подал заявку. Но пока не можем её принять."]
    assert reg_finalize._auto_reject_admin_text(full, texts, "Младше 18").endswith("правило «Младше 18»")
    # без имени — прежний запасной вариант: первое предложение текста отказа
    assert reg_finalize._auto_reject_admin_text(full, texts).endswith(
        "правило «Мы рады, что ты подал заявку.»"
    )


def test_first_rule_name_reads_live_rule(tmp_path):
    _ready(tmp_path)
    named = _run(_create_rule(name="Младше 18"))
    unnamed = _run(_create_rule(name=None))
    assert _run(reg_finalize._first_rule_name({"auto_reject_rule_ids": json.dumps([named, unnamed])})) == "Младше 18"
    assert _run(reg_finalize._first_rule_name({"auto_reject_rule_ids": json.dumps([unnamed])})) is None
    assert _run(reg_finalize._first_rule_name({"auto_reject_rule_ids": json.dumps([987654])})) is None  # удалено
    assert _run(reg_finalize._first_rule_name({"auto_reject_rule_ids": "битое"})) is None
