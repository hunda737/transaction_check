# Deploying on Ubuntu

Runs as **your existing login user** — no service account needed. Port **8060**.
Replace `YOUR-USER/YOUR-REPO` with your repo.

> **The server must be in Ethiopia.** telebirr blocks or times out requests from
> foreign IPs, and CBE is unreachable from many networks. Verify with step 5
> before wiring this into anything.

## 1. Packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git nginx
```

## 2. Clone and install

```bash
cd ~
git clone https://github.com/YOUR-USER/YOUR-REPO.git transaction_check
cd transaction_check
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[api]"
```

## 3. API keys

Generate one key per calling application:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Store them in `.env` beside the code — already covered by `.gitignore`, so it
will never be committed:

```bash
cat > ~/transaction_check/.env <<'ENV'
ETHIOBANK_API_KEYS=key-for-deposit-app,key-for-admin-portal
ETHIOBANK_CONNECT_TIMEOUT=10
ETHIOBANK_READ_TIMEOUT=30
ENV
chmod 600 ~/transaction_check/.env
```

With no keys set the service fails closed and returns `503` to everything.

## 4. systemd

The installer detects your checkout path, your user, and your primary group, so
it works wherever you cloned:

```bash
./deploy/install-service.sh
```

Override the port with `PORT=9000 ./deploy/install-service.sh`. It refuses to
install if the venv or `.env` is missing, rather than leaving you with a service
that returns 503 to everything.

Logs: `journalctl -u ethiobank-receipts -f`

> The unit sets `ProtectHome=false` on purpose. The other hardening flags stay
> on, but `ProtectHome=true` would hide `/home` from the service and it could
> not start from a home directory.

`deploy/ethiobank-receipts.service` is the template it renders; edit that only if
you need options the script doesn't set.

## 5. Verify before exposing it

```bash
curl -s localhost:8060/health
# {"status":"ok","api_keys_configured":true}

curl -s -H "X-API-Key: key-for-deposit-app" \
  localhost:8060/receipts/tele/SOME_REAL_REFERENCE
```

A `502` here means the bank host is unreachable **from this server** — usually
geography or a firewall, not a bug. Test each bank you actually need before
going live.

## 6. nginx + TLS

```bash
sudo cp ~/transaction_check/deploy/nginx-ethiobank-receipts.conf \
  /etc/nginx/sites-available/ethiobank-receipts
sudo ln -s /etc/nginx/sites-available/ethiobank-receipts /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d receipts.internal.example.com
```

## 7. Firewall

The service binds to `127.0.0.1:8060`, so only nginx can reach it. Keep it that way.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

Do **not** open 8060 itself. Anyone who can reach the service directly can harvest
receipt data — real names, amounts, account suffixes — using your server's IP until
the banks block it.

## Updating

```bash
cd ~/transaction_check
git pull
.venv/bin/pip install -e ".[api]"
sudo systemctl restart ethiobank-receipts
```

## Running it by hand (development)

```bash
cd ~/transaction_check
set -a && source .env && set +a
.venv/bin/uvicorn ethiobank_receipts.api:app --port 8060
```

## Optional: Bank of Abyssinia

BOA is the only bank needing a real browser. Skip unless you use it.

```bash
sudo apt install -y chromium-browser chromium-chromedriver
```

Its WebDriver is serialized behind a lock, so BOA handles one request at a time
per worker and is memory-hungry.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `503` on every request | `ETHIOBANK_API_KEYS` not set — check `.env` and `EnvironmentFile` |
| `401` | Wrong or missing `X-API-Key` header |
| `502` for one bank | That bank is unreachable from this server |
| `500` | The bank changed their page layout — the parser needs updating |
| `uvicorn: not found` | Use the venv path: `.venv/bin/uvicorn` |
| `status=exited` right after start | Check `journalctl -u ethiobank-receipts -n 50`; usually a wrong path in the unit |
| `200 OK` but connection refused via nginx | nginx still points at 8000 — check `proxy_pass` |
