"""Форум-ночь п.8 (идея №19 бэклога чек-ина, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`):
«🆘 SOS» — делегат жмёт кнопку, карточка уходит в чат оргов, орг «берёт» (атомарный захват),
отвечает реплаем, при молчании эскалирует.

D-31 (24.09, `.planning/FORUM-CHECKIN.md`, «SOS без категорий»): «🆘 SOS» публикует карточку
МГНОВЕННО (без вопроса «что случилось»/кнопок категорий) и переводит делегата в режим
«дописываю SOS» (`SosReport.collecting`) — ЛЮБОЕ его сообщение (текст/фото/геопозиция) уходит в
тред карточки И дописывает саму карточку. Этот файл переписан под новый поток целиком.

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
    def __init__(self, cid, title=None, full_name=None, type=None):
        self.id = cid
        self.title = title
        self.full_name = full_name
        self.type = type


class FakeMessage:
    def __init__(self, text=None, user_id=None, chat_id=None, reply_to_message=None,
                 photo=None, caption=None, location=None, forward_origin=None, chat_type=None):
        self.text = text
        self.html_text = text
        self.caption = caption
        self.photo = photo
        self.location = location
        self.forward_origin = forward_origin
        self.markup = None
        self.from_user = FakeUser(user_id) if user_id is not None else None
        self.chat = FakeChat(chat_id if chat_id is not None else user_id, type=chat_type)
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


class _FakeChatMember:
    def __init__(self, status):
        self.status = status


class FakeBot:
    def __init__(self):
        self.id = 999999999
        self.sent = []          # (chat_id, text, kwargs)
        self.edited = []        # (chat_id, message_id, text, kwargs)
        self.next_message_id = 5000
        # chat_id -> статус getChatMember(chat_id, bot.id); отсутствие ключа = "member"
        # (ревью 24.09, находка 2 — членство бота проверяется перед привязкой чата).
        self.chat_member_status = {}
        # chat_id -> очередь исключений, которые send_message бросит по одному на вызов
        # (ревью 24.09, находки 1/2 — симуляция провала доставки/миграции чата).
        self.send_failures: dict = {}

    async def get_chat_member(self, chat_id, user_id):
        return _FakeChatMember(self.chat_member_status.get(chat_id, "member"))

    async def send_message(self, chat_id, text, **kwargs):
        queue = self.send_failures.get(chat_id)
        if queue:
            raise queue.pop(0)
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
    return f"🆘 SOS #{report_id}\n🆔 {telegram_id} Тест Делегатов"


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _collecting_state(user_id, report_id, city=None, started=None):
    """Сессия «дописываю SOS» уже открыта (карточка уже создана/отправлена) — та форма, что
    сеет `handlers/sos.py::_enter_collecting`, без похода через `sos_start`."""
    state = _fresh_state(user_id)
    _run(state.set_state(SosReport.collecting))
    _run(state.update_data(
        sos_collecting_report_id=report_id, sos_collecting_city=city,
        sos_collecting_started=(started or msk_now()).strftime("%Y-%m-%d %H:%M:%S"),
    ))
    return state


# ══════════════════════════════════════════════════════════════════════════════════════════
# services/sos.py — чистые функции
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_report_status_open_claimed_resolved():
    assert sos_service.report_status({"claimed_by": None, "resolved_at": None}) == sos_service.STATUS_OPEN
    assert sos_service.report_status({"claimed_by": 1, "resolved_at": None}) == sos_service.STATUS_CLAIMED
    assert sos_service.report_status({"claimed_by": 1, "resolved_at": "x"}) == sos_service.STATUS_RESOLVED
    # Легаси-случай (form questions.py): resolved_at заполнен -> "resolved" даже без claimed_by.
    assert sos_service.report_status({"claimed_by": None, "resolved_at": "x"}) == sos_service.STATUS_RESOLVED


def test_render_card_text_has_markers_for_reply_detection():
    report = {
        "id": 7, "telegram_id": DELEGATE_ID, "city": "msk",
        "details_text": "Потерялся у входа", "details_photo_file_id": None,
        "latitude": 55.75, "longitude": 37.61, "created_at": "2026-10-15 10:00:00",
    }
    user = {"full_name": "Иван Иванов", "username": "ivan", "university": "МГУ", "phone": "+7999"}
    text = sos_service.render_card_text(report, user)
    assert "🆔" in text and "🆘" in text
    assert "SOS #7" in text
    assert "Потерялся у входа" in text
    assert "maps.google.com/?q=55.75,37.61" in text
    assert "подробности ещё не прислали" not in text  # уже есть подробности


def test_render_card_text_shows_urgent_marker_without_details_yet():
    """D-31: карточка публикуется МГНОВЕННО, до первого слова делегата — пока подробностей нет,
    место категории занимает пометка «🆘 СРОЧНО — подробности ещё не прислали»."""
    report = {
        "id": 8, "telegram_id": DELEGATE_ID, "city": None,
        "details_text": None, "details_photo_file_id": None,
        "latitude": None, "longitude": None, "created_at": "2026-10-15 10:00:00",
    }
    text = sos_service.render_card_text(report, None)
    assert "🆘 СРОЧНО — подробности ещё не прислали" in text


def test_render_card_text_fail_soft_without_user_row():
    report = {
        "id": 1, "telegram_id": DELEGATE_ID, "city": None,
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
# database/db.py — sos_reports аксессоры (атомарность, анти-спам, дозапись D-31)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_claim_sos_report_race_first_wins(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    first = _run(db.claim_sos_report(rid, ADMIN_ID, "Админ Первый"))
    second = _run(db.claim_sos_report(rid, MANAGER_ID, "Менеджер Второй"))
    assert first is True
    assert second is False
    row = _run(db.get_sos_report(rid))
    assert row["claimed_by"] == ADMIN_ID
    assert row["claimed_by_name"] == "Админ Первый"


def test_resolve_sos_report_implicitly_claims_when_open(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    resolved = _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))
    assert resolved is True
    row = _run(db.get_sos_report(rid))
    assert row["claimed_by"] == ADMIN_ID  # COALESCE подставил резолвера
    assert row["resolved_by"] == ADMIN_ID


def test_resolve_sos_report_twice_second_call_is_noop(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    first = _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))
    second = _run(db.resolve_sos_report(rid, MANAGER_ID, "Менеджер"))
    assert first is True
    assert second is False


def test_get_open_sos_report_none_when_resolved(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    assert _run(db.get_open_sos_report(DELEGATE_ID))["id"] == rid
    _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))
    assert _run(db.get_open_sos_report(DELEGATE_ID)) is None


def test_set_sos_escalated_idempotent(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    first = _run(db.set_sos_escalated(rid))
    second = _run(db.set_sos_escalated(rid))
    assert first is True
    assert second is False


def test_add_sos_details_first_text_wins_second_ignored(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.add_sos_details(rid, text="Болит нога"))
    _run(db.add_sos_details(rid, text="Уже не болит"))  # второй текст НЕ переписывает первый
    row = _run(db.get_sos_report(rid))
    assert row["details_text"] == "Болит нога"


def test_add_sos_details_text_and_photo_are_independent(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.add_sos_details(rid, photo_file_id="ph1"))
    _run(db.add_sos_details(rid, text="Комментарий"))
    row = _run(db.get_sos_report(rid))
    assert row["details_photo_file_id"] == "ph1"
    assert row["details_text"] == "Комментарий"


def test_set_sos_location_overwrites_on_repeat(tmp_path):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.set_sos_location(rid, 55.0, 37.0))
    _run(db.set_sos_location(rid, 55.5, 37.5))  # последняя точка побеждает
    row = _run(db.get_sos_report(rid))
    assert row["latitude"] == 55.5
    assert row["longitude"] == 37.5


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers/sos.py — делегатский поток: мгновенная карточка + режим «дописываю SOS» (D-31)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_start_creates_report_and_card_instantly(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    msg.bot = FakeBot()
    _run(sos_handlers.sos_start(msg, state))

    row = _run(db.get_open_sos_report(DELEGATE_ID))
    assert row is not None
    assert any("Сигнал отправлен оргкомитету" in a[0] for a in msg.answers)
    assert _run(state.get_state()) == SosReport.collecting.state
    data = _run(state.get_data())
    assert data["sos_collecting_report_id"] == row["id"]


def test_sos_anti_spam_blocks_second_open_report(tmp_path):
    """Повторное «🆘 SOS», пока предыдущий свой же SOS ещё свежий (`sos_reopen_window_minutes`),
    НЕ создаёт вторую строку — делегат попадает в тот же режим «дописываю SOS»."""
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    msg.bot = FakeBot()
    _run(sos_handlers.sos_start(msg, state))
    assert any("Сигнал уже у оргкомитета" in a[0] for a in msg.answers)
    # Второй строки в БД не появилось.
    assert len(_run(db.list_sos_reports_page(limit=10))) == 1
    data = _run(state.get_data())
    assert data["sos_collecting_report_id"] == rid


def test_sos_double_tap_concurrent_creates_single_card(tmp_path, monkeypatch):
    """Двойной тап: два апдейта обрабатываются параллельно — карточка одна, второй тап
    попадает в ветку «сигнал уже у оргкомитета». Медленная вставка расширяет окно гонки
    (без лока здесь стабильно две карточки)."""
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    orig_create = sos_handlers.create_sos_report

    async def slow_create(*a, **k):
        await asyncio.sleep(0.05)
        return await orig_create(*a, **k)

    monkeypatch.setattr(sos_handlers, "create_sos_report", slow_create)

    state = _fresh_state(DELEGATE_ID)
    bot = FakeBot()
    m1 = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    m2 = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    m1.bot = m2.bot = bot

    async def both():
        await asyncio.gather(sos_handlers.sos_start(m1, state), sos_handlers.sos_start(m2, state))

    _run(both())
    assert len(_run(db.list_sos_reports_page(limit=10))) == 1
    answers = [a[0] for a in m1.answers + m2.answers]
    assert any("Сигнал уже у оргкомитета" in a for a in answers)
    assert sos_handlers._sos_start_locks == {}


# ══════════════════════════════════════════════════════════════════════════════════════════
# Режим «дописываю SOS» (D-31): текст/фото/геопозиция уходят в тред И дописывают карточку
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_collecting_step_first_text_sets_details_and_relays(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))
    row = _run(db.get_sos_report(rid))

    state = _collecting_state(DELEGATE_ID, rid)
    msg = FakeMessage(text="Болит нога", user_id=DELEGATE_ID)
    msg.bot = bot
    _run(sos_handlers.sos_collecting_step(msg, state))

    updated = _run(db.get_sos_report(rid))
    assert updated["details_text"] == "Болит нога"
    assert msg.copies == [(row["chat_id"], row["card_message_id"])]
    # Карточка перерисована — маркер «СРОЧНО» больше не должен остаться на новом рендере.
    assert bot.edited
    assert "подробности ещё не прислали" not in bot.edited[-1][2]
    # Режим НЕ закрывается одним сообщением (персистентный, в отличие от старого followup).
    assert _run(state.get_state()) == SosReport.collecting.state


def test_sos_collecting_step_accepts_photo_without_text(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    state = _collecting_state(DELEGATE_ID, rid)

    class _Photo:
        file_id = "photo123"

    msg = FakeMessage(user_id=DELEGATE_ID, photo=[_Photo()])
    msg.bot = FakeBot()
    _run(sos_handlers.sos_collecting_step(msg, state))
    row = _run(db.get_sos_report(rid))
    assert row["details_photo_file_id"] == "photo123"


def test_sos_collecting_location_step_stores_coordinates_and_relays(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    state = _collecting_state(DELEGATE_ID, rid)

    class _Loc:
        latitude = 55.1
        longitude = 37.2

    msg = FakeMessage(user_id=DELEGATE_ID, location=_Loc())
    msg.bot = FakeBot()
    _run(sos_handlers.sos_collecting_location(msg, state))
    row = _run(db.get_sos_report(rid))
    assert row["latitude"] == 55.1
    assert row["longitude"] == 37.2


def test_sos_collecting_done_clears_state_and_sends_main_menu(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    state = _collecting_state(DELEGATE_ID, rid)

    msg = FakeMessage(text="Готово", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_collecting_done(msg, state))

    assert _run(state.get_state()) is None
    assert any("Принято" in a[0] for a in msg.answers)


def test_sos_collecting_expired_closes_session_silently(tmp_path):
    """D-31: без действия делегата (никакого «Готово») — режим закрывается сам после
    `sos_collecting_timeout_minutes` (дефолт 30), следующее сообщение НЕ дописывает карточку."""
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    stale_start = msk_now() - timedelta(minutes=31)
    state = _collecting_state(DELEGATE_ID, rid, started=stale_start)

    msg = FakeMessage(text="Ещё тут?", user_id=DELEGATE_ID)
    msg.bot = FakeBot()
    _run(sos_handlers.sos_collecting_step(msg, state))

    assert _run(state.get_state()) is None
    assert any("Сессия SOS закрыта по времени" in a[0] for a in msg.answers)
    row = _run(db.get_sos_report(rid))
    assert row["details_text"] is None  # сообщение НЕ ушло в карточку — сессия уже закрыта


def test_sos_collecting_respects_custom_timeout_setting(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.set_setting("sos_collecting_timeout_minutes", "5"))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    stale_start = msk_now() - timedelta(minutes=6)
    state = _collecting_state(DELEGATE_ID, rid, started=stale_start)

    msg = FakeMessage(text="Привет", user_id=DELEGATE_ID)
    msg.bot = FakeBot()
    _run(sos_handlers.sos_collecting_step(msg, state))
    assert _run(state.get_state()) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Карточка: чат привязан vs. фоллбэк-веер (пункт 3 плана + предупреждение экрана менеджера)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_post_card_to_bound_chat_stores_card_message(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    bot = FakeBot()
    result = _run(sos_service.post_card(bot, rid))
    assert result.chat_delivered is True
    assert result.delivered_total == 1
    assert bot.sent[0][0] == CHAT_ID
    row = _run(db.get_sos_report(rid))
    assert row["chat_id"] == CHAT_ID
    assert row["card_message_id"] is not None


def test_post_card_falls_back_to_moderate_reg_dm_without_bound_chat(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    bot = FakeBot()
    result = _run(sos_service.post_card(bot, rid))
    assert result.chat_delivered is False
    assert result.dm_delivered == 2
    recipients = {chat_id for chat_id, _t, _kw in bot.sent}
    assert ADMIN_ID in recipients  # bootstrap admin holds moderate_reg
    assert MANAGER_ID in recipients
    row = _run(db.get_sos_report(rid))
    assert row["chat_id"] is None  # известное ограничение фоллбэка — треда нет


def test_fallback_dm_copies_all_refreshed_on_claim(tmp_path):
    """Чат SOS не привязан — карточка ушла в личку двум админам. «Беру» одного из них обязан
    перерисовать ОБЕ копии, иначе второй не видит, что заявка взята, и берёт её сам."""
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    bot = FakeBot()
    result = _run(sos_service.post_card(bot, rid))
    assert result.dm_delivered == 2
    copies = dict(_run(db.list_sos_card_copies(rid)))
    assert set(copies) == {ADMIN_ID, MANAGER_ID}

    cb = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_claim(cb, bot))
    edited = {(chat_id, message_id) for chat_id, message_id, _t, _kw in bot.edited}
    assert edited == {(ADMIN_ID, copies[ADMIN_ID]), (MANAGER_ID, copies[MANAGER_ID])}


def test_refresh_card_retries_copy_once_after_retry_after(tmp_path, monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter

    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.add_sos_card_copy(rid, ADMIN_ID, 11))
    _run(db.add_sos_card_copy(rid, MANAGER_ID, 12))
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(sos_service.asyncio, "sleep", fake_sleep)
    bot = FakeBot()
    orig_edit = bot.edit_message_text
    failures = {ADMIN_ID: 1}

    async def flaky_edit(text, chat_id=None, message_id=None, **kwargs):
        if failures.get(chat_id):
            failures[chat_id] -= 1
            raise TelegramRetryAfter(method=None, message="flood", retry_after=3)
        await orig_edit(text, chat_id=chat_id, message_id=message_id, **kwargs)

    bot.edit_message_text = flaky_edit
    _run(sos_service.refresh_card(bot, rid))
    assert slept == [3]
    assert {(c, m) for c, m, _t, _k in bot.edited} == {(ADMIN_ID, 11), (MANAGER_ID, 12)}


def test_purge_user_removes_sos_card_copies(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.add_sos_card_copy(rid, ADMIN_ID, 77))
    _run(db.purge_user(DELEGATE_ID))
    assert _run(db.list_sos_card_copies(rid)) == []


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


def test_row_text_shows_urgent_marker_without_details(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    row = _run(db.list_sos_reports_page(limit=10))[0]
    text = _run(admin_sos._row_text(row))
    assert "🆘 подробности ещё не прислали" in text


def test_row_text_hides_urgent_marker_once_details_present(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.add_sos_details(rid, text="Болит нога"))
    row = _run(db.list_sos_reports_page(limit=10))[0]
    text = _run(admin_sos._row_text(row))
    assert "подробности ещё не прислали" not in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# «🙋 Беру» / «✅ Решено» (пункт 3 плана) — конкуренция, эскалация снимается
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_claim_callback_first_wins_second_gets_alert(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
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
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    cancelled = []
    monkeypatch.setattr(sos_service, "cancel_escalation", lambda report_id: cancelled.append(report_id))

    cb = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_claim(cb, FakeBot()))
    assert cancelled == [rid]


def test_sos_resolve_callback_refreshes_card(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
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
        rid = _run(db.create_sos_report(DELEGATE_ID, "msk"))

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
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
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
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

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
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
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
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
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
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.claim_sos_report(rid, ADMIN_ID, "Админ"))
    _run(sos_service.escalation_job(rid))
    row = _run(db.get_sos_report(rid))
    assert row["escalated_at"] is None  # взяли ДО тика -> джоба не штампует эскалацию


def test_escalation_job_fires_when_still_open(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

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


def _patch_scheduler_bot(monkeypatch, bot):
    """Тот же приём, что `test_escalation_job_fires_when_still_open` — джобы читают бота через
    `services.scheduler.get_bot()`, планировщик в тестах не инициализирован вовсе."""
    class _SchedMod:
        @staticmethod
        def get_bot():
            return bot

    monkeypatch.setattr("services.scheduler.get_bot", _SchedMod.get_bot, raising=False)
    import services.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_bot", _SchedMod.get_bot)


def _async_result(value):
    async def _inner(*a, **kw):
        return value
    return _inner()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью 24.09, находка 1 — доставка честная: результат post_card, фоллбэк-текст делегату,
# повторная попытка через минуту. D-31 упростил тесты — вся цепочка теперь в одном `sos_start`.
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_start_total_failure_gives_honest_text_and_schedules_retry(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    # Ни привязанного чата, ни держателей moderate_reg (кроме забаненного ниже) — фоллбэк-веер
    # уходит нулю получателей: bootstrap ADMIN_ID тоже должен провалиться, иначе доставка
    # состоится через него.
    monkeypatch.setattr(
        "handlers.admin_caps.capability_holders",
        lambda cap, city=None: _async_result([]),
    )
    scheduled = []
    monkeypatch.setattr(
        sos_service, "schedule_delivery_retry", lambda report_id, delay_minutes=1: scheduled.append(report_id),
    )

    bot = FakeBot()
    bot.send_failures[ADMIN_ID] = [Exception("blocked")]  # единственный фоллбэк-получатель тоже недоступен

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    msg.bot = bot
    _run(sos_handlers.sos_start(msg, state))

    assert any("Не получилось передать SOS" in a[0] for a in msg.answers)
    row = _run(db.get_open_sos_report(DELEGATE_ID))
    assert row["delivery_failed_at"] is not None
    assert scheduled  # sos_service.schedule_delivery_retry(report_id) вызван
    # Делегат всё равно в режиме «дописываю SOS» — не брошен без состояния.
    assert _run(state.get_state()) == SosReport.collecting.state


def test_delivery_retry_job_success_clears_failed_flag(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.set_sos_delivery_failed(rid, True))

    bot = FakeBot()
    _patch_scheduler_bot(monkeypatch, bot)
    _run(sos_service.delivery_retry_job(rid))

    row = _run(db.get_sos_report(rid))
    assert row["delivery_failed_at"] is None  # ADMIN_ID/MANAGER_ID доступны -> доставлено


def test_delivery_retry_job_noop_when_already_resolved(tmp_path, monkeypatch):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.set_sos_delivery_failed(rid, True))
    _run(db.resolve_sos_report(rid, ADMIN_ID, "Админ"))

    bot = FakeBot()
    _patch_scheduler_bot(monkeypatch, bot)
    _run(sos_service.delivery_retry_job(rid))
    assert bot.sent == []  # решённая заявка не получает повторной попытки


def test_row_text_shows_not_delivered_marker(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.set_sos_delivery_failed(rid, True))
    row = _run(db.list_sos_reports_page(limit=10))[0]
    text = _run(admin_sos._row_text(row))
    assert "не доставлен" in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью 24.09, находка 2 — членство бота при привязке + здоровье привязанного чата
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_asos_bind_step_bot_not_member_keeps_pending_and_instructs(tmp_path):
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(sos_service.set_pending_bind(ADMIN_ID, None))
    _run(state.set_state(SosChatBind.waiting))

    class _Origin:
        chat = FakeChat(CHAT_ID, title="Чат оргов Москвы")

    bot = FakeBot()
    bot.chat_member_status[CHAT_ID] = "left"  # бота ещё нет в чате

    msg = FakeMessage(user_id=ADMIN_ID, forward_origin=_Origin())
    _run(admin_sos.asos_bind_step(msg, state, bot))

    assert any("Сначала добавьте бота" in a[0] for a in msg.answers)
    assert _run(state.get_state()) == SosChatBind.waiting.state  # заявка ещё активна
    assert _run(sos_service.get_pending_bind(ADMIN_ID)) is not None  # не потреблена
    assert _run(sos_service.sos_chat_for_city(None)) is None  # не привязано


def test_asos_bind_step_retries_after_bot_added_to_chat(tmp_path):
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(sos_service.set_pending_bind(ADMIN_ID, None))
    _run(state.set_state(SosChatBind.waiting))

    class _Origin:
        chat = FakeChat(CHAT_ID, title="Чат оргов Москвы")

    bot = FakeBot()
    bot.chat_member_status[CHAT_ID] = "left"
    msg1 = FakeMessage(user_id=ADMIN_ID, forward_origin=_Origin())
    _run(admin_sos.asos_bind_step(msg1, state, bot))
    assert _run(state.get_state()) == SosChatBind.waiting.state

    bot.chat_member_status[CHAT_ID] = "member"  # менеджер добавил бота в группу
    msg2 = FakeMessage(user_id=ADMIN_ID, forward_origin=_Origin())
    _run(admin_sos.asos_bind_step(msg2, state, bot))

    assert _run(state.get_state()) is None
    assert any("Готово" in a[0] for a in msg2.answers)
    assert _run(sos_service.sos_chat_for_city(None)) == {"chat_id": CHAT_ID, "title": "Чат оргов Москвы"}


def test_post_card_migrates_chat_and_delivers_to_new_id(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    from aiogram.exceptions import TelegramMigrateToChat

    NEW_CHAT_ID = -1009022222
    bot = FakeBot()
    bot.send_failures[CHAT_ID] = [
        TelegramMigrateToChat(method=None, message="migrated", migrate_to_chat_id=NEW_CHAT_ID),
    ]

    result = _run(sos_service.post_card(bot, rid))
    assert result.chat_delivered is True
    assert bot.sent[0][0] == NEW_CHAT_ID
    row = _run(db.get_sos_report(rid))
    assert row["chat_id"] == NEW_CHAT_ID
    chat = _run(sos_service.sos_chat_for_city(None))
    assert chat["chat_id"] == NEW_CHAT_ID  # перепривязка сохранена


def test_post_card_marks_chat_unhealthy_and_alerts_once_per_hour(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid1 = _run(db.create_sos_report(DELEGATE_ID, None))
    rid2 = _run(db.create_sos_report(DELEGATE_ID, None))

    alert_bot = FakeBot()
    _patch_scheduler_bot(monkeypatch, alert_bot)

    bot = FakeBot()
    bot.send_failures[CHAT_ID] = [Exception("kicked"), Exception("kicked again")]

    _run(sos_service.post_card(bot, rid1))
    assert sos_service.chat_is_unhealthy(CHAT_ID) is True
    first_alerts = [s for s in alert_bot.sent if "Чат SOS недоступен" in s[1]]
    assert len(first_alerts) == 1  # первый провал -> один алерт

    _run(sos_service.post_card(bot, rid2))
    second_alerts = [s for s in alert_bot.sent if "Чат SOS недоступен" in s[1]]
    assert len(second_alerts) == 1  # второй провал в пределах часа -> без повтора (троттлинг)


def test_render_sos_screen_shows_red_line_when_chat_unhealthy(tmp_path):
    _ready(tmp_path)
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    sos_service._unhealthy_chats.add(CHAT_ID)
    try:
        text, _kb = _run(admin_sos.render_sos_screen(ADMIN_ID))
        assert "🔴 Чат SOS не отвечает" in text
    finally:
        sos_service._unhealthy_chats.discard(CHAT_ID)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью 24.09, находка 3 — напоминание взявшему + окно повторного открытия SOS делегатом
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_claim_schedules_claimed_reminder(tmp_path, monkeypatch):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    scheduled = []
    monkeypatch.setattr(
        sos_service, "schedule_claimed_reminder",
        lambda report_id, minutes, claimant_id=None: scheduled.append((report_id, minutes, claimant_id)),
    )
    cb = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_claim(cb, FakeBot()))
    assert scheduled == [(rid, sos_service.DEFAULT_CLAIMED_REMIND_MINUTES, ADMIN_ID)]


def test_sos_resolve_cancels_claimed_reminder(tmp_path, monkeypatch):
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(db.claim_sos_report(rid, ADMIN_ID, "Админ"))
    cancelled = []
    monkeypatch.setattr(sos_service, "cancel_claimed_reminder", lambda report_id: cancelled.append(report_id))
    cb = FakeCallback(f"sos_resolve:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_resolve(cb, FakeBot()))
    assert cancelled == [rid]


def test_sos_start_recent_open_report_offers_followup_not_block(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_start(msg, state))

    assert any("Сигнал уже у оргкомитета" in a[0] and "ещё не взяли" in a[0] for a in msg.answers)
    assert _run(state.get_state()) == SosReport.collecting.state
    data = _run(state.get_data())
    assert data["sos_collecting_report_id"] == rid
    # Второй строки НЕ появилось — заявка не задублирована.
    assert len(_run(db.list_sos_reports_page(limit=10))) == 1


def test_sos_start_recent_followup_relays_into_thread_and_stays_collecting(tmp_path):
    """D-31: режим «дописываю SOS» персистентный — в отличие от старого одноразового followup,
    следующее сообщение НЕ закрывает состояние само."""
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))
    row = _run(db.get_sos_report(rid))

    state = _fresh_state(DELEGATE_ID)
    start_msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_start(start_msg, state))
    assert _run(state.get_state()) == SosReport.collecting.state

    followup_msg = FakeMessage(text="Ещё болит голова", user_id=DELEGATE_ID)
    followup_msg.bot = bot
    _run(sos_handlers.sos_collecting_step(followup_msg, state))

    assert followup_msg.copies == [(row["chat_id"], row["card_message_id"])]
    assert _run(state.get_state()) == SosReport.collecting.state  # НЕ закрылось одним сообщением


def test_sos_start_old_open_report_allows_new_sos_and_links_prior(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(db.set_setting("sos_reopen_window_minutes", "10"))
    old_rid = _run(db.create_sos_report(DELEGATE_ID, None))
    stale = (msk_now() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")

    async def _age_it():
        async with db.aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute("UPDATE sos_reports SET created_at = ? WHERE id = ?", (stale, old_rid))
            await conn.commit()

    _run(_age_it())

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    msg.bot = FakeBot()
    _run(sos_handlers.sos_start(msg, state))

    assert any("Сигнал отправлен оргкомитету" in a[0] for a in msg.answers)  # новая заявка, не followup

    new_report = _run(db.get_open_sos_report(DELEGATE_ID))
    # Обе заявки теперь открыты — берём новую (наибольший id).
    assert new_report["id"] != old_rid
    assert new_report["prior_open_report_id"] == old_rid
    text = sos_service.render_card_text(new_report, None)
    assert f"У делегата есть открытый SOS #{old_rid}" in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью 24.09, находка 4 — «✅ Решено» убирает кнопки под карточкой
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sos_resolve_removes_buttons_keeps_resolved_text(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))

    cb = FakeCallback(f"sos_resolve:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_resolve(cb, bot))

    chat_id, message_id, text, kwargs = bot.edited[-1]
    assert kwargs.get("reply_markup") is None
    assert "✅ Решено:" in text


def test_sos_claim_keeps_buttons_on_card(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot = FakeBot()
    _run(sos_service.post_card(bot, rid))

    cb = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_claim(cb, bot))

    chat_id, message_id, text, kwargs = bot.edited[-1]
    assert kwargs.get("reply_markup") is not None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревью 24.09, находка 5 — кнопки карточки работают только в её собственном чате
# ══════════════════════════════════════════════════════════════════════════════════════════

OTHER_GROUP_ID = -1009099999


def test_sos_claim_from_wrong_group_rejected(tmp_path):
    _ready(tmp_path)
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(sos_service.post_card(FakeBot(), rid))  # карточка живёт в CHAT_ID

    wrong_msg = FakeMessage(chat_id=OTHER_GROUP_ID, chat_type="group")
    cb = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID, message=wrong_msg)
    _run(admin_sos.sos_claim(cb, FakeBot()))

    assert cb.answers[0] == ("Эта карточка не из чата SOS", True)
    row = _run(db.get_sos_report(rid))
    assert row["claimed_by"] is None


def test_sos_claim_from_bound_chat_allowed_by_any_member(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(sos_service.post_card(FakeBot(), rid))

    right_msg = FakeMessage(chat_id=CHAT_ID, chat_type="group")
    cb = FakeCallback(f"sos_claim:{rid}", user_id=MANAGER_ID, message=right_msg)
    _run(admin_sos.sos_claim(cb, FakeBot()))

    assert cb.answers[0][0] == "Взято."
    row = _run(db.get_sos_report(rid))
    assert row["claimed_by"] == MANAGER_ID


def test_sos_resolve_from_wrong_group_rejected(tmp_path):
    _ready(tmp_path)
    _run(sos_service.bind_sos_chat(ADMIN_ID, CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    _run(sos_service.post_card(FakeBot(), rid))

    wrong_msg = FakeMessage(chat_id=OTHER_GROUP_ID, chat_type="group")
    cb = FakeCallback(f"sos_resolve:{rid}", user_id=ADMIN_ID, message=wrong_msg)
    _run(admin_sos.sos_resolve(cb, FakeBot()))

    assert cb.answers[0] == ("Эта карточка не из чата SOS", True)
    row = _run(db.get_sos_report(rid))
    assert row["resolved_at"] is None


def test_sos_claim_fallback_dm_still_works_from_private_chat(tmp_path):
    """Личка (фоллбэк-веер, `chat_id` не сохраняется) — `chat_type` по умолчанию "private",
    происхождение не проверяется предметно (уже под капой `moderate_reg`)."""
    _ready(tmp_path)
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    cb = FakeCallback(f"sos_claim:{rid}", user_id=ADMIN_ID)  # message chat_id=user_id, private
    _run(admin_sos.sos_claim(cb, FakeBot()))
    assert cb.answers[0][0] == "Взято."


# ══════════════════════════════════════════════════════════════════════════════════════════
# Часть А (ревью SOS-переводов): sos_delivery_failed_text/sos_recent_followup_text/
# sos_fallback_contact_text уходили делегату сырой русской строкой (`sos_recent_followup_text`
# — из-за подстановки {claim_status} ДО перевода шаблона, тот же класс бага, что LANG-02 уже
# закрыла везде в чате). Ниже — lang="en" тесты по всем трём + честный HTML-фоллбэк контакта +
# «здоровье чата» на эскалации (тот же контур, что уже был у карточки).
# ══════════════════════════════════════════════════════════════════════════════════════════

from services.i18n_form_manual import FORM_DEFAULT_EN, seed  # noqa: E402


async def _make_english_delegate(tid: int, **kwargs):
    await _add_delegate(tid, **kwargs)
    await db.set_setting("delegate_lang_enabled", "on")
    await seed("en")
    await db.set_user_lang(tid, "en")


def test_sos_start_total_failure_translates_for_english_delegate(tmp_path, monkeypatch):
    """`sos_delivery_failed_text` уже шло через `reg_i18n.say` до этой правки — тест закрепляет
    поведение (регрессия), а не чинит его."""
    _ready(tmp_path)
    _run(_make_english_delegate(DELEGATE_ID))
    monkeypatch.setattr(
        "handlers.admin_caps.capability_holders",
        lambda cap, city=None: _async_result([]),
    )
    monkeypatch.setattr(sos_service, "schedule_delivery_retry", lambda report_id, delay_minutes=1: None)

    bot = FakeBot()
    bot.send_failures[ADMIN_ID] = [Exception("blocked")]

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    msg.bot = bot
    _run(sos_handlers.sos_start(msg, state))

    default_ru = (
        "Не получилось передать SOS оргкомитету. Подойди к стойке регистрации или к любому "
        "человеку в форме оргкомитета."
    )
    assert any(a[0] == FORM_DEFAULT_EN[default_ru] for a in msg.answers)
    assert not any(a[0] == default_ru for a in msg.answers)  # русский НЕ ушёл вперемешку


def test_sos_start_emergency_contact_falls_back_without_html_on_parse_error(tmp_path, monkeypatch):
    """Контакт — свободный ввод менеджера, не переводится (правило), но обязан дойти даже если
    в нём затесался невалидный для HTML-разметки символ (`<3` и т.п.) — один повтор без
    `parse_mode`, тот же WR-04-приём, что `handlers/user_actions.py::show_contacts`."""
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    contact_text = "Экстренный телефон: +7 999 <3 000-00-00"
    _run(db.set_setting("sos_fallback_contact_text", contact_text))
    monkeypatch.setattr(
        "handlers.admin_caps.capability_holders",
        lambda cap, city=None: _async_result([]),
    )
    monkeypatch.setattr(sos_service, "schedule_delivery_retry", lambda report_id, delay_minutes=1: None)

    bot = FakeBot()
    bot.send_failures[ADMIN_ID] = [Exception("blocked")]

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    msg.bot = bot

    calls = {"n": 0}
    real_answer = msg.answer

    async def flaky_answer(text, parse_mode=None, reply_markup=None):
        if text == contact_text:
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("can't parse entities: unsupported start tag \"3\"")
        return await real_answer(text, parse_mode=parse_mode, reply_markup=reply_markup)

    msg.answer = flaky_answer

    _run(sos_handlers.sos_start(msg, state))

    assert calls["n"] == 2  # первая попытка упала, повтор без разметки дошёл
    assert any(a[0] == contact_text for a in msg.answers)  # делегат всё равно получил контакт


def test_recent_followup_text_translates_open_and_claimed_for_english_delegate(tmp_path):
    _ready(tmp_path)
    _run(_make_english_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    state = _fresh_state(DELEGATE_ID)
    msg = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_start(msg, state))

    assert any("signal is already with the organizers" in a[0] and "not picked up yet" in a[0] for a in msg.answers)
    assert not any("Сигнал уже у оргкомитета" in a[0] for a in msg.answers)

    _run(db.claim_sos_report(rid, ADMIN_ID, "Иван"))
    state2 = _fresh_state(DELEGATE_ID)
    msg2 = FakeMessage(text="🆘 SOS", user_id=DELEGATE_ID)
    _run(sos_handlers.sos_start(msg2, state2))
    assert any("picked up by Иван" in a[0] for a in msg2.answers)


def test_escalation_migrates_chat_before_alerting(tmp_path, monkeypatch):
    """Ревью 24.09 (находка 2, добавка): эскалация теперь проходит тот же контур «здоровье
    чата», что карточка — миграция в супергруппу перепривязывает `sos_chat_id` и повторяет ОДНУ
    попытку в новый chat_id вместо простого `logger.warning`."""
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    ESCALATION_CHAT_ID = -1009024001
    _run(sos_service.bind_sos_chat(ADMIN_ID, ESCALATION_CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    from aiogram.exceptions import TelegramMigrateToChat

    new_chat_id = -1009024002
    bot = FakeBot()
    bot.send_failures[ESCALATION_CHAT_ID] = [
        TelegramMigrateToChat(method=None, message="migrated", migrate_to_chat_id=new_chat_id),
    ]
    _patch_scheduler_bot(monkeypatch, bot)

    _run(sos_service.escalation_job(rid))

    row = _run(db.get_sos_report(rid))
    assert row["escalated_at"] is not None
    assert any(s[0] == new_chat_id and "Никто не взял" in s[1] for s in bot.sent)  # текст ушёл в НОВЫЙ chat_id
    chat = _run(sos_service.sos_chat_for_city(None))
    assert chat["chat_id"] == new_chat_id  # перепривязка сохранена
    assert sos_service.chat_is_unhealthy(new_chat_id) is False


def test_escalation_marks_chat_unhealthy_on_send_failure(tmp_path, monkeypatch):
    """Провал отправки текста эскалации в привязанный чат (не миграция — обычная ошибка) метит
    чат нездоровым и алертит держателей `settings`, тот же приём, что у карточки."""
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    ESCALATION_CHAT_ID = -1009024011
    _run(sos_service.bind_sos_chat(ADMIN_ID, ESCALATION_CHAT_ID, "Чат оргов", None))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))

    bot = FakeBot()
    bot.send_failures[ESCALATION_CHAT_ID] = [Exception("kicked")]
    _patch_scheduler_bot(monkeypatch, bot)
    sos_service._chat_alert_sent_at.pop(ESCALATION_CHAT_ID, None)  # изоляция от др. тестов/чатов

    try:
        _run(sos_service.escalation_job(rid))
        assert sos_service.chat_is_unhealthy(ESCALATION_CHAT_ID) is True
        assert any("Чат SOS недоступен" in s[1] for s in bot.sent)
    finally:
        sos_service._unhealthy_chats.discard(ESCALATION_CHAT_ID)
        sos_service._chat_alert_sent_at.pop(ESCALATION_CHAT_ID, None)


# ══════════════════════════════════════════════════════════════════════════════════════════
# «✅ Решено» у орга закрывает режим «дописываю SOS» у делегата
# ══════════════════════════════════════════════════════════════════════════════════════════

def _delegate_ctx(storage, bot):
    return FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=DELEGATE_ID, user_id=DELEGATE_ID))


def _resolve_with_storage(rid, bot, storage):
    cb = FakeCallback(f"sos_resolve:{rid}", user_id=ADMIN_ID)
    _run(admin_sos.sos_resolve(cb, bot, fsm_storage=storage))
    return cb


def test_sos_resolve_clears_delegate_collecting_for_this_report(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    bot, storage = FakeBot(), MemoryStorage()
    ctx = _delegate_ctx(storage, bot)
    _run(sos_handlers._enter_collecting(ctx, rid, None))

    _resolve_with_storage(rid, bot, storage)
    assert _run(ctx.get_state()) is None
    assert _run(ctx.get_data()) == {}
    to_delegate = [s for s in bot.sent if s[0] == DELEGATE_ID]
    assert to_delegate and "Оргкомитет отметил вопрос решённым" in to_delegate[-1][1]
    assert to_delegate[-1][2].get("reply_markup") is not None  # вернули главное меню


def test_sos_resolve_keeps_collecting_for_other_report(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    old = _run(db.create_sos_report(DELEGATE_ID, None))
    new = _run(db.create_sos_report(DELEGATE_ID, None))
    bot, storage = FakeBot(), MemoryStorage()
    ctx = _delegate_ctx(storage, bot)
    _run(sos_handlers._enter_collecting(ctx, new, None))

    _resolve_with_storage(old, bot, storage)
    assert _run(ctx.get_state()) == SosReport.collecting.state
    assert _run(ctx.get_data())["sos_collecting_report_id"] == new
    to_delegate = [s for s in bot.sent if s[0] == DELEGATE_ID]
    assert to_delegate and to_delegate[-1][2].get("reply_markup") is None


def test_sos_resolve_notifies_delegate_in_english(tmp_path, monkeypatch):
    from services import i18n as i18n_service
    from services.i18n_form_manual import FORM_DEFAULT_EN
    _ready(tmp_path)
    _run(_add_delegate(DELEGATE_ID))
    rid = _run(db.create_sos_report(DELEGATE_ID, None))
    ru = "Оргкомитет отметил вопрос решённым. Снова нужна помощь — жми «🆘 SOS»."

    async def _ctx(_tid):
        return "en", {i18n_service.src_hash(ru): FORM_DEFAULT_EN[ru]}

    monkeypatch.setattr(i18n_service, "context", _ctx)
    bot = FakeBot()
    _resolve_with_storage(rid, bot, None)
    assert [s[1] for s in bot.sent if s[0] == DELEGATE_ID][-1] == FORM_DEFAULT_EN[ru]
