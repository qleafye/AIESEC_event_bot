"""Форум-ночь п.8 (идея №19 бэклога чек-ина, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`):
«🆘 SOS» — делегат жмёт кнопку, карточка уходит в чат оргов, орг «берёт» (атомарный захват),
отвечает реплаем, при молчании эскалирует.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async-вызов
через asyncio.run(), config.DB_PATH указывает на файл в tmp_path. БД — tests/_dbtpl.py::
fast_init_db. Fake-объекты — форма tests/test_roles_phase8.py (FakeUser/FakeChat/FakeMessage/
FakeCallback/FakeBot), расширенная тем, что реально использует sos-хендлеры (photo/location/
forward_origin/copy_to/edit_reply_markup/reply_to_message_id у bot.send_message).
"""
import asyncio
from datetime import datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from config import config
from database import db
from handlers import admin_sos, sos as sos_handlers
from handlers import group_chat
from handlers.states import SosChatBind, SosReport
from services import sos as sos_service
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db

ADMIN_ID = 902001
MANAGER_ID = 902002
STRANGER_ID = 902003
DELEGATE_ID = 902010
DELEGATE2_ID = 902011
CHAT_ID = -1009021001


def _ready(tmp_path, name="test_sos_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


async def _add_delegate(tid: int, *, full_name="Тест Делегатов", username="testdel",
                         university="ВШЭ", phone="+79990000000", event_city=None):
    await db.add_user({
        "telegram_id": tid, "full_name": full_name, "username": username,
        "university": university, "phone": phone, "event_city": event_city,
        "registration_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "approved",
    })


class FakeUser:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


class FakeChat:
    def __init__(self, cid, title=None, full_name=None):
        self.id = cid
        self.title = title
        self.full_name = full_name


class FakeMessage:
    def __init__(self, text=None, user_id=None, chat_id=None, reply_to_message=None,
                 photo=None, caption=None, location=None, forward_origin=None):
        self.text = text
        self.html_text = text
        self.caption = caption
        self.photo = photo
        self.location = location
        self.forward_origin = forward_origin
        self.markup = None
        self.from_user = FakeUser(user_id) if user_id is not None else None
        self.chat = FakeChat(chat_id if chat_id is not None else user_id)
        self.reply_to_message = reply_to_message
        self.answers = []
        self.copies = []
        self.edit_markup_calls = []
        self.bot = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))

    async def reply(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.edit_markup_calls.append(reply_markup)

    async def copy_to(self, chat_id, reply_to_message_id=None):
        self.copies.append((chat_id, reply_to_message_id))


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message or FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []          # (chat_id, text, kwargs)
        self.edited = []        # (chat_id, message_id, text, kwargs)
        self.next_message_id = 5000

    async def send_message(self, chat_id, text, **kwargs):
        self.next_message_id += 1
        self.sent.append((chat_id, text, kwargs))

        class _Msg:
            pass

        m = _Msg()
        m.message_id = self.next_message_id
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edited.append((chat_id, message_id, text, kwargs))

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        self.sent.append((chat_id, f"[copy from {from_chat_id}/{message_id}]", kwargs))


def _plain_card_text(report_id: int, telegram_id: int) -> str:
    """Тот же приём, что `tests/test_roles_phase8.py::_question_notification_text`: reply_to_
    message.text у Telegram — ПЛОСКИЙ текст, HTML-разметка (`<code>`/`<b>`) в нём не участвует
    (уходит в entities отдельно) — реальный маркер выглядит как "🆔 123", не "🆔 <code>123</code>"."""
    return f"🆘 SOS #{report_id} · bad\n🆔 {telegram_id} Тест Делегатов"


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


# ══════════════════════════════════════════════════════════════════════════════════════════
# services/sos.py — чистые функции
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_report_status_open_claimed_resolved():
    assert sos_service.report_status({"claimed_by": None, "resolved_at": None}) == sos_service.STATUS_OPEN
    assert sos_service.report_status({"claimed_by": 1, "resolved_at": None}) == sos_service.STATUS_CLAIMED
    assert sos_service.report_status({"claimed_by": 1, "resolved_at": "x"}) == sos_service.STATUS_RESOLVED
    # Легаси-случай (form questions.py): resolved_at заполнен -> "resolved" даже без claimed_by.
    assert sos_service.report_status({"claimed_by": None, "resolved_at": "x"}) == sos_service.STATUS_RESOLVED


def test_category_labels_cover_every_category_in_order():
    assert set(sos_service.CATEGORY_ORDER) == set(sos_service.CATEGORY_LABELS)
    assert len(sos_service.CATEGORY_ORDER) == 4


def test_render_card_text_has_markers_for_reply_detection():
    report = {
        "id": 7, "telegram_id": DELEGATE_ID, "city": "msk", "category": "lost",
        "details_text": "Потерялся у входа", "details_photo_file_id": None,
        "latitude": 55.75, "longitude": 37.61, "created_at": "2026-10-15 10:00:00",
    }
    user = {"full_name": "Иван Иванов", "username": "ivan", "university": "МГУ", "phone": "+7999"}
    text = sos_service.render_card_text(report, user)
    assert "🆔" in text and "🆘" in text
    assert "SOS #7" in text
    assert "🧭 Потерялся" in text
    assert "Потерялся у входа" in text
    assert "maps.google.com/?q=55.75,37.61" in text


def test_render_card_text_fail_soft_without_user_row():
    report = {
        "id": 1, "telegram_id": DELEGATE_ID, "city": None, "category": "other",
        "details_text": None, "details_photo_file_id": None,
        "latitude": None, "longitude": None, "created_at": "2026-10-15 10:00:00",
    }
    text = sos_service.render_card_text(report, None)
    assert "—" in text  # прочерки вместо ФИО/города/вуза/телефона, ничего не падает


# ── Гейт дня форума (пункт 1 плана) ──────────────────────────────────────────────────────

def test_sos_active_for_city_false_without_forum_date(tmp_path):
    _ready(tmp_path)
    assert _run(sos_service.is_sos_active_for_city(None)) is False


def test_sos_active_for_city_true_on_forum_date(tmp_path):
    _ready(tmp_path)
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", today))
    assert _run(sos_service.is_sos_active_for_city(None)) is True


def test_sos_active_for_city_true_within_default_two_day_window(tmp_path):
    _ready(tmp_path)
    yesterday = (msk_now() - timedelta(days=1)).strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", yesterday))
    assert _run(sos_service.is_sos_active_for_city(None)) is True  # день 2 из дефолтных 2


def test_sos_active_for_city_false_after_window_closes(tmp_path):
    _ready(tmp_path)
    three_days_ago = (msk_now() - timedelta(days=3)).strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", three_days_ago))
    assert _run(sos_service.is_sos_active_for_city(None)) is False


def test_sos_active_for_city_respects_custom_active_days(tmp_path):
    _ready(tmp_path)
    two_days_ago = (msk_now() - timedelta(days=2)).strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", two_days_ago))
    _run(db.set_setting("sos_active_days", "1"))
    assert _run(sos_service.is_sos_active_for_city(None)) is False  # день 3 при окне в 1 день


def test_sos_active_for_city_false_on_garbage_date(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "не дата"))
    assert _run(sos_service.is_sos_active_for_city(None)) is False


