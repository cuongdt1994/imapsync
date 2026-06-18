"""
All web routes for the imapsync web application.
"""

import json
import uuid

from flask import (
    Blueprint, Response, flash, g, jsonify, redirect, render_template,
    request, url_for,
)

from models import account as account_model
from models import job as job_model
from services import crypto_service, imapsync_service, job_service
from web.auth import require_auth

web_bp = Blueprint("web", __name__)


@web_bp.app_context_processor
def inject_globals():
    """Make settings available in all templates."""
    from flask import current_app
    from models.job import get_all_settings
    return {
        "settings": get_all_settings(),
        "app_config": current_app.config,
    }


def _csrf_token() -> str:
    """Generate or retrieve a simple CSRF token stored in cookie."""
    token = request.cookies.get("csrf_token")
    if not token:
        token = uuid.uuid4().hex
    # Store in g so after_request can set it as a cookie if needed
    g.csrf_token = token
    return token


def _check_csrf() -> bool:
    """Validate CSRF token for POST requests."""
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return True
    token = request.form.get("csrf_token", "")
    cookie_token = request.cookies.get("csrf_token", "")
    if not token or not cookie_token:
        return False
    return token == cookie_token


@web_bp.before_request
@require_auth
def before_request():
    """All web routes require authentication."""
    pass


@web_bp.after_request
def set_csrf_cookie(response):
    """Ensure every response has a CSRF cookie set."""
    if not request.cookies.get("csrf_token"):
        token = getattr(g, "csrf_token", uuid.uuid4().hex)
        response.set_cookie("csrf_token", token, httponly=True, samesite="Strict")
    return response


# ---- Dashboard ----

@web_bp.route("/")
def dashboard():
    stats = job_service.get_stats()
    recent_jobs = job_model.list_jobs(limit=10)
    return render_template("dashboard.html", stats=stats, recent_jobs=recent_jobs)


# ---- Accounts ----

@web_bp.route("/accounts")
def account_list():
    accounts = account_model.list_accounts()
    csrf = _csrf_token()
    return render_template("accounts/list.html", accounts=accounts, csrf_token=csrf)


@web_bp.route("/accounts/add", methods=["GET", "POST"])
def account_add():
    if request.method == "POST":
        if not _check_csrf():
            return Response("CSRF validation failed", 400)
        try:
            encrypted_pw = crypto_service.encrypt(request.form["password"])
            account_model.create_account(
                name=request.form["name"].strip(),
                imap_host=request.form["imap_host"].strip(),
                imap_port=int(request.form.get("imap_port", 993)),
                username=request.form["username"].strip(),
                password=encrypted_pw,
                provider=request.form.get("provider", "generic").strip(),
                role=request.form.get("role", "source").strip(),
                notes=request.form.get("notes", "").strip(),
            )
            flash("Account created successfully.", "success")
            return redirect(url_for("web.account_list"))
        except Exception as exc:
            flash(f"Error creating account: {exc}", "error")

    csrf = _csrf_token()
    return render_template("accounts/add.html", csrf_token=csrf)


@web_bp.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
def account_edit(account_id: int):
    account = account_model.get_account(account_id)
    if not account:
        flash("Account not found.", "error")
        return redirect(url_for("web.account_list"))

    if request.method == "POST":
        if not _check_csrf():
            return Response("CSRF validation failed", 400)
        try:
            password = request.form["password"].strip()
            if password:
                encrypted_pw = crypto_service.encrypt(password)
            else:
                encrypted_pw = None  # Keep existing

            account_model.update_account(
                account_id=account_id,
                name=request.form["name"].strip(),
                imap_host=request.form["imap_host"].strip(),
                imap_port=int(request.form.get("imap_port", 993)),
                username=request.form["username"].strip(),
                password=encrypted_pw,
                provider=request.form.get("provider", "generic").strip(),
                role=request.form.get("role", "source").strip(),
                notes=request.form.get("notes", "").strip(),
            )
            flash("Account updated.", "success")
            return redirect(url_for("web.account_list"))
        except Exception as exc:
            flash(f"Error updating account: {exc}", "error")

    csrf = _csrf_token()
    return render_template("accounts/edit.html", account=account, csrf_token=csrf)


