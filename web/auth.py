"""
HTTP Basic Auth for the web interface.
Simple, no session — just check credentials on every request.
"""

import functools
import hashlib

from flask import Blueprint, Response, current_app, request

auth_bp = Blueprint("auth", __name__)


def check_credentials(username: str, password: str) -> bool:
    """Verify username and password against configured values."""
    from models.job import get_setting

    cfg_user = get_setting("auth_username") or "admin"
    cfg_pass_hash = get_setting("auth_password_hash")

    if not cfg_pass_hash:
        # If no hash is set, use the env var password directly
        cfg_pass_hash = _hash_password(
            current_app.config.get("AUTH_PASSWORD", "admin")
        )

    if username != cfg_user:
        return False

    return _hash_password(password) == cfg_pass_hash


def _hash_password(password: str) -> str:
    """SHA-256 hash of password (adequate for internal tool)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def require_auth(view_func):
    """Decorator that requires HTTP Basic Auth on a view."""

    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_credentials(auth.username, auth.password):
            return Response(
                "Access denied. Please log in.",
                401,
                {"WWW-Authenticate": 'Basic realm="IMAPsync Web"'},
            )
        return view_func(*args, **kwargs)

    return wrapped
