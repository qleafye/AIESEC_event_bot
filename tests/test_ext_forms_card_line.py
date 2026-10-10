"""Строка «📝 Формы» и кнопка «📝 Ответы форм» в карточке делегата /find."""
from __future__ import annotations

import asyncio

from database import ext_forms_db as efd
from handlers import admin as admin_mod
from services.applications.delegate_card import ext_forms_card_lines
from tests.test_checkin_reissue_260924 import (
    ADMIN_ID, DELEGATE_ID, _FakeMessage, _db_ready, _insert_user,
)


async def _answer(form_id, aid, tid):
    await efd.insert_answer(form_id=form_id, answer_id=aid, answered_at=None,
                            received_at="2026-10-01", payload=[], raw=None,
                            matched_telegram_id=tid, match_how="username")


async def _seed_forms():
    f1 = await efd.create_form(platform="yandex", external_id="a", title="РилТолк'Медиа")
    f2 = await efd.create_form(platform="yandex", external_id="b", title="Опрос <b>")
    await _answer(f1, "1", DELEGATE_ID)
    await _answer(f1, "2", DELEGATE_ID)
    await _answer(f2, "1", DELEGATE_ID)


def test_lines_escaped_and_deduplicated(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, username="seeded"))
    asyncio.run(_seed_forms())
    line, has = asyncio.run(ext_forms_card_lines(DELEGATE_ID))
    assert has is True
    assert line.startswith("\n📝 Формы: ")
    assert line.count("РилТолк'Медиа ✓") == 1 and line.count("РилТолк") == 1
    assert "Опрос &lt;b&gt; ✓" in line


def test_lines_empty_without_answers(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, username="seeded"))
    assert asyncio.run(ext_forms_card_lines(DELEGATE_ID)) == ("", False)


def test_find_card_button_only_with_answers(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, username="seeded"))
    msg = _FakeMessage("/find @seeded", ADMIN_ID)
    asyncio.run(admin_mod.cmd_find_user(msg))
    text, _, kb = msg.answers[0]
    assert "📝 Формы" not in text
    assert f"extf_view:{DELEGATE_ID}" not in [b.callback_data for r in kb.inline_keyboard for b in r]

    asyncio.run(_seed_forms())
    msg = _FakeMessage("/find @seeded", ADMIN_ID)
    asyncio.run(admin_mod.cmd_find_user(msg))
    text, _, kb = msg.answers[0]
    assert "📝 Формы:" in text
    assert f"extf_view:{DELEGATE_ID}" in [b.callback_data for r in kb.inline_keyboard for b in r]
