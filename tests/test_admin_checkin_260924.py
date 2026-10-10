"""Phase 12 (FORUM-CHECKIN.md): раздел «✅ Отметки на форуме» (handlers/forum/admin_checkin.py) —
загрузка выгрузки офлайн-сканера, выбор точки, отчёт, счётчик.

Формат QR/денайл (`build_payload`/`checkin_denial`) — из `services/checkin.py` (Квик 260923,
уже покрыт `tests/test_checkin_qr_260923.py`); здесь — только хендлер раздела.

Стиль фикстур — тот же приём, что `tests/test_season_import_073.py` (`_FakeBot.download`,
`_FakeMessage`/`_FakeCallback`, `asyncio.run`, без pytest-asyncio); БД — шаблонная копия через
`tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import io
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
from config import config
from database import db
from database.db import _connect
from handlers.forum import admin_checkin
from handlers.states import CheckinImport
from services.checkin import build_payload
from services import timeutil as timeutil_mod
from services.settings.audit import set_setting_by_admin
from tests._dbtpl import fast_init_db

ADMIN_ID = 910101


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_admin_checkin_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _insert_user(telegram_id, *, status="approved", season=None, full_name="Тест Тестов"):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season) VALUES (?, ?, ?, ?)",
            (telegram_id, full_name, status, season),
        )
        await conn.commit()


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, user_id, text=None, document=None):
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.document = document
        self.sent = []  # list[(text, reply_markup)]

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeCallbackMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeCallbackMessage()
        self.answers = []
        self.bot = None  # CallbackQuery.bot — хендлер загрузки CSV отдаёт его слушателям первой отметки

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class _FakeDocument:
    def __init__(self, file_id="doc1", file_size=1024):
        self.file_id = file_id
        self.file_size = file_size


class _FakeBot:
    def __init__(self, content: bytes):
        self.content = content
        self.downloaded_file_ids = []

    async def download(self, file_id):
        self.downloaded_file_ids.append(file_id)
        return io.BytesIO(self.content)


def _flat_text(fake) -> list[str]:
    return [t for t, _rm in fake.sent]


async def _set_season(season: str):
    await set_setting_by_admin(ADMIN_ID, "event_season", season)


BOUND_ID = 910102


async def _setup_bound_manager(city: str):
    """D-26 (24.09): волонтёр/менеджер, НАСТОЯЩЕ привязанный к городу (`staff.city`, не просто
    выбравший фильтр в панели) — тот же приём, что `tests/test_admin_program_260924.py`."""
    from handlers.access.admin_caps import role_caps_key
    await db.set_setting(role_caps_key("reg_manager"), "checkin")
    await db.add_staff(BOUND_ID, "reg_manager", ADMIN_ID)
    await db.set_staff_city(BOUND_ID, city)


def test_screen_shows_counter_and_upload_button(tmp_path):
    _db_ready(tmp_path)
    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    texts = _flat_text(cb.message)
    assert any("Пришли за форум: 0 из 0 одобренных" in t for t in texts)
    kb = cb.message.sent[0][1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "checkin_upload_start" in cbs


def test_upload_start_sets_waiting_file_state(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    cb = _FakeCallback("checkin_upload_start", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_upload_start(cb, state))
    assert asyncio.run(state.get_state()) == CheckinImport.waiting_file.state


def test_file_with_no_matching_codes_shows_friendly_error_and_stays_in_state(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBot("some,unrelated,csv\n1,2,3\n".encode("utf-8"))
    asyncio.run(admin_checkin.checkin_import_file_step(message, state, bot))
    texts = _flat_text(message)
    assert any("не нашёл ни одного QR форума" in t for t in texts)
    assert asyncio.run(state.get_state()) == CheckinImport.waiting_file.state


def test_file_too_large_is_rejected_before_download(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(ADMIN_ID, document=_FakeDocument(file_size=30 * 1024 * 1024))
    bot = _FakeBot(b"")
    asyncio.run(admin_checkin.checkin_import_file_step(message, state, bot))
    assert bot.downloaded_file_ids == []
    assert any("больше 20 МБ" in t for t in _flat_text(message))


def test_full_flow_new_duplicate_not_found_not_approved(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(_insert_user(1, status="approved", season="YL'26", full_name="Иванов Иван"))
    asyncio.run(_insert_user(2, status="pending", season="YL'26", full_name="Петров Пётр"))

    tok1 = asyncio.run(db.get_or_create_checkin_token(1))
    tok2 = asyncio.run(db.get_or_create_checkin_token(2))

    qr_ok = build_payload("YL26", "Иванов Иван", "Казань", tok1)
    qr_pending = build_payload("YL26", "Петров Пётр", "Москва", tok2)
    qr_unknown = build_payload("YL26", "Чужой Чужаков", "Тюмень", "no-such-token")

    csv_text = (
        "ts,qr\n"
        f"2026-10-03T09:00:00,{qr_ok}\n"
        f"2026-10-03T09:01:00,{qr_pending}\n"
        f"2026-10-03T09:02:00,{qr_unknown}\n"
    )

    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBot(csv_text.encode("utf-8"))
    asyncio.run(admin_checkin.checkin_import_file_step(message, state, bot))
    assert any("Нашёл кодов: 3" in t for t in _flat_text(message))

    cb = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    report = _flat_text(cb.message)[0]
    assert "Отмечено новых: 1" in report
    assert "уже были: 0" in report
    assert "не найдено: 1" in report
    assert "не одобрены: 1" in report
    assert "Чужой Чужаков" in report
    assert "Петров Пётр" in report
    assert asyncio.run(state.get_state()) is None

    # Второй заход тем же файлом на ту же точку -- одобренный уже отмечен -> дубликат.
    cb2 = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=[{"qr": qr_ok, "scanned_at": "2026-10-03 09:00:00"}]))
    asyncio.run(admin_checkin.checkin_point_pick(cb2, state))
    report2 = _flat_text(cb2.message)[0]
    assert "Отмечено новых: 0" in report2
    assert "уже были: 1" in report2


def test_denied_qr_content_is_html_escaped_in_report(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    malicious_qr = build_payload("YL26", "<b>Иванов</b>", "<i>Казань</i>", "no-such-token")
    state = _new_state(ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=[{"qr": malicious_qr, "scanned_at": None}]))
    cb = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    report = _flat_text(cb.message)[0]
    assert "<b>Иванов</b>" not in report
    assert "&lt;b&gt;Иванов&lt;/b&gt;" in report


def test_counter_reflects_current_season_and_arrivals(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(_insert_user(1, status="approved", season="YL'26"))
    asyncio.run(_insert_user(2, status="approved", season="YL'26"))
    asyncio.run(_insert_user(3, status="approved", season="YL'25"))  # прошлый сезон -- не в знаменателе
    asyncio.run(db.get_or_create_checkin_token(1))
    asyncio.run(db.record_checkin(1, "entry", source="csv"))

    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    texts = _flat_text(cb.message)
    assert any("Сегодня пришли: 1 из 2 одобренных" in t for t in texts)


# ── A2 (FORUM-CHECKIN.md): счётчик по городам, 03.10 регионы + Москва набирает параллельно ──

async def _insert_user_city(telegram_id, city, *, status="approved", season="YL'26"):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season, event_city) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, f"Тест {telegram_id}", status, season, city),
        )
        await conn.commit()


def test_counter_all_cities_breaks_down_per_city_with_total(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__spb", "СПб"))
    asyncio.run(db.set_setting("city_label__msk", "Москва"))
    # По умолчанию (без явного выбора) `admin_selected_city` скопирует менеджера на дефолтный
    # город (msk) -- та же логика, что у остальных экранов админки; здесь нужен явный выбор
    # «Все города», чтобы увидеть построчную разбивку A2.
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))
    # msk -- ещё набор, никто не пришёл; spb -- форум уже идёт, один пришёл из двух одобренных;
    # tyumen -- нет одобренных вовсе -> строка города не должна появиться (A2).
    asyncio.run(_insert_user_city(1, "spb"))
    asyncio.run(_insert_user_city(2, "spb"))
    asyncio.run(_insert_user_city(3, "msk"))
    asyncio.run(db.get_or_create_checkin_token(1))
    asyncio.run(db.record_checkin(1, "entry", source="miniapp"))

    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    text = _flat_text(cb.message)[0]
    assert "\nСегодня:\n" in text
    assert "СПб: пришли 1 из 2" in text
    assert "Москва: пришли 0 из 1" in text
    assert "Тюмень" not in text  # нет одобренных -- строку не показываем
    assert "Итого: 1 из 3" in text


def test_counter_scoped_manager_sees_only_own_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb"))
    asyncio.run(_insert_user_city(1, "spb"))
    asyncio.run(_insert_user_city(2, "msk"))
    asyncio.run(db.get_or_create_checkin_token(1))
    asyncio.run(db.record_checkin(1, "entry", source="miniapp"))

    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    text = _flat_text(cb.message)[0]
    assert "Сегодня пришли: 1 из 1 одобренных" in text  # только СПб, Москва не примешивается
    assert "Итого" not in text
    assert "Москва" not in text


def test_counter_module_off_stays_unscoped_byte_for_byte(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    # event_city_enabled НЕ включаем -- старое поведение (общий счётчик, без городов).
    asyncio.run(_insert_user_city(1, "spb"))
    asyncio.run(_insert_user_city(2, "msk"))
    asyncio.run(db.get_or_create_checkin_token(1))
    asyncio.run(db.record_checkin(1, "entry", source="miniapp"))

    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    text = _flat_text(cb.message)[0]
    assert "Сегодня пришли: 1 из 2 одобренных" in text


# ── форум-ночь п.5 (D-18..D-20): точки-сессии в загрузке CSV ────────────────────────────────

def _one_record_csv(tag="YL26", token="any-token"):
    qr = build_payload(tag, "Кто-то", "Город", token)
    return f"ts,qr\n2026-10-03T09:00:00,{qr}\n".encode("utf-8")


def test_import_file_step_shows_point_picker_with_sessions_when_city_resolved(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    monkeypatch.setattr(timeutil_mod, "msk_now", lambda: datetime(2026, 10, 3, 10, 30))
    default_code = cities.default_city_code()
    sid = asyncio.run(db.create_program_session(default_code, "2026-10-03", "10:00", "11:00", "Открытие"))

    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBot(_one_record_csv())
    asyncio.run(admin_checkin.checkin_import_file_step(message, state, bot))

    texts = _flat_text(message)
    assert any("Отметить точкой" in t for t in texts)
    kb = message.sent[-1][1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "checkin_point:entry" in cbs
    assert f"checkin_point:session:{sid}" in cbs


def test_import_file_step_shows_city_picker_when_admin_sees_all_cities(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))

    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinImport.waiting_file))
    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBot(_one_record_csv())
    asyncio.run(admin_checkin.checkin_import_file_step(message, state, bot))

    texts = _flat_text(message)
    assert any("Из какого города точка" in t for t in texts)
    kb = message.sent[-1][1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(cb.startswith("checkin_point_city:") for cb in cbs)


def test_checkin_point_city_pick_shows_point_picker_for_chosen_city(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    monkeypatch.setattr(timeutil_mod, "msk_now", lambda: datetime(2026, 10, 3, 10, 30))
    sid = asyncio.run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))

    cb = _FakeCallback("checkin_point_city:spb", ADMIN_ID)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=[{"qr": "YL26·А·spb·tok", "scanned_at": None}]))
    asyncio.run(admin_checkin.checkin_point_city_pick(cb, state))

    kb = cb.message.sent[-1][1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "checkin_point:entry" in cbs
    assert f"checkin_point:session:{sid}" in cbs


def test_checkin_point_pick_session_new_wrong_city_and_not_found(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(_insert_user(1, status="approved", season="YL'26", full_name="Иванов Иван"))
    sid = asyncio.run(db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))

    async def _set_city(uid, city):
        async with _connect() as conn:
            await conn.execute("UPDATE users SET event_city = ? WHERE telegram_id = ?", (city, uid))
            await conn.commit()

    asyncio.run(_set_city(1, "msk"))
    asyncio.run(_insert_user(2, status="approved", season="YL'26", full_name="Петров Пётр"))
    asyncio.run(_set_city(2, "spb"))  # другой город форума -- отказ

    tok1 = asyncio.run(db.get_or_create_checkin_token(1))
    tok2 = asyncio.run(db.get_or_create_checkin_token(2))
    qr_ok = build_payload("YL26", "Иванов Иван", "Казань", tok1)
    qr_wrong_city = build_payload("YL26", "Петров Пётр", "Москва", tok2)
    qr_unknown = build_payload("YL26", "Чужой Чужаков", "Тюмень", "no-such-token")

    state = _new_state(ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=[
        {"qr": qr_ok, "scanned_at": "2026-10-03 10:05:00"},
        {"qr": qr_wrong_city, "scanned_at": "2026-10-03 10:05:00"},
        {"qr": qr_unknown, "scanned_at": None},
    ]))
    cb = _FakeCallback(f"checkin_point:session:{sid}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    report = _flat_text(cb.message)[0]
    assert "Отмечено новых: 1" in report
    assert "не найдено: 1" in report
    assert "Другой город форума: 1" in report
    assert asyncio.run(db.count_checkins_by_point(f"session:{sid}")) == 1
    assert asyncio.run(db.count_checkins_by_point("entry")) == 1  # авто-вход от новой сессии


def test_checkin_point_pick_entry_other_city_not_marked_for_bound_manager(tmp_path):
    """D-26 (24.09): волонтёр, привязанный к городу, загружает выгрузку сканера на точке
    «Вход» — делегаты ЧУЖОГО города НЕ отмечаются, отдельная строка отчёта «Другой город»."""
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(_setup_bound_manager("spb"))

    asyncio.run(_insert_user(1, status="approved", season="YL'26", full_name="Иванов Иван"))
    asyncio.run(_insert_user(2, status="approved", season="YL'26", full_name="Петров Пётр"))

    async def _set_city(uid, city):
        async with _connect() as conn:
            await conn.execute("UPDATE users SET event_city = ? WHERE telegram_id = ?", (city, uid))
            await conn.commit()

    asyncio.run(_set_city(1, "spb"))  # свой город волонтёра
    asyncio.run(_set_city(2, "msk"))  # чужой город

    tok1 = asyncio.run(db.get_or_create_checkin_token(1))
    tok2 = asyncio.run(db.get_or_create_checkin_token(2))
    qr_own = build_payload("YL26", "Иванов Иван", "СПб", tok1)
    qr_other = build_payload("YL26", "Петров Пётр", "Москва", tok2)

    state = _new_state(BOUND_ID)
    asyncio.run(state.update_data(checkin_records=[
        {"qr": qr_own, "scanned_at": "2026-10-03 10:05:00"},
        {"qr": qr_other, "scanned_at": "2026-10-03 10:05:00"},
    ]))
    cb = _FakeCallback("checkin_point:entry", BOUND_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    report = _flat_text(cb.message)[0]
    assert "Отмечено новых: 1" in report
    assert "Другой город: 1 (не отмечены)" in report
    assert asyncio.run(db.count_checkins_by_point("entry")) == 1


def test_checkin_point_pick_entry_unbound_admin_not_scoped(tmp_path):
    """Суперадмин (без привязки) загружает выгрузку на «Вход» — города не ограничивает, как
    раньше (D-15 нетронут для непривязанных)."""
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    asyncio.run(_insert_user(1, status="approved", season="YL'26", full_name="Иванов Иван"))
    asyncio.run(_insert_user(2, status="approved", season="YL'26", full_name="Петров Пётр"))

    async def _set_city(uid, city):
        async with _connect() as conn:
            await conn.execute("UPDATE users SET event_city = ? WHERE telegram_id = ?", (city, uid))
            await conn.commit()

    asyncio.run(_set_city(1, "spb"))
    asyncio.run(_set_city(2, "msk"))

    tok1 = asyncio.run(db.get_or_create_checkin_token(1))
    tok2 = asyncio.run(db.get_or_create_checkin_token(2))
    qr1 = build_payload("YL26", "Иванов Иван", "СПб", tok1)
    qr2 = build_payload("YL26", "Петров Пётр", "Москва", tok2)

    state = _new_state(ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=[
        {"qr": qr1, "scanned_at": "2026-10-03 10:05:00"},
        {"qr": qr2, "scanned_at": "2026-10-03 10:05:00"},
    ]))
    cb = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))
    report = _flat_text(cb.message)[0]
    assert "Отмечено новых: 2" in report
    assert "Другой город" not in report
    assert asyncio.run(db.count_checkins_by_point("entry")) == 2


def test_checkin_point_pick_session_moved_and_outside_time_window(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(_insert_user(1, status="approved", season="YL'26", full_name="Иванов Иван"))

    async def _set_city(uid, city):
        async with _connect() as conn:
            await conn.execute("UPDATE users SET event_city = ? WHERE telegram_id = ?", (city, uid))
            await conn.commit()

    asyncio.run(_set_city(1, "msk"))
    sid1 = asyncio.run(db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Зал А"))
    sid2 = asyncio.run(db.create_program_session("msk", "2026-10-03", "10:30", "11:30", "Зал Б"))
    tok1 = asyncio.run(db.get_or_create_checkin_token(1))
    qr_ok = build_payload("YL26", "Иванов Иван", "Казань", tok1)

    # Первая выгрузка -- отметка на sid1.
    state1 = _new_state(ADMIN_ID)
    asyncio.run(state1.update_data(checkin_records=[{"qr": qr_ok, "scanned_at": "2026-10-03 10:05:00"}]))
    cb1 = _FakeCallback(f"checkin_point:session:{sid1}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb1, state1))
    assert "Отмечено новых: 1" in _flat_text(cb1.message)[0]

    # Вторая выгрузка -- тот же делегат перешёл на sid2 (D-20 «перенос»), время скана вне
    # интервала sid2 (10:30-11:30 ±30 мин = 10:00-12:00) -- «05:00» вне окна.
    state2 = _new_state(ADMIN_ID)
    asyncio.run(state2.update_data(checkin_records=[{"qr": qr_ok, "scanned_at": "2026-10-03 05:00:00"}]))
    cb2 = _FakeCallback(f"checkin_point:session:{sid2}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb2, state2))
    report2 = _flat_text(cb2.message)[0]
    assert "Перенесено с другой сессии слота: 1" in report2
    assert "Время скана вне интервала сессии: 1" in report2
    assert asyncio.run(db.count_checkins_by_point(f"session:{sid1}")) == 0
    assert asyncio.run(db.count_checkins_by_point(f"session:{sid2}")) == 1


def test_counter_all_cities_today_only_cities_with_forum_today(tmp_path):
    """«Все города» в день форума: в «Сегодня» только города, где сегодня идёт форум (как
    счётчик сканера Mini App); Москва с форумом в другой день не стоит строкой с нулём и не
    подмешивается в «Итого»; один город — без отдельной строки «Итого»."""
    from services.timeutil import msk_now

    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__spb", "СПб"))
    asyncio.run(db.set_setting("city_label__msk", "Москва"))
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))
    asyncio.run(db.set_setting("forum_date__city__spb", msk_now().strftime("%d.%m.%Y")))
    asyncio.run(db.set_setting("forum_date__city__msk", "24.09.2020"))
    asyncio.run(_insert_user_city(1, "spb"))
    asyncio.run(_insert_user_city(2, "spb"))
    asyncio.run(_insert_user_city(3, "msk"))
    asyncio.run(db.get_or_create_checkin_token(1))
    asyncio.run(db.record_checkin(1, "entry", source="miniapp"))

    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    text = _flat_text(cb.message)[0]
    head = text.split("\n\n🎟")[0]
    assert "СПб: пришли 1 из 2" in head
    assert "Москва: пришли" not in head
    assert "Итого" not in head
