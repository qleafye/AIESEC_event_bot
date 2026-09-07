"""Quick 260907-4ai — P0 SkillUp5: фоновый повтор выгрузки резюме в Nextcloud.

Задача 1: реестровая настройка частоты (settings_schema/_SYSTEM_FIELD_ORDER/synonyms —
их сторожа гоняются отдельными файлами из <verify>, здесь только смысловая проверка ключа),
запрос очереди недогруженных резюме в БД, единый гейт `nextcloud.is_configured()`.
Задача 2 (тесты дописаны в этот же файл, не заводим второй): worker
`retry_pending_resume_uploads` + регистрация interval-джобы в планировщике.

Без pytest-asyncio (как везде в проекте): asyncio.run(...) + config.DB_PATH на tmp_path.
"""
import asyncio
import inspect
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


# ── Задача 2: retry_pending_resume_uploads ───────────────────────────────────────────────

class _FakeTgFile:
    def __init__(self, file_path, file_size=None):
        self.file_path = file_path
        self.file_size = file_size


class _FakeBot:
    def __init__(self, file_path="resumes/r1.pdf", get_file_error: Exception | None = None,
                 file_size=None):
        self._file_path = file_path
        self._get_file_error = get_file_error
        self._file_size = file_size
        self.get_file_calls = []

    async def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        if self._get_file_error:
            raise self._get_file_error
        return _FakeTgFile(self._file_path, self._file_size)


def _configure_nextcloud(monkeypatch):
    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "https://cloud.example.org/remote.php/dav/files/bot")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "https://cloud.example.org")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "TOK")


def test_retry_returns_zero_and_touches_nothing_when_not_configured(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "")

    from services import reg_finalize

    async def _boom(*a, **kw):
        raise AssertionError("get_resume_upload_backlog must not be called when unconfigured")
    monkeypatch.setattr(db, "get_resume_upload_backlog", _boom)

    class _BoomBot:
        async def get_file(self, *a, **kw):
            raise AssertionError("bot must not be touched when unconfigured")

    result = asyncio.run(reg_finalize.retry_pending_resume_uploads(_BoomBot()))
    assert result == 0


def test_retry_uploads_file_resume_and_updates_url(tmp_path, monkeypatch):
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=101, full_name="Иван Файлов", username="ivan",
                 registration_date=_ts(30), resume_file_id="FILE101", resume_url=None,
                 event_city=None, participant_type=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod
    from services import sheets as sheets_mod

    upload_calls = []

    async def _fake_upload_resume(bot, file_id, filename):
        upload_calls.append((file_id, filename))
        return "https://cloud.example.org/s/TOK/download?path=%2F&files=x.pdf"

    async def _fake_update_row_by_id(tab, tid, row):
        return True

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume)
    monkeypatch.setattr(sheets_mod, "update_row_by_id", _fake_update_row_by_id)

    result = asyncio.run(reg_finalize.retry_pending_resume_uploads(_FakeBot(file_path="resumes/r.pdf")))
    assert result == 1
    assert upload_calls, "upload_resume должен быть вызван"
    file_id, filename = upload_calls[0]
    assert file_id == "FILE101"
    assert filename.endswith(".pdf")

    full = asyncio.run(db.get_user(101))
    assert full["resume_url"] == "https://cloud.example.org/s/TOK/download?path=%2F&files=x.pdf"


def test_retry_uploads_text_resume_with_txt_extension(tmp_path, monkeypatch):
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=102, full_name="Текстовый", registration_date=_ts(30),
                 resume_text="мой текст резюме", resume_url=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod
    from services import sheets as sheets_mod

    upload_calls = []

    async def _fake_upload_text_resume(text, filename):
        upload_calls.append((text, filename))
        return "https://cloud.example.org/s/TOK/download?path=%2F&files=y.txt"

    async def _fake_update_row_by_id(tab, tid, row):
        return True

    monkeypatch.setattr(nextcloud_mod, "upload_text_resume", _fake_upload_text_resume)
    monkeypatch.setattr(sheets_mod, "update_row_by_id", _fake_update_row_by_id)

    result = asyncio.run(reg_finalize.retry_pending_resume_uploads(_FakeBot()))
    assert result == 1
    assert upload_calls[0][1].endswith(".txt")


def test_retry_upload_returning_none_leaves_url_empty_and_no_exception(tmp_path, monkeypatch):
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=103, full_name="Неудача", registration_date=_ts(30),
                 resume_file_id="FILE103", resume_url=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod

    async def _fake_upload_resume(bot, file_id, filename):
        return None

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume)

    result = asyncio.run(reg_finalize.retry_pending_resume_uploads(_FakeBot()))
    assert result == 0
    full = asyncio.run(db.get_user(103))
    assert not full.get("resume_url")


