"""29.09: сотрудник, до которого бот не может достучаться, виден менеджерам.

Прод 19.09: `notify_by_capability` падал на части модераторов (заблокировали бота / ни разу не
нажали /start) — десятки ERROR в логе, заявки копились, а менеджеры не знали, кому уведомления
не доходят. Эти тесты закрепляют:

- отметку «недоступен» в БД (`staff_unreachable`) при Forbidden / «chat not found» и снятие её
  при следующей удачной доставке или когда человек сам пишет боту;
- что отметка НЕ меняет, кому и в каком порядке уходит уведомление, и что сбой самой отметки
  не ломает цикл рассылки (fail-soft);
- «chat not found» — один WARNING на человека в сутки вместо ERROR на каждую заявку;
- пометку в «👥 Роли и доступы» и одну строку в «📊 Итоги дня».

pytest-asyncio в проекте нет — async через asyncio.run(); БД — tmp_path.
"""
import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from config import config
from database import db
from handlers.access import admin_caps, admin_roles
from services import daily_digest as dd
from services.access import staff_reach
from services.infra.timeutil import msk_now
from tests._dbtpl import fast_init_db

ADMIN_A = 929001
ADMIN_B = 929002
MOD_BLOCKED = 929003
MOD_GHOST = 929004  # ни разу не нажимал /start — «chat not found»
MOD_OK = 929005


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "staff_unreachable.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_A, ADMIN_B]
    admin_caps._blocked_notified_at.clear()
    staff_reach.reset_state()


def _add_mods():
    for uid in (MOD_BLOCKED, MOD_GHOST, MOD_OK):
        asyncio.run(db.add_staff(uid, "reg_manager", ADMIN_A))


class _Bot:
    def __init__(self, forbidden=(), ghost=(), other=()):
        self.calls = []
        self.forbidden = set(forbidden)
        self.ghost = set(ghost)
        self.other = set(other)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.calls.append(chat_id)
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
        if chat_id in self.ghost:
            raise TelegramBadRequest(method=None, message="Bad Request: chat not found")
        if chat_id in self.other:
            raise RuntimeError("network boom")


# ── БД ──────────────────────────────────────────────────────────────────────────────────────

def test_db_mark_list_clear(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "blocked")) is True
    assert asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "blocked")) is False  # уже отмечен
    marks = asyncio.run(db.list_staff_unreachable())
    assert set(marks) == {MOD_BLOCKED}
    assert marks[MOD_BLOCKED]["since"]
    assert asyncio.run(db.clear_staff_unreachable(MOD_BLOCKED)) is True
    assert asyncio.run(db.clear_staff_unreachable(MOD_BLOCKED)) is False
    assert asyncio.run(db.list_staff_unreachable()) == {}


def test_repeat_mark_keeps_first_since(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "blocked"))
    since = asyncio.run(db.list_staff_unreachable())[MOD_BLOCKED]["since"]
    asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "chat_not_found"))
    row = asyncio.run(db.list_staff_unreachable())[MOD_BLOCKED]
    assert row["since"] == since
    assert row["reason"] == "chat_not_found"


def test_table_classified_for_user_purge():
    assert "staff_unreachable" in db.USER_PURGE_EXCLUDED


# ── Классификация ошибок ────────────────────────────────────────────────────────────────────

def test_is_unreachable_error():
    assert staff_reach.is_unreachable_error(
        TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user"))
    assert staff_reach.is_unreachable_error(
        TelegramForbiddenError(method=None, message="Forbidden: user is deactivated"))
    assert staff_reach.is_unreachable_error(
        TelegramBadRequest(method=None, message="Bad Request: chat not found"))
    assert not staff_reach.is_unreachable_error(
        TelegramBadRequest(method=None, message="Bad Request: can't parse entities"))
    assert not staff_reach.is_unreachable_error(RuntimeError("network boom"))


# ── notify_by_capability ────────────────────────────────────────────────────────────────────

def test_notify_marks_blocked_and_ghost_but_not_other_errors(tmp_path):
    _ready(tmp_path)
    _add_mods()
    bot = _Bot(forbidden=[MOD_BLOCKED], ghost=[MOD_GHOST], other=[MOD_OK])

    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "новая заявка"))

    assert sent == 2  # только два суперадмина
    assert set(asyncio.run(db.list_staff_unreachable())) == {MOD_BLOCKED, MOD_GHOST}


def test_notify_recipients_and_order_unchanged(tmp_path):
    _ready(tmp_path)
    _add_mods()
    asyncio.run(db.mark_staff_unreachable(MOD_OK, "blocked"))  # старая отметка
    expected = asyncio.run(admin_caps.capability_holders("moderate_reg"))
    bot = _Bot(forbidden=[MOD_BLOCKED], ghost=[MOD_GHOST])

    asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "новая заявка"))

    main = [c for c in bot.calls if c in expected]
    assert main[: len(expected)] == expected  # отмеченным тоже шлём, порядок тот же


def test_successful_delivery_clears_mark(tmp_path):
    _ready(tmp_path)
    _add_mods()
    asyncio.run(db.mark_staff_unreachable(MOD_OK, "blocked"))
    staff_reach.reset_state()  # как после рестарта: кэш поднимется из БД

    asyncio.run(admin_caps.notify_by_capability(_Bot(), "moderate_reg", "новая заявка"))

    assert asyncio.run(db.list_staff_unreachable()) == {}


