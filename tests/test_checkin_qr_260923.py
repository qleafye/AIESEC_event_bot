"""Квик 260923 (форум-чекин, D-01..D-04): личный QR одобренного делегата.

Покрывает:
- `database.db.get_or_create_checkin_token` — генерация уникальна и стабильна (второй запрос
  того же делегата отдаёт тот же токен), пользователя нет -- `None`.
- миграция (`checkin_token` колонка + частичный уникальный индекс `idx_users_checkin_token`)
  не ломает существующие записи `users` и идемпотентна при повторном `init_db()`.
- `services.checkin.build_payload`/`build_checkin_payload` — формат строки внутри QR
  (`тег·ФИО·город·токен`), без декодирования самой картинки (см. задание — декодировать не
  обязательно, проверяется собранная строка).
- `handlers.user_actions.show_my_checkin_qr` — гейт «не одобрен» (переиспользует
  `ensure_registered`, отдельного текста для этого случая не заводили) и гейт «тумблер выкл».
- `keyboards.builders.get_main_menu_kb` — кнопка «🎟 Мой QR» скрыта, пока `checkin_qr_enabled`
  выключен (дефолт), и появляется при включённом тумблере.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async-хелпер
гоняется через asyncio.run(), config.DB_PATH указывает на файл в tmp_path.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from config import config
from database import db
from services import checkin as checkin_mod
from keyboards.builders import get_main_menu_kb, MENU_TEXTS
from handlers import user_actions as ua_mod

UID = 260923001


def _use_tmp_db(tmp_path, name="test_checkin_qr_260923.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _seed_user(uid, city="Казань", full_name="Иванов Иван"):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": full_name, "registration_date": "2026-01-01",
        "event_city": city,
    }))


def _set_status(uid, status):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, uid))
    conn.commit()
    conn.close()


async def _set_setting(key, value):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value),
        )
        await conn.commit()


# ── get_or_create_checkin_token: уникальность и стабильность ────────────────────────────────

def test_token_is_stable_across_repeated_requests(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    first = asyncio.run(db.get_or_create_checkin_token(UID))
    second = asyncio.run(db.get_or_create_checkin_token(UID))
    assert first == second
    assert first  # непусто
    assert 8 <= len(first) <= 16  # ~10-12 символов, D-01


def test_tokens_differ_between_users(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, full_name="Первый")
    _seed_user(UID + 1, full_name="Второй")
    t1 = asyncio.run(db.get_or_create_checkin_token(UID))
    t2 = asyncio.run(db.get_or_create_checkin_token(UID + 1))
    assert t1 != t2


def test_missing_user_returns_none(tmp_path):
    _use_tmp_db(tmp_path)
    assert asyncio.run(db.get_or_create_checkin_token(999999999)) is None


# ── миграция: колонка + индекс, существующие записи не ломаются ─────────────────────────────

def test_migration_adds_column_and_unique_index(tmp_path):
    _use_tmp_db(tmp_path)
    conn = sqlite3.connect(config.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    assert "checkin_token" in cols
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(users)")}
    assert "idx_users_checkin_token" in indexes
    conn.close()


def test_migration_rerun_does_not_lose_existing_rows(tmp_path):
    """D-01: `init_db()` повторно на БД с уже существующей строкой не бэкафилит
    `checkin_token` и не трогает остальные поля -- ноль потери данных."""
    _use_tmp_db(tmp_path)
    _seed_user(UID, full_name="Старая Запись", city="Владивосток")
    asyncio.run(db.init_db())  # повторный прогон -- идемпотентность
    u = asyncio.run(db.get_user(UID))
    assert u["full_name"] == "Старая Запись"
    assert u["event_city"] == "Владивосток"
    assert u["checkin_token"] is None  # лениво, без бэкафилла


def test_unique_index_rejects_duplicate_token(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    _seed_user(UID + 1)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET checkin_token = 'dup-token' WHERE telegram_id = ?", (UID,))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE users SET checkin_token = 'dup-token' WHERE telegram_id = ?", (UID + 1,)
        )
    conn.close()


# ── services.checkin: формат содержимого QR ──────────────────────────────────────────────────

def test_build_payload_field_order_and_separator():
    payload = checkin_mod.build_payload("YL26", "Иванов Иван", "Казань", "k7Qx9abc12")
    assert payload == "YL26·Иванов Иван·Казань·k7Qx9abc12"
    assert payload.split("·")[-1] == "k7Qx9abc12"  # токен всегда хвостом (D-04)


def test_build_payload_blank_name_and_city_fall_back_to_dash():
    payload = checkin_mod.build_payload("YL26", "", None, "tok")
    assert payload == "YL26·—·—·tok"


def test_build_checkin_payload_uses_explicit_event_tag(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="Казань", full_name="Иванов Иван")
    asyncio.run(_set_setting("checkin_event_tag", "YL26"))
    user = asyncio.run(db.get_user(UID))
    payload = asyncio.run(checkin_mod.build_checkin_payload(user))
    token = asyncio.run(db.get_or_create_checkin_token(UID))
    assert payload == f"YL26·Иванов Иван·Казань·{token}"


def test_build_checkin_payload_falls_back_to_event_season_without_apostrophe(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID, city="Москва", full_name="Петров Пётр")
    asyncio.run(_set_setting("event_season", "YL'26"))
    user = asyncio.run(db.get_user(UID))
    payload = asyncio.run(checkin_mod.build_checkin_payload(user))
    assert payload.startswith("YL26·")


def test_build_checkin_payload_default_tag_when_nothing_configured(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    user = asyncio.run(db.get_user(UID))
    payload = asyncio.run(checkin_mod.build_checkin_payload(user))
    assert payload.startswith("EVENT·")


def test_build_checkin_payload_missing_user_returns_none(tmp_path):
    _use_tmp_db(tmp_path)
    assert asyncio.run(checkin_mod.build_checkin_payload({"telegram_id": 999999999})) is None


def test_build_checkin_qr_returns_png_bytes_and_caption(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    png_bytes, caption = asyncio.run(checkin_mod.build_checkin_qr({
        "telegram_id": UID, "full_name": "Иванов Иван", "event_city": "Казань",
    }))
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
    assert caption and "QR" in caption


# ── handlers.user_actions.show_my_checkin_qr: гейты D-02/D-03 ───────────────────────────────

class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, uid=UID):
        self.text = "🎟 Мой QR"
        self.chat = _FakeChat(uid)
        self.from_user = _FakeUser(uid)
        self.answers = []
        self.photos = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        return "sent"

    async def answer_photo(self, photo, caption=None, **kwargs):
        self.photos.append((photo, caption))
        return "sent-photo"


def test_show_my_checkin_qr_denies_pending_user(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    _set_status(UID, "pending")
    asyncio.run(_set_setting("checkin_qr_enabled", "on"))
    message = _FakeMessage()
    asyncio.run(ua_mod.show_my_checkin_qr(message))
    assert not message.photos, "заявка не одобрена -- фото уйти не должно"
    assert message.answers, "делегат обязан получить понятный ответ (D-02)"


def test_show_my_checkin_qr_denies_rejected_user(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    _set_status(UID, "rejected")
    asyncio.run(_set_setting("checkin_qr_enabled", "on"))
    message = _FakeMessage()
    asyncio.run(ua_mod.show_my_checkin_qr(message))
    assert not message.photos
    assert message.answers


def test_show_my_checkin_qr_sends_photo_for_approved_user(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)  # add_user не пишет status -- миграционный дефолт 'approved'
    asyncio.run(_set_setting("checkin_qr_enabled", "on"))
    message = _FakeMessage()
    asyncio.run(ua_mod.show_my_checkin_qr(message))
    assert message.photos, "одобренный делегат обязан получить QR"
    assert not message.answers


def test_show_my_checkin_qr_module_off_sends_disabled_text_not_photo(tmp_path):
    """Тумблер checkin_qr_enabled выключен (дефолт) -- защитная перепроверка в самом
    хендлере (T-19-54 idiom), даже если делегат как-то дотянулся до кнопки."""
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    message = _FakeMessage()
    asyncio.run(ua_mod.show_my_checkin_qr(message))
    assert not message.photos
    assert message.answers


# ── keyboards.builders.get_main_menu_kb: кнопка появляется только при включённом тумблере ──

def test_menu_button_hidden_when_module_disabled(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    kb = asyncio.run(get_main_menu_kb(UID))
    labels = {btn.text for row in kb.keyboard for btn in row}
    assert not (labels & MENU_TEXTS["menu_checkin_qr"])


def test_menu_button_visible_when_module_enabled(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user(UID)
    asyncio.run(_set_setting("checkin_qr_enabled", "on"))
    kb = asyncio.run(get_main_menu_kb(UID))
    labels = {btn.text for row in kb.keyboard for btn in row}
    assert labels & MENU_TEXTS["menu_checkin_qr"]