def test_retry_sheet_update_failure_does_not_roll_back_db_url(tmp_path, monkeypatch):
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=104, full_name="Лист падает", registration_date=_ts(30),
                 resume_file_id="FILE104", resume_url=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod
    from services import sheets as sheets_mod

    async def _fake_upload_resume(bot, file_id, filename):
        return "https://cloud.example.org/s/TOK/download?path=%2F&files=z.pdf"

    async def _boom_update_row_by_id(tab, tid, row):
        raise RuntimeError("sheets down")

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume)
    monkeypatch.setattr(sheets_mod, "update_row_by_id", _boom_update_row_by_id)

    result = asyncio.run(reg_finalize.retry_pending_resume_uploads(_FakeBot()))
    assert result == 1
    full = asyncio.run(db.get_user(104))
    assert full["resume_url"] == "https://cloud.example.org/s/TOK/download?path=%2F&files=z.pdf"


def test_retry_dead_file_id_warns_and_is_skipped_until_restart(tmp_path, monkeypatch, caplog):
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=105, full_name="Мёртвый файл", registration_date=_ts(30),
                 resume_file_id="FILE105", resume_url=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod
    reg_finalize._resume_retry_dead.discard(105)  # изоляция от прочих тестов модуля

    upload_calls = []

    async def _fake_upload_resume(bot, file_id, filename):
        upload_calls.append(file_id)
        return "should-not-be-called"

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume)

    bad_bot = _FakeBot(get_file_error=Exception("Bad Request: file not found"))
    result = asyncio.run(reg_finalize.retry_pending_resume_uploads(bad_bot))
    assert result == 0
    assert upload_calls == []
    assert 105 in reg_finalize._resume_retry_dead

    # повторный прогон эту строку уже не берёт — bot.get_file больше не зовётся для неё
    bad_bot2 = _FakeBot(get_file_error=Exception("Bad Request: file not found"))
    result2 = asyncio.run(reg_finalize.retry_pending_resume_uploads(bad_bot2))
    assert result2 == 0
    assert bad_bot2.get_file_calls == []
    reg_finalize._resume_retry_dead.discard(105)


def test_retry_oversized_file_is_marked_dead_and_not_uploaded(tmp_path, monkeypatch):
    """Файл больше RESUME_MAX_MB облако не примет никогда — строка помечается, как и
    недоступный file_id, иначе одно и то же предупреждение каждые N минут до рестарта."""
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=108, full_name="Большой файл", registration_date=_ts(30),
                 resume_file_id="FILE108", resume_url=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod
    reg_finalize._resume_retry_dead.discard(108)

    upload_calls = []

    async def _fake_upload_resume(bot, file_id, filename):
        upload_calls.append(file_id)
        return "should-not-be-called"

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume)
    monkeypatch.setattr(config, "RESUME_MAX_MB", 10)

    big_bot = _FakeBot(file_size=11 * 1024 * 1024)
    assert asyncio.run(reg_finalize.retry_pending_resume_uploads(big_bot)) == 0
    assert upload_calls == []
    assert 108 in reg_finalize._resume_retry_dead

    big_bot2 = _FakeBot(file_size=11 * 1024 * 1024)
    asyncio.run(reg_finalize.retry_pending_resume_uploads(big_bot2))
    assert big_bot2.get_file_calls == []
    reg_finalize._resume_retry_dead.discard(108)


def test_retry_one_bad_row_does_not_block_the_next(tmp_path, monkeypatch):
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=106, full_name="Падает", registration_date=_ts(31),
                 resume_file_id="FILE106", resume_url=None)
    _insert_user(telegram_id=107, full_name="Успех", registration_date=_ts(30),
                 resume_file_id="FILE107", resume_url=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod
    from services import sheets as sheets_mod

    calls = []

    async def _fake_upload_resume(bot, file_id, filename):
        calls.append(file_id)
        if file_id == "FILE106":
            raise RuntimeError("boom")
        return "https://cloud.example.org/s/TOK/download?path=%2F&files=ok.pdf"

    async def _fake_update_row_by_id(tab, tid, row):
        return True

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume)
    monkeypatch.setattr(sheets_mod, "update_row_by_id", _fake_update_row_by_id)

    result = asyncio.run(reg_finalize.retry_pending_resume_uploads(_FakeBot()))
    assert result == 1
    full107 = asyncio.run(db.get_user(107))
    assert full107["resume_url"] == "https://cloud.example.org/s/TOK/download?path=%2F&files=ok.pdf"


def test_scheduler_registers_resume_upload_retry_job():
    import services.scheduler as scheduler_mod

    src = inspect.getsource(scheduler_mod.init_scheduler)
    assert "resume_upload_retry" in src
    assert "resume_retry_minutes" in src