def test_marking_failure_never_breaks_the_loop(tmp_path, monkeypatch):
    _ready(tmp_path)
    _add_mods()

    async def boom(*a, **kw):
        raise RuntimeError("db is down")
    monkeypatch.setattr(db, "mark_staff_unreachable", boom)
    monkeypatch.setattr(db, "clear_staff_unreachable", boom)
    monkeypatch.setattr(db, "list_staff_unreachable", boom)
    bot = _Bot(forbidden=[MOD_BLOCKED], ghost=[MOD_GHOST])

    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "новая заявка"))

    assert sent == 3  # ADMIN_A, ADMIN_B, MOD_OK
    assert MOD_OK in bot.calls


def test_chat_not_found_warns_once_per_day_not_error(tmp_path, caplog):
    _ready(tmp_path)
    _add_mods()
    bot = _Bot(ghost=[MOD_GHOST])

    with caplog.at_level(logging.DEBUG):
        asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "раз"))
        asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "два"))

    about_ghost = [r for r in caplog.records if str(MOD_GHOST) in r.getMessage()]
    assert not [r for r in about_ghost if r.levelno >= logging.ERROR]
    assert len([r for r in about_ghost if r.levelno == logging.WARNING]) == 1


# ── Человек сам пишет боту ─────────────────────────────────────────────────────────────────

def test_incoming_from_marked_person_clears_mark(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "blocked"))
    asyncio.run(staff_reach.note_incoming(MOD_OK))
    assert set(asyncio.run(db.list_staff_unreachable())) == {MOD_BLOCKED}
    asyncio.run(staff_reach.note_incoming(MOD_BLOCKED))
    assert asyncio.run(db.list_staff_unreachable()) == {}


def test_middleware_clears_and_always_calls_handler(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "blocked"))

    class _User:
        id = MOD_BLOCKED

    called = []

    async def handler(event, data):
        called.append(event)
        return "ok"

    mw = staff_reach.StaffReachMiddleware()
    assert asyncio.run(mw(handler, "upd", {"event_from_user": _User()})) == "ok"
    assert asyncio.run(db.list_staff_unreachable()) == {}

    async def boom(*a, **kw):
        raise RuntimeError("db is down")
    monkeypatch.setattr(staff_reach, "note_incoming", boom)
    assert asyncio.run(mw(handler, "upd2", {"event_from_user": _User()})) == "ok"
    assert asyncio.run(mw(handler, "upd3", {})) == "ok"
    assert called == ["upd", "upd2", "upd3"]


# ── Экран «👥 Роли и доступы» ───────────────────────────────────────────────────────────────

def test_roles_screen_marks_unreachable_people(tmp_path):
    _ready(tmp_path)
    _add_mods()
    asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "blocked"))
    asyncio.run(db.mark_staff_unreachable(ADMIN_B, "chat_not_found"))

    text = asyncio.run(admin_roles.render_roles_text())

    lines = text.split("\n")
    blocked_line = next(line for line in lines if str(MOD_BLOCKED) in line and line.startswith("•"))
    ok_line = next(line for line in lines if str(MOD_OK) in line and line.startswith("•"))
    assert "не получает уведомления" in blocked_line
    assert "/start" in blocked_line
    assert "не получает уведомления" not in ok_line
    assert any(str(ADMIN_B) in line and "не получает уведомления" in line for line in lines)


def test_roles_screen_survives_mark_lookup_failure(tmp_path, monkeypatch):
    _ready(tmp_path)
    _add_mods()

    async def boom(*a, **kw):
        raise RuntimeError("db is down")
    monkeypatch.setattr(db, "list_staff_unreachable", boom)
    text = asyncio.run(admin_roles.render_roles_text())
    assert str(MOD_BLOCKED) in text and "не получает уведомления" not in text


# ── «📊 Итоги дня» ─────────────────────────────────────────────────────────────────────────

def test_unreachable_line_pure():
    assert dd.unreachable_line([]) is None
    line = dd.unreachable_line(["Марина <b>", "менеджер #5"])
    assert line.startswith("⚠️")
    assert "Марина &lt;b&gt;" in line and "менеджер #5" in line
    assert "/start" in line and "Роли и доступы" in line


def test_digest_adds_one_line_when_a_recipient_is_unreachable(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.add_user({"telegram_id": 929050, "full_name": "Делегат",
                             "registration_date": msk_now().strftime("%Y-%m-%d 12:00:00")}))
    asyncio.run(db.add_user({"telegram_id": MOD_BLOCKED, "full_name": "Пётр Модератор",
                             "registration_date": "2026-01-15 12:00:00"}))
    asyncio.run(db.mark_staff_unreachable(MOD_BLOCKED, "blocked"))

    async def fake_recipients(city):
        return [ADMIN_A, MOD_BLOCKED]
    monkeypatch.setattr(dd, "digest_recipients", fake_recipients)
    bot = _Bot(forbidden=[MOD_BLOCKED])
    sent_texts = []
    orig = bot.send_message

    async def capture(chat_id, text, parse_mode=None, reply_markup=None):
        await orig(chat_id, text, parse_mode=parse_mode)
        sent_texts.append(text)
    bot.send_message = capture

    assert asyncio.run(dd.send_city_digest(bot, None)) == 1
    assert sent_texts[0].count("⚠️") == 1
    assert "Пётр Модератор" in sent_texts[0]


def test_digest_has_no_warning_line_when_everyone_reachable(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.add_user({"telegram_id": 929050, "full_name": "Делегат",
                             "registration_date": msk_now().strftime("%Y-%m-%d 12:00:00")}))

    async def fake_recipients(city):
        return [ADMIN_A]
    monkeypatch.setattr(dd, "digest_recipients", fake_recipients)
    sent_texts = []

    class _B:
        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            sent_texts.append(text)

    assert asyncio.run(dd.send_city_digest(_B(), None)) == 1
    assert "⚠️" not in sent_texts[0]
