"""upload_resume must check the Telegram file size (getFile metadata) BEFORE downloading:
over RESUME_MAX_MB → no download, no PUT, returns None (fail-soft: the registration keeps
the file_id, only the Nextcloud link is missing)."""
import asyncio
import io
import logging
from types import SimpleNamespace

from config import config
from services import nextcloud


class _FakeBot:
    def __init__(self, size, content=b"%PDF-1.4 fake"):
        self.size = size
        self.content = content
        self.downloaded = False

    async def get_file(self, file_id):
        return SimpleNamespace(file_id=file_id, file_path="documents/file_0.pdf", file_size=self.size)

    async def download_file(self, file_path, destination=None, **kw):
        self.downloaded = True
        return io.BytesIO(self.content)

    async def download(self, *a, **kw):  # legacy idiom — must no longer be used
        raise AssertionError("bot.download must not be called; use get_file + download_file")


def _enable(monkeypatch, max_mb=20):
    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "https://cloud.example.org/remote.php/dav/files/bot")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "https://cloud.example.org")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "TOK")
    monkeypatch.setattr(config, "RESUME_MAX_MB", max_mb)


def test_default_cap_is_20_mb():
    from config import Settings
    assert Settings.model_fields["RESUME_MAX_MB"].default == 20


def test_oversized_file_is_not_downloaded_and_returns_none(monkeypatch, caplog):
    _enable(monkeypatch, max_mb=20)
    puts = []

    async def fake_put(content, remote):
        puts.append(remote)
        return True

    monkeypatch.setattr(nextcloud, "_put_bytes", fake_put)
    bot = _FakeBot(size=20 * 1024 * 1024 + 1)
    caplog.set_level(logging.WARNING, logger="services.nextcloud")

    result = asyncio.run(nextcloud.upload_resume(bot, "fid", "cv.pdf"))

    assert result is None
    assert bot.downloaded is False
    assert puts == []
    assert "RESUME_MAX_MB" in caplog.text


def test_file_within_cap_is_downloaded_and_uploaded(monkeypatch):
    _enable(monkeypatch, max_mb=20)
    puts = []

    async def fake_put(content, remote):
        puts.append((remote, content))
        return True

    monkeypatch.setattr(nextcloud, "_put_bytes", fake_put)
    bot = _FakeBot(size=5 * 1024 * 1024)

    result = asyncio.run(nextcloud.upload_resume(bot, "fid", "cv.pdf"))

    assert result == nextcloud._file_link("cv.pdf")
    assert bot.downloaded is True
    assert puts == [("cv.pdf", b"%PDF-1.4 fake")]


def test_unknown_size_does_not_block(monkeypatch):
    """getFile without file_size (shouldn't happen, but be lenient): proceed as before."""
    _enable(monkeypatch)

    async def fake_put(content, remote):
        return True

    monkeypatch.setattr(nextcloud, "_put_bytes", fake_put)
    bot = _FakeBot(size=None)
    assert asyncio.run(nextcloud.upload_resume(bot, "fid", "cv.pdf")) is not None
    assert bot.downloaded is True


def test_cap_is_configurable(monkeypatch):
    _enable(monkeypatch, max_mb=1)

    async def fake_put(content, remote):
        return True

    monkeypatch.setattr(nextcloud, "_put_bytes", fake_put)
    bot = _FakeBot(size=2 * 1024 * 1024)
    assert asyncio.run(nextcloud.upload_resume(bot, "fid", "cv.pdf")) is None
    assert bot.downloaded is False
