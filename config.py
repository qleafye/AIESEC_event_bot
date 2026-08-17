from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
from pydantic import SecretStr

class Settings(BaseSettings):
    BOT_TOKEN: SecretStr
    PROXY_URL: SecretStr | None = None
    # Backup proxy link (independent egress -- different tunnel/server/channel). If set,
    # the bot rotates to it on TelegramNetworkError and stays sticky (services/proxy_session.py).
    PROXY_URL_BACKUP: SecretStr | None = None
    # Seconds to stay on the backup before retrying the primary proxy link.
    # DEPRECATED as a day-to-day setting (Phase 14, CFG-01): read ONCE at startup to seed
    # bot_settings.proxy_recheck_seconds (main.py::seed_proxy_settings_from_env); day-to-day
    # editing is from the bot — «⚙️ Настройки → 🔧 Система» (needs a restart to take effect,
    # FailoverAiohttpSession only reads the registry value once, at construction time).
    PROXY_RECHECK_SECONDS: int = 600
    # Bounds connection SETUP only (TCP/SOCKS handshake), not the response wait -- a dead
    # link now costs this many seconds instead of the full request timeout (20-90s). 0
    # disables the bound (escape hatch back to unbounded connect).
    # DEPRECATED as a day-to-day setting (Phase 14, CFG-01): same one-time seed story as
    # PROXY_RECHECK_SECONDS above -> bot_settings.proxy_connect_timeout.
    PROXY_CONNECT_TIMEOUT: int = 5
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
    # DEPRECATED (Phase 14, CFG-01): read ONCE at startup to seed bot_settings.main_sheet_tab
    # (main.py::seed_main_sheet_tab_from_env); day-to-day editing is from the bot — «⚙️
    # Настройки → 📄 Вкладки таблицы». Field stays here (not removed) as a safety net for the
    # 4-stage resolve chain in services/sheets.py in case bot_settings ever gets wiped.
    GOOGLE_SHEET_TAB: str = ""

    # Event cities (Phase 07.1, CITY-01): format `code|label|tab_base;...`, records separated
    # by `;`. `tab_base` is optional (empty = the city stays on the LEGACY tabs
    # GOOGLE_SHEET_TAB / short_sheet_tab / party_sheet_tab / «Незавершённые» — this is why
    # msk's tab_base is empty: no live-data migration). Значащие дефолты живут прямо в коде
    # (не только в .env.example) — сервер обязан подняться с тремя городами, даже если .env
    # ещё не обновлён на момент деплоя.
    EVENT_CITIES: str = "msk|Москва, 30-31 октября|;spb|Санкт-Петербург, 3 октября|СПб;tyumen|Тюмень, 3 октября|Тюмень"
    # Code from EVENT_CITIES used when a delegate's event_city is NULL/unknown (see
    # cities.normalize_city — the single point where "no city on record" resolves to Москва).
    EVENT_CITY_DEFAULT: str = "msk"

    # Nextcloud (self-hosted) resume upload. All empty = feature off (fail-soft, no upload).
    # The share password is NEVER written to DB or sheet — only the resulting share URL is.
    # IN-02: NEXTCLOUD_BASE_URL removed — it was declared but never read (nextcloud.py uses
    # WEBDAV_URL / PUBLIC_URL / FOLDER_SHARE_TOKEN). extra="ignore" tolerates it in old .env files.
    NEXTCLOUD_WEBDAV_URL: str = ""        # e.g. "https://cloud.example.org/remote.php/dav/files/botuser"
    NEXTCLOUD_USER: str = ""
    NEXTCLOUD_APP_PASS: SecretStr | None = None
    NEXTCLOUD_FOLDER: str = "resumes"
    NEXTCLOUD_SHARE_PASSWORD: SecretStr | None = None  # kept for compat; code no longer reads it
    # Self-signed-cert deployment → default False (ssl verification off). If you move to a
    # trusted cert (Let's Encrypt), set NEXTCLOUD_VERIFY_TLS=true for MITM protection.
    NEXTCLOUD_VERIFY_TLS: bool = False
    # Public address used to build deep-links, e.g. "https://91.223.28.229:8443".
    NEXTCLOUD_PUBLIC_URL: str = ""
    # Token XXXX from the ONE manual public folder-share link /s/XXXX (folder `resumes`).
    NEXTCLOUD_FOLDER_SHARE_TOKEN: str = ""

    # IN-06: pydantic v2 idiom (was the deprecated inner `class Config`).
    # extra="ignore" tolerates undeclared .env keys (e.g. legacy flags) — don't crash boot.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

config = Settings()