# ── Меню: кнопка «🆘 SOS» показывается только в окне (положительный кейс — дополняет
# tests/test_content_percity_consumers.py/test_content_percity_offparity.py, которые проверяют
# ТОЛЬКО отсутствие кнопки на пустой БД) ────────────────────────────────────────────────────

def test_menu_sos_button_shown_within_active_window(tmp_path):
    _ready(tmp_path)
    from keyboards.builders import get_main_menu_kb

    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", today))
    _run(_add_delegate(DELEGATE_ID))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    texts = [b.text for row in kb.keyboard for b in row]
    assert "🆘 SOS" in texts


def test_menu_sos_button_hidden_when_menu_toggle_off(tmp_path):
    _ready(tmp_path)
    from keyboards.builders import get_main_menu_kb

    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", today))
    _run(db.set_setting("menu_sos", "off"))
    _run(_add_delegate(DELEGATE_ID))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    texts = [b.text for row in kb.keyboard for b in row]
    assert "🆘 SOS" not in texts


# ══════════════════════════════════════════════════════════════════════════════════════════
# database/db.py — sos_reports аксессоры (атомарность, анти-спам)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_claim_sos_report_race_first_wins(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "lost", None, None, None, None))
    first = _run(db.claim_sos_report(rid, ADMIN_ID, "Админ Первый"))
    second = _run(db.claim_sos_report(rid, MANAGER_ID, "Менеджер Второй"))
    assert first is True
    assert second is False
    row = _run(db.get_sos_report(rid))
    assert row["claimed_by"] == ADMIN_ID
    assert row["claimed_by_name"] == "Админ Первый"


