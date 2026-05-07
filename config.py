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
    
    # Google Sheets
    GOOGLE_SHEET_ID: str = ""
    GOOGLE_CREDENTIALS_FILE: str = "google_credentials.json"
    
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

config = Settings()
