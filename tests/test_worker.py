"""Worker state machine, rate limiting and webhooks. Banks are stubbed."""
import json

import requests

from ethiobank_receipts import webhooks
from ethiobank_receipts.download import LAST_RATE_LIMIT
from ethiobank_receipts.errors import BankUnavailable, ReceiptNotFound, ReceiptParseError
from ethiobank_receipts.ratelimit import RateLimiter, load_rates
from ethiobank_receipts.store import Store, api_key_id
from ethiobank_receipts.worker import Worker

KEY = api_key_id("test-key")
HOOKS = {KEY: ("https://hook.test/cb", "s3cret")}


def tele_raw(ref):
    return {"invoice_no": ref, "settled_amount": "2,000 Birr", "status": "Completed",
            "payment_date": "28-08-2026 16:39:13"}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Bank:
    """Stub extract_receipt that plays back scripted outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, bank, ref):
        self.calls.append(ref)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "ok":
            return tele_raw(ref)
        raise outcome


def setup(tmp_path, bank, hooks=None, post=None, rates=None):
    clock = Clock()
    store = Store(str(tmp_path / "w.db"), clock=clock)
    worker = Worker(store, clock=clock, extract=bank, hooks=hooks or {},
                    post=post or (lambda *a, **k: None),
                    rates=rates or {b: (100, 1.0) for b in load_rates()}, threads=False)
    return clock, store, worker


def submit(store, ref="DHS48P7HGQ"):
    job, _ = store.submit(KEY, "tele", ref)
    return job["id"]


def test_success_completes_and_caches(tmp_path):
    clock, store, worker = setup(tmp_path, Bank("ok"))
    job_id = submit(store)
    worker.tick()
    job = store.get_job(job_id)
    assert job["status"] == "completed"
    assert Store.public(job)["receipt"]["amount"] == "2000.00"
    assert store.get_cached("tele", "DHS48P7HGQ")["amount"] == "2000.00"


def test_not_found_is_rechecked_once_then_final(tmp_path):
    bank = Bank(ReceiptNotFound("no"), ReceiptNotFound("no"))
    clock, store, worker = setup(tmp_path, bank)
    job_id = submit(store)
    worker.tick()
    assert store.get_job(job_id)["status"] == "queued"
    worker.tick()  # not due yet
    assert len(bank.calls) == 1
    clock.t += 60
    worker.tick()
    assert store.get_job(job_id)["status"] == "not_found"
    assert len(bank.calls) == 2


def test_not_found_then_appears(tmp_path):
    clock, store, worker = setup(tmp_path, Bank(ReceiptNotFound("no"), "ok"))
    job_id = submit(store)
    worker.tick()
    clock.t += 60
    worker.tick()
    assert store.get_job(job_id)["status"] == "completed"


def test_parse_error_goes_straight_to_review(tmp_path):
    clock, store, worker = setup(tmp_path, Bank(ReceiptParseError("layout")))
    job_id = submit(store)
    worker.tick()
    job = store.get_job(job_id)
    assert job["status"] == "needs_review" and "layout" in job["reason"]


def test_bank_failures_retry_with_backoff_until_deadline(tmp_path):
    bank = Bank(*[BankUnavailable("down")] * 50)
    clock, store, worker = setup(tmp_path, bank)
    job_id = submit(store)
    worker.tick()
    job = store.get_job(job_id)
    assert job["status"] == "queued"
    assert job["next_attempt_at"] == clock.t + 30  # first backoff

    delays = []
    while store.get_job(job_id)["status"] == "queued":
        before = clock.t
        clock.t = store.get_job(job_id)["next_attempt_at"]
        delays.append(clock.t - before)
        worker.tick()
    job = store.get_job(job_id)
    assert job["status"] == "needs_review"
    assert "Bank unavailable for" in job["reason"]
    assert delays[:3] == [30, 60, 120] and max(delays) == 120
    assert clock.t - job["first_attempt_at"] >= 600


def test_deadline_counts_from_first_attempt_not_queueing(tmp_path):
    bank = Bank(BankUnavailable("down"), "ok")
    clock, store, worker = setup(tmp_path, bank)
    job_id = submit(store)
    clock.t += 3600  # sat in a long rush-hour queue
    worker.tick()
    assert store.get_job(job_id)["status"] == "queued"  # retried, not sent to a human
    clock.t += 30
    worker.tick()
    assert store.get_job(job_id)["status"] == "completed"


def _read_timeout():
    exc = BankUnavailable("no answer")
    exc.__cause__ = requests.ReadTimeout("read timed out")
    return exc


def test_read_timeout_backs_off_only_that_job(tmp_path):
    bank = Bank(_read_timeout())
    clock, store, worker = setup(tmp_path, bank)
    a = submit(store, "AAAAAAAA1")
    b = submit(store, "BBBBBBBB1")
    worker.tick()  # A hangs
    worker.tick()  # B goes straight through; the bank is not paused
    assert bank.calls == ["AAAAAAAA1", "BBBBBBBB1"]
    assert store.get_job(b)["status"] == "completed"
    assert store.get_job(a)["status"] == "queued"


def test_read_timeout_pause_is_opt_in(tmp_path, monkeypatch):
    import ethiobank_receipts.worker as worker_mod
    monkeypatch.setattr(worker_mod, "TIMEOUT_PAUSE", 60.0)
    bank = Bank(_read_timeout())
    clock, store, worker = setup(tmp_path, bank)
    submit(store, "AAAAAAAA1")
    submit(store, "BBBBBBBB1")
    worker.tick()
    worker.tick()  # paused: B waits
    assert len(bank.calls) == 1
    clock.t += 61
    worker.tick()
    assert len(bank.calls) == 2


def test_low_remaining_header_pauses_bank(tmp_path):
    def extract(bank, ref):
        import time
        LAST_RATE_LIMIT["transactioninfo.ethiotelecom.et"] = {
            "remaining": 1, "limit": 5, "at": time.monotonic()}
        return tele_raw(ref)

    clock, store, worker = setup(tmp_path, extract,
                                 rates={b: (100, 40.0) for b in load_rates()})
    a, b = submit(store, "AAAAAAAA1"), submit(store, "BBBBBBBB1")
    worker.tick()
    worker.tick()
    assert store.get_job(a)["status"] == "completed"
    assert store.get_job(b)["status"] == "queued"
    clock.t += 41
    worker.tick()
    assert store.get_job(b)["status"] == "completed"
    LAST_RATE_LIMIT.clear()


def test_token_bucket_never_exceeds_capacity():
    clock = Clock()
    limiter = RateLimiter({"tele": (4, 40.0)}, clock.t)
    assert sum(limiter.try_acquire("tele", clock.t) for _ in range(10)) == 4
    clock.t += 39
    assert not limiter.try_acquire("tele", clock.t)
    clock.t += 1
    assert limiter.try_acquire("tele", clock.t)
    clock.t += 10_000
    assert sum(limiter.try_acquire("tele", clock.t) for _ in range(10)) == 4


def test_worker_respects_tele_rate(tmp_path):
    bank = Bank()
    clock, store, worker = setup(tmp_path, bank, rates={b: (4, 40.0) for b in load_rates()})
    for i in range(10):
        submit(store, f"REFREF{i:04d}")
    for _ in range(10):
        worker.tick()
    assert len(bank.calls) == 4
    clock.t += 40
    for _ in range(10):
        worker.tick()
    assert len(bank.calls) == 5


def test_rate_env_override():
    rates = load_rates({"ETHIOBANK_RATE_TELE": "2/90", "ETHIOBANK_RATE_CBE": "bad"})
    assert rates["tele"] == (2, 90.0)
    assert rates["cbe"] == (10, 5.0)


# --- webhooks ---------------------------------------------------------------

class Response:
    def __init__(self, status_code):
        self.status_code = status_code


def test_webhook_is_signed_and_delivered(tmp_path):
    sent = []

    def post(url, data, headers, timeout):
        sent.append((url, data, headers))
        return Response(200)

    clock, store, worker = setup(tmp_path, Bank("ok"), hooks=HOOKS, post=post)
    job_id = submit(store)
    worker.tick()  # bank call; marks webhook pending
    worker.tick()  # delivers
    assert len(sent) == 1
    url, body, headers = sent[0]
    assert url == "https://hook.test/cb"
    payload = json.loads(body)
    assert payload["success"] is True
    assert payload["verification"]["id"] == job_id
    assert payload["verification"]["status"] == "completed"
    ts = headers["X-Ethiobank-Timestamp"]
    assert headers["X-Ethiobank-Signature"] == webhooks.sign("s3cret", ts, body)
    assert store.get_job(job_id)["webhook_state"] == "delivered"


def test_webhook_retries_then_gives_up(tmp_path):
    calls = []

    def post(url, data, headers, timeout):
        calls.append(1)
        return Response(500)

    clock, store, worker = setup(tmp_path, Bank("ok"), hooks=HOOKS, post=post)
    job_id = submit(store)
    worker.tick()
    worker.tick()
    for delay in webhooks.RETRY_DELAYS:
        assert store.get_job(job_id)["webhook_state"] == "pending"
        clock.t += delay
        worker.tick()
    assert len(calls) == len(webhooks.RETRY_DELAYS) + 1
    job = store.get_job(job_id)
    assert job["webhook_state"] == "failed" and job["webhook_error"] == "HTTP 500"


def test_no_webhook_when_none_configured(tmp_path):
    clock, store, worker = setup(tmp_path, Bank("ok"))
    job_id = submit(store)
    worker.tick()
    assert store.get_job(job_id)["webhook_state"] == "none"


def test_load_webhooks():
    hooks = webhooks.load_webhooks({"ETHIOBANK_WEBHOOKS":
                                    "test-key|https://a.test/h|sec; bad-entry ;k2|ftp://x|s"})
    assert hooks == {KEY: ("https://a.test/h", "sec")}
