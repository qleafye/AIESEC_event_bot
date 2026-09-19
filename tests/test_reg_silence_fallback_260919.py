"""Квик 260919-u7e, пункт 2 (находка #3 аудита прода,
`.planning/review-260919-sections/01-reg-chat.md`): бот молчит, если рестарт контейнера снёс
FSM (MemoryStorage) посреди анкеты делегата. `reg_drafts` персистентен — ответы не потеряны,
но следующее сообщение делегата (или тап по старой inline-кнопке анкеты) не ловило НИ ОДИН
хендлер: единственный `@router.message()` без фильтра состояния (`handlers/group_chat.py`)
отфильтрован по группам. 14 из 38 делегатов, оказавшихся в анкете за 20 минут до рестарта
(05-16.09), не вернулись ни разу.

Механизм фикса (см. докстринг `handlers/reg_silence_fallback.py`): `handlers/user_actions.py::
reg_handoff_idle_fallback` (`StateFilter(None), F.text`) — уже существующий ПОСЛЕДНИЙ
message-хендлер в `user_actions.router`, единственная реально достижимая точка приватного
text-пайплайна без состояния (аiogram останавливает апдейт на первом совпавшем фильтре в
цепочке роутеров -- отдельный роутер этого модуля, включённый ПОСЛЕ user_actions.router в
main.py, никогда не увидел бы ни одного текстового апдейта). Текстовая ветка поэтому расширяет
именно эту функцию; `reg_silence_fallback.router` несёт нетекстовые сообщения (документ/фото)
и tap по устаревшей inline-кнопке (callback_query — здесь конкурентов нет, ни один роутер
раньше не держит безусловного catch-all по callback_query).

Стиль — тот же приём, что `tests/test_reg_handoff_260904.py` (`_FakeMessage2`/`_FakeCallback2`,
direct handler calls, `config.DB_PATH` в tmp_path); pytest-asyncio недоступен, async через
asyncio.run()."""
import asyncio
import time

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from config import config
from database import db
from handlers import reg_silence_fallback
from handlers import registration as registration_mod
from handlers import user_actions as user_actions_mod
from handlers.states import Registration
from handlers.user_actions import reg_handoff_idle_fallback
# Router — aiogram-singleton (T-2091 Pitfall): a real Router object can be attached to exactly
# ONE Dispatcher for the lifetime of the process (`router.parent_router` setter raises
# RuntimeError on a second attach). tests/test_refac_snapshot_260816.py already owns the one
# process-wide Dispatcher that includes registration.router/user_actions.router (its own
# `_full_dispatcher()`, module-level-cached) — a second, competing Dispatcher built here with
# the SAME router objects would collide with that file's test whenever both land in the same
# pytest-xdist worker. Reusing that cached instance (and extending it with
# reg_silence_fallback.router exactly once) is therefore not a style choice, it is the only
# safe way to prove real cross-router dispatch for this module.
from tests.test_refac_snapshot_260816 import _full_dispatcher, _spied

UID = 841001
ADMIN_UID = 841002
STAFF_UID = 841003


def _use_tmp_db(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_UID]


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, uid, username=None, text=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.text = text
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, self.from_user.username, text=self.text)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeDocMessage(_FakeMessage):
    """`_FakeMessage` без текста, но с документом -- имитирует «прислал резюме файлом снова
    после рестарта»; `F.chat.type == "private"` матчит по `.chat.type`, которого у
    `_FakeMessage` нет -- добавляем."""

    def __init__(self, uid, username=None):
        super().__init__(uid, username, text=None)
        self.document = object()


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _FakeMessage(user_id, username)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


def _texts(msg):
    return [t for (t, _, _) in msg.sent]


