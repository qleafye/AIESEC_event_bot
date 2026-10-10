"""10.10: делегат открыл «❓ Задать вопрос» и, передумав, нажал «🪙 Мои баллы» — подпись кнопки
уходила организаторам КАК ТЕКСТ ВОПРОСА (`MenuButton` в состояниях делегата не матчится, а
`process_question` забирает любой текст). То же в сдаче задания, комментарии к сессии и т.п.

Теперь точное совпадение с подписью кнопки меню в этих «ответах делегата» выводит из
состояния с короткой строкой («Вопрос не отправлен.») и открывает нажатый раздел. Анкета
(`Registration`, `_CompositeChat`, `_LookupChat`, `OnsiteReg`) не трогается: там меню не
показывается, а ответ может совпасть с подписью.

Сквозная проверка — через общий Dispatcher из tests/test_refac_snapshot_260816.py (реальный
порядок роутеров main.py, см. предупреждение про router-синглтон в
tests/test_reg_silence_fallback_260919.py)."""
import asyncio

import pytest
from aiogram import Bot

from database import db
from handlers.reg import menu_tap_escape
from handlers.i18n import reg_i18n
from handlers import user_actions as user_actions_mod
from handlers.states import GameSubmit, Question, Registration
from tests.test_refac_snapshot_260816 import _full_dispatcher, _make_message_update, _spied
from tests.test_reg_handoff_260904 import _ready

TOKEN = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
UID_BASE = 861000


@pytest.fixture
def said(monkeypatch):
    """Строки, которые бот сказал бы делегату (без сети: бот с фейковым токеном)."""
    out = []

    async def fake_say(message, text, **kwargs):
        out.append(text)

    monkeypatch.setattr(reg_i18n, "say", fake_say)
    return out


def _feed(uid, state_obj, text, spies):
    """Ставит делегату состояние, кормит апдейт, возвращает (вызовы шпионов, состояние после)."""
    async def go():
        dp = _full_dispatcher()
        bot = Bot(token=TOKEN)
        try:
            fsm = dp.fsm.resolve_context(bot, chat_id=uid, user_id=uid)
            await fsm.set_state(state_obj)
            from contextlib import ExitStack
            with ExitStack() as stack:
                calls = {name: stack.enter_context(_spied(user_actions_mod, "message", name))
                         for name in spies}
                await dp.feed_update(bot, _make_message_update(uid, text, uid))
                counts = {name: len(c) for name, c in calls.items()}
            return counts, await fsm.get_state()
        finally:
            await bot.session.close()

    return asyncio.run(go())


@pytest.mark.parametrize("caption", ["🪙 Мои баллы", "🪙 Мои монеты"])
def test_menu_tap_during_question_opens_section_and_drops_question(tmp_path, said, caption):
    _ready(tmp_path)
    uid = UID_BASE + (1 if caption.endswith("баллы") else 2)
    counts, after = _feed(uid, Question.waiting_for_question, caption,
                          ["process_question", "show_my_coins"])
    assert counts == {"process_question": 0, "show_my_coins": 1}, (
        "подпись кнопки меню ушла организаторам как текст вопроса"
    )
    assert after is None
    assert said == ["Вопрос не отправлен."]


def test_custom_caption_also_escapes(tmp_path, said):
    _ready(tmp_path)
    asyncio.run(db.set_setting("menu_coins_label", "🪙 Мой счёт"))
    counts, after = _feed(UID_BASE + 3, Question.waiting_for_question, "🪙 Мой счёт",
                          ["process_question", "show_my_coins"])
    assert counts == {"process_question": 0, "show_my_coins": 1}
    assert after is None


def test_plain_question_text_still_goes_to_organizers(tmp_path, said):
    _ready(tmp_path)
    counts, after = _feed(UID_BASE + 4, Question.waiting_for_question, "Когда трансфер?",
                          ["process_question", "show_my_coins"])
    assert counts == {"process_question": 1, "show_my_coins": 0}
    assert after == Question.waiting_for_question.state
    assert said == []


def test_menu_tap_during_game_submit(tmp_path, said):
    _ready(tmp_path)
    counts, after = _feed(UID_BASE + 5, GameSubmit.proof, "❓ Задать вопрос",
                          ["receive_proof", "ask_organizer_start"])
    assert counts == {"receive_proof": 0, "ask_organizer_start": 1}
    assert after is None
    assert said == ["Подтверждение задания не отправлено."]


def test_registration_states_untouched(tmp_path, said):
    """В анкете ответ может совпасть с подписью — состояние не снимаем, меню не открываем."""
    _ready(tmp_path)

    class _Msg:
        text = "🪙 Мои баллы"

        class chat:
            type = "private"

    seen = []

    async def handler(event, data):
        seen.append(data["raw_state"])

    async def go():
        dp = _full_dispatcher()
        bot = Bot(token=TOKEN)
        try:
            uid = UID_BASE + 6
            fsm = dp.fsm.resolve_context(bot, chat_id=uid, user_id=uid)
            await fsm.set_state(Registration.full_name)
            for raw in (Registration.full_name.state, "_CompositeChat:part", "_LookupChat:wait",
                        "OnsiteReg:name"):
                await menu_tap_escape.MenuTapEscape()(handler, _Msg(), {"raw_state": raw, "state": fsm})
            return await fsm.get_state()
        finally:
            await bot.session.close()

    after = asyncio.run(go())
    assert after == Registration.full_name.state
    assert seen == [Registration.full_name.state, "_CompositeChat:part", "_LookupChat:wait",
                    "OnsiteReg:name"]
    assert said == []


def test_escape_groups_are_delegate_answers_only():
    from handlers.states import DELEGATE_STATE_GROUPS
    groups = set(menu_tap_escape.ESCAPE_NOTICES)
    assert groups <= DELEGATE_STATE_GROUPS
    assert groups.isdisjoint({"Registration", "_CompositeChat", "_LookupChat", "OnsiteReg"})
    assert {"Question", "GameSubmit", "SessionFeedbackComment"} <= groups


def test_notices_have_english():
    from services.i18n_form_manual import FORM_DEFAULT_EN
    from services.i18n_sources import code_literals
    missing = [t for t in menu_tap_escape.ESCAPE_NOTICES.values() if t not in FORM_DEFAULT_EN]
    assert missing == []
    corpus = {(origin, text) for origin, text in code_literals()}
    absent = [(g, t) for g, t in menu_tap_escape.ESCAPE_NOTICES.items()
              if (f"lit:menu_tap_escape.{g}", t) not in corpus]
    assert absent == [], "строки выхода продублированы в services/i18n_sources.code_literals"
