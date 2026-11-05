"""Per-bank token buckets for the worker.

telebirr allows ~5 lookups per rolling window and refills roughly one every
30-40s. Over the limit it does not answer 429 -- it just never responds -- so the
only way to avoid failures is to never exceed it. Only the single worker process
calls banks, so these buckets can live in memory.

Configure with ``ETHIOBANK_RATE_<BANK>=<capacity>/<seconds per token>``, e.g.
``ETHIOBANK_RATE_TELE=4/40``.
"""
import logging
import os
import threading

from ethiobank_receipts.banks import BANKS

logger = logging.getLogger(__name__)

# capacity, seconds per token. tele keeps one of its 5 slots in reserve.
DEFAULT_RATES = {"tele": (4, 40.0)}
FALLBACK_RATE = (10, 5.0)


def load_rates(environ=None):
    """Return {bank: (capacity, interval_seconds)} from defaults + environment."""
    environ = os.environ if environ is None else environ
    rates = {}
    for bank in BANKS:
        capacity, interval = DEFAULT_RATES.get(bank, FALLBACK_RATE)
        raw = environ.get(f"ETHIOBANK_RATE_{bank.upper()}")
        if raw:
            try:
                cap_s, int_s = raw.split("/", 1)
                capacity, interval = max(1, int(cap_s)), max(0.1, float(int_s))
            except ValueError:
                logger.error("Ignoring malformed ETHIOBANK_RATE_%s=%r (want N/SECONDS)",
                             bank.upper(), raw)
        rates[bank] = (capacity, interval)
    return rates


class _Bucket:
    def __init__(self, capacity, interval, now):
        self.capacity = capacity
        self.interval = interval
        self.tokens = float(capacity)
        self.updated = now
        self.paused_until = 0.0

    def refill(self, now):
        if now > self.updated:
            self.tokens = min(self.capacity,
                              self.tokens + (now - self.updated) / self.interval)
            self.updated = now


class RateLimiter:
    def __init__(self, rates, now):
        self._lock = threading.Lock()
        self._buckets = {bank: _Bucket(cap, interval, now)
                         for bank, (cap, interval) in rates.items()}

    def try_acquire(self, bank, now):
        """Take one token for ``bank`` if one is available and it isn't paused."""
        with self._lock:
            bucket = self._buckets[bank]
            bucket.refill(now)
            if now < bucket.paused_until or bucket.tokens < 1:
                return False
            bucket.tokens -= 1
            return True

    def refund(self, bank):
        """Give back a token that was acquired but not used."""
        with self._lock:
            bucket = self._buckets[bank]
            bucket.tokens = min(bucket.capacity, bucket.tokens + 1)

    def pause(self, bank, seconds, now, reason=""):
        """Stop sending to ``bank`` for ``seconds`` and drain its burst allowance."""
        with self._lock:
            bucket = self._buckets[bank]
            bucket.refill(now)
            bucket.tokens = 0.0
            bucket.paused_until = max(bucket.paused_until, now + seconds)
        logger.warning("Pausing %s for %.0fs%s", bank, seconds,
                       f": {reason}" if reason else "")

    def interval(self, bank):
        return self._buckets[bank].interval
