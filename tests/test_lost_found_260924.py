"""Идея №20 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): бюро находок.

Покрытие: права (без `checkin` нельзя открыть мастер, чужой город менеджера), шаги мастера
FSM (фото -> где нашли -> предпросмотр), публикация (мок бота: пост в чат делегатов города +
кнопка «✅ Нашёлся хозяин»), нет привязанного чата (человеческая ошибка), «✅ Нашёлся хозяин»
от постороннего в группе (отказ) и от держателя `moderate_reg` (без `checkin`, тоже можно),
повторное нажатие (идемпотентность).

pytest-asyncio недоступна — async через `asyncio.run()` (конвенция проекта). Дублирует свой
маленький диспетч-харнесс `handlers.admin.router.propagate_event` (не импортирует
`FakeMessage`/`FakeCallback` из `tests/test_roles_phase8.py` напрямую — тем не хватает
`answer_photo`/`edit_reply_markup`/`edit_caption`, нужных этому модулю), `_fresh_state` — общий.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import domain.cities as cities_mod
from config import config
from database import db
from handlers import admin as admin_mod
from handlers import admin_lost_found as alf  # noqa: F401 -- регистрирует lost_found_*/lostfound_*
from handlers.access.admin_caps import resolve_capabilities
from domain.settings.schema import get_setting_typed
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import _fresh_state

ADMIN_ID = 924401
MANAGER_ID = 924410  # moderate_reg
VOLUNTEER_ID = 924420  # checkin
STRANGER_ID = 924430  # без единого права
DELEGATE_CHAT_ID = -1009244001


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="lost_found.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _two_cities():
    return [
        {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 1, "sort_order": 1},
    ]


# ── Мини-харнесс: реальный router.propagate_event (CapabilityMiddleware включена) ───────────

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, text=None, photo=None, user_id=None, chat_id=None, message_id=None):
        self.message_id = message_id
        self.text = text
        self.photo = photo
        self.html_text = text
        self.from_user = FakeUser(user_id) if user_id is not None else None
        self.chat = FakeChat(chat_id if chat_id is not None else user_id)
        self.answers = []  # (text, parse_mode, reply_markup)
        self.photo_answers = []  # (photo, caption, parse_mode, reply_markup)
        self.edit_calls = 0
        self.edit_reply_markup_calls = []
        self.edit_caption_calls = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.photo_answers.append((photo, caption, parse_mode, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.edit_calls += 1

    async def edit_reply_markup(self, reply_markup=None):
        self.edit_reply_markup_calls.append(reply_markup)

    async def edit_caption(self, caption=None, reply_markup=None):
        self.edit_caption_calls.append((caption, reply_markup))


class FakeCallback:
    def __init__(self, data, user_id, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message or FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeBot:
    def __init__(self):
        self.sent_photos = []  # [(chat_id, photo, caption, message_id)]
        self.edited_markups = []  # [(chat_id, message_id, reply_markup)]
        self._next_message_id = 5000
        self.fail_send_photo_for = set()

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        if chat_id in self.fail_send_photo_for:
            raise RuntimeError("бот не состоит в чате")
        self._next_message_id += 1
        msg_id = self._next_message_id
        self.sent_photos.append((chat_id, photo, caption, msg_id))
        return SimpleNamespace(message_id=msg_id)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edited_markups.append((chat_id, message_id, reply_markup))


def _dispatch_callback(data, user_id, *, bot=None, state=None, message=None):
    if bot is None:
        bot = FakeBot()
    if state is None:
        state = _fresh_state(user_id)
    event = FakeCallback(data, user_id, message=message)
    kwargs = dict(
        event_from_user=FakeUser(user_id), bot=bot, raw_state=None, state=state, event_update=None,
    )
    result = asyncio.run(admin_mod.router.propagate_event("callback_query", event, **kwargs))
    return result, event, bot


def _dispatch_message(text, user_id, *, photo=None, raw_state=None, bot=None, state=None):
    if bot is None:
        bot = FakeBot()
    if state is None:
        state = _fresh_state(user_id)
    event = FakeMessage(text=text, photo=photo, user_id=user_id, chat_id=user_id)
    kwargs = dict(
        event_from_user=FakeUser(user_id), bot=bot, raw_state=raw_state, state=state, event_update=None,
    )
    result = asyncio.run(admin_mod.router.propagate_event("message", event, **kwargs))
    return result, event, bot


def _bind_chat(city):
    from services import chat_tracking
    _run(chat_tracking.bind_chat(None, DELEGATE_CHAT_ID, "Чат делегатов", city))


def _enable(city=None):
    from domain.cities import per_city_key
    if city:
        _run(db.set_setting(per_city_key("lost_found_enabled", city), "on"))
    else:
        _run(db.set_setting("lost_found_enabled", "on"))


# ══════════════════════════════════════════════════════════════════════════════════════════
# database/db.py: create/get/mark_returned — идемпотентность
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_create_and_get_lost_found_item(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_lost_found_item(
        "msk", "photo123", "Нашли у зала А, забрать на стойке 1", ADMIN_ID, DELEGATE_CHAT_ID, 42,
    ))
    item = _run(db.get_lost_found_item(item_id))
    assert item["city"] == "msk"
    assert item["photo_file_id"] == "photo123"
    assert item["where_text"] == "Нашли у зала А, забрать на стойке 1"
    assert item["posted_by"] == ADMIN_ID
    assert item["chat_id"] == DELEGATE_CHAT_ID
    assert item["message_id"] == 42
    assert item["returned_at"] is None
    assert item["returned_by"] is None


def test_get_lost_found_item_unknown_returns_none(tmp_path):
    _ready(tmp_path)
    assert _run(db.get_lost_found_item(999)) is None


def test_mark_lost_found_returned_first_call_wins(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_lost_found_item("msk", "p", "где-то", ADMIN_ID, DELEGATE_CHAT_ID, 1))
    assert _run(db.mark_lost_found_returned(item_id, MANAGER_ID)) is True
    item = _run(db.get_lost_found_item(item_id))
    assert item["returned_at"] is not None
    assert item["returned_by"] == MANAGER_ID


def test_mark_lost_found_returned_second_call_is_noop(tmp_path):
    _ready(tmp_path)
    item_id = _run(db.create_lost_found_item("msk", "p", "где-то", ADMIN_ID, DELEGATE_CHAT_ID, 1))
    _run(db.mark_lost_found_returned(item_id, MANAGER_ID))
    assert _run(db.mark_lost_found_returned(item_id, VOLUNTEER_ID)) is False
    item = _run(db.get_lost_found_item(item_id))
    assert item["returned_by"] == MANAGER_ID  # первый победитель, не перезаписан


def test_mark_lost_found_returned_unknown_id_is_false(tmp_path):
    _ready(tmp_path)
    assert _run(db.mark_lost_found_returned(999, ADMIN_ID)) is False


def test_lost_found_table_excluded_from_user_purge(tmp_path):
    """posted_by/returned_by — сотрудник, chat_id — группа делегатов, не личный чат: та же
    классификация, что у sos_card_copies (USER_PURGE_EXCLUDED, не USER_PURGE_TABLES)."""
    assert "lost_found" not in [t for t, _c, _g in db.USER_PURGE_TABLES]
    assert "lost_found" in db.USER_PURGE_EXCLUDED


# ══════════════════════════════════════════════════════════════════════════════════════════
# Права: без checkin нельзя, чужой город
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_entry_without_checkin_is_denied(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))  # только moderate_reg
    result, event, _bot = _dispatch_callback("lost_found_new", MANAGER_ID)
    assert event.answers  # denial toast
    assert "прав" in (event.answers[0][0] or "").lower()


def test_entry_with_checkin_only_is_allowed(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))  # ровно checkin
    _enable()
    result, event, _bot = _dispatch_callback("lost_found_new", VOLUNTEER_ID)
    assert not any(a[0] for a in event.answers)  # ни одного алерта с текстом отказа
    assert event.message.answers  # приглашение прислать фото


# `/found` — команда: aiogram.filters.Command.__call__ требует isinstance(message,
# types.Message) буквально (не duck-typed, в отличие от CapabilityMiddleware) — через этот
# duck-typed харнесс её не продиспетчить. Капа "cmd:found" == "checkin" (та же запись, что у
# callback-входа "lost_found_new") уже покрыта общим сторожем
# `tests.test_roles_phase8::test_completeness_every_admin_handler_resolves_to_a_capability` —
# здесь достаточно прямого вызова функции-хендлера на её собственную логику (тумблер).

def test_cmd_found_direct_call_disabled_says_disabled(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    # lost_found_enabled НЕ включён (дефолт off).
    message = FakeMessage(user_id=VOLUNTEER_ID, chat_id=VOLUNTEER_ID)
    state = _fresh_state(VOLUNTEER_ID)
    _run(alf.lost_found_cmd(message, state))
    assert any("выключено" in (a[0] or "") for a in message.answers)


def test_manager_bound_to_other_city_cannot_open_city_picker_pick(tmp_path):
    _ready(tmp_path)
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())
        _run(db.set_setting("event_city_enabled", "on"))
        _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
        _run(db.set_staff_city(VOLUNTEER_ID, "spb"))

        result, event, _bot = _dispatch_callback("lostfound_city_pick:msk", VOLUNTEER_ID)
        assert event.answers
        assert "правит суперадмин" in (event.answers[0][0] or "")
    finally:
        cities_mod.set_cities_for_test(saved)


def test_lostfound_cfg_screen_requires_moderate_reg_not_bare_checkin(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    result, event, _bot = _dispatch_callback("lostfound_cfg:_all", VOLUNTEER_ID)
    assert event.answers  # denial toast, не экран тумблера


# ══════════════════════════════════════════════════════════════════════════════════════════
# Тумблер выключен: /found и кнопка отвечают человеческим текстом, мастер не начинается
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_disabled_toggle_lost_found_new_says_disabled(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    result, event, _bot = _dispatch_callback("lost_found_new", VOLUNTEER_ID)
    assert any("выключено" in (a[0] or "") for a in event.message.answers)


def test_lostfound_toggle_flips_setting(tmp_path):
    _ready(tmp_path)
    _dispatch_callback("lostfound_toggle:_all", ADMIN_ID)
    assert _run(get_setting_typed("lost_found_enabled")) == "on"
    _dispatch_callback("lostfound_toggle:_all", ADMIN_ID)
    assert _run(get_setting_typed("lost_found_enabled")) == "off"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Шаги FSM: фото -> где нашли -> предпросмотр -> публикация (мок бота)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_full_wizard_flow_publishes_to_chat_with_button(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _enable()
    _bind_chat(None)
    state = _fresh_state(VOLUNTEER_ID)
    bot = FakeBot()

    _dispatch_callback("lost_found_new", VOLUNTEER_ID, state=state, bot=bot)
    _dispatch_message(
        None, VOLUNTEER_ID, photo=[FakePhotoSize("ph1")],
        raw_state="LostFoundNew:waiting_photo", state=state, bot=bot,
    )
    _dispatch_message(
        "Нашли у зала А, забрать на стойке 1", VOLUNTEER_ID,
        raw_state="LostFoundNew:waiting_where", state=state, bot=bot,
    )
    result, event, _bot2 = _dispatch_callback("lostfound_publish", VOLUNTEER_ID, state=state, bot=bot)

    assert len(bot.sent_photos) == 1
    chat_id, photo, caption, msg_id = bot.sent_photos[0]
    assert chat_id == DELEGATE_CHAT_ID
    assert photo == "ph1"
    assert "Нашли у зала А, забрать на стойке 1" in caption

    assert len(bot.edited_markups) == 1
    edited_chat_id, edited_msg_id, markup = bot.edited_markups[0]
    assert edited_chat_id == DELEGATE_CHAT_ID
    assert edited_msg_id == msg_id
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert len(cbs) == 1 and cbs[0].startswith("lostfound_return:")

    item_id = int(cbs[0].split(":", 1)[1])
    item = _run(db.get_lost_found_item(item_id))
    assert item["chat_id"] == DELEGATE_CHAT_ID
    assert item["message_id"] == msg_id
    assert item["posted_by"] == VOLUNTEER_ID


def test_wizard_photo_step_rejects_non_photo_and_stays_in_state(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _enable()
    state = _fresh_state(VOLUNTEER_ID)

    _dispatch_callback("lost_found_new", VOLUNTEER_ID, state=state)
    result, event, _bot = _dispatch_message(
        "текст вместо фото", VOLUNTEER_ID, raw_state="LostFoundNew:waiting_photo", state=state,
    )
    assert any("фото" in (a[0] or "").lower() for a in event.answers)
    assert _run(state.get_state()) == "LostFoundNew:waiting_photo"  # шаг не продвинулся


def test_wizard_where_step_rejects_empty_text(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _enable()
    state = _fresh_state(VOLUNTEER_ID)

    _dispatch_callback("lost_found_new", VOLUNTEER_ID, state=state)
    _dispatch_message(
        None, VOLUNTEER_ID, photo=[FakePhotoSize("ph1")],
        raw_state="LostFoundNew:waiting_photo", state=state,
    )
    result, event, _bot = _dispatch_message(
        "   ", VOLUNTEER_ID, raw_state="LostFoundNew:waiting_where", state=state,
    )
    assert any("не понял" in (a[0] or "").lower() for a in event.answers)
    assert _run(state.get_state()) == "LostFoundNew:waiting_where"


def test_wizard_cancel_mid_flow_clears_state(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _enable()
    state = _fresh_state(VOLUNTEER_ID)

    _dispatch_callback("lost_found_new", VOLUNTEER_ID, state=state)
    _dispatch_message(
        "Отмена", VOLUNTEER_ID, raw_state="LostFoundNew:waiting_photo", state=state,
    )
    assert _run(state.get_state()) is None


def test_publish_without_bound_chat_shows_human_error_and_no_db_row(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _enable()
    # Чат делегатов НЕ привязан.
    state = _fresh_state(VOLUNTEER_ID)
    bot = FakeBot()

    _dispatch_callback("lost_found_new", VOLUNTEER_ID, state=state, bot=bot)
    _dispatch_message(
        None, VOLUNTEER_ID, photo=[FakePhotoSize("ph1")],
        raw_state="LostFoundNew:waiting_photo", state=state, bot=bot,
    )
    _dispatch_message(
        "где-то", VOLUNTEER_ID, raw_state="LostFoundNew:waiting_where", state=state, bot=bot,
    )
    result, event, _bot2 = _dispatch_callback("lostfound_publish", VOLUNTEER_ID, state=state, bot=bot)

    assert not bot.sent_photos
    assert any("не привязан" in (a[0] or "") for a in event.message.answers)


def test_publish_send_failure_shows_human_error(tmp_path):
    """Бот выгнан из чата/чат недоступен — send_photo падает, находка не теряется молча."""
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _enable()
    _bind_chat(None)
    state = _fresh_state(VOLUNTEER_ID)
    bot = FakeBot()
    bot.fail_send_photo_for.add(DELEGATE_CHAT_ID)

    _dispatch_callback("lost_found_new", VOLUNTEER_ID, state=state, bot=bot)
    _dispatch_message(
        None, VOLUNTEER_ID, photo=[FakePhotoSize("ph1")],
        raw_state="LostFoundNew:waiting_photo", state=state, bot=bot,
    )
    _dispatch_message(
        "где-то", VOLUNTEER_ID, raw_state="LostFoundNew:waiting_where", state=state, bot=bot,
    )
    result, event, _bot2 = _dispatch_callback("lostfound_publish", VOLUNTEER_ID, state=state, bot=bot)

    assert any("не привязан" in (a[0] or "") for a in event.message.answers)
    items = _run(db.get_lost_found_item(1))
    assert items is None  # ничего не записано


# ══════════════════════════════════════════════════════════════════════════════════════════
# «✅ Нашёлся хозяин»: checkin ИЛИ moderate_reg, посторонний — отказ, повторный тап
# ══════════════════════════════════════════════════════════════════════════════════════════

def _seed_item():
    return _run(db.create_lost_found_item(
        "msk", "ph1", "где-то", VOLUNTEER_ID, DELEGATE_CHAT_ID, 777,
    ))


def _post(message_id=777):
    """Пост находки в группе делегатов — кнопка «Нашёлся хозяин» жмётся под ним."""
    return FakeMessage(chat_id=DELEGATE_CHAT_ID, message_id=message_id)


def test_return_button_by_checkin_holder_marks_returned_and_edits_caption(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    item_id = _seed_item()

    result, event, _bot = _dispatch_callback(f"lostfound_return:{item_id}", VOLUNTEER_ID, message=_post())

    item = _run(db.get_lost_found_item(item_id))
    assert item["returned_at"] is not None
    assert item["returned_by"] == VOLUNTEER_ID
    assert event.message.edit_caption_calls
    caption, markup = event.message.edit_caption_calls[0]
    assert caption == "✅ Вещь вернули владельцу"
    assert markup is None


def test_return_button_by_moderate_reg_without_checkin_also_allowed(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))  # только moderate_reg
    item_id = _seed_item()

    result, event, _bot = _dispatch_callback(f"lostfound_return:{item_id}", MANAGER_ID, message=_post())

    item = _run(db.get_lost_found_item(item_id))
    assert item["returned_by"] == MANAGER_ID


def test_return_button_by_stranger_in_group_is_denied(tmp_path):
    """Посторонний в чате делегатов (ни checkin, ни moderate_reg) — молчаливый отказ (D-14),
    находка не закрывается."""
    _ready(tmp_path)
    item_id = _seed_item()

    from aiogram.dispatcher.event.bases import UNHANDLED
    result, event, _bot = _dispatch_callback(f"lostfound_return:{item_id}", STRANGER_ID, message=_post())

    assert result is UNHANDLED
    item = _run(db.get_lost_found_item(item_id))
    assert item["returned_at"] is None


def test_return_button_second_tap_is_idempotent(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    item_id = _seed_item()

    _dispatch_callback(f"lostfound_return:{item_id}", VOLUNTEER_ID, message=_post())
    result, event, _bot = _dispatch_callback(f"lostfound_return:{item_id}", MANAGER_ID, message=_post())

    assert event.answers
    assert "уже отмечено" in (event.answers[0][0] or "").lower()
    item = _run(db.get_lost_found_item(item_id))
    assert item["returned_by"] == VOLUNTEER_ID  # первый победитель не перезаписан
    assert not event.message.edit_caption_calls  # второй тап не трогал сообщение


def test_return_button_unknown_id_shows_alert(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    result, event, _bot = _dispatch_callback("lostfound_return:999999", VOLUNTEER_ID)
    assert event.answers
    assert "не найдена" in (event.answers[0][0] or "").lower()


def test_return_button_forged_id_under_other_post_is_rejected(tmp_path):
    """id в кнопке подделывается: тап под ДРУГИМ постом не закрывает чужую находку."""
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    item_id = _seed_item()

    result, event, _bot = _dispatch_callback(
        f"lostfound_return:{item_id}", VOLUNTEER_ID, message=_post(message_id=778),
    )

    assert event.answers and "неизвестная кнопка" in (event.answers[0][0] or "").lower()
    assert _run(db.get_lost_found_item(item_id))["returned_at"] is None
    assert not event.message.edit_caption_calls


def test_return_button_other_city_staff_is_rejected(tmp_path):
    _ready(tmp_path)
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
            {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 1, "sort_order": 1},
        ])
        _run(db.set_setting("event_city_enabled", "on"))
        _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
        _run(db.set_staff_city(VOLUNTEER_ID, "spb"))
        item_id = _seed_item()  # находка Москвы

        _dispatch_callback(f"lostfound_return:{item_id}", VOLUNTEER_ID, message=_post())

        assert _run(db.get_lost_found_item(item_id))["returned_at"] is None
    finally:
        cities_mod.set_cities_for_test(saved)


def test_publish_after_toggle_turned_off_during_preview_is_cancelled(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    _enable()
    _bind_chat(None)
    state = _fresh_state(VOLUNTEER_ID)
    bot = FakeBot()

    _dispatch_callback("lost_found_new", VOLUNTEER_ID, state=state, bot=bot)
    _dispatch_message(
        None, VOLUNTEER_ID, photo=[FakePhotoSize("ph1")],
        raw_state="LostFoundNew:waiting_photo", state=state, bot=bot,
    )
    _dispatch_message(
        "где-то", VOLUNTEER_ID, raw_state="LostFoundNew:waiting_where", state=state, bot=bot,
    )
    _run(db.set_setting("lost_found_enabled", "off"))
    result, event, _bot2 = _dispatch_callback("lostfound_publish", VOLUNTEER_ID, state=state, bot=bot)

    assert not bot.sent_photos
    assert event.answers and "выключено" in (event.answers[0][0] or "")
