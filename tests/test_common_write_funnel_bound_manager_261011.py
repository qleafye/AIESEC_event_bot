"""Общий ключ (одно значение на все города) менеджер, привязанный к городу, не меняет ни одним
путём бота: проверка живёт в самой воронке записи (`settings_audit.set_setting_by_admin` /
`delete_setting_by_admin`), а не в каждом хендлере. Воронка бросает `CommonSettingDenied`, хендлер
прерывается до «сохранено», а объяснение показывает обработчик ошибок, который `main.py` ставит
раньше общего. Суперадмин и менеджер без привязки пишут, как раньше; свой город — как раньше."""
import asyncio
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import domain.cities as cities
import domain.settings.ops as settings_ops
import main
from database import db
from handlers.amb import admin_amb_section as amb
from handlers.settings import admin_settings as st
from handlers.settings import admin_settings_audit as sa
from handlers.settings import admin_settings_global as gscope
from handlers.states import AmbSlotsEdit, EditSetting
from services.settings import audit
from tests.test_admin_sections_ia20 import FakeCallback
from tests.test_common_settings_bound_manager_261010 import _bound_manager, _state
from tests.test_roles_phase8 import ADMIN_ID, MANAGER_ID

DENIED = settings_ops.COMMON_DENIED_TEXT


class Msg:
    def __init__(self, uid, text=None, photo=None, document=None, caption=None):
        self.from_user = SimpleNamespace(id=uid)
        self.text = text
        self.caption = caption
        self.html_text = caption or text
        self.photo = [SimpleNamespace(file_id=photo)] if photo else None
        self.document = document
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


def _pdf(file_id="pdf-1"):
    return SimpleNamespace(file_id=file_id, mime_type="application/pdf", file_name="a.pdf", file_size=10)


async def _dispatch(handler, event, *args):
    """Как в боте: отказ воронки ловит обработчик ошибок из `main.register_error_handlers`."""
    try:
        await handler(event, *args)
    except audit.CommonSettingDenied:
        is_cb = hasattr(event, "data")
        update = SimpleNamespace(callback_query=event if is_cb else None, message=None if is_cb else event)
        state = next((a for a in args if isinstance(a, FSMContext)), None)
        assert await gscope.on_common_setting_denied(SimpleNamespace(update=update), state) is True


async def _state_with(uid, state_value, data):
    state = _state(uid)
    await state.set_state(state_value)
    await state.set_data(data)
    return state


# ── пути записи: (ключ, значение до нажатия, нажатие) ────────────────────────────────────────

async def _cb(handler, data, uid, *args):
    cb = FakeCallback(data, user_id=uid)
    await _dispatch(handler, cb, *args)
    return cb, None


async def _approval_go(uid):
    cb = FakeCallback("approval_auto_go:full_approval", user_id=uid)
    cb.from_user.username, cb.from_user.full_name = None, "Менеджер"
    await _dispatch(sa.approval_auto_go, cb, SimpleNamespace())
    return cb, None


async def _sheets_tab_confirm(uid):
    state = await _state_with(uid, None, {"pending_tab_key": "polls_sheet_tab", "pending_tab_value": "Новые опросы"})
    cb, _ = await _cb(st.sheets_tab_confirm_go, "sheets_tab_confirm", uid, state)
    return cb, state


async def _amb_slots(uid):
    state = await _state_with(uid, AmbSlotsEdit.waiting_for_limit, {})
    msg = Msg(uid, text="7")
    await _dispatch(amb.amb_limit_value, msg, state)
    return msg, state


async def _photo(uid):
    state = await _state_with(uid, EditSetting.waiting_for_photo, {"photo_setting": "program"})
    msg = Msg(uid, photo="photo-1", caption="Программа")
    await _dispatch(st.settings_receive_photo, msg, state)
    return msg, state


