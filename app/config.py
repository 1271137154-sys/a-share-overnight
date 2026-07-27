from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "A股尾盘选股")
    database_path: Path = ROOT / os.getenv("DATABASE_PATH", "data/stock_selector.db")
    log_dir: Path = ROOT / os.getenv("LOG_DIR", "logs")
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "900"))
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    request_retries: int = int(os.getenv("REQUEST_RETRIES", "3"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    session_secret: str = os.getenv("SESSION_SECRET", "development-only-change-me")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password_hash: str = os.getenv("ADMIN_PASSWORD_HASH", "")
    enable_scheduler: bool = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
    scheduler_hour: int = int(os.getenv("SCHEDULER_HOUR", "14"))
    scheduler_minute: int = int(os.getenv("SCHEDULER_MINUTE", "40"))

settings = Settings()