def test_resolve_sos_report_implicitly_claims_when_open(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    resolved = _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))
    assert resolved is True
    row = _run(db.get_sos_report(rid))
    assert row["claimed_by"] == ADMIN_ID  # COALESCE подставил резолвера
    assert row["resolved_by"] == ADMIN_ID


def test_resolve_sos_report_twice_second_call_is_noop(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    first = _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))
    second = _run(db.resolve_sos_report(rid, MANAGER_ID, "Менеджер"))
    assert first is True
    assert second is False


def test_get_open_sos_report_none_when_resolved(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    assert _run(db.get_open_sos_report(DELEGATE_ID))["id"] == rid
    _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))
    assert _run(db.get_open_sos_report(DELEGATE_ID)) is None


def test_set_sos_escalated_idempotent(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    first = _run(db.set_sos_escalated(rid))
    second = _run(db.set_sos_escalated(rid))
    assert first is True
    assert second is False


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers/sos.py — делегатский поток (пункт 1, анти-спам)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_full_flow_creates_report_and_confirms(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))

    state = _fresh_state(DELEGATE_ID)
    start_msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_start(start_msg, state))
    assert any("Что случилось?" in a[0] for a in start_msg.answers)

    cb = FakeCallback("sos_cat:lost", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_pick_category(cb, state))
    assert _run(state.get_state()) == SosReport.details.state

    details_msg = FakeMessage(text="Пропустить", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_details_skip(details_msg, state))
    assert _run(state.get_state()) == SosReport.location.state

    loc_msg = FakeMessage(user_id=DELEGATE_ID)
    _run(sos_handlers.sos_location_skip(loc_msg, state))

    assert _run(state.get_state()) is None
    row = _run(db.get_open_sos_report(DELEGATE_ID))
    assert row is not None
    assert row["category"] == "lost"
    assert any("Оргкомитет получил" in a[0] for a in loc_msg.answers)


def test_sos_anti_spam_blocks_second_open_report(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_start(msg, state))
    assert any("уже есть открытый SOS" in a[0] for a in msg.answers)
    # Второй строки в БД не появилось.
    assert len(_run(db.list_sos_reports_page(limit=10))) == 1


