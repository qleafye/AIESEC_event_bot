"""Приёмка 09.10 (дата 31.02): тестировщик видел два сообщения подряд — «Такой даты нет — проверь
число и месяц.» и следом «Формат даты: ДД.ММ.ГГГГ…». Код шлёт одно сообщение на один ввод
(проверено и прямым вызовом, и через полный Dispatcher); второе — ответ на отдельное
сообщение. Сторож фиксирует «один ввод — одно сообщение, новое».
"""
import asyncio

from handlers.reg import reg_flow
from handlers.states import Registration
from tests._dbtpl import fast_init_db
from tests.test_returning_delegate_073 import _KBCapturingMessage, _new_state, _use_tmp_db

UID = 861002


def test_impossible_date_sends_exactly_one_message(tmp_path):
    """Приёмка 09.10 (дата 31.02): одно сообщение на один ввод — «Такой даты нет»."""
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        for step in ("birth_date", "arrival_date"):
            msg = _KBCapturingMessage(UID, "delegate")
            msg.text = "31.02.2007"
            state = _new_state(UID)
            await state.set_state(Registration.date_input)
            await state.update_data(_current_date_step=step)
            await reg_flow.process_date_input(msg, state, bot=object())
            assert [t for (t, _, _) in msg.sent] == ["Такой даты нет — проверь число и месяц."]

    asyncio.run(go())
