"""
Application-level logging service.
Writes JSON-lines logs to files with rotation based on retention days.
"""

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

from config import Config


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            data["exc"] = str(record.exc_info[1])
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure application logging.

    - Console handler for development
    - Rotating file handler for persistent logs
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers
    root.handlers.clear()

    # Console handler (stderr)
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root.addHandler(console)

    # File handler with rotation
    log_dir = Config.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "app.log"
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    # Quiet noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    root.info("Logging initialized (level=%s, file=%s)", level, log_file)
