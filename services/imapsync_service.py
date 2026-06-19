"""
Wrapper around the imapsync Perl tool.
Handles command building, subprocess execution, output parsing, and stopping.

Lifecycle guarantees:
- Process runs under its own session (process group) so stop kills the
  entire tree, not just the shell wrapper.
- A heartbeat thread detects a stuck/zombie process and terminates it.
- A max-runtime timer prevents infinite hangs (default 24 h).
- On stop, SIGTERM → wait → SIGKILL to the whole process group.
- Temporary files are cleaned up after the run.
- Exit codes are decoded into human-readable messages.
"""

import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from config import Config
from models import job as job_model
from services.crypto_service import decrypt

logger = logging.getLogger("imapsync.service")

# Track running subprocesses for stopping
_running_processes: dict[int, subprocess.Popen] = {}
_lock = threading.Lock()

# ── imapsync exit code → human-readable message ──────────────────────
EXIT_CODE_MESSAGES: dict[int, str] = {
    0:   "OK — all messages transferred successfully.",
    1:   "Warning — some messages could not be transferred (see logs).",
    2:   "Error — critical failure, check source/destination connectivity.",
    12:  "Authentication failure — wrong username or password.",
    16:  "Some messages could not be transferred (size limit, timeout, or server rejection).",
    111: "Temporary failure — network or server issue, retry recommended.",
}


def _exit_code_message(exit_code: int) -> str:
    """Return a human-readable message for an imapsync exit code."""
    return EXIT_CODE_MESSAGES.get(
        exit_code,
        f"Unknown exit code {exit_code} — see imapsync logs for details.",
    )


# ── Command builder ───────────────────────────────────────────────────

def build_command(
    source: dict,
    dest: dict,
    folders: list[str] | None = None,
    extra_args: str | None = None,
) -> list[str]:
    """Build the imapsync command line arguments."""
    src_password = decrypt(source["password"])
    dest_password = decrypt(dest["password"])

    reconnect_retries = str(Config.IMAPSYNC_RECONNECT_RETRIES)

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
        # Connection resilience: retry on transient network drops
        "--reconnectretry1", reconnect_retries,
        "--reconnectretry2", reconnect_retries,
        # I/O timeouts so a single stuck message doesn't hang forever
        "--timeout1", "120",
        "--timeout2", "120",
    ]

    if folders:
        for folder in folders:
            cmd.extend(["--folder", folder])
    # else: --automap already handles folder mapping;
    # without explicit folders, sync all folders

    if extra_args:
        cmd.extend(shlex.split(extra_args))

    return cmd


# ── Output parsing ────────────────────────────────────────────────────

def _parse_progress_line(line: str) -> dict | None:
    """Parse imapsync progress/error lines for message counts.

    Handles two kinds of output:

    Per-message (real-time):
      "msg 123/500 - copied - 1.2 s"
      "msg 124/500 - skipped - 0.1 s"
      "msg 125/500 - error - reason"

    End-of-run summary:
      "Messages transferred: 123"
      "Messages skipped   : 45"
      "Messages error     : 2"
      "Total messages     : 500"
    """
    import re

    info: dict = {}

    # ── Per-message progress (real-time) ──
    # imapsync 2.x format: "msg 42/500 - copied - 1.2 s"
    m = re.match(
        r'msg\s+(\d+)/(\d+)\s*-\s*(copied|skipped|error)\b',
        line, re.IGNORECASE,
    )
    if m:
        current = int(m.group(1))
        total = int(m.group(2))
        action = m.group(3).lower()
        info["total"] = total
        if action == "copied":
            info["synced_inc"] = 1
            info["last_processed"] = current
        elif action == "skipped":
            info["skipped_inc"] = 1
            info["last_processed"] = current
        elif action == "error":
            info["errors_inc"] = 1
            info["last_processed"] = current
        return info

    # ── Also try: "Copied: 42  Skipped: 3  Error: 1" (older imapsync) ──
    m2 = re.match(
        r'(?i).*\bcopied\s*[:=]?\s*(\d+).*\bskipped\s*[:=]?\s*(\d+).*\berror\s*[:=]?\s*(\d+)',
        line,
    )
    if m2:
        info["synced"] = int(m2.group(1))
        info["skipped"] = int(m2.group(2))
        info["errors"] = int(m2.group(3))
        return info

    # ── End-of-run summary (exact counts, overrides incremental) ──
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
    elif "messages error" in lowered:
        try:
            info["errors"] = int(line.split(":")[-1].strip())
        except ValueError:
            pass
    elif "total messages" in lowered:
        try:
            info["total"] = int(line.split(":")[-1].strip())
        except ValueError:
            pass

    if "total bytes transferred" in lowered:
        try:
            info["bytes_transferred"] = int(line.split(":")[-1].strip())
        except ValueError:
            pass

    return info if info else None


