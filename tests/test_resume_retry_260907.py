"""Quick 260907-4ai — P0 SkillUp5: фоновый повтор выгрузки резюме в Nextcloud.

Задача 1: реестровая настройка частоты (settings_schema/_SYSTEM_FIELD_ORDER/synonyms —
их сторожа гоняются отдельными файлами из <verify>, здесь только смысловая проверка ключа),
запрос очереди недогруженных резюме в БД, единый гейт `nextcloud.is_configured()`.

Без pytest-asyncio (как везде в проекте): asyncio.run(...) + config.DB_PATH на tmp_path.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA
from services import nextcloud


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "resume_retry.db")
    asyncio.run(db.init_db())


def _insert_user(**cols):
    """Прямая вставка строки users поверх схемы, поднятой init_db (только telegram_id
    NOT NULL) — тот же приём, что test_consent_versioning_260822.py применяет к
    user_consents."""
    con = sqlite3.connect(config.DB_PATH)
    keys = ", ".join(cols.keys())
    placeholders = ", ".join("?" for _ in cols)
    con.execute(f"INSERT INTO users ({keys}) VALUES ({placeholders})", tuple(cols.values()))
    con.commit()
    con.close()


def _ts(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


# ── Задача 1: реестр ─────────────────────────────────────────────────────────────────────

def test_schema_key_resume_retry_minutes():
    v = SETTINGS_SCHEMA["resume_retry_minutes"]
    assert v["type"] == "int"
    assert v["group"] == "system"
    assert v["default"] == 10
    assert v["label"].strip()
    assert v["prompt"].strip()
    assert "минут" in v["prompt"].lower()


# ── Задача 1: nextcloud.is_configured() ──────────────────────────────────────────────────

def test_is_configured_false_when_any_field_empty(monkeypatch):
    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "https://x")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "TOK")
    assert nextcloud.is_configured() is False

    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "https://cloud/dav")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "")
    assert nextcloud.is_configured() is False

    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "https://x")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "")
    assert nextcloud.is_configured() is False


def test_is_configured_true_when_all_three_set(monkeypatch):
    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "https://cloud.example.org/remote.php/dav/files/bot")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "https://cloud.example.org")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "TOK")
    assert nextcloud.is_configured() is True


# ── Задача 1: get_resume_upload_backlog ──────────────────────────────────────────────────

def test_backlog_picks_file_and_text_rows_but_not_done_or_empty(tmp_path):
    _ready(tmp_path)
    old = _ts(30)
    _insert_user(telegram_id=1, full_name="Файл", registration_date=old, resume_file_id="FILE1", resume_url=None)
    _insert_user(telegram_id=2, full_name="Текст", registration_date=old, resume_text="моё резюме", resume_url=None)
    _insert_user(telegram_id=3, full_name="Уже загружено", registration_date=old, resume_file_id="FILE3", resume_url="https://cloud/s/x")
    _insert_user(telegram_id=4, full_name="Нет резюме вовсе", registration_date=old)

    before = _ts(2)
    rows = asyncio.run(db.get_resume_upload_backlog(before, limit=20))
    ids = {r["telegram_id"] for r in rows}
    assert ids == {1, 2}


def test_backlog_skips_fresh_rows_registered_after_cutoff(tmp_path):
    _ready(tmp_path)
    fresh = _ts(0.1)  # моложе отсечки — финал ещё может быть "в полёте"
    _insert_user(telegram_id=5, full_name="Свежий", registration_date=fresh, resume_file_id="FILE5", resume_url=None)

    before = _ts(2)
    rows = asyncio.run(db.get_resume_upload_backlog(before, limit=20))
    assert rows == []


def test_backlog_orders_by_registration_date_ascending_and_respects_limit(tmp_path):
    _ready(tmp_path)
    for i, minutes in enumerate([10, 60, 30], start=1):
        _insert_user(
            telegram_id=i, full_name=f"u{i}", registration_date=_ts(minutes),
            resume_file_id=f"F{i}", resume_url=None,
        )
    before = _ts(2)
    rows = asyncio.run(db.get_resume_upload_backlog(before, limit=2))
    assert len(rows) == 2
    # старейшая (minutes=60 -> telegram_id=2) первой
    assert [r["telegram_id"] for r in rows] == [2, 3]
