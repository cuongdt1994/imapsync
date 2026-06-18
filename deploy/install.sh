#!/usr/bin/env bash
#
# install.sh — Deploy IMAPsync Web on Debian 12 / Ubuntu 24.04
#
# Usage: sudo bash install.sh
#
# This script:
# 1. Installs system dependencies (imapsync, python3-venv, etc.)
# 2. Creates a system user
# 3. Sets up the application in /opt/imapsync-web
# 4. Creates a Python virtualenv and installs packages
# 5. Generates a Fernet key
# 6. Installs and enables a systemd service
#

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────
APP_DIR="/opt/imapsync-web"
APP_USER="imapsync"
APP_GROUP="imapsync"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
BIND_PORT="${BIND_PORT:-5000}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*"; exit 1; }

# ── Pre-flight check ───────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    err "This script must be run as root (sudo)."
fi

if [ -f /etc/os-release ]; then
    . /etc/os-release
    log "Detected OS: $NAME $VERSION_ID"
elif [ ! -f /etc/debian_version ]; then
    warn "This script is designed for Debian/Ubuntu. Proceeding anyway..."
fi

# ── System packages ────────────────────────────────────────────
log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    curl \
    wget \
    rsync \
    libauthen-ntlm-perl \
    libcgi-pm-perl \
    libcrypt-openssl-rsa-perl \
    libdata-uniqid-perl \
    libencode-imaputf7-perl \
    libfile-copy-recursive-perl \
    libfile-tail-perl \
    libio-socket-inet6-perl \
    libio-socket-ssl-perl \
    libio-tee-perl \
    libhtml-parser-perl \
    libjson-webtoken-perl \
    libmail-imapclient-perl \
    libparse-recdescent-perl \
    libreadonly-perl \
    libregexp-common-perl \
    libsys-meminfo-perl \
    libunicode-string-perl \
    liburi-perl \
    libwww-perl \
    make

# ── Install imapsync (not in Debian repos) ─────────────────────
IMAPSYNC_BIN="/usr/local/bin/imapsync"
if [ ! -f "$IMAPSYNC_BIN" ]; then
    log "Downloading imapsync from upstream..."
    wget -q -O "$IMAPSYNC_BIN" \
        "https://imapsync.lamiral.info/dist/imapsync"
    chmod +x "$IMAPSYNC_BIN"
fi

# Verify imapsync works
if "$IMAPSYNC_BIN" --version &>/dev/null; then
    log "imapsync installed: $($IMAPSYNC_BIN --version 2>&1 | head -1)"
else
    warn "imapsync still has issues. Check: perl -c $IMAPSYNC_BIN"
    warn "You may need: apt install libpar-packer-perl libmodule-scandeps-perl"
fi

# ── Create system user ─────────────────────────────────────────
if ! id -u "$APP_USER" &>/dev/null; then
    log "Creating system user: $APP_USER"
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
else
    log "User $APP_USER already exists."
fi

# ── Application directory ──────────────────────────────────────
log "Setting up application directory: $APP_DIR"
mkdir -p "$APP_DIR"

# If we're running from the repo directory, copy files
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$PROJECT_DIR/app.py" ]; then
    log "Copying project files from $PROJECT_DIR to $APP_DIR"
    rsync -a --exclude '__pycache__' --exclude '*.pyc' \
          --exclude 'data/' --exclude 'logs/' --exclude '.env' \
          "$PROJECT_DIR/" "$APP_DIR/"
else
    warn "Project files not found at $PROJECT_DIR."
    warn "Place the project files in $APP_DIR manually."
fi

# ── Virtualenv ─────────────────────────────────────────────────
log "Creating Python virtualenv..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# ── Directories ────────────────────────────────────────────────
log "Creating data and log directories..."
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chmod 700 "$APP_DIR/data"

# ── Fernet key ─────────────────────────────────────────────────
if [ ! -f "$APP_DIR/data/fernet.key" ]; then
    log "Generating Fernet encryption key..."
    "$APP_DIR/venv/bin/python" -c "
from cryptography.fernet import Fernet
from pathlib import Path
key = Fernet.generate_key()
key_file = Path('$APP_DIR/data/fernet.key')
key_file.write_bytes(key)
key_file.chmod(0o600)
print(f'Fernet key saved to {key_file}')
"
    chown "$APP_USER:$APP_GROUP" "$APP_DIR/data/fernet.key"
fi

# ── Environment file ───────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    log "Creating .env file..."
    ADMIN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
    FERNET_KEY=$("$APP_DIR/venv/bin/python" -c "
from pathlib import Path
print(Path('$APP_DIR/data/fernet.key').read_text().strip())
")

    cat > "$APP_DIR/.env" <<EOF
# IMAPsync Web configuration
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
FERNET_KEY=${FERNET_KEY}
AUTH_USERNAME=admin
AUTH_PASSWORD=${ADMIN_PASSWORD}
MAX_CONCURRENT_JOBS=3
LOG_RETENTION_DAYS=30
TIMEZONE=Asia/Ho_Chi_Minh
EOF
    chown "$APP_USER:$APP_GROUP" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"

    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  IMPORTANT: Save these credentials!                     ║"
    echo "  ║                                                        ║"
    echo "  ║  Admin login:  admin / ${ADMIN_PASSWORD}"
    echo "  ║                                                        ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
fi

# ── systemd service ────────────────────────────────────────────
log "Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/imapsync-web.service"

if [ -f "$APP_DIR/deploy/imapsync-web.service" ]; then
    cp "$APP_DIR/deploy/imapsync-web.service" "$SERVICE_FILE"
else
    cat > "$SERVICE_FILE" <<SYSTEMD
[Unit]
Description=IMAPsync Web
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/gunicorn \\
    --workers ${GUNICORN_WORKERS} \\
    --bind ${BIND_HOST}:${BIND_PORT} \\
    --access-logfile ${APP_DIR}/logs/gunicorn-access.log \\
    --error-logfile ${APP_DIR}/logs/gunicorn-error.log \\
    --capture-output \\
    --log-level info \\
    app:create_app\(\)
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD
fi

systemctl daemon-reload
systemctl enable imapsync-web.service

# ── Start the service ──────────────────────────────────────────
log "Starting IMAPsync Web..."
systemctl start imapsync-web.service
sleep 2

if systemctl is-active --quiet imapsync-web.service; then
    log "IMAPsync Web is running!"
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  IMAPsync Web is ready!                                 ║"
    echo "  ║                                                        ║"
    echo "  ║  URL:  http://$(hostname -I | awk '{print $1}'):${BIND_PORT}"
    echo "  ║                                                        ║"
    echo "  ║  Manage:  systemctl [start|stop|restart] imapsync-web  ║"
    echo "  ║  Logs:    journalctl -u imapsync-web -f                ║"
    echo "  ║  Config:  ${APP_DIR}/.env                  ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
else
    warn "Service did not start. Check logs: journalctl -u imapsync-web -n 50"
fi
