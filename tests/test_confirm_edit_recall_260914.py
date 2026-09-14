"""Инцидент «не все заявки доходят» (разбор прод-логов 14.09), правка №1: «Изменить» на
сводке анкеты перестаёт рестартовать весь флоу и становится правкой по полям через уже
существующий recall-механизм возвращенца (`handlers/reg_flow.py::process_confirm_edit`,
`services/reg_finalize.py`).

Прод 05-14.09: 175 рестартов через «Изменить», 19 делегатов бросили анкету, увидев снова
пустой первый вопрос. Отдельная утечка того же механизма: снимок ответов со сводки раньше
слепо клался в `_prior_answers`, и `reg_finalize` читал оттуда `prior.get("season")` как будто
это прошлый сезон — делегат получал «🔁 Повторный» в карточке модерации, хотя просто поправил
поле в ТЕКУЩЕЙ анкете. Маркер `_from_confirm` внутри снимка закрывает эту дыру.

Стиль дословно как tests/test_returning_prefill_073.py и tests/test_returning_delegate_073.py
(direct handler calls, real FSMContext over MemoryStorage, config.DB_PATH в tmp_path,
hand-rolled Fake*-двойники) -- pytest-asyncio в окружении нет; файл самодостаточен.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

import reg_options
from config import config
from database import db
from handlers import registration as reg
from handlers import reg_flow
from handlers.states import Registration

UID = 820001


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_confirm_edit_recall_260914.db")


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    """Records (text, reply_markup, parse_mode) triples from answer/answer_document."""

    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup, parse_mode)]

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def answer_document(self, *a, caption=None, reply_markup=None, parse_mode=None, **k):
        self.sent.append((caption, reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.username)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _KBCapturingMessage(0)
        self.answers = []  # list[(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


def _texts(msg: _KBCapturingMessage):
    return [t for (t, _, _) in msg.sent]


def _inline_kb_msgs(msg: _KBCapturingMessage):
    return [(t, rm, p) for (t, rm, p) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


def _callback_datas(markup):
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return []
    return [btn.callback_data for row in rows for btn in row]


async def _register(uid, username=None, status="approved", season=None, prev_season=None,
                     full_name="Тест Тестов"):
    await db.add_user({
        "telegram_id": uid, "full_name": full_name, "username": username,
        "registration_date": "2026-08-18 10:00:00",
        "season": season, "prev_season": prev_season,
    })
    await db.set_user_status(uid, status)


async def _drain_recall(state: FSMContext, msg: _KBCapturingMessage):
    """Гоняет recall_keep, пока FSM не дойдёт до Registration.confirm. Потолок итераций
    страхует от бесконечного цикла, если движок когда-нибудь перестанет сходиться (тот же
    приём, что tests/test_returning_delegate_073.py::test_advance_summary_always_carries_confirm_keyboard)."""
    for _ in range(30):
        state_name = await state.get_state()
        if state_name == Registration.confirm.state:
            return
        assert state_name == Registration.recall_pending.state, state_name
        data = await state.get_data()
        step_key = data["_recall_step"]
        cb = _FakeCallback(f"recall_keep:{step_key}", UID, "delegate")
        cb.message = msg  # тот же транскрипт .sent через весь цикл
        await reg.recall_keep(cb, state, bot=None)
    raise AssertionError("не дошли до Registration.confirm за 30 тапов")


# ── (а) первый экран после «Изменить» — recall ФИО, не пустой вопрос ───────────────────────

def test_confirm_edit_shows_recall_screen_for_full_name(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        # Данные уже введены делегатом в ЭТОЙ сессии (не из строки users) -- ровно то, что
        # лежит в FSM на экране сводки перед тапом «Изменить».
        await state.update_data(
            participant_type="full", full_name="Иванов Иван", university="МГУ",
        )
        await state.set_state(Registration.confirm)

        await reg_flow.process_confirm_edit(msg, state)

        texts = _texts(msg)
        assert any(t and "Прошлый ответ" in t and "Иванов Иван" in t for t in texts)
        inline = _inline_kb_msgs(msg)
        assert len(inline) == 1
        assert _callback_datas(inline[0][1]) == ["recall_keep:full_name", "recall_change:full_name"]
        assert await state.get_state() == Registration.recall_pending.state

        data = await state.get_data()
        prior = data.get("_prior_answers") or {}
        assert prior.get("full_name") == "Иванов Иван"
        assert prior.get("university") == "МГУ"
        # Маркер лежит ВНУТРИ снимка -- не отдельным FSM-ключом.
        assert prior.get("_from_confirm") is True

    asyncio.run(go())


def test_confirm_edit_snapshot_drops_empty_and_service_keys(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(
            participant_type="full", full_name="Иванов Иван",
            university="-",  # прочерк -- считается пустым, в снимок не идёт
            age=None,  # None -- тоже не идёт
            _reg_step=3, _reg_total=9,  # служебный `_`-ключ -- в снимок не копируется
        )
        await state.set_state(Registration.confirm)

        await reg_flow.process_confirm_edit(msg, state)

        data = await state.get_data()
        prior = data.get("_prior_answers") or {}
        assert "university" not in prior
        assert "age" not in prior
        assert "_reg_step" not in prior
        assert "season" not in prior  # season никогда не копируется в снимок

    asyncio.run(go())


# ── (г) регресс: настоящий возвращенец (снимок без маркера) prev_season по-прежнему получает ──

async def _run_finalize(uid, data_updates):
    state = _new_state(uid)
    await state.update_data(full_name="Тест Тестов", **data_updates)
    msg = _KBCapturingMessage(uid, "delegate")
    await reg.finalize_registration(msg, state, bot=object())
    return await db.get_user(uid)


def test_returning_delegate_without_marker_still_gets_prev_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        row = await _run_finalize(UID, {"_prior_answers": {"season": "YL'25"}})
        assert row["prev_season"] == "YL'25"

    asyncio.run(go())


def test_confirm_edit_marker_suppresses_prev_season_direct(tmp_path):
    """Тот же снимок, что и в тесте выше, но с маркером `_from_confirm` -- prev_season
    остаётся NULL, хотя в снимке есть "season" (то, что раньше ловило делегата после
    «Изменить»)."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        row = await _run_finalize(
            UID, {"_prior_answers": {"season": "YL'25", "_from_confirm": True}}
        )
        assert row["prev_season"] is None
        assert row["season"] == "YL'26"

    asyncio.run(go())


