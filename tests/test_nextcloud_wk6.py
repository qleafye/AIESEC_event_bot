"""WK6 Nextcloud resume-upload tests.

No pytest-asyncio in this env — async helpers driven via asyncio.run(), tmp DB via
config.DB_PATH. Network is never touched: upload_resume is exercised only in its
disabled (no config) and fail-soft (raising bot) paths.
"""
import asyncio
import sqlite3

from config import config
from database import db
from handlers import registration as reg
from services.nextcloud import upload_resume


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


# ── migration: resume_url column exists ──────────────────────────────────────

def test_resume_url_column_added(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    conn = sqlite3.connect(config.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert "resume_url" in cols


# ── sheet column present + maps the URL ──────────────────────────────────────

def test_resume_url_sheet_column_present(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_resume", "on")
        headers = await reg.active_sheet_headers()
        assert "Резюме (ссылка)" in headers

    asyncio.run(go())
    vmap = reg._sheet_value_map({"resume_url": "http://x/s/abc"})
    assert vmap["Резюме (ссылка)"] == "http://x/s/abc"


# ── upload_resume: disabled config → None, no network ────────────────────────

def test_upload_resume_disabled_returns_none():
    config.NEXTCLOUD_BASE_URL = ""
    config.NEXTCLOUD_WEBDAV_URL = ""
    result = asyncio.run(upload_resume(bot=object(), file_id="f", filename="r.pdf"))
    assert result is None


# ── upload_resume: any error is swallowed → None, never raises ───────────────

def test_upload_resume_failsoft_on_error():
    # Config "enabled" so we pass the disabled guard and reach bot.download, which raises.
    config.NEXTCLOUD_BASE_URL = "https://cloud.example.org"
    config.NEXTCLOUD_WEBDAV_URL = "https://cloud.example.org/remote.php/dav/files/bot"

    class _BadBot:
        async def download(self, file_id):
            raise RuntimeError("boom — telegram unreachable")

    try:
        result = asyncio.run(upload_resume(bot=_BadBot(), file_id="f", filename="r.pdf"))
        assert result is None
    finally:
        config.NEXTCLOUD_BASE_URL = ""
        config.NEXTCLOUD_WEBDAV_URL = ""


# ── add_user preserves resume_url on re-registration (COALESCE guard) ─────────

def test_add_user_preserves_resume_url(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.add_user({
            "telegram_id": 555,
            "full_name": "Res Ume",
            "registration_date": "2026-01-01",
            "resume_url": "https://cloud.example.org/s/link",
        })
        # Re-run without resume_url — must NOT wipe the stored link.
        await db.add_user({
            "telegram_id": 555,
            "full_name": "Res Ume",
            "registration_date": "2026-01-02",
        })
        return await db.get_user(555)

    row = asyncio.run(go())
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.execute("SELECT resume_url FROM users WHERE telegram_id=555")
    stored = cur.fetchone()[0]
    conn.close()
    assert stored == "https://cloud.example.org/s/link"
