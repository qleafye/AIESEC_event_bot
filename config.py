from pydantic_settings import BaseSettings
from typing import List
from pydantic import SecretStr

class Settings(BaseSettings):
    BOT_TOKEN: SecretStr
    PROXY_URL: SecretStr | None = None
    ADMIN_IDS: List[int]
    UNIVERSITIES: List[str] = [
        "ИТМО",
        "Политех",
        "ЛЭТИ",
        "ГУАП",
        "БОООНЧ",
        "Горный",
        "Военмех",
        "СПбГАСУ",
        "СПбГУТ",
        "Технологический институт (СПбГТИ)",
        "ВШЭ (Питер)"
    ]
    DB_PATH: str = "data/forum.db"
    
    # Logging: DEBUG | INFO | WARNING | ERROR. Controls the file-handler level; stdout
    # (docker logs) is pinned to WARNING+ regardless, to keep container logs clean.
    LOG_LEVEL: str = "INFO"

    # Google Sheets
    GOOGLE_SHEET_ID: str = ""
    GOOGLE_CREDENTIALS_FILE: str = "google_credentials.json"
    # Target data tab by NAME (e.g. "реги бот"). Empty = first tab by position (.sheet1),
    # the historical behaviour. Set this so reordering tabs can't redirect the bot's writes.
    GOOGLE_SHEET_TAB: str = ""

    # Nextcloud (self-hosted) resume upload. All empty = feature off (fail-soft, no upload).
    # The share password is NEVER written to DB or sheet — only the resulting share URL is.
    NEXTCLOUD_BASE_URL: str = ""          # e.g. "https://cloud.example.org"
    NEXTCLOUD_WEBDAV_URL: str = ""        # e.g. "https://cloud.example.org/remote.php/dav/files/botuser"
    NEXTCLOUD_USER: str = ""
    NEXTCLOUD_APP_PASS: SecretStr | None = None
    NEXTCLOUD_FOLDER: str = "resumes"
    NEXTCLOUD_SHARE_PASSWORD: SecretStr | None = None  # kept for compat; code no longer reads it
    # WR-07: secure by default — verify the Nextcloud TLS cert (the PUT carries the app-password
    # + resume PII). Operators with a self-signed cert must OPT OUT explicitly via
    # NEXTCLOUD_VERIFY_TLS=false in .env. (Was insecure-by-default: ssl verification off.)
    NEXTCLOUD_VERIFY_TLS: bool = True
    # Public address used to build deep-links, e.g. "https://91.223.28.229:8443".
    NEXTCLOUD_PUBLIC_URL: str = ""
    # Token XXXX from the ONE manual public folder-share link /s/XXXX (folder `resumes`).
    NEXTCLOUD_FOLDER_SHARE_TOKEN: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        extra = "ignore"  # tolerate undeclared .env keys (e.g. legacy flags) — don't crash boot

config = Settings()
