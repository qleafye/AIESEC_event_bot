"""Квик 260919 (P3, находка #03-moderation): `pending_reminder_loop` больше не шлёт только
`config.ADMIN_IDS` — на проде 7 reg_manager из `staff` не получали вообще ничего.

Покрывает: получатели = `capability_holders("moderate_reg")` (ADMIN_IDS + role-holders, без
дублей); счётчик персональный по городскому скоупу получателя (тот же резолвер, что у очереди
«📋 Заявки», `handlers.admin_core._admin_city_view`); нулевой счётчик -> получателю не шлём;
блокировка одного получателя не рвёт рассылку остальным (тот же `_blocked_admins`, что раньше).

pytest-asyncio в проекте нет — asyncio.run(); БД — tmp_path.
"""
import asyncio

from aiogram.exceptions import TelegramForbiddenError

from config import config
from database import db
import services.reminders as reminders_mod

ADMIN_ID = 926101
MSK_MANAGER_ID = 926102
SPB_MANAGER_ID = 926103
UNBOUND_MANAGER_ID = 926104


class _StopLoop(Exception):
    """Тот же трюк, что в tests/test_permanent_send_errors_260816.py: подменённый
    asyncio.sleep роняет исключение после ОДНОЙ итерации цикла."""


class _FakeBot:
    def __init__(self, forbidden_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self._forbidden_for = forbidden_for or set()

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self._forbidden_for:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_pending_reminder_capability_260919.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _add_pending(tid, city=None):
    asyncio.run(db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}", "event_city": city,
        "registration_date": "2026-09-19 00:00:00",
    }))


async def _set_pending(tid):
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = 'pending' WHERE telegram_id = ?", (tid,))
        await conn.commit()


def _run_one_iteration(bot, monkeypatch):
    """Оставляет `_blocked_admins` как есть ПОСЛЕ прогона — некоторые тесты именно её и
    проверяют. Чистит ТОЛЬКО перед стартом (изоляция от соседнего теста в том же
    pytest-xdist воркере)."""
    reminders_mod._blocked_admins.clear()

    async def fake_sleep(_seconds):
        raise _StopLoop()

    orig_sleep = reminders_mod.asyncio.sleep
    reminders_mod.asyncio.sleep = fake_sleep
    try:
        try:
            asyncio.run(reminders_mod.pending_reminder_loop(bot))
        except _StopLoop:
            pass
        else:
            raise AssertionError("pending_reminder_loop did not reach the sleep call")
    finally:
        reminders_mod.asyncio.sleep = orig_sleep


def _texts(bot):
    return {uid: text for uid, text in bot.sent}


# ── получатели = capability_holders, не только ADMIN_IDS ───────────────────────────────────

def test_staff_reg_manager_outside_admin_ids_gets_notified(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    _add_pending(1)
    asyncio.run(_set_pending(1))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    texts = _texts(bot)
    assert ADMIN_ID in texts and MSK_MANAGER_ID in texts
    assert "Заявок в ожидании: 1" in texts[ADMIN_ID]
    assert "Заявок в ожидании: 1" in texts[MSK_MANAGER_ID]


def test_recipients_deduped_no_double_send_for_admin_who_is_also_staff(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.add_staff(ADMIN_ID, "reg_manager", ADMIN_ID))  # admin also holds the role
    _add_pending(1)
    asyncio.run(_set_pending(1))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    assert [uid for uid, _ in bot.sent].count(ADMIN_ID) == 1


# ── персональный счётчик по городскому скоупу ───────────────────────────────────────────────

def test_city_bound_manager_sees_only_own_city_count(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    # Не привязан к городу и явно выбрал «🌍 Все города» в шапке своей панели — та же
    # семантика, что и у экрана очереди (Phase 09.3, CITY-08): 09.1 (C) заведён, чтобы
    # непривязанный менеджер по умолчанию видел ДЕФОЛТНЫЙ город, а не «всё» — «всё» это
    # отдельный, явный выбор.
    asyncio.run(db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", ADMIN_ID))
    from cities import ALL_CITIES, set_admin_city
    asyncio.run(set_admin_city(UNBOUND_MANAGER_ID, ALL_CITIES))
    _add_pending(1, city="msk")
    _add_pending(2, city="msk")
    _add_pending(3, city="spb")
    for tid in (1, 2, 3):
        asyncio.run(_set_pending(tid))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    texts = _texts(bot)
    assert "Заявок в ожидании (" in texts[MSK_MANAGER_ID]
    assert "): 2." in texts[MSK_MANAGER_ID]
    assert "): 1." in texts[SPB_MANAGER_ID]
    # ALL_CITIES -> глобальный, unscoped счётчик, но со своей меткой «🌍 Все города» — та же
    # метка, что видна в шапке экрана очереди у этого менеджера.
    assert "Заявок в ожидании (🌍 Все города): 3." in texts[UNBOUND_MANAGER_ID]
    # 09.1 (C)/09.3 (CITY-08): суперадмин БЕЗ собственного выбора в шапке — как и любой другой
    # непривязанный менеджер — по умолчанию видит ДЕФОЛТНЫЙ город (Москва), не «всё»; это
    # существующее поведение экрана «📋 Заявки» (`admin_selected_city`), не выдумка напоминалки.
    assert "Заявок в ожидании (" in texts[ADMIN_ID]
    assert "): 2." in texts[ADMIN_ID]


def test_city_count_matches_what_the_queue_screen_would_show(tmp_path, monkeypatch):
    """Напоминание обязано называть ТО ЖЕ число, что менеджер увидит, открыв «📋 Заявки» —
    тот же резолвер (`_admin_city_view`) и тот же `get_pending_count(city_scope=...)`."""
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    _add_pending(1, city="msk")
    _add_pending(2, city=None)  # NULL event_city -> дефолтный город (msk) считает тоже
    _add_pending(3, city="spb")
    for tid in (1, 2, 3):
        asyncio.run(_set_pending(tid))

    from handlers.admin_core import _admin_city_view
    from database.db import get_pending_count as real_get_pending_count

    scope, label = asyncio.run(_admin_city_view(MSK_MANAGER_ID))
    expected = asyncio.run(real_get_pending_count(city_scope=scope))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    text = _texts(bot)[MSK_MANAGER_ID]
    assert f": {expected}." in text
    if label:
        assert f"({label})" in text


# ── нулевой счётчик -> тишина ────────────────────────────────────────────────────────────────

def test_zero_count_recipient_gets_no_message(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    _add_pending(1, city="msk")
    asyncio.run(_set_pending(1))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    texts = _texts(bot)
    assert ADMIN_ID in texts  # global count is 1 -> admin still notified
    assert SPB_MANAGER_ID not in texts  # spb count is 0 -> silence


def test_all_zero_sends_nothing_at_all(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    assert bot.sent == []


# ── блокировка одного получателя не рвёт рассылку остальным ────────────────────────────────

def test_blocked_staff_recipient_does_not_stop_others(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    _add_pending(1)
    asyncio.run(_set_pending(1))

    bot = _FakeBot(forbidden_for={MSK_MANAGER_ID})
    _run_one_iteration(bot, monkeypatch)

    texts = _texts(bot)
    assert ADMIN_ID in texts
    assert MSK_MANAGER_ID not in texts
    assert MSK_MANAGER_ID in reminders_mod._blocked_admins
