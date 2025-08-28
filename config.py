import os
from dataclasses import dataclass

@dataclass
class Settings:
    token: str
    data_xlsx_path: str
    timezone: str
    check_interval_seconds: int

def load_settings() -> Settings:
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    data_xlsx_path = os.getenv("DATA_XLSX_PATH", "./data.xlsx").strip()
    timezone = os.getenv("TIMEZONE", "America/Fortaleza").strip()
    check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN não definido (.env).")
    return Settings(token, data_xlsx_path, timezone, check_interval_seconds)