def test_sos_details_step_accepts_photo_without_text(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    state = _fresh_state(DELEGATE_ID)
    _run(state.update_data(sos_category="bad", sos_city=None))
    _run(state.set_state(SosReport.details))

    class _Photo:
        file_id = "photo123"

    msg = FakeMessage(user_id=DELEGATE_ID, photo=[_Photo()])
    _run(sos_handlers.sos_details_step(msg, state))
    data = _run(state.get_data())
    assert data["sos_details_photo"] == "photo123"


def test_sos_location_step_stores_coordinates(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    state = _fresh_state(DELEGATE_ID)
    _run(state.update_data(sos_category="lost", sos_city=None,
                            sos_details_text=None, sos_details_photo=None))
    _run(state.set_state(SosReport.location))

    class _Loc:
        latitude = 55.1
        longitude = 37.2

    msg = FakeMessage(user_id=DELEGATE_ID, location=_Loc())
    _run(sos_handlers.sos_location_step(msg, state))
    row = _run(db.get_open_sos_report(DELEGATE_ID))
    assert row["latitude"] == 55.1
    assert row["longitude"] == 37.2


# ══════════════════════════════════════════════════════════════════════════════════════════
# Карточка: чат привязан vs. фоллбэк-веер (пункт 3 плана + предупреждение экрана менеджера)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_post_card_to_bound_chat_stores_card_message(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))

    bot = FakeBot()
    posted_to_chat = _run(sos_service.post_card(bot, rid))
    assert posted_to_chat is True
    assert bot.sent[0][0] == CHAT_ID
    row = _run(db.get_sos_report(rid))
    assert row["chat_id"] == CHAT_ID
    assert row["card_message_id"] is not None


def test_post_card_falls_back_to_moderate_reg_dm_without_bound_chat(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))

    bot = FakeBot()
    posted_to_chat = _run(sos_service.post_card(bot, rid))
    assert posted_to_chat is False
    recipients = {chat_id for chat_id, _t, _kw in bot.sent}
    assert ADMIN_ID in recipients  # bootstrap admin holds moderate_reg
    assert MANAGER_ID in recipients
    row = _run(db.get_sos_report(rid))
    assert row["chat_id"] is None  # известное ограничение фоллбэка — треда нет


def test_render_sos_screen_shows_warning_without_bound_chat(tmp_path):
    _ready(tmp_path)
    text, _kb = _run(admin_sos.render_sos_screen(ADMIN_ID))
    assert "не привязан" in text


def test_render_sos_screen_shows_chat_title_when_bound(tmp_path):
    _ready(tmp_path)
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    text, kb = _run(admin_sos.render_sos_screen(ADMIN_ID))
    assert "Чат оргов" in text
    assert "не привязан" not in text
    # Кнопка привязки остаётся доступной и на привязанном чате (перепривязка без похода в БД,
    # если бот добавили не в тот чат) — подпись меняется на «Перепривязать».
    all_buttons = [b.text for row in kb.inline_keyboard for b in row]
    assert "🔗 Перепривязать чат SOS" in all_buttons


# ══════════════════════════════════════════════════════════════════════════════════════════
# «🙋 Беру» / «✅ Решено» (пункт 3 плана) — конкуренция, эскалация снимается
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_claim_callback_first_wins_second_gets_alert(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    bot = FakeBot()

    cb1 = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_claim(cb1, bot))
    assert cb1.answers[0][0] == "Взято."

    cb2 = FakeCallback(f"sos_claim:{rid}", user_id=MANAGER_ID)
    _run(admin_sos.sos_claim(cb2, bot))
    assert "Уже взял" in cb2.answers[0][0]
    assert cb2.answers[0][1] is True  # show_alert


def test_sos_claim_cancels_pending_escalation(tmp_path, monkeypatch):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    cancelled = []
    monkeypatch.setattr(sos_service, "cancel_escalation", lambda report_id: cancelled.append(report_id))

    cb = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_claim(cb, FakeBot()))
    assert cancelled == [rid]


