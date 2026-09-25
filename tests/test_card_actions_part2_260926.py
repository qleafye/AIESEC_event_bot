"""Phase 33 (delegate-card admin actions, часть 2): четыре новых пункта карточки `/find` —
пометка «кем возвращена» в each-режиме (задача 0), «🧹 Сбросить зависшую анкету» (задача 1),
«👤 Роль для «только /start»» (задача 2), «📎 Заменить резюме» (задача 3). Каждая — свой раздел
файла, тем же приёмом, что tests/test_card_actions_260925.py: fake callback/message на слое
хендлера + прямой вызов сервиса на слое БД.

pytest-asyncio недоступна в этом окружении — `asyncio.run()` на каждый async-хелпер, БД —
шаблонная копия (`tests/_dbtpl.py::fast_init_db`)."""
from __future__ import annotations

import asyncio

import pytest

import cities
from config import config
from database import db
from handlers.admin_caps import role_caps_key
from services.revert_pending import revert_to_pending
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260926201
BOUND_MSK_ID = 260926202
BOUND_SPB_ID = 260926203
DELEGATE_ID = 260926301
STARTED_ONLY_ID = 260926302

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


def _db_ready(tmp_path, name="test_card_actions_part2_260926.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    config.GOOGLE_SHEET_ID = ""
    config.GOOGLE_CREDENTIALS_FILE = ""


async def _enable_cities_module():
    await db.set_setting("event_city_enabled", "on")


async def _setup_bound_staff():
    await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


async def _seed_user(telegram_id, *, city="msk", status="approved", full_name="Тест Тестов",
                      username="testdel"):
    await db.add_user({
        "telegram_id": telegram_id, "full_name": full_name, "username": username,
        "registration_date": "2026-01-01", "event_city": city,
    })
    if status != "approved":
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id))
            await conn.commit()


class _FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, text, kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return object()


class _FakeUser:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


class _FakeMessage:
    def __init__(self):
        self.edits = []  # (text, reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data, user_id, bot=None, username=None, full_name=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username=username, full_name=full_name)
        self.message = _FakeMessage()
        self.bot = bot or _FakeBot()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Задача 0: each-режим — карточка менеджерам называет админа, вернувшего заявку
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_revert_each_mode_admin_text_names_the_admin(tmp_path, monkeypatch):
    """each-режим (reg_submit_notify_mode=each, дефолт): admin_text, ушедший в
    notify_by_capability, обязан нести «...админом <имя>» — тот же посыл, что digest-блок
    «Возвращены на модерацию» уже несёт с 9d15517 (там причина видна из заголовка блока)."""
    _db_ready(tmp_path)
    calls = []

    async def _fake_notify_by_capability(bot, cap, text, **kwargs):
        calls.append(text)
        return 1

    monkeypatch.setattr("handlers.admin_caps.notify_by_capability", _fake_notify_by_capability)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(
            DELEGATE_ID, by_admin=SUPERADMIN_ID, admin_name="Мария Менеджерова",
            notify=False, bot=_FakeBot(),
        )

    _run(scenario())
    assert len(calls) == 1
    assert "↩️ <b>Возвращена на модерацию админом Мария Менеджерова</b>" in calls[0]


def test_revert_each_mode_without_admin_name_keeps_old_text(tmp_path, monkeypatch):
    """`admin_name` не передан (обратная совместимость) — текст байт-в-байт прежний."""
    _db_ready(tmp_path)
    calls = []

    async def _fake_notify_by_capability(bot, cap, text, **kwargs):
        calls.append(text)
        return 1

    monkeypatch.setattr("handlers.admin_caps.notify_by_capability", _fake_notify_by_capability)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False, bot=_FakeBot())

    _run(scenario())
    assert "↩️ <b>Возвращена на модерацию</b>" in calls[0]
    assert "админом" not in calls[0]


def test_revertp_apply_passes_admin_name_from_callback(tmp_path, monkeypatch):
    """UI-слой: `revertp_apply` вычисляет имя из `callback.from_user` тем же приёмом, что
    `admin_sos.py::sos_claim`/`sos_resolve` (full_name -> username -> код-фолбэк «Админ»)."""
    from handlers import admin_revert_pending

    _db_ready(tmp_path)
    calls = []

    async def _fake_revert(*a, **kw):
        calls.append(kw)
        return {"ok": True, "sheet_updated": True, "notified": False}

    monkeypatch.setattr("handlers.admin_revert_pending.revert_to_pending", _fake_revert)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(
            f"revertp_apply:{DELEGATE_ID}:0", SUPERADMIN_ID, full_name="Мария Менеджерова",
        )
        await admin_revert_pending.revertp_apply(cb)
        return cb

    _run(scenario())
    assert calls[0]["admin_name"] == "Мария Менеджерова"


def test_revertp_apply_admin_name_falls_back_to_username_then_code(tmp_path, monkeypatch):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)
    calls = []

    async def _fake_revert(*a, **kw):
        calls.append(kw)
        return {"ok": True, "sheet_updated": True, "notified": False}

    monkeypatch.setattr("handlers.admin_revert_pending.revert_to_pending", _fake_revert)

    async def scenario_username():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_apply:{DELEGATE_ID}:0", SUPERADMIN_ID, username="mgr_no_name")
        await admin_revert_pending.revertp_apply(cb)

    _run(scenario_username())
    assert calls[0]["admin_name"] == "mgr_no_name"

    calls.clear()
    _db_ready(tmp_path, name="test_card_actions_part2_260926_b.db")

    async def scenario_bare():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_apply:{DELEGATE_ID}:0", SUPERADMIN_ID)
        await admin_revert_pending.revertp_apply(cb)

    _run(scenario_bare())
    assert calls[0]["admin_name"] == "Админ"
