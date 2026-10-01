"""Режим «дописываю SOS»: два быстрых текста делегата подряд и провал правки карточки.

aiogram обрабатывает апдейты одного человека параллельно. Раньше оба сообщения читали пустые
подробности, оба решали «мой текст встанет в карточку» и выходили до копии в тред, а записывался
только первый: второй (часто самый важный, «астма, 2 этаж у лифта») пропадал целиком.
"""
import asyncio

from database import db
from handlers import sos as sos_handlers
from services import sos as sos_service
from tests.test_sos_260924 import (
    DELEGATE_ID, FakeBot, FakeMessage, _add_delegate, _collecting_state, _ready, _run,
)


def _text_is_visible(msg, row) -> bool:
    """Текст делегата виден команде: лёг в карточку или ушёл копией."""
    return msg.text == row["details_text"] or bool(msg.copies)


def test_two_quick_texts_second_not_lost(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))
    state = _collecting_state(DELEGATE_ID, rid)
    a = FakeMessage(text="помогите", user_id=DELEGATE_ID)
    a.bot = bot
    b = FakeMessage(text="у меня астма, 2 этаж у лифта", user_id=DELEGATE_ID)
    b.bot = bot

    async def both():
        await asyncio.gather(
            sos_handlers.sos_collecting_step(a, state),
            sos_handlers.sos_collecting_step(b, state),
        )

    asyncio.run(both())
    row = _run(db.get_sos_report(rid))
    assert row["details_text"] in ("помогите", "у меня астма, 2 этаж у лифта")
    assert _text_is_visible(a, row) and _text_is_visible(b, row)


def test_add_sos_details_reports_whether_text_landed(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    assert _run(db.add_sos_details(rid, text="первый")) is True
    assert _run(db.add_sos_details(rid, text="второй")) is False
    assert _run(db.add_sos_details(rid, photo_file_id="ph")) is False


def test_first_text_goes_as_copy_when_card_edit_fails(tmp_path):
    """Правка карточки упала (флуд, удалённая карточка, слишком длинный текст) — текст делегата
    всё равно уходит отдельной копией, а не остаётся только в БД."""
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))

    async def broken_edit(*args, **kwargs):
        raise RuntimeError("message to edit not found")

    bot.edit_message_text = broken_edit
    state = _collecting_state(DELEGATE_ID, rid)
    msg = FakeMessage(text="мне плохо", user_id=DELEGATE_ID)
    msg.bot = bot
    _run(sos_handlers.sos_collecting_step(msg, state))
    assert _run(db.get_sos_report(rid))["details_text"] == "мне плохо"
    assert msg.copies
