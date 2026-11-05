#!/usr/bin/env bash
# Generates and installs the systemd units (API + bank worker) with this
# checkout's real paths, the invoking user, and their primary group. Run from
# anywhere:
#   ./deploy/install-service.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"
RUN_GROUP="$(id -gn "$RUN_USER")"
PORT="${PORT:-8060}"
DATA_DIR="$APP_DIR/data"
UNIT=/etc/systemd/system/ethiobank-receipts.service
WORKER_UNIT=/etc/systemd/system/ethiobank-receipts-worker.service

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

# The job queue and receipt cache live here; it must be writable by the
# service user and is the only writable path under ProtectSystem=strict.
mkdir -p "$DATA_DIR"
chown "$RUN_USER:$RUN_GROUP" "$DATA_DIR" 2>/dev/null || sudo chown "$RUN_USER:$RUN_GROUP" "$DATA_DIR"
chmod 700 "$DATA_DIR"

echo "Installing services:"
echo "  user  : $RUN_USER:$RUN_GROUP"
echo "  dir   : $APP_DIR"
echo "  data  : $DATA_DIR"
echo "  port  : $PORT"

HARDENING="NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
# ProtectHome must stay false: the code lives under /home and ProtectHome=true
# would hide it from the service.
ProtectHome=false
ReadWritePaths=$DATA_DIR
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true"

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
Environment=ETHIOBANK_DB_PATH=$DATA_DIR/ethiobank.db

ExecStart=$APP_DIR/.venv/bin/uvicorn ethiobank_receipts.api:app \
    --host 127.0.0.1 --port $PORT --workers 4 --proxy-headers \
    --forwarded-allow-ips 127.0.0.1

Restart=always
RestartSec=5
TimeoutStopSec=45

$HARDENING

[Install]
WantedBy=multi-user.target
UNITEOF

# Exactly one worker: the per-bank rate limiter lives in this process.
sudo tee "$WORKER_UNIT" >/dev/null <<UNITEOF
[Unit]
Description=Ethiopian Bank Receipt Service (bank worker)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=ETHIOBANK_DB_PATH=$DATA_DIR/ethiobank.db

ExecStart=$APP_DIR/.venv/bin/python -m ethiobank_receipts.worker

Restart=always
RestartSec=5
TimeoutStopSec=60

$HARDENING

[Install]
WantedBy=multi-user.target
UNITEOF

sudo systemctl daemon-reload
sudo systemctl enable --now ethiobank-receipts ethiobank-receipts-worker
sleep 2
sudo systemctl status ethiobank-receipts ethiobank-receipts-worker --no-pager || true
echo
echo "Health check:"
curl -s "http://127.0.0.1:$PORT/health" || echo "  (no response yet -- check: journalctl -u ethiobank-receipts -n 50)"
echo
