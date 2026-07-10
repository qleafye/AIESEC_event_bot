"""Self-hosted Nextcloud resume upload — WebDAV PUT + deep-link into ONE manual folder share.

Sharing model (new): the bot NO LONGER creates a per-file OCS public share. Instead a
single public share of the `resumes` folder is created ONCE by hand in the Nextcloud UI
(its password is set there too). The bot only uploads bytes via WebDAV PUT and builds a
deep-link into that one folder share, pointing straight at the uploaded file:

    {PUBLIC_URL}/s/{FOLDER_SHARE_TOKEN}/download?path=%2F&files=<name>

Fully fail-soft: upload_resume()/upload_text_resume() wrap their whole body in try/except
and return None on ANY error (feature off, network down, bad credentials, timeout). They
never raise to the caller, so a Nextcloud outage can never block or break registration.
No password is ever read, logged, or returned by this module — only the deep-link URL is.
"""
import logging
import re
import uuid
from urllib.parse import quote

import aiohttp

from config import config

logger = logging.getLogger(__name__)


def _safe_name(filename: str) -> str:
    """Strip any path, keep only [\\w.\\-], fall back to 'resume'. Extension preserved."""
    base = (filename or "").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^\w.\-]", "_", base).strip("_")
    return base or "resume"


async def _put_bytes(content: bytes, remote_name: str) -> bool:
    """WebDAV PUT raw bytes to {WEBDAV_URL}/{folder}/{remote_name}. Returns True on
    2xx-ish success, False (with an error log) otherwise. Assumes config is present —
    callers guard for that. Never raises to the caller under normal aiohttp errors is
    NOT guaranteed here; the public entrypoints wrap this in try/except."""
    folder = config.NEXTCLOUD_FOLDER.strip("/")
    ssl_arg = None if config.NEXTCLOUD_VERIFY_TLS else False
    auth = aiohttp.BasicAuth(
        config.NEXTCLOUD_USER,
        config.NEXTCLOUD_APP_PASS.get_secret_value() if config.NEXTCLOUD_APP_PASS else "",
    )
    timeout = aiohttp.ClientTimeout(total=15)
    put_url = f"{config.NEXTCLOUD_WEBDAV_URL.rstrip('/')}/{folder}/{remote_name}"
    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        async with session.put(put_url, data=content, ssl=ssl_arg) as resp:
            if resp.status not in (200, 201, 204):
                body = await resp.text()
                logger.error("nextcloud WebDAV PUT failed: status=%s body=%s", resp.status, body[:300])
                return False
    return True


def _file_link(remote_name: str) -> str:
    """Build the deep-link into the single manual folder share, targeting one file.

    Kept as a separate function so the link format is trivial to change later
    (e.g. switch download → in-browser preview)."""
    return (
        f"{config.NEXTCLOUD_PUBLIC_URL.rstrip('/')}/s/{config.NEXTCLOUD_FOLDER_SHARE_TOKEN}"
        f"/download?path=%2F&files={quote(remote_name)}"
    )


async def upload_resume(bot, file_id: str, filename: str) -> str | None:
    """Download a Telegram file, WebDAV-PUT it to Nextcloud, return a deep-link into the
    manual folder share. Returns None on any failure or when the feature is unconfigured.
    Never raises."""
    try:
        if (
            not config.NEXTCLOUD_WEBDAV_URL
            or not config.NEXTCLOUD_PUBLIC_URL
            or not config.NEXTCLOUD_FOLDER_SHARE_TOKEN
        ):
            logger.debug(
                "nextcloud disabled (WEBDAV_URL/PUBLIC_URL/FOLDER_SHARE_TOKEN empty) — skipping upload"
            )
            return None

        buf = await bot.download(file_id)
        buf.seek(0)
        content = buf.read()

        remote = f"{uuid.uuid4().hex[:8]}_{_safe_name(filename)}"
        if await _put_bytes(content, remote):
            return _file_link(remote)
        return None

    except Exception:  # noqa: BLE001 — fully fail-soft: never break registration
        logger.exception("nextcloud upload_resume failed — returning None (registration unaffected)")
        return None


async def upload_text_resume(text: str, filename: str) -> str | None:
    """Upload a text resume as a .txt file via WebDAV PUT, return a deep-link into the
    manual folder share. No Bot needed. Returns None on failure / when unconfigured.
    Never raises."""
    try:
        if (
            not config.NEXTCLOUD_WEBDAV_URL
            or not config.NEXTCLOUD_PUBLIC_URL
            or not config.NEXTCLOUD_FOLDER_SHARE_TOKEN
        ):
            logger.debug(
                "nextcloud disabled (WEBDAV_URL/PUBLIC_URL/FOLDER_SHARE_TOKEN empty) — skipping upload"
            )
            return None

        content = (text or "").encode("utf-8")
        remote = f"{uuid.uuid4().hex[:8]}_{_safe_name(filename)}"
        if await _put_bytes(content, remote):
            return _file_link(remote)
        return None

    except Exception:  # noqa: BLE001 — fully fail-soft: never break registration
        logger.exception("nextcloud upload_text_resume failed — returning None (registration unaffected)")
        return None
