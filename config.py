# config.py
import os
from dataclasses import dataclass

@dataclass
class Settings:
    # Discord / app
    token: str
    timezone: str
    check_interval_seconds: int

    # Dados históricos (opcional)
    data_xlsx_path: str

    # Defaults do comando !surpresinha (podem ser sobrescritos no comando)
    surpresinha_default_n: int
    surpresinha_default_balanced: bool
    surpresinha_default_shuffle: bool

def _env_bool(name: str, default: bool = False) -> bool:
    """
    Converte variáveis de ambiente em bool.
    Aceita: "1", "true", "t", "yes", "y", "on" para True.
    """
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "t", "yes", "y", "on"}

def load_settings() -> Settings:
    from dotenv import load_dotenv
    load_dotenv()

    # Obrigatório
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN não definido (.env).")

    # Gerais
    timezone = os.getenv("TIMEZONE", "America/Fortaleza").strip()
    check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))

    # Históricos
    data_xlsx_path = os.getenv("DATA_XLSX_PATH", "./data.xlsx").strip()

    # Defaults do !surpresinha (podem ser ajustados no .env)
    surpresinha_default_n = int(os.getenv("SURPRESINHA_COUNT", "10"))
    surpresinha_default_balanced = _env_bool("SURPRESINHA_BALANCED", False)
    surpresinha_default_shuffle = _env_bool("SURPRESINHA_SHUFFLE", True)

    return Settings(
        token=token,
        timezone=timezone,
        check_interval_seconds=check_interval_seconds,
        data_xlsx_path=data_xlsx_path,
        surpresinha_default_n=surpresinha_default_n,
        surpresinha_default_balanced=surpresinha_default_balanced,
        surpresinha_default_shuffle=surpresinha_default_shuffle,
    )
