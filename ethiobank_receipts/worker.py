"""The only process that calls banks: drains the verification queue.

Run exactly one instance next to the API:

    python -m ethiobank_receipts.worker

Each bank is called one request at a time and no faster than its token bucket
allows (see ``ratelimit.py``). A bank that is slow or unreachable is retried with
backoff; a job only becomes ``needs_review`` once the bank has kept failing for
``ETHIOBANK_REVIEW_DEADLINE`` seconds (default 600) from its first attempt, so a
long rush-hour queue alone never sends deposits to humans.
"""
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ethiobank_receipts import extract_receipt
from ethiobank_receipts.banks import BANKS
from ethiobank_receipts.download import LAST_RATE_LIMIT
from ethiobank_receipts.errors import (
    BankUnavailable,
    InvalidReference,
    ReceiptNotFound,
    ReceiptParseError,
)
from ethiobank_receipts.ratelimit import RateLimiter, load_rates
from ethiobank_receipts.schema import normalize
from ethiobank_receipts.store import Store
from ethiobank_receipts import webhooks

logger = logging.getLogger(__name__)

REVIEW_DEADLINE = float(os.environ.get("ETHIOBANK_REVIEW_DEADLINE", 600))
# A reference the bank says doesn't exist is checked this many times, this far
# apart, before it is reported not_found (covers a payment not yet visible).
NOT_FOUND_CHECKS = 2
NOT_FOUND_RECHECK = 60.0
BACKOFF_BASE, BACKOFF_CAP = 30.0, 120.0
# Seconds to pause a whole bank after a read timeout. Off by default: the
# observed telebirr hangs are per reference, so pausing the bank only delays
# every other deposit. Set >0 if a bank turns out to throttle per IP.
TIMEOUT_PAUSE = float(os.environ.get("ETHIOBANK_TIMEOUT_PAUSE", 0))


def _caused_by_read_timeout(exc):
    while exc is not None:
        if isinstance(exc, requests.ReadTimeout):
            return True
        exc = exc.__cause__
    return False


