"""Приёмка 09.10 (C10b): свободный текст на сводке «Проверь свои ответы» молча игнорировался —
делегат писал «когда ответ» вместо кнопки и не получал ничего. Теперь — подсказка нажать
«Всё верно» или «Изменить» и та же клавиатура подтверждения; состояние сводки не меняется.
"""
import asyncio

from aiogram.types import ReplyKeyboardMarkup

from handlers import reg_flow
from handlers.states import Registration
from tests._dbtpl import fast_init_db
from tests.test_returning_delegate_073 import _KBCapturingMessage, _new_state, _use_tmp_db
from tests.test_refac_snapshot_260816 import _full_dispatcher, _make_message_update

UID = 861001
HINT = "Нажми «Всё верно», чтобы отправить анкету, или «Изменить», чтобы поправить ответы."


def _labels(markup):
    return [b.text for row in markup.keyboard for b in row]


def test_free_text_on_summary_gets_hint_and_buttons(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        msg.text = "когда ответ"
        state = _new_state(UID)
        await state.set_state(Registration.confirm)
        await state.update_data(full_name="Тест Тестов")
        await reg_flow.process_confirm_other(msg, state)

        assert [t for (t, _, _) in msg.sent] == [HINT]
        markup = msg.sent[0][1]
        assert isinstance(markup, ReplyKeyboardMarkup)
        assert _labels(markup) == ["Всё верно", "Изменить"]
        assert await state.get_state() == Registration.confirm.state
        assert (await state.get_data())["full_name"] == "Тест Тестов"

    asyncio.run(go())


def test_summary_hint_is_routed_after_confirm_and_edit_words():
    """Порядок хендлеров: «Всё верно»/«Изменить» ловятся своими хендлерами, всё прочее в
    состоянии сводки — подсказкой (а не уходит мимо роутера анкеты в тишину)."""
    observer = reg_flow.router.message
    names = [h.callback.__name__ for h in observer.handlers]
    assert names.index("process_confirm_ok") < names.index("process_confirm_other")
    assert names.index("process_confirm_edit") < names.index("process_confirm_other")


def test_summary_free_text_through_dispatcher(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    dp = _full_dispatcher()
    calls = []

    async def _spy(message, state, *a, **k):
        calls.append(message.text)

    handler = next(h for h in reg_flow.router.message.handlers if h.callback.__name__ == "process_confirm_other")
    monkeypatch.setattr(handler, "callback", _spy)

    async def go():
        fast_init_db()
        from aiogram import Bot
        from aiogram.fsm.storage.base import StorageKey
        bot = Bot(token="123456:ABCDEF")
        key = StorageKey(bot_id=bot.id, chat_id=UID, user_id=UID)
        await dp.storage.set_state(key, Registration.confirm.state)
        await dp.feed_update(bot, _make_message_update(1, "когда ответ", UID))
        await dp.storage.set_state(key, None)

    asyncio.run(go())
    assert calls == ["когда ответ"]