@web_bp.route("/accounts/<int:account_id>/delete", methods=["POST"])
def account_delete(account_id: int):
    if not _check_csrf():
        return Response("CSRF validation failed", 400)

    account = account_model.get_account(account_id)
    if not account:
        flash("Account not found.", "error")
        return redirect(url_for("web.account_list"))

    # Check if account is used in any pending/running jobs
    running_jobs = job_model.get_running_jobs()
    for rj in running_jobs:
        if rj["source_account_id"] == account_id or rj["dest_account_id"] == account_id:
            flash("Cannot delete account: it is used in a running migration.", "error")
            return redirect(url_for("web.account_list"))

    account_model.delete_account(account_id)
    flash("Account deleted.", "success")
    return redirect(url_for("web.account_list"))


@web_bp.route("/accounts/quick-setup", methods=["GET", "POST"])
def account_quick_setup():
    """Create both source and destination accounts in one form."""
    if request.method == "POST":
        if not _check_csrf():
            return Response("CSRF validation failed", 400)
        try:
            # Create source account
            src_pw = crypto_service.encrypt(request.form["source_password"])
            account_model.create_account(
                name=request.form["source_name"].strip(),
                imap_host=request.form["source_imap_host"].strip(),
                imap_port=int(request.form.get("source_imap_port", 993)),
                username=request.form["source_username"].strip(),
                password=src_pw,
                provider=request.form.get("source_provider", "generic").strip(),
                role="source",
                notes=request.form.get("source_notes", "").strip(),
            )
            # Create destination account
            dest_pw = crypto_service.encrypt(request.form["dest_password"])
            account_model.create_account(
                name=request.form["dest_name"].strip(),
                imap_host=request.form["dest_imap_host"].strip(),
                imap_port=int(request.form.get("dest_imap_port", 993)),
                username=request.form["dest_username"].strip(),
                password=dest_pw,
                provider=request.form.get("dest_provider", "generic").strip(),
                role="destination",
                notes=request.form.get("dest_notes", "").strip(),
            )
            flash("Both accounts created successfully.", "success")
            return redirect(url_for("web.account_list"))
        except Exception as exc:
            flash(f"Error creating accounts: {exc}", "error")

    csrf = _csrf_token()
    return render_template("accounts/quick_setup.html", csrf_token=csrf)


# ---- Jobs ----

@web_bp.route("/jobs")
def job_list():
    status_filter = request.args.get("status", "")
    page = int(request.args.get("page", 1))
    per_page = 20
    offset = (page - 1) * per_page

    if status_filter:
        jobs = job_model.list_jobs(status=status_filter, limit=per_page, offset=offset)
    else:
        jobs = job_model.list_jobs(limit=per_page, offset=offset)

    csrf = _csrf_token()
    return render_template(
        "jobs/list.html",
        jobs=jobs,
        status_filter=status_filter,
        page=page,
        csrf_token=csrf,
    )


@web_bp.route("/jobs/create", methods=["GET", "POST"])
def job_create():
    sources = account_model.list_accounts(role="source")
    dests = account_model.list_accounts(role="destination")

    if request.method == "POST":
        if not _check_csrf():
            return Response("CSRF validation failed", 400)
        try:
            source_id = int(request.form["source_account_id"])
            dest_id = int(request.form["dest_account_id"])

            if source_id == dest_id:
                flash("Source and destination must be different.", "error")
                return render_template(
                    "jobs/create.html",
                    sources=sources, dests=dests,
                    csrf_token=_csrf_token(),
                )

            folders_str = request.form.get("folders", "").strip()
            folders = [f.strip() for f in folders_str.split(",") if f.strip()] if folders_str else None

            extra_args = request.form.get("extra_args", "").strip() or None

            job_id = job_model.create_job(
                source_account_id=source_id,
                dest_account_id=dest_id,
                folders=folders,
                extra_args=extra_args,
            )
            flash(f"Migration job #{job_id} created.", "success")

            # Auto-start if requested
            if request.form.get("start_now"):
                started = job_service.start_job(job_id)
                if started:
                    flash(f"Job #{job_id} started.", "success")
                else:
                    flash(
                        f"Job #{job_id} created but could not start (queue full). "
                        "Start it manually from the jobs list.",
                        "warning",
                    )

            return redirect(url_for("web.job_list"))

        except Exception as exc:
            flash(f"Error creating job: {exc}", "error")

    csrf = _csrf_token()
    return render_template(
        "jobs/create.html",
        sources=sources, dests=dests,
        csrf_token=csrf,
    )


@web_bp.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    job = job_model.get_job(job_id)
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("web.job_list"))

    # Load recent logs (last 200 lines by default)
    log_count = job_model.get_job_log_count(job_id)
    logs = job_model.get_job_logs(job_id, limit=200, offset=max(0, log_count - 200))

    csrf = _csrf_token()
    return render_template(
        "jobs/detail.html",
        job=job,
        logs=logs,
        log_count=log_count,
        csrf_token=csrf,
    )