def test_sos_resolve_callback_refreshes_card(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))

    cb = FakeCallback(f"sos_resolve:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_resolve(cb, bot))
    assert cb.answers[0][0] == "Отмечено решённым."
    assert bot.edited  # карточка перерисована
    assert "✅ Решено:" in bot.edited[-1][2]


def test_post_card_shows_human_city_label_not_raw_code(tmp_path):
    """CLAUDE.md: «Кодовые значения ... человеку не показываем» — карточка обязана показать
    подпись города («Москва»), не код («msk»). `cities.CITIES` — модульный глобал, сохраняем и
    откатываем (та же дисциплина, что tests/test_chat_binding_260914.py::_cities_on) — иначе
    привязка «msk» протекает в остальные тесты этого процесса."""
    _ready(tmp_path)
    saved = list(cities.CITIES)
    try:
        _run(db.insert_city("msk", "Москва", None, 0, 1))
        _run(cities.reload_cities())
        _run(db.set_setting("event_city_enabled", "on"))
        _run(_add_delegate(DELEGATE_ID, event_city="msk"))
        rid = _run(db.create_sos_report(DELEGATE_ID, "msk", "bad", None, None, None, None))

        bot = FakeBot()
        _run(sos_service.post_card(bot, rid))
        text = bot.sent[0][1]
        assert "Москва" in text
        assert "msk" not in text.lower()
    finally:
        cities.set_cities_for_test(saved)


def test_card_text_shows_claimed_by_and_time_after_claim(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    _run(db.claim_sos_report(rid, ADMIN_ID, "Админ Первый"))
    report = _run(db.get_sos_report(rid))
    text = sos_service.render_card_text(report, None)
    assert "✍️ Взял(а): Админ Первый в" in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реплай туда-обратно (пункт 3) — тихие часы НЕ применяются к ответу по SOS
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_admin_reply_to_sos_delivers_immediately_during_quiet_hours(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    report = _run(db.get_sos_report(rid))

    # Тихие часы ВКЛЮЧЕНЫ и сейчас внутри окна «весь день» — обычный ответ на вопрос делегата
    # ушёл бы в очередь; SOS обязан доставиться немедленно (пункт 3 плана).
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "00:00"))
    _run(db.set_setting("quiet_hours_end", "23:59"))

    card_text = _plain_card_text(rid, DELEGATE_ID)
    replied_to = FakeMessage(text=card_text, chat_id=CHAT_ID)
    reply_msg = FakeMessage(
        text="Уже иду к тебе!", user_id=ADMIN_ID, chat_id=CHAT_ID, reply_to_message=replied_to,
    )
    bot = FakeBot()
    _run(admin_sos.admin_reply_to_sos(reply_msg, bot))

    delivered = [s for s in bot.sent if s[0] == DELEGATE_ID]
    assert delivered, "ответ обязан уйти немедленно, а не встать в очередь тихих часов"
    assert "Уже иду к тебе!" in delivered[0][1]
    row = _run(db.get_sos_report(rid))
    assert row["claimed_by"] == ADMIN_ID  # реплай неявно захватывает


def test_admin_reply_to_sos_second_responder_blocked(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    card_text = _plain_card_text(rid, DELEGATE_ID)

    replied_to = FakeMessage(text=card_text, chat_id=CHAT_ID)
    first = FakeMessage(text="Иду", user_id=ADMIN_ID, chat_id=CHAT_ID, reply_to_message=replied_to)
    _run(admin_sos.admin_reply_to_sos(first, FakeBot()))

    second = FakeMessage(text="И я тоже", user_id=MANAGER_ID, chat_id=CHAT_ID, reply_to_message=replied_to)
    bot2 = FakeBot()
    _run(admin_sos.admin_reply_to_sos(second, bot2))
    assert not [s for s in bot2.sent if s[0] == DELEGATE_ID]
    assert any("уже взял" in a[0] for a in second.answers)


def test_sos_delegate_followup_relays_into_chat_thread(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))
    row = _run(db.get_sos_report(rid))

    org_reply_header = FakeMessage(text=f"🆘 Ответ по SOS #{rid}:", chat_id=DELEGATE_ID)
    followup = FakeMessage(
        text="Спасибо, жду", user_id=DELEGATE_ID, chat_id=DELEGATE_ID,
        reply_to_message=org_reply_header,
    )
    _run(sos_handlers.sos_delegate_followup(followup))
    assert followup.copies == [(row["chat_id"], row["card_message_id"])]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Эскалация (пункт 4) — снимается claim/resolve, срабатывает только пока открыт
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_escalation_job_noop_after_claim(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))
    _run(db.claim_sos_report(rid, ADMIN_ID, "Админ"))
    _run(sos_service.escalation_job(rid))
    row = _run(db.get_sos_report(rid))
    assert row["escalated_at"] is None  # взяли ДО тика -> джоба не штампует эскалацию


def test_escalation_job_fires_when_still_open(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None, "bad", None, None, None, None))

    bot = FakeBot()

    class _SchedMod:
        @staticmethod
        def get_bot():
            return bot

    monkeypatch.setattr("services.scheduler.get_bot", _SchedMod.get_bot, raising=False)
    import services.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_bot", _SchedMod.get_bot)

    _run(sos_service.escalation_job(rid))
    row = _run(db.get_sos_report(rid))
    assert row["escalated_at"] is not None
    assert any("без ответа" in s[1] for s in bot.sent)