def _kb_msgs(msg):
    return [(t, rm, p) for (t, rm, p) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


async def _seed_fresh_new_draft(uid):
    return await db.upsert_reg_draft(
        uid, kind="new", participant_type="full", source="bot",
        patch={"age": "20"}, active_surface="bot",
    )


# ── 1. Живой черновик, бот держит владение -- предлагает продолжить (находка #3 закрыта) ──────

def test_idle_fallback_offers_resume_after_restart_when_draft_held_by_bot(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_offer.db")

    async def go():
        await _seed_fresh_new_draft(UID)
        msg = _FakeMessage(UID, "delegate", text="20")  # делегат отвечает как ни в чём не бывало
        await reg_handoff_idle_fallback(msg)
        return msg

    msg = asyncio.run(go())
    texts = _texts(msg)
    assert any("перезапускался" in (t or "") for t in texts), (
        "БАГ находки #3: бот молчит вместо «Бот перезапускался...» -- делегат отвечает в никуда"
    )
    inline = _kb_msgs(msg)
    assert inline, "следом обязан идти обычный экран «Продолжить/Заново» (offer_resume)"
    datas = [btn.callback_data for row in inline[-1][1].inline_keyboard for btn in row]
    assert "reg_resume:continue" in datas
    assert "reg_resume:restart" in datas


# ── 2. Нет черновика вообще -- тишина, поведение НЕ изменилось ─────────────────────────────────

def test_idle_fallback_stays_silent_without_any_draft(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_no_draft.db")

    async def go():
        msg = _FakeMessage(UID, "delegate", text="привет")
        await reg_handoff_idle_fallback(msg)
        return msg

    msg = asyncio.run(go())
    assert msg.sent == []


# ── 3. Регресс: черновик держит приложение -- прежняя «плита», не экран рестарта ───────────────

def test_idle_fallback_app_held_draft_still_shows_handoff_plate(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_app_held.db")

    async def go():
        await db.upsert_reg_draft(
            UID, kind="new", participant_type="full", source="miniapp",
            patch={"full_name": "Иван"}, active_surface="app",
        )
        msg = _FakeMessage(UID, "delegate", text="привет")
        await reg_handoff_idle_fallback(msg)
        return msg

    msg = asyncio.run(go())
    texts = _texts(msg)
    assert texts, "плита эстафеты (владение у приложения) не должна пропасть"
    assert not any("перезапускался" in (t or "") for t in texts), (
        "владение у приложения -- это НЕ повод показывать текст про рестарт бота"
    )


# ── 4. TTL не тронут: протухший kind='new' черновик -- тишина, как и раньше ────────────────────

def test_idle_fallback_respects_ttl_stale_new_draft_stays_silent(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_stale.db")

    async def go():
        await db.set_setting("reg_resume_ttl_hours", "1")
        await _seed_fresh_new_draft(UID)
        from datetime import datetime, timedelta
        stale = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_drafts SET created_at = ? WHERE telegram_id = ?", (stale, UID)
            )
            await conn.commit()
        msg = _FakeMessage(UID, "delegate", text="20")
        await reg_handoff_idle_fallback(msg)
        return msg

    msg = asyncio.run(go())
    assert msg.sent == []


# ── 5. Админ -- никогда не видит делегатский экран, даже с живым черновиком ────────────────────

def test_offer_if_resumable_silent_for_admin_even_with_live_draft(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_admin.db")

    async def go():
        await _seed_fresh_new_draft(ADMIN_UID)
        msg = _FakeMessage(ADMIN_UID, "admin", text="проверка")
        await reg_handoff_idle_fallback(msg)
        return msg

    msg = asyncio.run(go())
    assert msg.sent == []


# ── 6. Держатель произвольной роли (staff, не ADMIN_IDS) -- тоже тишина ────────────────────────

def test_offer_if_resumable_silent_for_staff_role_even_with_live_draft(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_staff.db")

    async def go():
        await db.add_staff(STAFF_UID, "reg_manager", ADMIN_UID)
        await _seed_fresh_new_draft(STAFF_UID)
        msg = _FakeMessage(STAFF_UID, "manager", text="проверка")
        await reg_handoff_idle_fallback(msg)
        return msg

    msg = asyncio.run(go())
    assert msg.sent == []


# ── 7. Нетекстовое сообщение (документ) -- ловит reg_silence_fallback.router напрямую ──────────

def test_nontext_message_offers_resume(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_doc.db")

    async def go():
        await _seed_fresh_new_draft(UID)
        msg = _FakeDocMessage(UID, "delegate")
        await reg_silence_fallback.catch_silent_nontext_message(msg)
        return msg

    msg = asyncio.run(go())
    assert any("перезапускался" in (t or "") for t in _texts(msg))
    assert _kb_msgs(msg)


# ── 8. Устаревшая inline-кнопка анкеты (callback) -- гасит "часики" И предлагает продолжить ────

def test_stale_callback_offers_resume_and_answers(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_cb.db")

    async def go():
        await _seed_fresh_new_draft(UID)
        cb = _FakeCallback("recall_keep:vk", UID, "delegate")  # кнопка из ДО-рестартной сессии
        await reg_silence_fallback.catch_stale_callback(cb)
        return cb

    cb = asyncio.run(go())
    assert len(cb.answers) == 1  # "часики" погашены
    assert any("перезапускался" in (t or "") for t in _texts(cb.message))
    assert _kb_msgs(cb.message)


# ── 9. Устаревшая кнопка БЕЗ черновика -- всё равно гасит "часики", ничего не пишет ────────────

def test_stale_callback_without_draft_still_answers_but_silent_message(tmp_path):
    _use_tmp_db(tmp_path, "silence_fallback_cb_nodraft.db")

    async def go():
        cb = _FakeCallback("consent_accept:personal_data", UID, "delegate")
        await reg_silence_fallback.catch_stale_callback(cb)
        return cb

    cb = asyncio.run(go())
    assert len(cb.answers) == 1
    assert cb.message.sent == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# 10-13. Полная цепочка роутеров (registration -> user_actions -> reg_silence_fallback), тот
# же приём, что tests/test_refac_snapshot_260816.py::test_feed_update_smoke_cross_router_first_
# match -- проверяет РЕАЛЬНЫЙ порядок аiogram, не прямой вызов функции. Админ/платёжный/
# групповой роутеры сюда не нужны: их порядок и фильтры этим квиком не тронуты, отдельно
# закреплены тем файлом.
# ══════════════════════════════════════════════════════════════════════════════════════════

_EXTENDED_ONCE = {"done": False}


def _dispatcher():
    """Переиспользует process-wide кэшированный Dispatcher из
    tests/test_refac_snapshot_260816.py::_full_dispatcher (admin -> payment -> registration ->
    user_actions, реальный порядок main.py) и дополняет его reg_silence_fallback.router ОДИН
    РАЗ за процесс — см. предупреждение у импорта наверху файла про router-синглтон."""
    dp = _full_dispatcher()
    if not _EXTENDED_ONCE["done"]:
        dp.include_router(reg_silence_fallback.router)
        _EXTENDED_ONCE["done"] = True
    return dp


def _msg_update(update_id, text, user_id):
    user = User(id=user_id, is_bot=False, first_name="Test")
    chat = Chat(id=user_id, type="private")
    msg = Message(message_id=update_id, date=int(time.time()), chat=chat, from_user=user, text=text)
    return Update(update_id=update_id, message=msg)


def _cb_update(update_id, data, user_id):
    user = User(id=user_id, is_bot=False, first_name="Test")
    chat = Chat(id=user_id, type="private")
    msg = Message(message_id=update_id, date=int(time.time()), chat=chat, from_user=user, text="stub")
    cb = CallbackQuery(id=str(update_id), from_user=user, chat_instance="test", data=data, message=msg)
    return Update(update_id=update_id, callback_query=cb)


# Каждому dispatcher-тесту -- СВОЙ telegram_id (не общий UID): Dispatcher/FSM storage,
# построенные `_dispatcher()`, кэшируются на весь процесс (см. докстринг выше про
# router-синглтон) -- общий id между тестами сделал бы их порядко-зависимыми через утечку
# FSM-состояния одного теста в другой, тот же класс хрупкости, которого избегает сам кэш.
DISPATCH_UID_STATE = UID + 100
DISPATCH_UID_MENU = UID + 101
DISPATCH_UID_OFFER = UID + 102
DISPATCH_UID_CB = UID + 103


def test_active_registration_state_wins_over_fallback(tmp_path):
    """Живой шаг анкеты -- фолбэк НЕ вмешивается (StateFilter(None) исключает активную сессию)."""
    _use_tmp_db(tmp_path, "silence_fallback_dispatch_state.db")

    async def go():
        await db.set_setting("reg_q_age", "on")
        await _seed_fresh_new_draft(DISPATCH_UID_STATE)
        dp = _dispatcher()
        bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        try:
            fsm = dp.fsm.resolve_context(bot, chat_id=DISPATCH_UID_STATE, user_id=DISPATCH_UID_STATE)
            await fsm.set_state(Registration.age)
            await fsm.update_data(participant_type="full", _draft_kind="new")
            with _spied(registration_mod, "message", "process_age") as calls:
                await dp.feed_update(bot, _msg_update(1, "20", DISPATCH_UID_STATE))
                assert len(calls) == 1
        finally:
            await bot.session.close()

    asyncio.run(go())


def test_existing_stateless_menu_button_still_wins_over_fallback(tmp_path):
    """Существующая кнопка меню (свой стейтлесс-хендлер РАНЬШЕ в user_actions.router) не
    перехватывается новым хвостом -- ordering внутри роутера не тронут."""
    _use_tmp_db(tmp_path, "silence_fallback_dispatch_menu.db")

    async def go():
        # даже с живым черновиком кнопка меню остаётся кнопкой
        await _seed_fresh_new_draft(DISPATCH_UID_MENU)
        dp = _dispatcher()
        bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        try:
            with _spied(user_actions_mod, "message", "show_my_coins") as calls:
                await dp.feed_update(bot, _msg_update(2, "🪙 Мои монеты", DISPATCH_UID_MENU))
                assert len(calls) == 1
        finally:
            await bot.session.close()

    asyncio.run(go())


def test_no_state_no_other_match_live_draft_reaches_idle_fallback_through_real_chain(tmp_path):
    """Сквозная проверка находки #3: апдейт реально доезжает до reg_handoff_idle_fallback через
    registration.router (нет совпадения) -> user_actions.router (совпадение, последний
    хендлер), не через прямой вызов функции."""
    _use_tmp_db(tmp_path, "silence_fallback_dispatch_offer.db")

    async def go():
        await _seed_fresh_new_draft(DISPATCH_UID_OFFER)
        dp = _dispatcher()
        bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        try:
            result = await dp.feed_update(bot, _msg_update(3, "20", DISPATCH_UID_OFFER))
            from aiogram.dispatcher.event.bases import UNHANDLED
            assert result is not UNHANDLED
        finally:
            await bot.session.close()

    asyncio.run(go())


def test_stale_callback_reaches_reg_silence_fallback_through_real_chain(tmp_path):
    """Тап по неизвестному/устаревшему callback_data без состояния доезжает до самого хвоста
    цепочки (registration.router -> user_actions.router -> reg_silence_fallback.router).
    `unknown_stale_button:123` -- намеренно НЕ префикс ни одного реального фильтра (не
    `recall_keep:`/`consent_accept:`/т.п.), чтобы результат не зависел от того, какое
    FSM-состояние мог оставить другой тест в общем (кэшированном) хранилище."""
    _use_tmp_db(tmp_path, "silence_fallback_dispatch_cb.db")

    async def go():
        await _seed_fresh_new_draft(DISPATCH_UID_CB)
        dp = _dispatcher()
        bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        try:
            with _spied(reg_silence_fallback, "callback_query", "catch_stale_callback") as calls:
                await dp.feed_update(bot, _cb_update(4, "unknown_stale_button:123", DISPATCH_UID_CB))
                assert len(calls) == 1
        finally:
            await bot.session.close()

    asyncio.run(go())
