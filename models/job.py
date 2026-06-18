"""
Job model — CRUD operations for migration jobs and their logs.
"""

import json
from datetime import datetime, timezone

from .database import get_connection

VALID_STATUSES = {"pending", "running", "completed", "failed", "stopped"}


# ---- Jobs ----

def list_jobs(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """List jobs, newest first, optionally filtered by status."""
    conn = get_connection()
    if status:
        rows = conn.execute(
            """
            SELECT j.*,
                   sa.name AS source_name,
                   da.name AS dest_name
            FROM jobs j
            JOIN accounts sa ON j.source_account_id = sa.id
            JOIN accounts da ON j.dest_account_id = da.id
            WHERE j.status = ?
            ORDER BY j.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        )
    else:
        rows = conn.execute(
            """
            SELECT j.*,
                   sa.name AS source_name,
                   da.name AS dest_name
            FROM jobs j
            JOIN accounts sa ON j.source_account_id = sa.id
            JOIN accounts da ON j.dest_account_id = da.id
            ORDER BY j.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
    return [dict(r) for r in rows]


def get_job(job_id: int) -> dict | None:
    """Get a single job with source/dest account names."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT j.*,
               sa.name AS source_name,
               da.name AS dest_name
        FROM jobs j
        JOIN accounts sa ON j.source_account_id = sa.id
        JOIN accounts da ON j.dest_account_id = da.id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def create_job(
    source_account_id: int,
    dest_account_id: int,
    folders: list[str] | None = None,
    extra_args: str | None = None,
) -> int:
    """Create a new migration job. Returns the new job ID."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_connection()
    cur = conn.execute(
        """
        INSERT INTO jobs (source_account_id, dest_account_id, folders, extra_args, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source_account_id, dest_account_id,
         json.dumps(folders) if folders else None,
         extra_args, now),
    )
    conn.commit()
    return cur.lastrowid


def update_job_status(
    job_id: int,
    status: str,
    **kwargs,
) -> bool:
    """Update job status and optional fields (pid, exit_code, message counts, timestamps)."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    conn = get_connection()

    set_clauses = ["status = ?"]
    params = [status]

    field_map = {
        "pid": "pid",
        "exit_code": "exit_code",
        "total_messages": "total_messages",
        "synced_messages": "synced_messages",
        "skipped_messages": "skipped_messages",
        "error_messages": "error_messages",
    }

    for kwarg_key, col in field_map.items():
        if kwarg_key in kwargs:
            set_clauses.append(f"{col} = ?")
            params.append(kwargs[kwarg_key])

    if status == "running" and "started_at" not in kwargs:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        set_clauses.append("started_at = ?")
        params.append(now)

    if status in ("completed", "failed", "stopped") and "completed_at" not in kwargs:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        set_clauses.append("completed_at = ?")
        params.append(now)

    # Allow explicit timestamp override
    for ts_field in ("started_at", "completed_at"):
        if ts_field in kwargs:
            # already handled above via status, but allow override
            pass

    params.append(job_id)
    sql = f"UPDATE jobs SET {', '.join(set_clauses)} WHERE id = ?"
    conn.execute(sql, params)
    conn.commit()
    return conn.total_changes > 0


def get_running_jobs() -> list[dict]:
    """Get all currently running jobs."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'running' ORDER BY started_at"
    )
    return [dict(r) for r in rows]


def count_jobs_by_status() -> dict[str, int]:
    """Return counts of jobs grouped by status."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
    )
    return {r["status"]: r["cnt"] for r in rows}


# ---- Job Logs ----

def add_job_log(job_id: int, stream: str, line: str, timestamp: str | None = None) -> None:
    """Append a log line to a job."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_connection()
    conn.execute(
        "INSERT INTO job_logs (job_id, stream, line, timestamp) VALUES (?, ?, ?, ?)",
        (job_id, stream, line, timestamp),
    )
    conn.commit()


def add_job_logs_batch(entries: list[tuple[int, str, str, str]]) -> None:
    """Batch insert log lines. Each entry: (job_id, stream, line, timestamp)."""
    if not entries:
        return
    conn = get_connection()
    conn.executemany(
        "INSERT INTO job_logs (job_id, stream, line, timestamp) VALUES (?, ?, ?, ?)",
        entries,
    )
    conn.commit()


def get_job_logs(job_id: int, limit: int = 500, offset: int = 0) -> list[dict]:
    """Get log lines for a job, oldest first."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM job_logs
        WHERE job_id = ?
        ORDER BY id ASC
        LIMIT ? OFFSET ?
        """,
        (job_id, limit, offset),
    )
    return [dict(r) for r in rows]


def get_job_log_count(job_id: int) -> int:
    """Count total log lines for a job."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM job_logs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def delete_old_logs(before_date: str) -> int:
    """Delete job_logs older than the given date. Returns count deleted."""
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM job_logs WHERE timestamp < ?", (before_date,)
    )
    conn.commit()
    return cur.rowcount


def delete_old_jobs(before_date: str) -> int:
    """Delete completed/failed/stopped jobs older than the given date. Returns count deleted."""
    conn = get_connection()
    cur = conn.execute(
        """
        DELETE FROM jobs
        WHERE status IN ('completed', 'failed', 'stopped')
          AND created_at < ?
        """,
        (before_date,),
    )
    conn.commit()
    return cur.rowcount


# ---- Settings ----

def get_setting(key: str, default: str | None = None) -> str | None:
    """Get a setting value by key."""
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """Set a setting value (upsert)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )
    conn.commit()


def get_all_settings() -> dict[str, str]:
    """Get all settings as a dict."""
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key")
    return {r["key"]: r["value"] for r in rows}
