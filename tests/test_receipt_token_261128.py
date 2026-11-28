"""Старая карточка чека не подтверждает и не отклоняет уже другой файл.

В кнопке короткий токен (12 hex sha256 от receipt_file_id), не сам file_id:
callback Telegram умеет 64 байта. Голая старая кнопка оплату не меняет.
"""
import asyncio
import hashlib

from config import config
from database import db
from handlers.access.admin_caps import required_capability
from handlers.applications import admin_moderation
from tests._dbtpl import fast_init_db

ADMIN = 900128
UID = 800128
FILE_A = "file-receipt-A"
FILE_B = "file-receipt-B" + ("x" * 180)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "receipt-token.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]


def _token(file_id: str) -> str:
    return hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:12]


class _Key:
    def __init__(self, user_id):
        self.user_id = user_id


class _State:
    def __init__(self, user_id=ADMIN):
        self.key = _Key(user_id)
        self.data = {}
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, state):
        self.state = state


class _Message:
    def __init__(self, text=None, bot=None):
        self.text = text
        self.bot = bot
        self.answers = []
        self.markup = None

    async def answer(self, text=None, parse_mode=None, reply_markup=None):
        self.answers.append(text)
        self.markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _Callback:
    def __init__(self, data, bot=None):
        self.data = data
        self.bot = bot or _Bot()
        self.from_user = type("U", (), {"id": ADMIN})()
        self.message = _Message(bot=self.bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _noop_card(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(admin_moderation, "_show_current_receipt_card", _noop)


async def _seed(file_id, status="receipt_sent", uid=UID):
    await db.add_user({
        "telegram_id": uid,
        "full_name": "Делегат",
        "registration_date": "2026-11-28",
        "payment_status": status,
        "receipt_file_id": file_id,
    })


def test_card_buttons_carry_short_token_of_the_current_file(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed(FILE_B)
        target = _Message()
        await admin_moderation._show_current_receipt_card(target, _State())
        return target.markup

    kb = asyncio.run(go())
    confirm, reject = [b.callback_data for b in kb.inline_keyboard[0]]
    view, skip = [b.callback_data for b in kb.inline_keyboard[1]]
    token = _token(FILE_B)
    assert confirm == f"rcpt_confirm:{UID}:{token}"
    assert reject == f"rcpt_reject:{UID}:{token}"
    assert view == f"rcpt_view:{UID}"
    assert skip == f"rcpt_skip:{UID}"
    assert FILE_B not in confirm
    assert len(token) == 12
    assert len(confirm.encode("utf-8")) <= 64
    assert len(reject.encode("utf-8")) <= 64
    assert required_capability(callback_data=confirm) == "moderate_receipts"
    assert required_capability(callback_data=reject) == "moderate_receipts"


def test_bare_old_confirm_does_not_mark_paid(tmp_path, monkeypatch):
    _ready(tmp_path)
    _noop_card(monkeypatch)

    async def go():
        await _seed(FILE_A)
        cb = _Callback(f"rcpt_confirm:{UID}")
        await admin_moderation.rcpt_confirm(cb, _State())
        user = await db.get_user(UID)
        return cb.answers, user["payment_status"], cb.bot.sent

    answers, status, sent = asyncio.run(go())
    assert status == "receipt_sent"
    assert sent == []
    assert answers == [(admin_moderation._STALE_RECEIPT_ALERT, True)]
    assert "Чеки" in answers[0][0]


def test_token_of_the_replaced_file_does_not_mark_the_new_one_paid(tmp_path, monkeypatch):
    _ready(tmp_path)
    _noop_card(monkeypatch)

    async def go():
        await _seed(FILE_A)
        await db.update_payment_status(UID, "not_paid", require_status="receipt_sent")
        await db.update_payment_status(UID, "receipt_sent", receipt_file_id=FILE_B)
        cb = _Callback(f"rcpt_confirm:{UID}:{_token(FILE_A)}")
        await admin_moderation.rcpt_confirm(cb, _State())
        user = await db.get_user(UID)
        return cb.answers, user["payment_status"], user["receipt_file_id"]

    answers, status, file_id = asyncio.run(go())
    assert status == "receipt_sent"
    assert file_id == FILE_B
    assert answers == [(admin_moderation._STALE_RECEIPT_ALERT, True)]


def test_matching_token_confirms_only_that_file(tmp_path, monkeypatch):
    _ready(tmp_path)
    _noop_card(monkeypatch)
    import services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "cancel_payment_reminders", lambda user_id: None)

    async def fake_completion(*args, **kwargs):
        return None

    monkeypatch.setattr("handlers.registration.send_completion_and_bonus", fake_completion)

    async def go():
        await db.set_setting("quiet_hours_enabled", "off")
        await _seed(FILE_A)
        cb = _Callback(f"rcpt_confirm:{UID}:{_token(FILE_A)}")
        await admin_moderation.rcpt_confirm(cb, _State())
        user = await db.get_user(UID)
        return cb.answers, user["payment_status"]

    answers, status = asyncio.run(go())
    assert status == "paid"
    assert answers[-1][0] == "Оплата подтверждена"


def test_same_token_on_an_already_paid_row_does_not_reset(tmp_path, monkeypatch):
    _ready(tmp_path)
    _noop_card(monkeypatch)

    async def go():
        await _seed(FILE_A, status="paid")
        cb = _Callback(f"rcpt_confirm:{UID}:{_token(FILE_A)}")
        await admin_moderation.rcpt_confirm(cb, _State())
        user = await db.get_user(UID)
        return cb.answers, user["payment_status"]

    answers, status = asyncio.run(go())
    assert status == "paid"
    assert answers[0][0] == "Чек уже обработан."


def test_foreign_reject_does_not_ask_and_does_not_clear_payment(tmp_path, monkeypatch):
    _ready(tmp_path)
    _noop_card(monkeypatch)

    async def go():
        await _seed(FILE_B)
        state = _State()
        cb = _Callback(f"rcpt_reject:{UID}:{_token(FILE_A)}")
        await admin_moderation.rcpt_reject_start(cb, state)
        user = await db.get_user(UID)
        return cb.answers, cb.message.answers, state.data, user["payment_status"]

    answers, asked, data, status = asyncio.run(go())
    assert status == "receipt_sent"
    assert asked == []
    assert "rcpt_reject_uid" not in data
    assert answers == [(admin_moderation._STALE_RECEIPT_ALERT, True)]


def test_reject_without_token_does_not_change_payment(tmp_path, monkeypatch):
    _ready(tmp_path)
    _noop_card(monkeypatch)

    async def go():
        await _seed(FILE_A)
        state = _State()
        await state.update_data(rcpt_reject_uid=UID)
        message = _Message(text="не тот файл", bot=_Bot())
        await admin_moderation.rcpt_reject_reason(message, state)
        user = await db.get_user(UID)
        return message.answers, user["payment_status"], message.bot.sent

    answers, status, sent = asyncio.run(go())
    assert status == "receipt_sent"
    assert sent == []
    assert admin_moderation._STALE_RECEIPT_ALERT in answers[0]


def test_matching_reject_clears_only_that_file(tmp_path, monkeypatch):
    _ready(tmp_path)
    _noop_card(monkeypatch)

    async def go():
        await db.set_setting("quiet_hours_enabled", "off")
        await _seed(FILE_A)
        state = _State()
        await state.update_data(rcpt_reject_uid=UID, rcpt_reject_token=_token(FILE_A))
        message = _Message(text="нечитаемо", bot=_Bot())
        await admin_moderation.rcpt_reject_reason(message, state)
        user = await db.get_user(UID)
        return user["payment_status"], message.bot.sent

    status, sent = asyncio.run(go())
    assert status == "not_paid"
    assert sent and "Чек отклонён" in sent[0][0][1]


def test_update_payment_status_refuses_a_different_file(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed(FILE_A)
        missed = await db.update_payment_status(
            UID, "paid", require_status="receipt_sent", require_receipt_file_id=FILE_B,
        )
        stayed = await db.get_user(UID)
        hit = await db.update_payment_status(
            UID, "paid", require_status="receipt_sent", require_receipt_file_id=FILE_A,
        )
        paid = await db.get_user(UID)
        return missed, stayed["payment_status"], hit, paid["payment_status"]

    missed, stayed, hit, paid = asyncio.run(go())
    assert missed == 0
    assert stayed == "receipt_sent"
    assert hit == 1
    assert paid == "paid"