def _read_stream(stream, job_id: int, stream_name: str, batch_callback: Callable):
    """Read lines from a stream and log them. Lines are buffered and
    flushed periodically via batch_callback."""
    buffer: list[tuple[int, str, str, str]] = []
    for raw_line in iter(stream.readline, ""):
        if not raw_line:
            break
        line = raw_line.rstrip("\n").rstrip("\r")
        if not line:
            continue

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        buffer.append((job_id, stream_name, line, ts))

        # Parse progress / errors
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


# ── Heartbeat & max-runtime watchdog ──────────────────────────────────

def _watchdog(
    job_id: int,
    process: subprocess.Popen,
    stop_event: threading.Event,
    max_runtime_seconds: int,
):
    """Background thread that monitors the imapsync process.

    - Heartbeat: periodically updates the job record with a "last seen"
      timestamp so the UI can detect a truly stuck job.
    - Max runtime: if the process exceeds max_runtime_seconds, it is
      terminated gracefully (SIGTERM → SIGKILL).
    """
    started = time.monotonic()
    heartbeat_interval = 30  # seconds

    while not stop_event.is_set():
        stop_event.wait(timeout=heartbeat_interval)
        if stop_event.is_set():
            return

        # Check if process is still alive
        poll_result = process.poll()
        if poll_result is not None:
            # Process exited on its own — watchdog can exit
            return

        elapsed = time.monotonic() - started

        # Update heartbeat in DB
        job_model.update_job_status(
            job_id, "running",
            heartbeat_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        )

        # Max runtime exceeded?
        if elapsed > max_runtime_seconds:
            logger.error(
                "Job %d exceeded max runtime (%d s). Forcing termination.",
                job_id, max_runtime_seconds,
            )
            try:
                # Kill the entire process group
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                time.sleep(5)
                if process.poll() is None:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError) as exc:
                logger.warning("Watchdog kill error (already dead?): %s", exc)
            return


# ── Temp-file cleanup ─────────────────────────────────────────────────

def _cleanup_tmp(job_id: int, pid: int | None) -> None:
    """Remove imapsync temporary files for a given job/pid."""
    if pid is None:
        return
    tmp_dir = Path(Config.IMAPSYNC_TMPDIR)
    if not tmp_dir.is_dir():
        return
    # imapsync creates files like: /tmp/imapsync_<pid>/
    # and /tmp/.imapsync_cache or similar
    patterns = [
        tmp_dir / f"imapsync_{pid}",
        tmp_dir / f"imapsync_{pid}.txt",
        tmp_dir / f".imapsync_{pid}",
    ]
    for path in patterns:
        try:
            if path.is_dir():
                import shutil
                shutil.rmtree(path, ignore_errors=True)
                logger.debug("Cleaned up tmp dir: %s", path)
            elif path.is_file():
                path.unlink(missing_ok=True)
                logger.debug("Cleaned up tmp file: %s", path)
        except OSError as exc:
            logger.debug("Could not clean up %s: %s", path, exc)


# ── Main job runner ───────────────────────────────────────────────────

