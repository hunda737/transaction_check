"""Signed result callbacks to the calling application.

Callback URLs are configured per API key on the server, never taken from a
request, so a caller cannot point this service at an arbitrary host:

    ETHIOBANK_WEBHOOKS=<api-key>|<https://callback-url>|<signing-secret>;...

Each delivery is a JSON POST with:
    X-Ethiobank-Timestamp: <unix seconds>
    X-Ethiobank-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>
"""
import hashlib
import hmac
import json
import logging
import os
import time

import requests

from ethiobank_receipts.store import api_key_id

logger = logging.getLogger(__name__)

# Delays (seconds) before each re-delivery after a failed attempt.
RETRY_DELAYS = (10, 60, 300, 1800)
TIMEOUT = (5, 10)


def load_webhooks(environ=None):
    """Return {api_key_id: (url, secret)} from ETHIOBANK_WEBHOOKS."""
    environ = os.environ if environ is None else environ
    hooks = {}
    for entry in environ.get("ETHIOBANK_WEBHOOKS", "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            logger.error("Ignoring malformed ETHIOBANK_WEBHOOKS entry (want key|url|secret)")
            continue
        key, url, secret = (p.strip() for p in parts)
        if not url.lower().startswith(("https://", "http://")):
            logger.error("Ignoring webhook with non-HTTP URL %r", url)
            continue
        hooks[api_key_id(key)] = (url, secret)
    return hooks


def sign(secret, timestamp, body):
    message = f"{timestamp}.".encode("utf-8") + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_payload(public_job):
    return json.dumps({"success": True, "event": "verification.finished",
                       "verification": public_job}, separators=(",", ":")).encode("utf-8")


def deliver(url, secret, body, post=requests.post, now=time.time):
    """POST one webhook. Returns ``(delivered, error)``."""
    timestamp = str(int(now()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ethiobank-receipts-webhook/1",
        "X-Ethiobank-Timestamp": timestamp,
        "X-Ethiobank-Signature": sign(secret, timestamp, body),
    }
    try:
        response = post(url, data=body, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if 200 <= response.status_code < 300:
        return True, None
    return False, f"HTTP {response.status_code}"
