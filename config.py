"""
Application configuration.
Reads from environment variables with sensible defaults.
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


class Config:
    """Flask configuration."""

    SECRET_KEY: str = os.environ.get("SECRET_KEY", secrets.token_hex(32))

    # Directories
    DATA_DIR: Path = DATA_DIR
    LOGS_DIR: Path = LOGS_DIR

    # Database
    DATABASE_PATH: Path = DATA_DIR / "imapsync.db"

    # Crypto
    FERNET_KEY: str | None = os.environ.get("FERNET_KEY")
    FERNET_KEY_FILE: Path = DATA_DIR / "fernet.key"

    # imapsync
    IMAPSYNC_PATH: str = os.environ.get(
        "IMAPSYNC_PATH", "/usr/local/bin/imapsync"
    )
    IMAPSYNC_TMPDIR: str = os.environ.get("IMAPSYNC_TMPDIR", "/tmp")

    # Number of reconnect attempts on transient network drops
    IMAPSYNC_RECONNECT_RETRIES: int = int(
        os.environ.get("IMAPSYNC_RECONNECT_RETRIES", "5")
    )

    # Maximum runtime per job in seconds (default 24 h).
    # The watchdog will kill the process if it exceeds this.
    IMAPSYNC_MAX_RUNTIME_SECONDS: int = int(
        os.environ.get("IMAPSYNC_MAX_RUNTIME_SECONDS", str(24 * 3600))
    )

    # Concurrency
    MAX_CONCURRENT_JOBS: int = int(
        os.environ.get("MAX_CONCURRENT_JOBS", "3")
    )

    # Log retention (days)
    LOG_RETENTION_DAYS: int = int(
        os.environ.get("LOG_RETENTION_DAYS", "30")
    )

    # Timezone
    TIMEZONE: str = os.environ.get(
        "TIMEZONE", "Asia/Ho_Chi_Minh"
    )

    # Auth
    AUTH_USERNAME: str = os.environ.get("AUTH_USERNAME", "admin")
    AUTH_PASSWORD: str = os.environ.get(
        "AUTH_PASSWORD", secrets.token_urlsafe(16)
    )

    # Flask
    DEBUG: bool = os.environ.get("DEBUG", "0") == "1"
    TESTING: bool = os.environ.get("TESTING", "0") == "1"

    ENV: str = os.environ.get("FLASK_ENV", "production")
