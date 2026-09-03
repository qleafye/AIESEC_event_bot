"""Phase 21 Plan 09 (FORM-SYNC-02/03): бот подхватывает общий черновик анкеты (`reg_drafts`,
план 21-05) — точки синхронизации в `_advance`/`_stamp_reg_step`/`_start_registration_flow`,
шов `handlers/reg_resume.py` («▶️ Продолжить с шага N / 🔄 Заново»), deep-link
`?start=continue|edit`, отсечка догонялки для делегата, активного в приложении прямо сейчас.

RED (задача 1): `handlers/reg_resume.py` и функции `_sync_draft_out`/`_sync_draft_in` в
`handlers/registration.py` ещё не существуют — весь набор обязан падать на
ImportError/AttributeError с их именами, не на фикстурах.

pytest-asyncio недоступен — async через asyncio.run(), фикстура временной БД и стиль
Fake-объектов aiogram — тот же приём, что `tests/test_returning_delegate_073.py`
(`_KBCapturingMessage`/`_FakeCallback`/`FakeCommand`, byte-for-byte reused idiom, не копия
чужого класса — свои классы того же устройства, чтобы не тянуть весь модуль как зависимость).
`USER_ID` переиспользован из `tests/test_reg_resume_ttl_260820.py`, чтобы оба TTL-набора
(reg_started и reg_drafts) говорили об одном и том же тестовом делегате.
"""
import asyncio
from datetime import datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_flow
from handlers import reg_steps
from handlers.states import Registration

from tests.test_reg_resume_ttl_260820 import USER_ID

OTHER_ID = USER_ID + 1


