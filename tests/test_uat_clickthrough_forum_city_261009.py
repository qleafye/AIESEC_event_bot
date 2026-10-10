"""Приёмка 09.10: город форума не виден делегату ни после выбора, ни в сводке.

- после тапа по кнопке города кнопки пропадали, выбор нигде не подтверждался;
- в сводке «Проверь свои ответы» не было строки «Город форума» — делегат не мог проверить,
  в какой город подаёт заявку;
- ответ «Пока нет» на вопрос об амбассадорстве в сводке не показывался вовсе.

Фейки — из tests/test_city_flow_phase71.py."""
import asyncio

from database import db
from handlers import reg_flow
from handlers.reg_city_gate import summary_data
from handlers import registration as reg
from domain.regform.engine import summary_fields
from tests._dbtpl import fast_init_db
from tests.test_city_flow_phase71 import _FakeCallback, _new_state, _use_tmp_db


def test_city_pick_confirms_chosen_city_in_chat(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 811001

    async def go():
        fast_init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_label__spb", "Санкт-Петербург, 3 октября")
        state = _new_state(uid)
        cb = _FakeCallback("city_pick:spb", uid, "u")
        await reg_flow.city_pick(cb, state)
        return cb.message.texts

    texts = asyncio.run(go())
    assert any("Город форума" in (t or "") and "Санкт-Петербург, 3 октября" in t for t in texts), texts


def test_summary_shows_forum_city_first(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_label__msk", "Москва, 30-31 октября")
        data = await summary_data({"full_name": "Иванов Иван", "event_city": "msk"})
        return reg._build_summary(data)

    summary = asyncio.run(go())
    lines = summary.split("\n")
    assert lines[2] == "<b>Город форума:</b> Москва, 30-31 октября", lines


def test_summary_without_forum_city_has_no_city_line(tmp_path):
    """Модуль городов выключен — строки нет (как у события без городов)."""
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        data = await summary_data({"full_name": "Иванов Иван", "event_city": None})
        return reg._build_summary(data)

    assert "Город форума" not in asyncio.run(go())


def test_summary_shows_ambassador_no_answer():
    fields = dict(summary_fields({"full_name": "Иванов Иван", "is_ambassador_candidate": False}))
    assert fields["Амбассадор"] == "Нет"


def test_summary_hides_ambassador_when_question_not_asked():
    fields = dict(summary_fields({"full_name": "Иванов Иван"}))
    assert "Амбассадор" not in fields


def test_city_pick_confirmation_escapes_manager_label(tmp_path):
    """Ревью: подпись города от менеджера с «&»/«<» уходила сырой при parse_mode=HTML —
    Telegram отклонил бы сообщение, и city_pick упал бы до перехода к следующему шагу."""
    _use_tmp_db(tmp_path)
    uid = 811002

    async def go():
        fast_init_db()
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("city_label__spb", "Питер & <Ко>")
        state = _new_state(uid)
        cb = _FakeCallback("city_pick:spb", uid, "u")
        await reg_flow.city_pick(cb, state)
        return cb.message.texts

    confirm = [t for t in asyncio.run(go()) if t and "Город форума" in t]
    assert confirm == ["✅ Город форума: Питер &amp; &lt;Ко&gt;"], confirm