def test_cancel_escalation_is_fail_soft_without_scheduler(tmp_path):
    _ready(tmp_path)
    # Планировщик не инициализирован в тестах вовсе — тот же критерий приёмки, что у
    # chat_tracking.schedule_bind_reconcile (см. её докстринг): не должно бросать исключение.
    sos_service.cancel_escalation(999999)
    sos_service.schedule_escalation(999999, 5)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Привязка чата SOS (пункт 2 плана)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_pending_bind_ttl_expires(tmp_path):
    _ready(tmp_path)
    _run(sos_service.set_pending_bind(ADMIN_ID, None))
    stale = (msk_now() - timedelta(minutes=sos_service.PENDING_BIND_TTL_MINUTES + 1)) \
        .strftime("%Y-%m-%d %H:%M:%S")

    async def _age_it():
        async with db.aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "UPDATE sos_chat_bind_pending SET requested_at = ? WHERE admin_id = ?",
                (stale, ADMIN_ID),
            )
            await conn.commit()

    _run(_age_it())
    assert _run(sos_service.get_pending_bind(ADMIN_ID)) is None
    assert _run(db.get_sos_bind_pending(ADMIN_ID)) is None  # уборка по чтению


def test_asos_bind_start_sets_pending_and_state(tmp_path):
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    cb = FakeCallback("asos_bind", user_id=ADMIN_ID)
    _run(admin_sos.asos_bind_start(cb, state))
    assert _run(state.get_state()) == SosChatBind.waiting.state
    assert _run(sos_service.get_pending_bind(ADMIN_ID)) is not None


def test_asos_bind_step_forwarded_chat_message_completes_bind(tmp_path):
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(sos_service.set_pending_bind(ADMIN_ID, None))
    _run(state.set_state(SosChatBind.waiting))

    class _Origin:
        chat = FakeChat(CHAT_ID, title="Чат оргов Москвы")

    msg = FakeMessage(user_id=ADMIN_ID, forward_origin=_Origin())
    bot = FakeBot()
    _run(admin_sos.asos_bind_step(msg, state, bot))

    assert _run(state.get_state()) is None
    chat = _run(sos_service.sos_chat_for_city(None))
    assert chat == {"chat_id": CHAT_ID, "title": "Чат оргов Москвы"}
    assert any("Готово" in a[0] for a in msg.answers)


def test_group_sos_id_command_completes_bind_from_within_group(tmp_path):
    _ready(tmp_path)
    _run(sos_service.set_pending_bind(ADMIN_ID, None))

    msg = FakeMessage(text="/sos_id", user_id=ADMIN_ID, chat_id=CHAT_ID)
    msg.chat.title = "Чат оргов из группы"
    bot = FakeBot()
    _run(group_chat.on_sos_id_command(msg, bot))

    chat = _run(sos_service.sos_chat_for_city(None))
    assert chat == {"chat_id": CHAT_ID, "title": "Чат оргов из группы"}
    # Бот никогда не пишет В САМУ ГРУППУ (D-1) — подтверждение только личным сообщением.
    assert all(s[0] != CHAT_ID for s in bot.sent)
    assert any(s[0] == ADMIN_ID for s in bot.sent)


def test_group_sos_id_command_silent_without_pending_request(tmp_path):
    _ready(tmp_path)
    msg = FakeMessage(text="/sos_id", user_id=STRANGER_ID, chat_id=CHAT_ID)
    bot = FakeBot()
    _run(group_chat.on_sos_id_command(msg, bot))
    assert bot.sent == []  # D-9/D-1: без заявки — тишина, ни в группу, ни в личку


# ══════════════════════════════════════════════════════════════════════════════════════════
# Права (пункт 6 плана) — стороннему пользователю экран/карточка недоступны
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_stranger_has_no_sos_capability(tmp_path):
    _ready(tmp_path)
    from handlers.admin_caps import has_capability, required_capability

    cap = required_capability(callback_data="admin_sos")
    assert cap == "moderate_reg"
    assert _run(has_capability(STRANGER_ID, cap)) is False
    assert _run(has_capability(ADMIN_ID, cap)) is True


def test_asos_bind_capability_is_settings_not_moderate_reg(tmp_path):
    _ready(tmp_path)
    from handlers.admin_caps import has_capability, required_capability

    cap = required_capability(callback_data="asos_bind")
    assert cap == "settings"
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))  # holds moderate_reg, NOT settings
    assert _run(has_capability(MANAGER_ID, cap)) is False
