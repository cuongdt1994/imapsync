"""
SQLite database initialization and connection management.
Uses WAL mode for better concurrent read/write performance.
"""

import sqlite3
import threading
from pathlib import Path

from config import Config

# Thread-local storage for connections (one per thread)
_local = threading.local()

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    imap_host   TEXT    NOT NULL,
    imap_port   INTEGER NOT NULL DEFAULT 993,
    username    TEXT    NOT NULL,
    password    TEXT    NOT NULL,   -- Fernet-encrypted
    provider    TEXT    NOT NULL DEFAULT 'generic',  -- workmail / gmail / generic
    role        TEXT    NOT NULL DEFAULT 'source',   -- source / destination
    notes       TEXT    DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    dest_account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    status              TEXT    NOT NULL DEFAULT 'pending',
                           -- pending / running / completed / failed / stopped
    folders             TEXT,      -- JSON array of folders, null = all
    extra_args          TEXT,      -- additional imapsync flags
    pid                 INTEGER,   -- OS process ID while running
    total_messages      INTEGER DEFAULT 0,
    synced_messages     INTEGER DEFAULT 0,
    skipped_messages    INTEGER DEFAULT 0,
    error_messages      INTEGER DEFAULT 0,
    exit_code           INTEGER,
    heartbeat_at        TEXT,   -- last watchdog heartbeat timestamp
    started_at          TEXT,
    completed_at        TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stream      TEXT    NOT NULL,   -- stdout / stderr
    line        TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,      -- JSON-encoded
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_SETTINGS = {
    "max_concurrent_jobs": "3",
    "log_retention_days": "30",
    "imapsync_path": "/usr/local/bin/imapsync",
    "timezone": "Asia/Ho_Chi_Minh",
    "auth_username": "admin",
    # auth_password_hash is set during setup
}


def init_db(db_path: Path | None = None) -> None:
    """Create tables and default settings if they don't exist."""
    if db_path is None:
        db_path = Config.DATABASE_PATH

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)

    # ── Schema migrations (for databases created before these columns existed) ──
    # Add heartbeat_at if missing
    cur = conn.execute("PRAGMA table_info(jobs)")
    job_columns = {row[1] for row in cur.fetchall()}
    if "heartbeat_at" not in job_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT")
        conn.commit()

    # Insert default settings if not present
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    conn.commit()
    conn.close()


def get_connection() -> sqlite3.Connection:
    """Get a thread-local database connection."""
    if not hasattr(_local, "connection") or _local.connection is None:
        conn = sqlite3.connect(str(Config.DATABASE_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.connection = conn
    return _local.connection


def close_connection() -> None:
    """Close the thread-local connection if open."""
    if hasattr(_local, "connection") and _local.connection is not None:
        _local.connection.close()
        _local.connection = None