# ── (б)+(в) цепочка «Оставить» по всем шагам -> confirm с теми же ответами; prev_season NULL ──

def test_confirm_edit_recall_chain_preserves_answers_no_prev_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("registration_mode", "full")
        await db.set_setting("event_season", "YL'26")
        # Настоящий возвращенец: строка users с прошлым сезоном -- сценарий, в котором риск
        # утечки LEAK-01 максимален (делегат реально «был на прошлом событии», строка users
        # это подтверждает). Изолированная проверка самого маркера -- в
        # test_confirm_edit_marker_suppresses_prev_season_direct выше.
        await _register(UID, "delegate", status="rejected", season="YL'25")
        # work_status=0 (не 1): иначе включается доп. шаг "work_sphere", для которого нет
        # прошлого ответа, и рекол-цикл упрётся в обычный вопрос вместо recall_keep (тот же
        # приём, что tests/test_returning_delegate_073.py).
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET age=25, vk_username='@delegate', "
                "education_status='Нет, не получал(а) образование', "
                "expectations='Нетворкинг', source=?, work_status=0, "
                "missing_skills='Тайм-менеджмент' WHERE telegram_id=?",
                (reg_options.SOURCE_NOT_ASKED, UID),
            )
            await conn.commit()

        state = _new_state(UID)
        callback = _FakeCallback("rereg_start", UID, "delegate")
        await reg_flow.rereg_start(callback, state)
        msg = callback.message
        # `_FakeCallback.message` is built as `_KBCapturingMessage(0)` -- a stand-in for "the
        # message the bot authored", not "the message the delegate will send next". Production
        # code never hits this: rereg_start's own `tap_message` (from_user swapped to the
        # tapping delegate, same model_copy idiom as admin_rereg/party_pick above) is what
        # downstream handlers actually see, and a REAL subsequent "Изменить" arrives as its own
        # Telegram message already carrying the delegate's own from_user. Here `msg` is reused
        # as that next message (process_confirm_edit/finalize_registration below key off
        # `message.from_user.id`), so the id has to be corrected the same way tap_message's
        # was -- otherwise finalize_registration operates on telegram_id=0, and the assertions
        # below silently check an UNTOUCHED UID row instead of the one the flow actually wrote.
        msg.from_user = _FakeUser(UID, "delegate")
        assert await state.get_state() == Registration.recall_pending.state

        await _drain_recall(state, msg)
        data_before = await state.get_data()
        answers_before = {k: v for k, v in data_before.items() if not k.startswith("_")}

        # «Изменить» на сводке -- НЕ рестарт: снимок текущих ответов, recall-проход заново.
        # Проверяем именно НОВЫЕ сообщения (после этой точки), а не весь транскрипт -- иначе
        # проверка не отличит «показали recall заново» от «recall остался с первого прохода».
        sent_before_edit = len(msg.sent)
        await reg_flow.process_confirm_edit(msg, state)
        assert await state.get_state() == Registration.recall_pending.state
        new_msgs = msg.sent[sent_before_edit:]
        assert any(t and "Прошлый ответ" in t and "Тест Тестов" in t for (t, _, _) in new_msgs)
        new_inline = [(t, rm, p) for (t, rm, p) in new_msgs if isinstance(rm, InlineKeyboardMarkup)]
        assert new_inline, "после «Изменить» не показан ни один recall-экран"
        assert _callback_datas(new_inline[-1][1]) == ["recall_keep:full_name", "recall_change:full_name"]

        await _drain_recall(state, msg)
        data_after = await state.get_data()
        answers_after = {k: v for k, v in data_after.items() if not k.startswith("_")}
        assert answers_after == answers_before

        await reg.finalize_registration(msg, state, bot=object())
        row = await db.get_user(UID)
        # Карточка модерации не должна показывать «🔁 Повторный» -- делегат просто поправил
        # свою же, ещё не сохранённую анкету, а не вернулся из прошлого сезона.
        assert row["prev_season"] is None
        assert row["season"] == "YL'26"

    asyncio.run(go())


# ── (д) consent-шаги после «Изменить»: зафиксировать фактическое поведение ─────────────────

def test_confirm_edit_asks_consent_again_before_recall_when_enabled(tmp_path):
    """Характеризующий тест (не требование): при включённом consent_enabled после «Изменить»
    первым экраном снова идёт СОГЛАСИЕ (реальный вопрос, не recall-карточка) -- `_prior_answers`
    не участвует в consent-шагах вовсе (`consent:*`-ключи вне STEP_TO_COLUMN). Отдельного кода
    для пропуска согласий план не требует -- тест просто закрепляет факт, как и для
    rereg_start (tests/test_returning_prefill_073.py::test_consents_never_recalled)."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("consent_enabled", "on")
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full", full_name="Иванов Иван")
        await state.set_state(Registration.confirm)

        await reg_flow.process_confirm_edit(msg, state)

        inline = _inline_kb_msgs(msg)
        for (_t, rm, _p) in inline:
            for cd in _callback_datas(rm):
                assert not (cd or "").startswith("recall_keep:")
        assert await state.get_state() != Registration.recall_pending.state

    asyncio.run(go())
