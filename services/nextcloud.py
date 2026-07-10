"""Self-hosted Nextcloud resume upload — WebDAV PUT + OCS public-share link.

Fully fail-soft: upload_resume() wraps its entire body in try/except and returns None
on ANY error (feature off, network down, bad credentials, timeout, unexpected shape).
It never raises to the caller, so a Nextcloud outage can never block or break
registration. The share password is read via SecretStr.get_secret_value() and is NEVER
logged or returned — only the resulting public share URL is.
"""
import logging
import re
import uuid

import aiohttp

from config import config

logger = logging.getLogger(__name__)


def _safe_name(filename: str) -> str:
    """Strip any path, keep only [\\w.\\-], fall back to 'resume'. Extension preserved."""
    base = (filename or "").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^\w.\-]", "_", base).strip("_")
    return base or "resume"


async def upload_resume(bot, file_id: str, filename: str) -> str | None:
    """Download a Telegram file, upload to Nextcloud, return a password-protected
    public share URL. Returns None on any failure or when the feature is unconfigured.
    Never raises."""
    try:
        # 1. Feature off if base/webdav URLs are not configured.
        if not config.NEXTCLOUD_BASE_URL or not config.NEXTCLOUD_WEBDAV_URL:
            logger.debug("nextcloud disabled (NEXTCLOUD_BASE_URL/WEBDAV_URL empty) — skipping upload")
            return None

        # 2. Download the file bytes from Telegram.
        buf = await bot.download(file_id)
        buf.seek(0)
        content = buf.read()

        # 3. Collision-safe remote name, extension preserved from the original.
        unique_name = f"{uuid.uuid4().hex[:8]}_{_safe_name(filename)}"

        # 4. TLS: ssl=False disables verification for self-signed certs.
        ssl_arg = None if config.NEXTCLOUD_VERIFY_TLS else False

        # 5. Basic auth from app password (never logged).
        auth = aiohttp.BasicAuth(
            config.NEXTCLOUD_USER,
            config.NEXTCLOUD_APP_PASS.get_secret_value() if config.NEXTCLOUD_APP_PASS else "",
        )

        folder = config.NEXTCLOUD_FOLDER.strip("/")
        remote_path = f"/{folder}/{unique_name}"
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
            # PUT the file to WebDAV.
            put_url = f"{config.NEXTCLOUD_WEBDAV_URL.rstrip('/')}/{folder}/{unique_name}"
            async with session.put(put_url, data=content, ssl=ssl_arg) as resp:
                if resp.status not in (200, 201, 204):
                    body = await resp.text()
                    logger.error("nextcloud WebDAV PUT failed: status=%s body=%s", resp.status, body[:300])
                    return None

            # POST the public share via OCS Share API.
            share_url = f"{config.NEXTCLOUD_BASE_URL.rstrip('/')}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json"
            form = {
                "path": remote_path,
                "shareType": "3",   # public link
                "permissions": "1", # read-only
            }
            if config.NEXTCLOUD_SHARE_PASSWORD:
                form["password"] = config.NEXTCLOUD_SHARE_PASSWORD.get_secret_value()

            async with session.post(
                share_url,
                data=form,
                headers={"OCS-APIRequest": "true"},
                ssl=ssl_arg,
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.error("nextcloud OCS share failed: status=%s body=%s", resp.status, body[:300])
                    return None
                payload = await resp.json(content_type=None)

        # 6. Extract the URL from OCS v2 JSON shape: ocs.data.url
        url = (payload or {}).get("ocs", {}).get("data", {}).get("url")
        if not url:
            logger.error("nextcloud OCS share response missing ocs.data.url")
            return None
        return url

    except Exception:  # noqa: BLE001 — fully fail-soft: never break registration
        logger.exception("nextcloud upload_resume failed — returning None (registration unaffected)")
        return None