def _use_tmp_db(tmp_path, name="test_reg_resume_draft.db"):
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
    """Same shape as tests/test_returning_delegate_073.py's helper — records
    (text, reply_markup, parse_mode) triples from answer/answer_photo, own class (not an
    import) so this file stays runnable standalone against a red/missing reg_resume module."""

    def __init__(self, uid, username=None, text=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.text = text
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def answer_photo(self, *a, caption=None, reply_markup=None, parse_mode=None, **k):
        self.sent.append((caption if caption is not None else "<photo>", reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.username, text=self.text)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _KBCapturingMessage(user_id, username)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class FakeCommand:
    def __init__(self, args=None):
        self.args = args


def _texts(msg: _KBCapturingMessage):
    return [t for (t, _, _) in msg.sent]


def _inline_kb_msgs(msg: _KBCapturingMessage):
    return [(t, rm, p) for (t, rm, p) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


def _callback_datas(markup):
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return []
    return [btn.callback_data for row in rows for btn in row]


async def _seed_new_draft(uid, **overrides):
    row = {
        "kind": "new",
        "participant_type": "full",
        "step": "phone",
        "patch": {"age": "20"},
        "source": "bot",
    }
    row.update(overrides)
    patch = row.pop("patch")
    return await db.upsert_reg_draft(uid, patch=patch, **row)


# ── 1. Ответ на шаг в чате -> reg_drafts получает колонку + шаг, version растёт ────────────

def test_advance_writes_answer_and_step_and_bumps_version(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_phone", "on")
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", _draft_kind="new", phone="+79990000000")
        await reg._advance("phone", msg, state, bot=object())
        return await db.get_reg_draft(USER_ID)

    draft = asyncio.run(go())
    assert draft is not None
    assert draft["answers"].get("phone") == "+79990000000"
    assert draft["step"] is not None
    assert draft["version"] >= 1


# ── 2/3. Черновик из приложения подмешивается, just_answered не перетирается, one-time текст ─

def test_advance_merges_miniapp_draft_without_clobbering_just_answered(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_phone", "on")
        # приложение уже ответило на ФИО и телефон
        await db.upsert_reg_draft(
            USER_ID, kind="new", participant_type="full", step="phone",
            patch={"full_name": "Из Приложения", "phone": "+7111"}, source="miniapp",
        )
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        # делегат ТОЛЬКО ЧТО ответил на телефон в чате -- другим значением, чем в приложении
        await state.update_data(
            participant_type="full", _draft_kind="new", phone="+7999", _draft_version=0,
        )
        await reg._advance("phone", msg, state, bot=object())
        data = await state.get_data()
        return data, msg

    data, msg = asyncio.run(go())
    # чат победил на своём поле
    assert data.get("phone") == "+7999"
    # чужое поле подмешалось
    assert data.get("full_name") == "Из Приложения"
    # one-time уведомление ушло
    assert any(t == "📲 Подхватил ответы, которые вы ввели в приложении." for t in _texts(msg))


def test_advance_no_notification_when_no_foreign_change(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", _draft_kind="new", age="20")
        await reg._advance("age", msg, state, bot=object())
        return msg

    msg = asyncio.run(go())
    assert not any("Подхватил" in (t or "") for t in _texts(msg))


# ── 4. /start при свежем kind='new' черновике -> экран «Продолжить/Заново» ─────────────────

def test_start_with_fresh_new_draft_offers_resume_screen(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_new_draft(USER_ID)
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    inline = _inline_kb_msgs(msg)
    assert len(inline) == 1
    datas = _callback_datas(inline[0][1])
    assert "reg_resume:continue" in datas
    assert "reg_resume:restart" in datas
    # первый вопрос анкеты НЕ задан
    assert not any("phone" in (t or "").lower() for t in _texts(msg))


# ── 5. «Продолжить» ставит нужный шаг, восстанавливает ответы; Pitfall 6 fallback ──────────

def test_reg_resume_continue_restores_step_and_answers(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_phone", "on")
        await db.set_setting("reg_q_age", "on")
        await _seed_new_draft(USER_ID, step="phone", patch={"age": "22"})
        from handlers import reg_resume
        callback = _FakeCallback("reg_resume:continue", USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg_resume.reg_resume_continue(callback, state, bot=object())
        data = await state.get_data()
        return data, callback.message

    data, msg = asyncio.run(go())
    assert data.get("age") == "22"
    assert any("телефон" in (t or "").lower() or "phone" in (t or "").lower() for t in _texts(msg)) or msg.sent


def test_reg_resume_continue_step_no_longer_enabled_falls_back(tmp_path):
    """Pitfall 6: менеджер выключил вопрос, пока черновик лежал -- продолжение не падает."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        # черновик указывает на шаг, который сейчас выключен
        await _seed_new_draft(USER_ID, step="a_step_turned_off_meanwhile", patch={"age": "22"})
        from handlers import reg_resume
        callback = _FakeCallback("reg_resume:continue", USER_ID, "delegate")
        state = _new_state(USER_ID)
        # must not raise
        await reg_resume.reg_resume_continue(callback, state, bot=object())
        return callback.message

    msg = asyncio.run(go())
    assert msg.sent  # какой-то экран показан, хендлер не упал


# ── 6. «Заново» -> подтверждение с числом ответов; согласие удаляет черновик, стартует заново ─

def test_reg_resume_restart_shows_confirm_with_count(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_new_draft(USER_ID, patch={"age": "22", "phone": "+7999"})
        from handlers import reg_resume
        callback = _FakeCallback("reg_resume:restart", USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg_resume.reg_resume_restart(callback, state)
        return callback.message

    msg = asyncio.run(go())
    texts = _texts(msg)
    assert any("2" in (t or "") for t in texts)


def test_reg_resume_restart_yes_deletes_draft_and_restarts(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_new_draft(USER_ID, patch={"age": "22"})
        from handlers import reg_resume
        callback = _FakeCallback("reg_resume:restart_yes", USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg_resume.reg_resume_restart_yes(callback, state, bot=object())
        draft_after = await db.get_reg_draft(USER_ID)
        return draft_after, callback.message

    draft_after, msg = asyncio.run(go())
    # старый черновик стёрт (не тот же answers, что был), а не оставлен как есть -- анкета
    # стартовала ЗАНОВО, _start_registration_flow тут же завела свежий пустой черновик
    assert draft_after is not None
    assert draft_after["answers"] == {}
    assert msg.sent  # анкета стартовала заново (приветствие/первый шаг)


# ── 7. TTL: kind='new' истекает, kind='edit' -- нет ─────────────────────────────────────────

def test_stale_new_draft_not_offered(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_resume_ttl_hours", "24")
        await _seed_new_draft(USER_ID)
        stale = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_drafts SET created_at = ? WHERE telegram_id = ?", (stale, USER_ID)
            )
            await conn.commit()
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    inline = _inline_kb_msgs(msg)
    assert not any("reg_resume:continue" in _callback_datas(rm) for (_, rm, _) in inline)


def test_stale_edit_draft_still_offered(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.add_user({
            "telegram_id": USER_ID, "full_name": "Тест Тестов", "username": "delegate",
            "registration_date": "2026-08-01 10:00:00", "season": "YL'26",
        })
        await db.set_user_status(USER_ID, "approved")
        await db.set_setting("event_season", "YL'26")
        stale = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        await db.upsert_reg_draft(
            USER_ID, kind="edit", participant_type="full", step="phone",
            patch={"phone": "+7999"}, source="bot",
        )
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_drafts SET created_at = ? WHERE telegram_id = ?", (stale, USER_ID)
            )
            await conn.commit()
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    inline = _inline_kb_msgs(msg)
    assert any("reg_resume:continue" in _callback_datas(rm) for (_, rm, _) in inline)


# ── 8. ?start=continue / ?start=edit -- распознаются, эксклюзивны с прочими токенами ────────

def test_extract_resume_arg_recognizes_tokens():
    assert reg._extract_resume_arg("continue") == "continue"
    assert reg._extract_resume_arg("edit") == "edit"
    assert reg._extract_resume_arg(None) is None
    assert reg._extract_resume_arg("") is None
    assert reg._extract_resume_arg("editorial") is None


def test_extract_resume_arg_mutually_exclusive_with_other_tokens():
    assert reg._extract_resume_arg("123456") is None
    assert reg._extract_resume_arg("src_vk") is None
    assert reg._extract_resume_arg("party_over") is None
    assert reg._extract_referrer_id("continue", 1) is None
    assert reg._extract_source_tag("continue") is None
    assert reg._extract_party_track("continue") is None


def test_start_continue_arg_offers_resume_even_without_ttl_freshness(tmp_path):
    """?start=continue -- синоним, работает даже если черновик формально протух по TTL."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_resume_ttl_hours", "1")
        await _seed_new_draft(USER_ID)
        stale = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_drafts SET created_at = ? WHERE telegram_id = ?", (stale, USER_ID)
            )
            await conn.commit()
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand("continue"))
        return msg

    msg = asyncio.run(go())
    inline = _inline_kb_msgs(msg)
    assert any("reg_resume:continue" in _callback_datas(rm) for (_, rm, _) in inline)


# ── 9. reg_started / догонялка не сломаны (регресс-смоук) ──────────────────────────────────

def test_reg_started_dropout_snapshot_unaffected(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        await db.set_setting("reg_q_phone", "on")
        await db.mark_reg_started(USER_ID, "delegate", "full", None)
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", _draft_kind="new", age="20")
        # "age" -> следующий включённый шаг ("phone") задаётся через _ask_step_or_recall,
        # который и стемпит reg_started.partial_data (_stamp_reg_step) -- отдельно от
        # reg_drafts, только регресс-смоук, что этот путь не сломан синхроном черновика.
        await reg._advance("age", msg, state, bot=object())
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT partial_data FROM reg_started WHERE telegram_id = ?", (USER_ID,)
            ) as cur:
                return await cur.fetchone()

    row = asyncio.run(go())
    assert row is not None and row[0] is not None


# ── 10. Активный в приложении сейчас -> не в кандидатах догонялки; со старым -- в кандидатах ─

def test_active_in_app_excluded_from_nudge_candidates(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.mark_reg_started(USER_ID, "delegate", "full", None)
        await db.mark_reg_started(OTHER_ID, "other", "full", None)
        old_cutoff = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_started SET started_at = ? WHERE telegram_id IN (?, ?)",
                (old_cutoff, USER_ID, OTHER_ID),
            )
            await conn.commit()
        # USER_ID активен в приложении ПРЯМО СЕЙЧАС
        await db.upsert_reg_draft(USER_ID, kind="new", patch={"age": "20"}, source="miniapp")
        await db.touch_reg_draft_activity(USER_ID)
        cutoff = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        return await db.get_nudge_candidates(cutoff)

    candidates = asyncio.run(go())
    assert USER_ID not in candidates
    assert OTHER_ID in candidates


# ── 11. Отмена анкеты удаляет черновик ──────────────────────────────────────────────────────

def test_cancel_registration_deletes_draft(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_new_draft(USER_ID)
        callback = _FakeCallback("reg_cancel_yes", USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.set_state(Registration.age)
        await reg_flow.cancel_registration_confirm(callback, state)
        return await db.get_reg_draft(USER_ID)

    draft_after = asyncio.run(go())
    assert draft_after is None


# ── _stamp_reg_step: активность + шаг в reg_drafts (fail-soft, шов активности D-21) ─────────

def test_stamp_reg_step_writes_draft_step_and_touches_activity(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", _draft_kind="new")
        await reg._stamp_reg_step("age", msg, state, {"participant_type": "full", "_draft_kind": "new"})
        return await db.get_reg_draft(USER_ID)

    draft = asyncio.run(go())
    assert draft is not None
    assert draft["step"] == "age"


# ── Сбой записи черновика не блокирует вопрос делегату (fail-soft) ─────────────────────────

def test_sync_draft_out_failure_is_soft(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reg, "upsert_reg_draft", boom)

    async def go():
        await db.set_setting("reg_q_age", "on")
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", _draft_kind="new", age="20")
        # must not raise
        await reg._advance("age", msg, state, bot=object())
        return msg

    msg = asyncio.run(go())
    assert msg.sent  # вопрос всё равно задан (следующий шаг или confirm)


# ── UAT 21-12 round 2, находка: ФИО обязано попасть в общий reg_drafts ─────────────────────
# ФИО спрашивается ДО движка REG_FLOW-шагов (_ask_full_name/process_full_name/_after_full_name),
# поэтому _advance/_sync_draft_out его никогда не видит — единственная функция, которая пишет
# ответ в reg_drafts.answers. `_start_registration_flow` уже создаёт строку reg_drafts ДО того,
# как задан вопрос про ФИО, так что finalize_registration находит именно её (без fallback на
# живой FSM dict, где full_name как раз есть) — и add_user() пишет пустую строку.

def test_after_full_name_syncs_answer_into_shared_draft(tmp_path, monkeypatch):
    """`_after_full_name` — общий хвост и для набранного текста (process_full_name), и для
    «Оставить» прошлого ФИО (recall_keep:full_name, D-26) — синхрон должен жить именно здесь,
    одной точкой на оба вызывающих, тем же приёмом, что `_advance` использует для остальных
    шагов."""
    _use_tmp_db(tmp_path)
    finalize_calls = []

    async def fake_finalize(message, state, bot):
        finalize_calls.append(1)

    monkeypatch.setattr(reg, "finalize_registration", fake_finalize)

    async def go():
        # Черновик уже существует к этому моменту — тот же порядок, что производит
        # _start_registration_flow (план 21-09): создан ДО вопроса про ФИО, без единого ответа.
        await db.upsert_reg_draft(USER_ID, kind="new", participant_type="short", source="bot")
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="short", _draft_kind="new", full_name="Иванов Иван")
        await reg._after_full_name(msg, state, bot=object())
        return await db.get_reg_draft(USER_ID)

    draft = asyncio.run(go())
    assert len(finalize_calls) == 1  # short-трек без включённых __short вопросов финализирует сразу
    assert draft is not None
    assert draft["answers"].get("full_name") == "Иванов Иван"


def test_short_track_new_registration_saves_full_name_to_users(tmp_path, monkeypatch):
    """Конец в конец: совсем новый делегат коротким треком вводит ФИО текстом -> сразу финал
    (нет включённых __short вопросов) -> users.full_name обязано содержать введённое имя, не
    пустую строку (`database.db.add_user` дефолтит `full_name` на `''`, если его нет в
    answers — реальный симптом UAT round 2)."""
    _use_tmp_db(tmp_path, "short_full_name.db")
    named_calls = []
    main_calls = []

    async def fake_named(tab_name, data):
        named_calls.append((tab_name, data))

    async def fake_main(data):
        main_calls.append(data)

    monkeypatch.setattr(reg, "append_to_named_sheet", fake_named)
    monkeypatch.setattr(reg, "append_to_sheet", fake_main)

    async def go():
        await db.set_setting("registration_mode", "short")
        await db.set_setting("short_approval", "manual")
        # Тот же порядок, что производит _start_registration_flow: черновик создан ДО ФИО.
        await db.upsert_reg_draft(OTHER_ID, kind="new", participant_type="short", source="bot")
        msg = _KBCapturingMessage(OTHER_ID, "delegate", text="Новый Делегат")
        state = _new_state(OTHER_ID)
        await state.update_data(participant_type="short", _draft_kind="new")
        await reg_steps.process_full_name(msg, state, bot=None)
        return await db.get_user(OTHER_ID)

    user = asyncio.run(go())
    assert user is not None
    assert user["full_name"] == "Новый Делегат"
