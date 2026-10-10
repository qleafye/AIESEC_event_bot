"""Переводы SOS для делегата на английском (приёмка 01.10): кнопка геопозиции, шапка ответа."""
from aiogram.types import ReplyKeyboardMarkup

from handlers.i18n import reg_i18n
from services import i18n
from services.i18n_form_manual import FORM_DEFAULT_EN

EN_MAP = {i18n.src_hash(ru): en for ru, en in FORM_DEFAULT_EN.items()}


def test_location_button_is_translated_for_english_delegate():
    from handlers.sos import _collecting_kb

    kb = reg_i18n.tr_kb(_collecting_kb(), "en", EN_MAP)
    assert isinstance(kb, ReplyKeyboardMarkup)
    texts = [b.text for row in kb.keyboard for b in row]
    assert "📍 Send location" in texts
    assert "Done" in texts


class _Sink:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    @property
    def id(self):
        return 1


class _OrgMessage:
    """Ответ орга текстом — то, что читают deliver_org_reply и _deliver_question_reply."""

    def __init__(self, text="Иду к тебе"):
        from types import SimpleNamespace

        self.text = text
        self.html_text = text
        self.from_user = SimpleNamespace(id=777, full_name="Орг", username=None)
        self.chat = SimpleNamespace(id=777, type="private")
        self.message_id = 1
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


def _english(monkeypatch):
    from services import i18n as i18n_service

    async def fake_context(telegram_id, language_code=None):
        return "en", EN_MAP

    monkeypatch.setattr(i18n_service, "context", fake_context)


def test_sos_reply_header_in_english_keeps_followup_marker(monkeypatch):
    import asyncio

    from services import sos as sos_service

    _english(monkeypatch)

    async def claim(*a, **k):
        return True

    async def noop(*a, **k):
        return None

    import database.db as db_module

    monkeypatch.setattr(db_module, "claim_sos_report", claim)
    monkeypatch.setattr(sos_service, "refresh_card", noop)
    bot = _Sink()
    ok = asyncio.run(sos_service.deliver_org_reply(bot, _OrgMessage(), {"id": 8, "telegram_id": 42}))
    assert ok
    text = bot.sent[0][1]
    assert text.startswith("🆘 <b>Reply to SOS #8:</b>")
    assert "🆘" in text and "SOS #" in text  # handlers/sos.py::_is_sos_followup


def test_question_reply_header_in_english(monkeypatch):
    import asyncio

    from handlers import admin
    from services import quiet_hours

    _english(monkeypatch)
    captured = []

    async def send_now(now, user_id, text, *, sender, **kwargs):
        captured.append(text)
        return True

    async def notice(now, user_id):
        return ""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(quiet_hours, "send_or_queue_text", send_now)
    monkeypatch.setattr(quiet_hours, "manager_notice", notice)
    monkeypatch.setattr(admin, "_notify_other_moderate_reg_holders", noop)
    asyncio.run(admin._deliver_question_reply(_OrgMessage("Да, можно"), _Sink(), 42, "Орг"))
    assert captured[0].startswith("💬 <b>Reply from the organizers:</b>")


def test_rereg_button_is_translated_for_english_delegate():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001f680 Обновить анкету", callback_data="rereg_start"),
    ]])
    out = reg_i18n.tr_kb(kb, "en", EN_MAP)
    assert out.inline_keyboard[0][0].text == "\U0001f680 Update my application"
    assert out.inline_keyboard[0][0].callback_data == "rereg_start"
