"""Phase 12 (FORUM-CHECKIN.md): раздел «✅ Отметки на форуме» (handlers/admin_checkin.py) —
загрузка выгрузки офлайн-сканера, выбор точки, отчёт, счётчик.

Формат QR/денайл (`build_payload`/`checkin_denial`) — из `services/checkin.py` (Квик 260923,
уже покрыт `tests/test_checkin_qr_260923.py`); здесь — только хендлер раздела.

Стиль фикстур — тот же приём, что `tests/test_season_import_073.py` (`_FakeBot.download`,
`_FakeMessage`/`_FakeCallback`, `asyncio.run`, без pytest-asyncio); БД — шаблонная копия через
`tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import io

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from config import config
from database import db
from database.db import _connect
from handlers import admin_checkin
from handlers.states import CheckinImport
from services.checkin import build_payload
from settings_audit import set_setting_by_admin
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


def test_screen_shows_counter_and_upload_button(tmp_path):
    _db_ready(tmp_path)
    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    texts = _flat_text(cb.message)
    assert any("Пришли: 0 из 0 одобренных" in t for t in texts)
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
    assert any("Пришли: 1 из 2 одобренных" in t for t in texts)


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
    assert "Пришли: 1 из 1 одобренных" in text  # только СПб, Москва не примешивается
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
    assert "Пришли: 1 из 2 одобренных" in text
    assert "Итого" not in text
