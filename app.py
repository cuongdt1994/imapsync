"""
IMAPsync Web — A web interface for managing IMAP migrations via imapsync.

Entry point for both development server and Gunicorn.
"""

import os

from flask import Flask

from config import Config
from models.database import close_connection, init_db
from services.log_service import setup_logging
from web.auth import auth_bp
from web.routes import web_bp


def create_app(config: type | None = None) -> Flask:
    """Flask application factory."""
    app = Flask(__name__, template_folder="web/templates")

    if config is None:
        config = Config

    app.config.from_object(config)

    # Initialize database
    init_db(app.config["DATABASE_PATH"])

    # Set up logging
    level = "DEBUG" if app.config.get("DEBUG") else "INFO"
    setup_logging(level)

    # Register blueprints
    app.register_blueprint(web_bp)
    app.register_blueprint(auth_bp)

    # Teardown: close DB connection
    app.teardown_appcontext(lambda exc: close_connection())

    return app


def main():
    """Development server entry point."""
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("DEBUG", "1")

    app = create_app()

    # Set default auth password for first run
    from models.job import get_setting, set_setting
    from web.auth import _hash_password
    if not get_setting("auth_password_hash"):
        pw = app.config.get("AUTH_PASSWORD", "admin")
        set_setting("auth_password_hash", _hash_password(pw))

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))

    print(f"\n  IMAPsync Web starting at http://{host}:{port}")
    print(f"  Default login: admin / {app.config.get('AUTH_PASSWORD', 'see .env')}\n")

    app.run(host=host, port=port, debug=app.config["DEBUG"])


if __name__ == "__main__":
    main()
