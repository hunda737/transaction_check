""" Download PDF from URL and return local path. """
import logging
import os
import tempfile
import time
from urllib.parse import urlparse

import requests
import urllib3

logger = logging.getLogger(__name__)

session = requests.Session()

# (connect, read) seconds. Bank receipt hosts routinely accept the TLS
# handshake and then never answer for an unknown reference, so a read
# timeout is what keeps a bad reference from hanging forever.
DEFAULT_TIMEOUT = (
    float(os.environ.get("ETHIOBANK_CONNECT_TIMEOUT", 10)),
    float(os.environ.get("ETHIOBANK_READ_TIMEOUT", 30)),
)

# Transient failures are retried up to MAX_RETRIES times (so MAX_RETRIES + 1
# attempts in total), sleeping RETRY_BACKOFF * attempt seconds in between.
MAX_RETRIES = int(os.environ.get("ETHIOBANK_MAX_RETRIES", 2))

# Last rate-limit headers seen per host: {host: {"remaining": int, "limit": int,
# "at": monotonic}}. The worker reads this to slow down before the bank starts
# silently dropping requests (telebirr hangs instead of answering 429).
LAST_RATE_LIMIT = {}
RETRY_BACKOFF = float(os.environ.get("ETHIOBANK_RETRY_BACKOFF", 0.5))
RETRY_STATUSES = frozenset({502, 503, 504})


def get_with_retry(url, max_retries=None, backoff=None, **kwargs):
    """``session.get`` with a small retry loop for transient failures.

    Retried: connection errors (including connect timeouts) and 502/503/504.
    Not retried: read timeouts -- these usually mean the bank is sitting on an
    unknown reference, and retrying would just triple the wait -- and 4xx.

    After the last attempt the final response is returned (or the final
    exception raised) unchanged, so callers keep their own error mapping.
    """
    max_retries = MAX_RETRIES if max_retries is None else max_retries
    backoff = RETRY_BACKOFF if backoff is None else backoff
    host = urlparse(url).hostname

    attempt = 0
    while True:
        started = time.monotonic()
        try:
            response = session.get(url, **kwargs)
        except requests.ReadTimeout as exc:
            logger.warning("%s attempt %d: ReadTimeout after %.1fs (not retried): %s",
                           host, attempt + 1, time.monotonic() - started, exc)
            raise
        except requests.ConnectionError as exc:
            logger.warning("%s attempt %d: %s after %.1fs: %s",
                           host, attempt + 1, type(exc).__name__,
                           time.monotonic() - started, exc)
            if attempt >= max_retries:
                raise
            reason = type(exc).__name__
        except requests.RequestException as exc:
            logger.warning("%s attempt %d: %s after %.1fs: %s",
                           host, attempt + 1, type(exc).__name__,
                           time.monotonic() - started, exc)
            raise
        else:
            _record_rate_limit(host, response)
            logger.info("%s attempt %d: HTTP %d in %.1fs%s",
                        host, attempt + 1, response.status_code,
                        time.monotonic() - started, _rate_limit_info(response))
            if response.status_code not in RETRY_STATUSES or attempt >= max_retries:
                return response
            reason = f"HTTP {response.status_code}"

        attempt += 1
        logger.warning("Retrying %s (%d/%d) after %s", host, attempt, max_retries, reason)
        time.sleep(backoff * attempt)


def _record_rate_limit(host, response):
    headers = getattr(response, "headers", None) or {}
    try:
        remaining = int(headers["X-RateLimit-Remaining"])
    except (KeyError, TypeError, ValueError):
        return
    try:
        limit = int(headers.get("X-RateLimit-Limit"))
    except (TypeError, ValueError):
        limit = None
    LAST_RATE_LIMIT[host] = {"remaining": remaining, "limit": limit,
                             "at": time.monotonic()}


def _rate_limit_info(response):
    """Summarize any rate-limit headers the bank sent, for the log line."""
    headers = getattr(response, "headers", None) or {}
    parts = [f"{name}={headers[name]}" for name in
             ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")
             if name in headers]
    return f" [{', '.join(parts)}]" if parts else ""


def download_pdf_from_url(url, verify_ssl=False, timeout=DEFAULT_TIMEOUT):
    """Downloads a PDF from a URL and saves it to a temp file with connection pooling."""
    if not verify_ssl:
        # Skipping verification is deliberate here (CBE serves an untrusted
        # cert), so don't spam the caller with a warning about it.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = get_with_retry(url, verify=verify_ssl, timeout=timeout)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
        tmp_file.write(response.content)
        return tmp_file.name


def fetch_json(url, headers=None, verify_ssl=False, timeout=DEFAULT_TIMEOUT):
    """GET a URL and return the parsed JSON body, sharing the pooled session.

    Used by banks that serve receipt data as JSON rather than a PDF.
    """
    if not verify_ssl:
        # Same untrusted-cert situation as the PDF hosts.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = get_with_retry(url, headers=headers, verify=verify_ssl, timeout=timeout)
    response.raise_for_status()
    return response.json()