async def _file_photo(uid):
    state = await _state_with(uid, EditSetting.waiting_for_file, {"file_setting": "reg_bonus"})
    msg = Msg(uid, photo="photo-2")
    await _dispatch(st.settings_receive_file_photo, msg, state)
    return msg, state


async def _file_doc(uid):
    state = await _state_with(uid, EditSetting.waiting_for_file, {"file_setting": "reg_bonus"})
    msg = Msg(uid, document=_pdf("doc-1"))
    await _dispatch(st.settings_receive_file_doc, msg, state)
    return msg, state


async def _consent_pdf(uid):
    state = await _state_with(uid, EditSetting.waiting_for_file, {"raw_file_key": "consent_pdf_pd"})
    msg = Msg(uid, document=_pdf("doc-2"))
    await _dispatch(st.settings_receive_file_doc, msg, state)
    return msg, state


PATHS = {
    "модерация полной формы": ("full_approval", "auto", lambda u: _cb(st.toggle_full_approval, "settings_toggle_full_approval", u)),
    "подтверждение «авто»": ("full_approval", "manual", _approval_go),
    "ВУЗ/курс только студентам": ("edu_conditional", None, lambda u: _cb(st.toggle_edu_conditional, "toggle_edu_conditional", u)),
    "нумерация вопросов": ("reg_show_progress", None, lambda u: _cb(st.toggle_show_progress, "toggle_show_progress", u)),
    "напоминания об оплате": ("payment_reminders_enabled", None, lambda u: _cb(st.toggle_payment_reminders, "toggle_payment_reminders", u)),
    "режим выбора вуза": ("reg_university_mode", None, lambda u: _cb(st.toggle_uni_mode, "toggle_uni_mode", u)),
    "имена в рейтинге волны": ("wave_rating_show_names", None, lambda u: _cb(st.toggle_wave_rating_show_names, "toggle_wave_rating_show_names", u)),
    "баллы анкеты": ("reg_scoring_enabled", None, lambda u: _cb(st.toggle_reg_scoring_enabled, "toggle_reg_scoring_enabled", u)),
    "уведомления о заявках": ("pending_notify_mode", None, lambda u: _cb(st.toggle_notify_mode, "settings_toggle_notify", u)),
    "бонус за регистрацию": ("reg_bonus_enabled", None, lambda u: _cb(st.toggle_bonus, "settings_toggle_bonus", u)),
    "перезапись вкладки": ("polls_sheet_tab", None, _sheets_tab_confirm),
    "вход в амбассадоры": ("amb_join_mode", "instant", lambda u: _cb(amb.amb_mode_apply, "ambs_mode_go:selection", u)),
    "лимит амбассадоров": ("amb_slots_limit", None, _amb_slots),
    "фото программы": ("program_photo_file_id", None, _photo),
    "фото бонуса файлом": ("reg_bonus_photo_file_id", None, _file_photo),
    "файл бонуса": ("reg_bonus_doc_file_id", None, _file_doc),
    "PDF согласия": ("consent_pdf_pd", None, _consent_pdf),
}


@pytest.fixture
def quiet_notify(monkeypatch):
    async def _no_send(*a, **k):
        return None

    monkeypatch.setattr(sa, "notify_by_capability", _no_send)


@pytest.mark.parametrize("name", list(PATHS))
def test_bound_manager_gets_explanation_and_common_key_stays(tmp_path, quiet_notify, name):
    key, before, press = PATHS[name]
    _bound_manager(tmp_path)
    if before is not None:
        asyncio.run(db.set_setting(key, before))
    event, state = asyncio.run(press(MANAGER_ID))
    assert asyncio.run(db.get_setting(key)) == before
    if isinstance(event, Msg):
        assert event.answers == [DENIED]  # ни «сохранено», ни экрана после
        assert asyncio.run(state.get_state()) is None  # ввод не ждёт следующего отказа
    else:
        assert event.answers == [(DENIED, True)]
        assert event.message.edit_calls == 0


