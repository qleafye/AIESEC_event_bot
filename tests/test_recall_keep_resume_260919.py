"""Квик 260919-u7e, пункт 1 (находка #2 аудита прода, `.planning/review-260919-sections/
01-reg-chat.md`): «📎 Оставить прошлое резюме» на recall-экране после «Изменить» затирало
резюме, а не сохраняло его. 33 подтверждённых потери с 15.09 (23 уже отклонены).

Механизм (проверено посекундно по прод-логам, раздел «Разбор находок» → 2):
1. Делегат отвечает на резюме файлом → `_advance` пишет весь набор колонок шага
   (`reg_engine.columns_for_step("resume")`) в общий `reg_drafts` — файл в черновике цел.
2. На сводке жмёт «Изменить» → `process_confirm_edit` строит снимок `_prior_answers` из ЖИВОЙ
   FSM `data`, затем `_start_registration_flow` делает `state.clear()` — ответы теперь живут
   ТОЛЬКО в `_prior_answers`, а исходный `reg_drafts` НЕ трогается (`upsert_reg_draft` вызывается
   без `patch`).
3. Делегат доходит до recall-экрана резюме («📎 Оставить прошлое резюме / 📤 Загрузить новое»)
   и жмёт «Оставить».
4. ДО фикса: `recall_keep` для `step_key == "resume"` не писал ничего в `data` (комментарий
   ссылался на `COALESCE` в `add_user`, которое спасает только ВОЗВРАЩЕНЦА с уже существующей
   строкой `users`). У новичка со сводки строки `users` ещё нет — `_advance` следом зовёт
   `_sync_draft_out(answered_col=columns_for_step("resume"))`, чей патч собирается из пустой
   `data`, и `upsert_reg_draft` (`database/db.py::answers.update(clean_patch)`) затирает уже
   сохранённый файл значениями `None`. `finalize_registration` читает именно этот черновик
   (`claim_reg_draft`), не FSM — резюме теряется безвозвратно.

Стиль — тот же приём, что `tests/test_confirm_edit_recall_260914.py`/`tests/test_reg_resume_draft.py`
(direct handler calls, real FSMContext over MemoryStorage, config.DB_PATH в tmp_path,
hand-rolled Fake*-двойники; pytest-asyncio в окружении нет — async через asyncio.run())."""
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

UID_A = 831001  # новичок: отвечает резюме файлом впервые, строки users ещё нет
UID_B = 831002  # возвращенец: строка users уже есть, резюме — старый файл с прошлого сезона


