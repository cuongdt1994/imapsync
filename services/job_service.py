"""
Job queue service — concurrency control, lifecycle management, retry.
"""

import json
import logging
import threading

from config import Config
from models import job as job_model
from services import imapsync_service

logger = logging.getLogger("job.service")

# Semaphore to limit concurrent jobs
_semaphore = threading.BoundedSemaphore(Config.MAX_CONCURRENT_JOBS)
# Track active worker threads
_active_threads: dict[int, threading.Thread] = {}


def can_start_new() -> bool:
    """Check if we can start another migration."""
    running = job_model.get_running_jobs()
    return len(running) < Config.MAX_CONCURRENT_JOBS


def start_job(job_id: int) -> bool:
    """Start a migration job in a background thread.
    Returns True if the job was started, False if the queue is full.
    """
    if not can_start_new():
        logger.warning("Cannot start job %d: queue full", job_id)
        return False

    def _on_complete(job_id: int, exit_code: int):
        """Called when imapsync finishes."""
        logger.info("Job %d completed with exit code %d", job_id, exit_code)
        # Release a slot
        _active_threads.pop(job_id, None)

    def _worker():
        """Wrapper that acquires semaphore before running."""
        acquired = _semaphore.acquire(blocking=False)
        if not acquired:
            return
        try:
            imapsync_service.run_job(job_id, _on_complete)
        finally:
            _semaphore.release()

    thread = threading.Thread(
        target=_worker,
        name=f"imapsync-job-{job_id}",
        daemon=True,
    )
    _active_threads[job_id] = thread
    thread.start()
    logger.info("Job %d dispatched to worker thread", job_id)
    return True


def stop_job(job_id: int) -> tuple[bool, str]:
    """Stop a running job. Returns (success, message)."""
    job = job_model.get_job(job_id)
    if not job:
        return False, "Job not found."
    if job["status"] != "running":
        return False, f"Job is not running (status: {job['status']})."

    success = imapsync_service.stop_job(job_id)
    if success:
        return True, "Job stopped."
    else:
        return False, "Failed to stop the imapsync process."


def retry_job(job_id: int) -> int | None:
    """Create a new job with the same config as the given job.
    Returns the new job ID, or None if the original job is not found.
    """
    original = job_model.get_job(job_id)
    if not original:
        return None

    new_job_id = job_model.create_job(
        source_account_id=original["source_account_id"],
        dest_account_id=original["dest_account_id"],
        folders=json.loads(original["folders"]) if original.get("folders") else None,
        extra_args=original.get("extra_args"),
    )
    logger.info(
        "Retry: job %d → new job %d (source %s → dest %s)",
        job_id, new_job_id,
        original["source_name"], original["dest_name"],
    )
    return new_job_id


def get_stats() -> dict:
    """Get overall migration statistics for the dashboard."""
    counts = job_model.count_jobs_by_status()
    running = job_model.get_running_jobs()

    return {
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "stopped": counts.get("stopped", 0),
        "pending": counts.get("pending", 0),
        "max_concurrent": Config.MAX_CONCURRENT_JOBS,
        "running_jobs": [dict(r) for r in running],
        "queue_available": can_start_new(),
    }


def cleanup_old_logs() -> int:
    """Remove logs older than LOG_RETENTION_DAYS. Returns count deleted."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) -
              timedelta(days=Config.LOG_RETENTION_DAYS))
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    deleted_logs = job_model.delete_old_logs(cutoff_str)
    deleted_jobs = job_model.delete_old_jobs(cutoff_str)

    logger.info(
        "Log cleanup: removed %d log lines and %d old jobs (cutoff: %s)",
        deleted_logs, deleted_jobs, cutoff_str,
    )
    return deleted_logs + deleted_jobs
