"""SQLite job queue and receipt cache."""
import threading

from ethiobank_receipts.store import Store, is_cacheable

RECEIPT = {"bank": "cbe", "reference": "FT26203953V8", "status": "COMPLETED",
           "amount": "100.00"}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, clock=None):
    return Store(str(tmp_path / "s.db"), clock=clock or Clock())


def test_cache_found_by_submitted_and_returned_reference(tmp_path):
    store = make(tmp_path)
    store.put_cached("cbe", ["v2-token123", "FT26203953V8"], RECEIPT)
    assert store.get_cached("cbe", "v2-token123") == RECEIPT
    assert store.get_cached("cbe", "FT26203953V8") == RECEIPT
    assert store.get_cached("tele", "v2-token123") is None


def test_pending_receipts_are_not_cached(tmp_path):
    store = make(tmp_path)
    assert not store.put_cached("tele", ["REF1"], {**RECEIPT, "status": "PENDING"})
    assert store.get_cached("tele", "REF1") is None
    assert is_cacheable({"status": None})
    assert is_cacheable({"status": "FAILED"})


def test_duplicate_active_submission_returns_same_job(tmp_path):
    store = make(tmp_path)
    first, created1 = store.submit("k", "tele", "REF12345")
    second, created2 = store.submit("k", "tele", "REF12345")
    assert created1 and not created2
    assert first["id"] == second["id"]
    # Another API key gets its own job.
    other, created3 = store.submit("k2", "tele", "REF12345")
    assert created3 and other["id"] != first["id"]


def test_client_ref_is_idempotent_even_after_finish(tmp_path):
    store = make(tmp_path)
    job, _ = store.submit("k", "tele", "REF12345", client_ref="dep-1")
    store.finish(job["id"], "not_found", reason="nope")
    again, created = store.submit("k", "tele", "OTHERREF1", client_ref="dep-1")
    assert not created and again["id"] == job["id"] and again["status"] == "not_found"


def test_finished_job_does_not_block_new_submission(tmp_path):
    store = make(tmp_path)
    job, _ = store.submit("k", "tele", "REF12345")
    store.finish(job["id"], "needs_review", reason="down")
    new, created = store.submit("k", "tele", "REF12345")
    assert created and new["id"] != job["id"]


def test_cached_submission_is_completed_immediately(tmp_path):
    store = make(tmp_path)
    job, created = store.submit("k", "cbe", "v2-token123", cached_receipt=RECEIPT)
    assert created and job["status"] == "completed"
    assert Store.public(job)["receipt"] == RECEIPT


def test_claim_order_and_due_time(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock)
    a, _ = store.submit("k", "tele", "AAAAAAAA1")
    clock.t += 1
    b, _ = store.submit("k", "tele", "BBBBBBBB1")
    store.requeue(a["id"], clock.t + 100, "later")
    assert store.claim_next("tele", clock.t)["id"] == b["id"]
    assert store.claim_next("tele", clock.t) is None  # a not due yet
    claimed = store.claim_next("tele", clock.t + 100)
    assert claimed["id"] == a["id"] and claimed["attempts"] == 1


def test_queue_position(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock)
    # Same timestamp on purpose: submissions within one clock tick.
    jobs = [store.submit("k", "tele", f"REFREF{i:04d}")[0] for i in range(3)]
    assert [store.queue_position(j) for j in jobs] == [0, 1, 2]


def test_recover_stale_requeues_processing(tmp_path):
    store = make(tmp_path)
    store.submit("k", "tele", "REF12345")
    job = store.claim_next("tele", store.clock())
    assert store.get_job(job["id"])["status"] == "processing"
    assert store.recover_stale() == (1, 0)
    assert store.get_job(job["id"])["status"] == "queued"


def test_concurrent_claims_never_double_claim(tmp_path):
    path = str(tmp_path / "s.db")
    setup = Store(path)
    for i in range(20):
        setup.submit("k", "tele", f"REFREF{i:04d}")
    claimed, lock = [], threading.Lock()

    def claimer():
        store = Store(path)  # separate connection, like a separate process
        while True:
            job = store.claim_next("tele", 10 ** 12)
            if job is None:
                return
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=claimer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 20 and len(set(claimed)) == 20