@web_bp.route("/jobs/<int:job_id>/start", methods=["POST"])
def job_start(job_id: int):
    if not _check_csrf():
        return Response("CSRF validation failed", 400)

    job = job_model.get_job(job_id)
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("web.job_list"))

    if job["status"] == "running":
        flash("Job is already running.", "warning")
        return redirect(url_for("web.job_detail", job_id=job_id))

    success = job_service.start_job(job_id)
    if success:
        flash(f"Job #{job_id} started.", "success")
    else:
        flash("Cannot start job: maximum concurrent migrations reached.", "error")

    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.route("/jobs/<int:job_id>/stop", methods=["POST"])
def job_stop(job_id: int):
    if not _check_csrf():
        return Response("CSRF validation failed", 400)

    success, message = job_service.stop_job(job_id)
    if success:
        flash(message, "success")
    else:
        flash(message, "error")

    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.route("/jobs/<int:job_id>/retry", methods=["POST"])
def job_retry(job_id: int):
    if not _check_csrf():
        return Response("CSRF validation failed", 400)

    new_id = job_service.retry_job(job_id)
    if new_id is None:
        flash("Original job not found.", "error")
        return redirect(url_for("web.job_list"))

    flash(f"New migration job #{new_id} created from job #{job_id}.", "success")
    return redirect(url_for("web.job_detail", job_id=new_id))


@web_bp.route("/jobs/<int:job_id>/delete", methods=["POST"])
def job_delete(job_id: int):
    if not _check_csrf():
        return Response("CSRF validation failed", 400)

    success, message = job_model.delete_job(job_id)
    if success:
        flash(message, "success")
        return redirect(url_for("web.job_list"))
    else:
        flash(message, "error")
        return redirect(url_for("web.job_detail", job_id=job_id))


# ---- API (JSON) ----

@web_bp.route("/api/jobs/<int:job_id>")
@require_auth
def api_job_detail(job_id: int):
    """JSON endpoint for auto-refreshing job detail page."""
    job = job_model.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    # Get logs after a given offset (for polling)
    log_offset = request.args.get("log_offset", 0, type=int)
    new_logs = job_model.get_job_logs(job_id, limit=500, offset=log_offset)

    return jsonify({
        "job": job,
        "new_logs": [dict(l) for l in new_logs],
        "log_offset": log_offset,
        "next_offset": log_offset + len(new_logs),
        "log_total": job_model.get_job_log_count(job_id),
    })


@web_bp.route("/api/stats")
@require_auth
def api_stats():
    """JSON endpoint for dashboard stats."""
    return jsonify(job_service.get_stats())


# ---- Settings ----

@web_bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        if not _check_csrf():
            return Response("CSRF validation failed", 400)
        try:
            for key in ["max_concurrent_jobs", "log_retention_days", "timezone"]:
                value = request.form.get(key, "").strip()
                if value:
                    job_model.set_setting(key, value)

            # Auth settings
            new_username = request.form.get("auth_username", "").strip()
            if new_username:
                job_model.set_setting("auth_username", new_username)

            new_password = request.form.get("auth_password", "").strip()
            if new_password:
                from web.auth import _hash_password
                job_model.set_setting(
                    "auth_password_hash",
                    _hash_password(new_password),
                )

            flash("Settings saved.", "success")
        except Exception as exc:
            flash(f"Error saving settings: {exc}", "error")

    all_settings = job_model.get_all_settings()
    imapsync_ok = imapsync_service.is_imapsync_installed()
    csrf = _csrf_token()

    return render_template(
        "settings/index.html",
        settings=all_settings,
        imapsync_ok=imapsync_ok,
        imapsync_path=imapsync_service.Config.IMAPSYNC_PATH,
        csrf_token=csrf,
    )


# ---- Helpers for templates ----

@web_bp.app_template_filter("mask_password")
def mask_password_filter(password: str) -> str:
    """Show a masked version of an encrypted password."""
    if not password:
        return ""
    if len(password) <= 8:
        return "***"
    return password[:4] + "…" + password[-4:]


@web_bp.app_template_filter("simplify_folder")
def simplify_folder_filter(folders_str: str | None) -> str:
    """Simplify the JSON folders field for display."""
    if not folders_str:
        return "All folders"
    try:
        folders = json.loads(folders_str)
        if not folders:
            return "All folders"
        if len(folders) <= 3:
            return ", ".join(folders)
        return ", ".join(folders[:3]) + f" +{len(folders) - 3} more"
    except (json.JSONDecodeError, TypeError):
        return folders_str
