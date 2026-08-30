#!/usr/bin/env bash
# Generates and installs the systemd unit with this checkout's real paths,
# the invoking user, and their primary group. Run from anywhere:
#   ./deploy/install-service.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"
PORT="${PORT:-8060}"
UNIT=/etc/systemd/system/ethiobank-receipts.service

if [ ! -x "$APP_DIR/.venv/bin/uvicorn" ]; then
    echo "ERROR: $APP_DIR/.venv/bin/uvicorn not found." >&2
    echo "Create the venv first:  python3 -m venv .venv && .venv/bin/pip install -e '.[api]'" >&2
    exit 1
fi

if [ ! -f "$APP_DIR/.env" ]; then
    echo "ERROR: $APP_DIR/.env not found -- the service would return 503 to everything." >&2
    echo "Create it with ETHIOBANK_API_KEYS=... (see DEPLOY.md step 3)." >&2
    exit 1
fi

echo "Installing service:"
echo "  user  : $RUN_USER:$RUN_GROUP"
echo "  dir   : $APP_DIR"
echo "  port  : $PORT"

sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=Ethiopian Bank Receipt Service
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env

ExecStart=$APP_DIR/.venv/bin/uvicorn ethiobank_receipts.api:app \
    --host 127.0.0.1 --port $PORT --workers 4 --proxy-headers \
    --forwarded-allow-ips 127.0.0.1

Restart=always
RestartSec=5
TimeoutStopSec=45

# ProtectHome must stay false: the code lives under /home and ProtectHome=true
# would hide it from the service.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=false
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNITEOF

sudo systemctl daemon-reload
sudo systemctl enable --now ethiobank-receipts
sleep 2
sudo systemctl status ethiobank-receipts --no-pager || true
echo
echo "Health check:"
curl -s "http://127.0.0.1:$PORT/health" || echo "  (no response yet -- check: journalctl -u ethiobank-receipts -n 50)"
echo