@pytest.mark.parametrize("name", list(PATHS))
def test_superadmin_writes_through_every_path(tmp_path, quiet_notify, name):
    key, before, press = PATHS[name]
    _bound_manager(tmp_path)
    if before is not None:
        asyncio.run(db.set_setting(key, before))
    event, _ = asyncio.run(press(ADMIN_ID))
    assert asyncio.run(db.get_setting(key)) not in (None, before)
    answers = event.answers
    assert (DENIED, True) not in answers and DENIED not in answers


@pytest.mark.parametrize("handler, data", [
    (st.settings_photo_start, "settings_photo:speakers"),  # «program» при городе в шапке — своё фото города
    (st.settings_file_start, "settings_file:reg_bonus"),
    (st.consent_pdf_set, "consent_pdf_set:pd"),
])
def test_bound_manager_is_not_asked_for_a_file_that_would_not_be_saved(tmp_path, handler, data):
    _bound_manager(tmp_path)
    state = _state(MANAGER_ID)
    cb = FakeCallback(data, user_id=MANAGER_ID)
    asyncio.run(handler(cb, state))
    assert cb.answers == [(DENIED, True)]
    assert asyncio.run(state.get_state()) is None


def test_unbound_manager_writes_common_and_bound_writes_own_city(tmp_path):
    code = _bound_manager(tmp_path, bound=False)
    asyncio.run(audit.set_setting_by_admin(MANAGER_ID, "reg_bonus_enabled", "on"))
    assert asyncio.run(db.get_setting("reg_bonus_enabled")) == "on"

    asyncio.run(db.set_staff_city(MANAGER_ID, code))
    composed = cities.per_city_key("registration_mode", code)
    asyncio.run(audit.set_setting_by_admin(MANAGER_ID, composed, "short"))
    asyncio.run(audit.delete_setting_by_admin(MANAGER_ID, composed))
    with pytest.raises(audit.CommonSettingDenied):
        asyncio.run(audit.delete_setting_by_admin(MANAGER_ID, "reg_bonus_enabled"))
    assert asyncio.run(db.get_setting("reg_bonus_enabled")) == "on"


def test_bound_manager_writes_common_when_cities_module_is_off(tmp_path):
    _bound_manager(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "off"))
    asyncio.run(audit.set_setting_by_admin(MANAGER_ID, "reg_bonus_enabled", "on"))
    assert asyncio.run(db.get_setting("reg_bonus_enabled")) == "on"


def test_bot_error_handlers_explain_before_the_generic_one(tmp_path, monkeypatch):
    """Настоящий диспетчер: обработчик ошибок отказа стоит раньше общего (тот бы промолчал)."""
    _bound_manager(tmp_path)
    shown = []

    async def _answer(self, text=None, show_alert=None, **kw):
        shown.append((text, show_alert))

    monkeypatch.setattr(CallbackQuery, "answer", _answer)
    router = Router()

    @router.callback_query(F.data == "flip")
    async def flip(callback: CallbackQuery):
        await audit.set_setting_by_admin(callback.from_user.id, "reg_bonus_enabled", "on")
        await callback.answer("✅ Сохранено")

    dp = Dispatcher()
    main.register_error_handlers(dp)
    dp.include_router(router)
    user = User(id=MANAGER_ID, is_bot=False, first_name="М")
    chat = Chat(id=MANAGER_ID, type="private")
    msg = Message(message_id=1, date=1700000000, chat=chat, from_user=user, text="экран")
    update = Update(update_id=1, callback_query=CallbackQuery(
        id="q1", from_user=user, chat_instance="c", data="flip", message=msg))

    async def run():
        bot = Bot("123456:dummy-test-token")
        try:
            await dp.feed_update(bot, update)
        finally:
            await bot.session.close()

    asyncio.run(run())
    assert shown == [(DENIED, True)]
    assert asyncio.run(db.get_setting("reg_bonus_enabled")) is None