def run_job(job_id: int, on_complete: Callable | None = None) -> None:
    """Run an imapsync job in a subprocess.  Called from a worker thread.

    Args:
        job_id:      The job ID to run.
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

    # Clear CGI-related environment variables so imapsync does NOT
    # auto-detect CGI mode (it would hard-code /var/tmp/imapsync_cgi/
    # and ignore --tmpdir).  See INSTALL.OnlineUI.txt lines 387-390.
    clean_env = os.environ.copy()
    for cgi_var in (
        "GATEWAY_INTERFACE", "SERVER_SOFTWARE", "REQUEST_METHOD",
        "QUERY_STRING", "CONTENT_TYPE", "CONTENT_LENGTH",
        "HTTP_ACCEPT", "HTTP_USER_AGENT", "REMOTE_ADDR",
        "SERVER_PROTOCOL", "SERVER_NAME", "SERVER_PORT",
        "SCRIPT_NAME", "PATH_INFO", "PATH_TRANSLATED",
        "REMOTE_HOST", "REMOTE_IDENT", "AUTH_TYPE",
        "REMOTE_USER", "DOCUMENT_ROOT", "HTTP_HOST",
        "HTTP_COOKIE", "HTTP_REFERER",
    ):
        clean_env.pop(cgi_var, None)

    process = None
    pid = None
    watchdog_stop = threading.Event()
    watchdog_thread = None
    exit_code = -1

    try:
        # start_new_session=True creates a new process group (setsid)
        # so we can kill the entire tree on stop.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=clean_env,
            start_new_session=True,
        )

        pid = process.pid

        # Track process for stopping
        with _lock:
            _running_processes[job_id] = process

        job_model.update_job_status(job_id, "running", pid=pid)
        logger.info("Job %d running (pid=%d, pgid=%d)", job_id, pid, os.getpgid(pid))

        # Start watchdog / heartbeat thread
        max_runtime = Config.IMAPSYNC_MAX_RUNTIME_SECONDS
        watchdog_thread = threading.Thread(
            target=_watchdog,
            args=(job_id, process, watchdog_stop, max_runtime),
            name=f"watchdog-job-{job_id}",
            daemon=True,
        )
        watchdog_thread.start()

        # Progress tracking (shared mutable container)
        progress_state: dict = {
            "total": 0,
            "synced": 0,
            "skipped": 0,
            "errors": 0,
            "bytes_transferred": 0,
        }

        def handle_progress(progress: dict):
            # Absolute values (from summary lines) — override
            if "total" in progress:
                progress_state["total"] = progress["total"]
            if "synced" in progress:
                progress_state["synced"] = progress["synced"]
            if "skipped" in progress:
                progress_state["skipped"] = progress["skipped"]
            if "errors" in progress:
                progress_state["errors"] = progress["errors"]
            # Incremental values (from per-message lines) — accumulate
            if "synced_inc" in progress:
                progress_state["synced"] += progress["synced_inc"]
            if "skipped_inc" in progress:
                progress_state["skipped"] += progress["skipped_inc"]
            if "errors_inc" in progress:
                progress_state["errors"] += progress["errors_inc"]
            # Update DB periodically (every line for real-time feel)
            job_model.update_job_status(
                job_id, "running",
                total_messages=progress_state["total"],
                synced_messages=progress_state["synced"],
                skipped_messages=progress_state["skipped"],
                error_messages=progress_state["errors"],
            )

        # Read stdout and stderr in parallel threads
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

        # Block until process exits
        exit_code = process.wait()
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)

        # Stop watchdog
        watchdog_stop.set()
        if watchdog_thread:
            watchdog_thread.join(timeout=5)

        # ── Final status ──────────────────────────────────────────
        # Update DB BEFORE removing from _running_processes so that
        # stop_job() never sees a "running" job with no process to kill.
        current_job = job_model.get_job(job_id)
        was_stopped = current_job and current_job["status"] == "stopped"

        if not was_stopped:
            if exit_code == 0:
                job_model.update_job_status(
                    job_id, "completed", exit_code=exit_code,
                    total_messages=progress_state["total"],
                    synced_messages=progress_state["synced"],
                    skipped_messages=progress_state["skipped"],
                    error_messages=progress_state["errors"],
                )
                logger.info(
                    "Job %d completed OK — %d synced, %d skipped, %d errors",
                    job_id, progress_state["synced"],
                    progress_state["skipped"], progress_state["errors"],
                )
            else:
                exit_msg = _exit_code_message(exit_code)
                job_model.update_job_status(
                    job_id, "failed", exit_code=exit_code,
                    total_messages=progress_state["total"],
                    synced_messages=progress_state["synced"],
                    skipped_messages=progress_state["skipped"],
                    error_messages=progress_state["errors"],
                )
                logger.warning(
                    "Job %d failed (exit=%d): %s — %d synced, %d skipped, %d errors",
                    job_id, exit_code, exit_msg,
                    progress_state["synced"], progress_state["skipped"],
                    progress_state["errors"],
                )

        # Only now remove from the process map
        with _lock:
            _running_processes.pop(job_id, None)

    except Exception as exc:
        logger.exception("Exception running job %d: %s", job_id, exc)
        # Best-effort cleanup
        watchdog_stop.set()
        with _lock:
            _running_processes.pop(job_id, None)
        job_model.update_job_status(job_id, "failed", exit_code=-1)

    finally:
        # Always clean up temporary files
        _cleanup_tmp(job_id, pid)

        # Safety: ensure process is reaped
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            except Exception:
                pass

        if on_complete:
            on_complete(job_id, exit_code)


# ── Stop ──────────────────────────────────────────────────────────────

def stop_job(job_id: int) -> bool:
    """Stop a running imapsync process.

    Sends SIGTERM to the entire process group, waits, then SIGKILL if
    still running.  Falls back gracefully if the process already exited.

    Returns True if the process was found and stopped (or if the process
    had already exited but the job was still marked 'running' — we update
    the status to 'stopped' in that case).
    """
    with _lock:
        process = _running_processes.get(job_id)

    if not process:
        # Process may have already exited on its own (crash, error, etc.)
        # while the job status was still "running".  Update the status so
        # the UI doesn't show a zombie "running" job.
        job = job_model.get_job(job_id)
        if job and job["status"] == "running":
            logger.info(
                "Job %d: no process found but status still 'running' — "
                "marking as stopped (process exited on its own).",
                job_id,
            )
            job_model.update_job_status(job_id, "stopped")
            return True
        return False

    pid = process.pid
    logger.info("Stopping job %d (pid=%d)", job_id, pid)

    success = True
    try:
        # Send SIGTERM to the entire process group first
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            logger.debug("Sent SIGTERM to process group %d", pgid)
        except (ProcessLookupError, OSError):
            # Process already dead or pgid not available
            process.terminate()

        # Wait up to 15 s for graceful shutdown
        try:
            process.wait(timeout=15)
            logger.info("Job %d stopped gracefully (SIGTERM).", job_id)
        except subprocess.TimeoutExpired:
            # Force kill the entire process group
            logger.warning("Job %d did not stop, sending SIGKILL", job_id)
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                process.kill()
            process.wait(timeout=5)
            logger.info("Job %d killed (SIGKILL).", job_id)

    except Exception as exc:
        logger.error("Error stopping job %d: %s", job_id, exc)
        success = False

    with _lock:
        _running_processes.pop(job_id, None)

    if success:
        job_model.update_job_status(job_id, "stopped")

    # Clean up temp files
    _cleanup_tmp(job_id, pid)

    return success


# ── Health check ──────────────────────────────────────────────────────

def is_imapsync_installed() -> bool:
    """Check if imapsync is available on the system."""
    path = Config.IMAPSYNC_PATH
    return os.path.isfile(path) and os.access(path, os.X_OK)


def get_stuck_jobs() -> list[int]:
    """Return IDs of jobs whose process is no longer alive but still
    marked 'running' in the DB.  Call this periodically (e.g. via cron
    or a scheduler) to auto-repair zombie statuses."""
    stuck: list[int] = []
    with _lock:
        tracked_ids = list(_running_processes.keys())

    for job_id in tracked_ids:
        with _lock:
            process = _running_processes.get(job_id)
        if process is None:
            continue
        if process.poll() is not None:
            # Process exited but job is still in _running_processes
            # (shouldn't happen with the current run_job logic, but
            #  safety net)
            stuck.append(job_id)

    return stuck
