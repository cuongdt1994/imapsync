"""
Wrapper around the imapsync Perl tool.
Handles command building, subprocess execution, output parsing, and stopping.
"""

import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timezone
from typing import Callable

from config import Config
from models import job as job_model
from services.crypto_service import decrypt

logger = logging.getLogger("imapsync.service")

# Track running subprocesses for stopping
_running_processes: dict[int, subprocess.Popen] = {}
_lock = threading.Lock()


def build_command(
    source: dict,
    dest: dict,
    folders: list[str] | None = None,
    extra_args: str | None = None,
) -> list[str]:
    """Build the imapsync command line arguments."""
    src_password = decrypt(source["password"])
    dest_password = decrypt(dest["password"])

    cmd = [
        str(Config.IMAPSYNC_PATH),
        "--host1", source["imap_host"],
        "--port1", str(source.get("imap_port", 993)),
        "--user1", source["username"],
        "--password1", src_password,
        "--host2", dest["imap_host"],
        "--port2", str(dest.get("imap_port", 993)),
        "--user2", dest["username"],
        "--password2", dest_password,
        "--ssl1",
        "--ssl2",
        "--automap",
        "--useheader", "Message-Id",
        "--skipsize",
        "--tmpdir", str(Config.IMAPSYNC_TMPDIR),
        "--noreleasecheck",
    ]

    if folders:
        for folder in folders:
            cmd.extend(["--folder", folder])
    else:
        # --automap already handles folder mapping;
        # without explicit folders, sync all folders
        pass

    if extra_args:
        cmd.extend(shlex.split(extra_args))

    return cmd


def _parse_progress_line(line: str) -> dict | None:
    """Try to parse imapsync progress lines for message counts.
    imapsync outputs lines like:
      "msg 123/456 - copied - 1.2 s"
      "msg 123/456 - skipped - 0.1 s"
      "msg 123/456 - error - reason"
    We also look for totals at the end.
    """
    info: dict = {}

    # End-of-run summary lines (higher priority)
    if "Total bytes transferred" in line:
        return info
    if "Total bytes skipped" in line:
        return info

    # Message-level progress
    # "msg 42/100 - copied" or similar
    if "/" not in line:
        return info

    # Try to extract counts from imapsync's "Messages transferred" summary
    # Example: "Messages transferred: 123"
    # Example: "Messages skipped   : 45"
    # Example: "Messages error     : 2"
    lowered = line.lower()
    if "messages transferred" in lowered:
        try:
            info["synced"] = int(line.split(":")[-1].strip())
        except ValueError:
            pass
    elif "messages skipped" in lowered:
        try:
            info["skipped"] = int(line.split(":")[-1].strip())
        except ValueError:
            pass
    elif "messages found" in lowered and "total" in lowered:
        try:
            info["total"] = int(line.split(":")[-1].strip())
        except ValueError:
            pass

    return info


def _read_stream(stream, job_id: int, stream_name: str, batch_callback: Callable):
    """Read lines from a stream and log them. Lines are buffered and flushed
    periodically via batch_callback."""
    buffer: list[tuple[int, str, str, str]] = []
    for raw_line in iter(stream.readline, ""):
        if not raw_line:
            break
        line = raw_line.rstrip("\n").rstrip("\r")
        if not line:
            continue

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        buffer.append((job_id, stream_name, line, ts))

        # Parse progress
        progress = _parse_progress_line(line)
        if progress:
            batch_callback(progress)

        # Flush buffer periodically
        if len(buffer) >= 50:
            job_model.add_job_logs_batch(buffer)
            buffer.clear()

    # Flush remaining
    if buffer:
        job_model.add_job_logs_batch(buffer)


def run_job(job_id: int, on_complete: Callable | None = None) -> None:
    """Run an imapsync job in a subprocess. This is meant to be called
    from a worker thread.

    Args:
        job_id: The job ID to run.
        on_complete: Optional callback(job_id, exit_code) when done.
    """
    job = job_model.get_job(job_id)
    if not job:
        logger.error("Job %d not found", job_id)
        return

    from models.account import get_account
    source = get_account(job["source_account_id"])
    dest = get_account(job["dest_account_id"])

    if not source or not dest:
        logger.error("Source or dest account not found for job %d", job_id)
        job_model.update_job_status(job_id, "failed", exit_code=-1)
        if on_complete:
            on_complete(job_id, -1)
        return

    folders = None
    if job.get("folders"):
        import json
        folders = json.loads(job["folders"])

    cmd = build_command(source, dest, folders, job.get("extra_args"))

    # Mask passwords in logged command
    masked_cmd = list(cmd)
    for i, arg in enumerate(masked_cmd):
        if i > 0 and masked_cmd[i - 1] in ("--password1", "--password2"):
            masked_cmd[i] = "***"
    logger.info("Starting job %d: %s", job_id, " ".join(masked_cmd))

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # Track process for stopping
        with _lock:
            _running_processes[job_id] = process

        pid = process.pid
        job_model.update_job_status(job_id, "running", pid=pid)

        # Progress tracking (shared mutable container)
        progress_state: dict = {"total": 0, "synced": 0, "skipped": 0}

        def handle_progress(progress: dict):
            if "total" in progress:
                progress_state["total"] = progress["total"]
            if "synced" in progress:
                progress_state["synced"] = progress["synced"]
            if "skipped" in progress:
                progress_state["skipped"] = progress["skipped"]
            # Update DB periodically
            job_model.update_job_status(
                job_id, "running",
                total_messages=progress_state["total"],
                synced_messages=progress_state["synced"],
                skipped_messages=progress_state["skipped"],
            )

        # Read stdout and stderr in threads
        stdout_thread = threading.Thread(
            target=_read_stream,
            args=(process.stdout, job_id, "stdout", handle_progress),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_stream,
            args=(process.stderr, job_id, "stderr", handle_progress),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        # Wait for the process
        exit_code = process.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

        # Determine final status
        with _lock:
            _running_processes.pop(job_id, None)

        current_job = job_model.get_job(job_id)
        was_stopped = current_job and current_job["status"] == "stopped"

        if was_stopped:
            # Don't overwrite stopped status
            pass
        elif exit_code == 0:
            job_model.update_job_status(job_id, "completed", exit_code=exit_code)
        else:
            job_model.update_job_status(job_id, "failed", exit_code=exit_code)

        logger.info("Job %d finished with exit code %d", job_id, exit_code)

        if on_complete:
            on_complete(job_id, exit_code)

    except Exception as exc:
        logger.exception("Exception running job %d: %s", job_id, exc)
        with _lock:
            _running_processes.pop(job_id, None)
        job_model.update_job_status(job_id, "failed", exit_code=-1)
        if on_complete:
            on_complete(job_id, -1)


def stop_job(job_id: int) -> bool:
    """Stop a running imapsync process. Returns True if a process was found."""
    with _lock:
        process = _running_processes.get(job_id)

    if not process:
        return False

    pid = process.pid
    logger.info("Stopping job %d (pid %d)", job_id, pid)

    try:
        # Send SIGTERM first (imapsync handles this gracefully)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Force kill if still running
            logger.warning("Job %d did not stop, sending SIGKILL", job_id)
            process.kill()
            process.wait(timeout=5)
    except Exception as exc:
        logger.error("Error stopping job %d: %s", job_id, exc)
        return False

    with _lock:
        _running_processes.pop(job_id, None)

    job_model.update_job_status(job_id, "stopped")
    return True


def is_imapsync_installed() -> bool:
    """Check if imapsync is available on the system."""
    path = Config.IMAPSYNC_PATH
    return os.path.isfile(path) and os.access(path, os.X_OK)
