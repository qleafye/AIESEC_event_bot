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
    
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        extra = "ignore"  # tolerate undeclared .env keys (e.g. legacy flags) — don't crash boot

config = Settings()
