"""Квик 260919-u7e (P3, находка #03-moderation): `pending_reminder_loop` больше не шлёт только
`config.ADMIN_IDS` — на проде 7 reg_manager из `staff` не получали вообще ничего.

Покрывает: получатели = `capability_holders("moderate_reg")` (ADMIN_IDS + role-holders, без
дублей); ПРИВЯЗАННЫЙ к городу (`staff.city`) получатель — счётчик своего города; ЛЮБОЙ другой
(staff без города, ADMIN_IDS) — общее число + разбивка по городам в одной строке, независимо от
того, что выбрано у него в шапке панели (owner correction поверх первой версии этого квика —
на проде ни один менеджер не привязан к spb/tyumen, а непривязанные по умолчанию смотрят на
дефолтный город, и напоминание про эти города не будило никого); нулевой счётчик -> получателю
не шлём; блокировка одного получателя не рвёт рассылку остальным (тот же `_blocked_admins`).

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


# ── привязанный к городу получатель — счётчик своего города ────────────────────────────────

def test_city_bound_manager_sees_only_own_city_count(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    _add_pending(1, city="msk")
    _add_pending(2, city="msk")
    _add_pending(3, city="spb")
    for tid in (1, 2, 3):
        asyncio.run(_set_pending(tid))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    texts = _texts(bot)
    assert "Заявок в ожидании (Москва, 30-31 октября): 2." in texts[MSK_MANAGER_ID]
    assert "Заявок в ожидании (Санкт-Петербург, 3 октября): 1." in texts[SPB_MANAGER_ID]


def test_bound_manager_ignores_own_panel_header_choice(tmp_path, monkeypatch):
    """Owner correction: привязанный менеджер получает счётчик СВОЕГО города даже если у него
    в шапке панели (`admin_city__{id}`) сохранён другой выбор — привязка сильнее шапки, и
    напоминалка её вообще не читает."""
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.set_setting(f"admin_city__{MSK_MANAGER_ID}", "spb"))
    _add_pending(1, city="msk")
    _add_pending(2, city="spb")
    for tid in (1, 2):
        asyncio.run(_set_pending(tid))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    assert "Заявок в ожидании (Москва, 30-31 октября): 1." in _texts(bot)[MSK_MANAGER_ID]


# ── непривязанный получатель (в т.ч. ADMIN_IDS) — всегда общее число + разбивка ────────────

def test_unbound_recipient_and_admin_always_see_global_count_with_breakdown(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", ADMIN_ID))  # без города
    _add_pending(1, city="msk")
    _add_pending(2, city="msk")
    _add_pending(3, city="spb")
    _add_pending(4, city="tyumen")
    for tid in (1, 2, 3, 4):
        asyncio.run(_set_pending(tid))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    expected = (
        "📋 Заявок в ожидании: 4 (Москва, 30-31 октября — 2, "
        "Санкт-Петербург, 3 октября — 1, Тюмень, 3 октября — 1). Открой /admin → Заявки."
    )
    assert _texts(bot)[UNBOUND_MANAGER_ID] == expected
    assert _texts(bot)[ADMIN_ID] == expected


def test_breakdown_skips_zero_cities(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _add_pending(1, city="msk")
    asyncio.run(_set_pending(1))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    text = _texts(bot)[ADMIN_ID]
    assert text == "📋 Заявок в ожидании: 1 (Москва, 30-31 октября — 1). Открой /admin → Заявки."
    assert "Санкт-Петербург" not in text
    assert "Тюмень" not in text


def test_breakdown_folds_empty_event_city_into_default_the_same_way_the_queue_does(tmp_path, monkeypatch):
    """NULL `event_city` должен попадать в ТУ ЖЕ корзину, что и очередь заявок (city_scope
    дефолтного города — исключение остальных известных кодов, значит NULL туда тоже входит)."""
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _add_pending(1, city=None)  # без города -> дефолтный город (msk), как и в очереди
    _add_pending(2, city="spb")
    for tid in (1, 2):
        asyncio.run(_set_pending(tid))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    text = _texts(bot)[ADMIN_ID]
    assert "Москва, 30-31 октября — 1" in text
    assert "Санкт-Петербург, 3 октября — 1" in text
    assert "без города" not in text  # не отдельная корзина — слита с дефолтным городом


def test_no_breakdown_when_cities_module_off(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _add_pending(1, city="msk")
    _add_pending(2, city="spb")
    for tid in (1, 2):
        asyncio.run(_set_pending(tid))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    assert _texts(bot)[ADMIN_ID] == "📋 Заявок в ожидании: 2. Открой /admin → Заявки."


# ── совпадение с очередью ────────────────────────────────────────────────────────────────────

def test_city_bound_count_matches_what_the_queue_screen_would_show(tmp_path, monkeypatch):
    """Напоминание обязано называть ТО ЖЕ число, что привязанный менеджер увидит, открыв
    «📋 Заявки» — тот же `city_scope`/`get_pending_count`, что использует очередь."""
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    _add_pending(1, city="msk")
    _add_pending(2, city=None)  # NULL event_city -> дефолтный город (msk) считает тоже
    _add_pending(3, city="spb")
    for tid in (1, 2, 3):
        asyncio.run(_set_pending(tid))

    from cities import city_scope
    from database.db import get_pending_count as real_get_pending_count

    expected = asyncio.run(real_get_pending_count(city_scope=city_scope("msk")))

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    assert f": {expected}." in _texts(bot)[MSK_MANAGER_ID]


def test_unbound_count_matches_global_pending_count(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", ADMIN_ID))
    _add_pending(1, city="msk")
    _add_pending(2, city="spb")
    for tid in (1, 2):
        asyncio.run(_set_pending(tid))

    from database.db import get_pending_count as real_get_pending_count

    expected = asyncio.run(real_get_pending_count())

    bot = _FakeBot()
    _run_one_iteration(bot, monkeypatch)

    assert f": {expected} (" in _texts(bot)[UNBOUND_MANAGER_ID]


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