class Worker:
    def __init__(self, store, clock=time.time, extract=extract_receipt, rates=None,
                 hooks=None, post=requests.post, deadline=REVIEW_DEADLINE, threads=True):
        self.store = store
        self.clock = clock
        self.extract = extract
        self.limiter = RateLimiter(rates or load_rates(), clock())
        self.hooks = webhooks.load_webhooks() if hooks is None else hooks
        self.post = post
        self.deadline = deadline
        self._busy = set()
        self._busy_lock = threading.Lock()
        self._sending = set()
        self._pool = ThreadPoolExecutor(max_workers=len(BANKS) + 4) if threads else None

    # --- scheduling ---------------------------------------------------------

    def _run(self, fn, *args):
        if self._pool is None:
            fn(*args)
        else:
            self._pool.submit(fn, *args)

    def tick(self):
        """Start whatever work is due right now. Safe to call in a tight loop."""
        now = self.clock()
        for bank in self.store.due_banks(now):
            if bank not in BANKS:
                continue
            with self._busy_lock:
                if bank in self._busy:
                    continue
                if not self.limiter.try_acquire(bank, now):
                    continue
                job = self.store.claim_next(bank, now)
                if job is None:
                    self.limiter.refund(bank)
                    continue
                self._busy.add(bank)
            self._run(self._process, job)

        for job in self.store.claim_webhooks(now):
            self._run(self._send_webhook, job)

    def run_forever(self, stop, poll_interval=0.5):
        jobs, hooks = self.store.recover_stale()
        if jobs or hooks:
            logger.warning("Recovered %d in-flight job(s) and %d webhook(s) from last run",
                           jobs, hooks)
        logger.info("Worker started; webhooks configured for %d API key(s)", len(self.hooks))
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Worker tick failed")
            stop.wait(poll_interval)
        if self._pool is not None:
            logger.info("Stopping: waiting for in-flight bank calls")
            self._pool.shutdown(wait=True)

    # --- one bank lookup ----------------------------------------------------

    def _process(self, job):
        bank, ref = job["bank"], job["reference"]
        mono_started = time.monotonic()
        try:
            try:
                raw = self.extract(bank, ref)
                receipt = normalize(bank, raw, reference=ref)
            except ReceiptNotFound as exc:
                self._not_found(job, exc)
            except ReceiptParseError as exc:
                logger.error("Parse failure %s/%s: %s", bank, ref, exc)
                self._finish(job, "needs_review", reason=f"Receipt layout not recognized: {exc}")
            except InvalidReference as exc:
                self._finish(job, "needs_review", reason=f"Invalid reference: {exc}")
            except Exception as exc:  # BankUnavailable, network errors, anything else
                if not isinstance(exc, (BankUnavailable, requests.RequestException)):
                    logger.exception("Unexpected error checking %s/%s", bank, ref)
                # A read timeout only backs off this job, not the whole bank:
                # telebirr has been seen hanging on one (repeatedly looked-up)
                # reference while answering others normally at Remaining=0.
                if _caused_by_read_timeout(exc) and TIMEOUT_PAUSE > 0:
                    self.limiter.pause(bank, TIMEOUT_PAUSE, self.clock(),
                                       "no answer (ETHIOBANK_TIMEOUT_PAUSE)")
                self._retry_later(job, exc, self.clock())
            else:
                self.store.put_cached(bank, [ref, receipt.get("reference")], receipt)
                self._finish(job, "completed", result=receipt)
                logger.info("Verified %s/%s (job %s, attempt %d)",
                            bank, ref, job["id"], job["attempts"])
            self._adapt_to_headers(bank, mono_started)
        finally:
            with self._busy_lock:
                self._busy.discard(bank)

    def _finish(self, job, status, result=None, reason=None):
        self.store.finish(job["id"], status, result=result, reason=reason,
                          notify=job["api_key_id"] in self.hooks)

    def _not_found(self, job, exc):
        seen = job["not_found_count"] + 1
        if seen >= NOT_FOUND_CHECKS:
            logger.info("Not found %s/%s after %d checks", job["bank"], job["reference"], seen)
            self._finish(job, "not_found", reason=str(exc))
        else:
            self.store.requeue(job["id"], self.clock() + NOT_FOUND_RECHECK,
                               f"Not found yet, rechecking: {exc}", not_found_count=seen)

    def _retry_later(self, job, exc, now):
        reason = f"{type(exc).__name__}: {exc}"
        failing_for = now - (job["first_attempt_at"] or now)
        if failing_for >= self.deadline:
            logger.warning("Giving up on %s/%s after %.0fs of failures: %s",
                           job["bank"], job["reference"], failing_for, reason)
            self._finish(job, "needs_review",
                         reason=f"Bank unavailable for {failing_for:.0f}s: {reason}")
            return
        failures = max(1, job["attempts"] - job["not_found_count"])
        delay = min(BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_CAP)
        logger.warning("Retrying %s/%s in %.0fs (attempt %d failed: %s)",
                       job["bank"], job["reference"], delay, job["attempts"], reason)
        self.store.requeue(job["id"], now + delay, reason)

    def _adapt_to_headers(self, bank, since):
        info = LAST_RATE_LIMIT.get(BANKS[bank].host)
        if info and info["at"] >= since and info["remaining"] <= 1:
            self.limiter.pause(bank, self.limiter.interval(bank), self.clock(),
                               f"bank reports {info['remaining']} request(s) remaining")

    # --- webhooks -----------------------------------------------------------

    def _send_webhook(self, job):
        hook = self.hooks.get(job["api_key_id"])
        if hook is None:
            self.store.webhook_done(job["id"], delivered=False, error="no webhook configured")
            return
        url, secret = hook
        body = webhooks.build_payload(Store.public(job))
        delivered, error = webhooks.deliver(url, secret, body, post=self.post, now=self.clock)
        attempt = job["webhook_attempts"] + 1
        if delivered:
            logger.info("Webhook delivered for job %s", job["id"])
            self.store.webhook_done(job["id"], delivered=True)
            return
        if attempt <= len(webhooks.RETRY_DELAYS):
            delay = webhooks.RETRY_DELAYS[attempt - 1]
            logger.warning("Webhook for job %s failed (%s); retry in %ds", job["id"], error, delay)
            self.store.webhook_done(job["id"], delivered=False,
                                    next_at=self.clock() + delay, error=error)
        else:
            logger.error("Webhook for job %s failed permanently: %s", job["id"], error)
            self.store.webhook_done(job["id"], delivered=False, error=error)


def main():
    from ethiobank_receipts.logconfig import configure_logging
    configure_logging()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    Worker(Store()).run_forever(stop)


if __name__ == "__main__":
    main()
