"""Квик 260917-en (живая проверка 17.09, находка «б») — заголовок карточки согласия
(«Согласие на обработку персональных данных») оставался русским при lang=en, хотя это
НАЗВАНИЕ документа, а не сам юридический текст (LANG-09 запрещает переводить именно текст
согласия, PDF). Фикс — `handlers/registration.py::_ask_step` (ветка `consent:*`) и
`handlers/reg_consent.py::_send_renew_card` переводят `caption` через `reg_i18n.tr_text` с
ПУСТЫМ `tr_map`: срабатывает ТОЛЬКО ярус A (точный рукописный литерал), машинный перевод
(легальный override менеджера) сюда не подключается ни при каких условиях.

`_send_renew_card` — тот же код, что и ветка `consent:*` в `_ask_step`, но без FSM/степ-машины
вокруг — используется как более лёгкая точка интеграционной проверки того же фикса."""
import asyncio

from config import config
from database import db
from handlers import reg_consent, reg_i18n

UID = 260917002
_TITLE = "Согласие на обработку персональных данных"


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_consent_title_i18n_260917.db")
    asyncio.run(db.init_db())


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, chat_id=UID):
        self.chat = _FakeChat(chat_id)
        self.sent = []       # (text, kwargs) через .answer
        self.documents = []  # (file_id, caption, kwargs) через .answer_document

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def answer_document(self, file_id, caption=None, **kwargs):
        self.documents.append((file_id, caption, kwargs))


def _patch_ctx(monkeypatch, lang, tr_map):
    async def _ctx(_target):
        return lang, dict(tr_map)

    monkeypatch.setattr(reg_i18n, "ctx_for", _ctx)


# ── Юнит: только ярус A, без обращения к БД ────────────────────────────────────────────────

def test_default_consent_title_translated_via_layer_a_only():
    assert reg_i18n.tr_text(_TITLE, "en", {}) == "Consent to personal data processing"


def test_ru_identity_unchanged():
    assert reg_i18n.tr_text(_TITLE, "ru", {}) is _TITLE


def test_manager_legal_override_is_not_touched_by_layer_a():
    """LANG-09: если менеджер задал полный легальный текст вместо короткого названия, tier A
    не совпадает (другая строка) и, поскольку caption всегда переводится с пустым tr_map,
    ярус B (машинный перевод) тоже не подключается — override остаётся русским как есть."""
    custom_legal_text = "Настоящим я даю согласие на обработку моих персональных данных..."
    assert reg_i18n.tr_text(custom_legal_text, "en", {}) == custom_legal_text


# ── Интеграция: _send_renew_card (тот же фикс, что и ветка consent:* в _ask_step) ──────────

def test_send_renew_card_translates_default_title_at_en(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage()

    asyncio.run(reg_consent._send_renew_card(msg, _TITLE, "personal_data"))

    assert msg.sent, "ожидался message.answer без PDF (consent_pdf_personal_data не задан)"
    text, _kwargs = msg.sent[0]
    assert text == "Consent to personal data processing"


def test_send_renew_card_keeps_russian_title_at_ru(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _patch_ctx(monkeypatch, "ru", {})
    msg = _FakeMessage()

    asyncio.run(reg_consent._send_renew_card(msg, _TITLE, "personal_data"))

    text, _kwargs = msg.sent[0]
    assert text == _TITLE


def test_send_renew_card_custom_manager_override_stays_russian_at_en(tmp_path, monkeypatch):
    """Менеджер задал override reg_prompt_consent_personal_data — не совпадает с UI_EN
    (LANG-09: caption с легальным override не переводится, даже при lang=en)."""
    _db_ready(tmp_path)
    custom_legal_text = "Полный текст согласия на обработку персональных данных, редакция v2."
    asyncio.run(db.set_setting("reg_prompt_consent_personal_data", custom_legal_text))
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage()

    asyncio.run(reg_consent._send_renew_card(msg, _TITLE, "personal_data"))

    text, _kwargs = msg.sent[0]
    assert text == custom_legal_text
