"""10.10: одобренный делегат жмёт кнопку старой клавиатуры (подпись переименовали) или пишет
непонятный текст — бот молчал. Последняя ветка `reg_silence_fallback.reply_idle` теперь
отвечает «меню обновилось» и присылает актуальную главную клавиатуру — не чаще раза в
`MENU_HINT_COOLDOWN_SECONDS` на человека, только делегату с поданной анкетой этого сезона.

Фейки — из tests/test_reg_handoff_260904.py, посев строки — как в
tests/test_uat_clickthrough_submitted_text_261009.py."""
import asyncio

from aiogram.types import ReplyKeyboardMarkup

from config import config
from database import db
from handlers.reg import reg_silence_fallback
from domain.settings.schema import SETTINGS_SCHEMA
from tests.test_reg_handoff_260904 import USER_ID, _FakeMessage2, _ready
from tests.test_uat_clickthrough_submitted_text_261009 import SUBMITTED_TEXT, _seed_user

HINT_TEXT = SETTINGS_SCHEMA["menu_refreshed_text"]["default"]


def _send(text="🪙 Мои монетки"):
    from handlers.user_actions import reg_handoff_idle_fallback
    msg = _FakeMessage2(USER_ID, text=text)
    asyncio.run(reg_handoff_idle_fallback(msg))
    return msg.sent


def _fresh(tmp_path):
    _ready(tmp_path)
    reg_silence_fallback._menu_hint_sent_at.clear()


def test_setting_registered_with_english():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    assert "Меню обновилось" in HINT_TEXT
    assert HINT_TEXT in FORM_DEFAULT_EN


def test_approved_delegate_gets_hint_and_main_menu(tmp_path):
    _fresh(tmp_path)
    _seed_user("approved")
    sent = _send()
    assert [t for t, _, _ in sent] == [HINT_TEXT]
    kb = sent[0][1]
    assert isinstance(kb, ReplyKeyboardMarkup), "к подсказке обязана прийти главная клавиатура"
    captions = {b.text for row in kb.keyboard for b in row}
    assert "❓ Задать вопрос" in captions


def test_repeat_within_cooldown_is_silent(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _seed_user("approved")
    clock = [1000.0]
    monkeypatch.setattr(reg_silence_fallback.time, "monotonic", lambda: clock[0])
    assert len(_send()) == 1
    clock[0] += 60
    assert _send("ещё что-то") == []
    clock[0] += reg_silence_fallback.MENU_HINT_COOLDOWN_SECONDS
    assert len(_send()) == 1


def test_pending_branch_keeps_priority(tmp_path):
    _fresh(tmp_path)
    _seed_user("pending")
    assert [t for t, _, _ in _send()] == [SUBMITTED_TEXT]


def test_not_submitted_or_rejected_stay_silent(tmp_path):
    _fresh(tmp_path)
    assert _send() == []  # строки нет — человек только жал /start
    _seed_user("rejected")
    assert _send() == []


def test_staff_gets_no_hint(tmp_path):
    _fresh(tmp_path)
    _seed_user("approved")
    saved = list(config.ADMIN_IDS)
    config.ADMIN_IDS = [USER_ID]
    try:
        assert _send() == []
    finally:
        config.ADMIN_IDS = saved


def test_group_chat_gets_no_hint(tmp_path):
    _fresh(tmp_path)
    _seed_user("approved")
    msg = _FakeMessage2(USER_ID, text="привет")
    msg.chat.type = "group"
    asyncio.run(reg_silence_fallback.reply_idle(msg))
    assert msg.sent == []


def test_nontext_message_gets_no_hint(tmp_path):
    _fresh(tmp_path)
    _seed_user("approved")
    msg = _FakeMessage2(USER_ID, text=None)
    asyncio.run(reg_silence_fallback.reply_idle(msg))
    assert msg.sent == []