def _use_tmp_db(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


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
    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.sent = []

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


class _FakeDocument:
    def __init__(self, file_id="FILE_ID_1", file_name="cv.pdf", file_size=1024):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class _FakeResumeMessage(_KBCapturingMessage):
    def __init__(self, uid, username=None, document=None):
        super().__init__(uid, username)
        self.document = document


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _KBCapturingMessage(user_id, username)
        self.answers = []

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


async def _drain_recall(uid, state: FSMContext, msg: _KBCapturingMessage):
    """Тот же хелпер, что test_confirm_edit_recall_260914.py::_drain_recall -- гоняет
    recall_keep, пока FSM не дойдёт до Registration.confirm."""
    for _ in range(30):
        state_name = await state.get_state()
        if state_name == Registration.confirm.state:
            return
        assert state_name == Registration.recall_pending.state, state_name
        data = await state.get_data()
        step_key = data["_recall_step"]
        cb = _FakeCallback(f"recall_keep:{step_key}", uid, "delegate")
        cb.message = msg
        await reg.recall_keep(cb, state, bot=None)
    raise AssertionError("не дошли до Registration.confirm за 30 тапов")


# ── Полная цепочка: файл → сводка → «Изменить» → recall-цепочка → «Оставить прошлое резюме» ──
# → финал. Ровно прод-сценарий находки #2, только один включённый вопрос (резюме), чтобы
# цепочка recall была короткой и детерминированной.

def test_recall_keep_resume_survives_edit_for_newcomer(tmp_path):
    _use_tmp_db(tmp_path, "recall_keep_resume_newcomer.db")

    async def go():
        # Трек "short" -- курируемое подмножество: ключ без "__short"-суффикса не наследует
        # глобальный `reg_q_*`, отсутствие = выключено (reg_engine.is_step_enabled_for_track).
        # "reg_q_resume__short" -- единственный включённый вопрос всей анкеты.
        await db.set_setting("reg_q_resume__short", "on")
        state = _new_state(UID_A)
        await state.update_data(participant_type="short", _draft_kind="new", full_name="Иванов Иван")
        await db.upsert_reg_draft(UID_A, kind="new", participant_type="short", source="bot")

        # 1. Резюме файлом -- последний и единственный включённый шаг -> сразу сводка.
        msg = _FakeResumeMessage(
            UID_A, "delegate", document=_FakeDocument(file_id="FILE_ID_1", file_name="cv.pdf"),
        )
        await reg_flow.process_resume(msg, state, bot=None)
        assert await state.get_state() == Registration.confirm.state
        draft_before_edit = await db.get_reg_draft(UID_A)
        assert draft_before_edit["answers"].get("resume_file_id") == "FILE_ID_1"

        # 2. «Изменить» -- recall-цепочка вместо рестарта, тот же снимок FSM в _prior_answers.
        await reg_flow.process_confirm_edit(msg, state)
        assert await state.get_state() == Registration.recall_pending.state
        prior = (await state.get_data()).get("_prior_answers") or {}
        assert prior.get("resume_file_id") == "FILE_ID_1"
        assert prior.get("resume_file_name") == "cv.pdf", (
            "снимок process_confirm_edit обязан нести имя файла -- иначе Nextcloud-загрузка "
            "потеряет расширение даже после того, как file_id восстановлен"
        )

        # 3. Драним recall-цепочку (ФИО -> резюме) -- на резюме обязан показаться карвинг-экран
        #    «Оставить прошлое резюме», а не обычный вопрос.
        for _ in range(30):
            state_name = await state.get_state()
            if state_name == Registration.confirm.state:
                break
            data = await state.get_data()
            step_key = data["_recall_step"]
            if step_key == "resume":
                inline = _inline_kb_msgs(msg)
                assert _callback_datas(inline[-1][1]) == [
                    "recall_keep:resume", "recall_change:resume",
                ]
            cb = _FakeCallback(f"recall_keep:{step_key}", UID_A, "delegate")
            cb.message = msg
            await reg.recall_keep(cb, state, bot=None)
        else:
            raise AssertionError("не дошли до Registration.confirm")

        # 4. Черновик В МОМЕНТ recall_keep:resume не должен быть занулён.
        draft_after_keep = await db.get_reg_draft(UID_A)
        assert draft_after_keep["answers"].get("resume_file_id") == "FILE_ID_1", (
            "БАГ находки #2: recall_keep:resume зачищал черновик до None вместо сохранения "
            "прежнего файла -- reg_drafts.answers = {resume_file_id: None, ...}"
        )
        assert draft_after_keep["answers"].get("resume_file_name") == "cv.pdf"

        # 5. Финал -- users обязан получить файл, а не пустоту.
        await reg.finalize_registration(msg, state, bot=object())
        user = await db.get_user(UID_A)
        return user

    user = asyncio.run(go())
    assert user is not None
    assert user["resume_file_id"] == "FILE_ID_1", (
        "БАГ находки #2: резюме новичка исчезает после «Изменить» -> «Оставить прошлое резюме»"
    )


# ── Регресс: настоящий возвращенец с уже существующей строкой users -- поведение НЕ ухудшилось ─

def test_recall_keep_resume_still_works_for_returning_delegate_with_users_row(tmp_path):
    _use_tmp_db(tmp_path, "recall_keep_resume_returning.db")

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("reg_q_resume", "on")
        # Возвращенец с прошлого сезона: строка users уже есть, резюме -- старый файл.
        # work_status=0 (не 1): иначе включается доп. шаг "work_sphere", для которого нет
        # прошлого ответа, и recall-цикл упрётся в обычный вопрос (тот же приём, что
        # tests/test_confirm_edit_recall_260914.py::test_confirm_edit_recall_chain_preserves_answers_no_prev_season).
        await db.add_user({
            "telegram_id": UID_B, "full_name": "Старый Делегат",
            "registration_date": "2026-08-01 10:00:00",
            "resume_file_id": "OLD_FILE_ID", "season": "YL'25",
        })
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET age=25, vk_username='@delegate', "
                "education_status='Нет, не получал(а) образование', "
                "expectations='Нетворкинг', source=?, work_status=0, "
                "missing_skills='Тайм-менеджмент' WHERE telegram_id=?",
                (reg_options.SOURCE_NOT_ASKED, UID_B),
            )
            await conn.commit()
        await db.set_user_status(UID_B, "rejected")
        await db.set_setting("event_season", "YL'26")

        state = _new_state(UID_B)
        callback = _FakeCallback("rereg_start", UID_B, "delegate")
        await reg_flow.rereg_start(callback, state)
        msg = callback.message
        msg.from_user = _FakeUser(UID_B, "delegate")
        assert await state.get_state() == Registration.recall_pending.state

        await _drain_recall(UID_B, state, msg)
        await reg.finalize_registration(msg, state, bot=object())
        user = await db.get_user(UID_B)
        return user

    user = asyncio.run(go())
    assert user is not None
    assert user["resume_file_id"] == "OLD_FILE_ID", (
        "регресс: у возвращенца с уже существующей строкой users фикс не должен ухудшить то, "
        "что раньше спасал add_user's COALESCE"
    )


# ── Прицельный юнит-тест самого recall_keep:resume (без полной цепочки) -----------------------

def test_recall_keep_resume_writes_all_columns_into_fsm_from_prior_answers(tmp_path):
    _use_tmp_db(tmp_path, "recall_keep_resume_unit.db")

    async def go():
        state = _new_state(UID_A)
        await state.update_data(
            _prior_answers={
                "resume_file_id": "FILE_ID_9", "resume_file_name": "resume.docx",
                "full_name": "Кто-То",
            },
            _recall_step="resume",
        )
        await state.set_state(Registration.recall_pending)
        cb = _FakeCallback("recall_keep:resume", UID_A, "delegate")
        await reg.recall_keep(cb, state, bot=None)
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("resume_file_id") == "FILE_ID_9"
    assert data.get("resume_file_name") == "resume.docx"
    # resume_text отсутствовал в _prior_answers (не был отвечен текстом) -- НЕ должен появиться
    # в data ложным None-ключом сверх того, что там уже не было (совместимо с прежним поведением).
    assert data.get("resume_text") is None
