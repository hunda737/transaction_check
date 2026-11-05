"""SQLite-backed job queue and receipt cache.

The API runs several uvicorn worker processes and a separate bank worker, so all
shared state (jobs, cached receipts) lives in one SQLite file in WAL mode rather
than in memory.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

ACTIVE = ("queued", "processing")
TERMINAL = ("completed", "not_found", "needs_review")

# A receipt is cached only once its status can no longer change. Banks that
# report no status (awash, dashen, boa) only show settled transfers.
_FINAL_STATUS = re.compile(r"COMPLET|SUCCESS|FAIL|REVERS|CANCEL|DECLIN|REJECT")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    api_key_id       TEXT NOT NULL,
    bank             TEXT NOT NULL,
    reference        TEXT NOT NULL,
    client_ref       TEXT,
    status           TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    not_found_count  INTEGER NOT NULL DEFAULT 0,
    first_attempt_at REAL,
    next_attempt_at  REAL NOT NULL,
    reason           TEXT,
    result_json      TEXT,
    webhook_state    TEXT NOT NULL DEFAULT 'none',
    webhook_attempts INTEGER NOT NULL DEFAULT 0,
    webhook_next_at  REAL,
    webhook_error    TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_client_ref
    ON jobs (api_key_id, client_ref) WHERE client_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, bank, next_attempt_at);
CREATE INDEX IF NOT EXISTS jobs_lookup ON jobs (api_key_id, bank, reference);
CREATE INDEX IF NOT EXISTS jobs_webhook ON jobs (webhook_state, webhook_next_at);

CREATE TABLE IF NOT EXISTS receipts (
    bank         TEXT NOT NULL,
    reference    TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    fetched_at   REAL NOT NULL,
    PRIMARY KEY (bank, reference)
);
"""


def api_key_id(api_key):
    """Stable, non-secret identifier for an API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def is_cacheable(receipt):
    status = (receipt.get("status") or "").upper()
    return not status or bool(_FINAL_STATUS.search(status))


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path=None, clock=time.time):
        self.path = path or os.environ.get("ETHIOBANK_DB_PATH", "ethiobank.db")
        self.clock = clock
        self._local = threading.local()
        conn = self._conn()
        conn.executescript(_SCHEMA)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # Autocommit; write paths open BEGIN IMMEDIATE explicitly so two
            # processes cannot both claim the same job.
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _tx(self):
        return _Tx(self._conn())

    # --- receipt cache ------------------------------------------------------

    def get_cached(self, bank, reference):
        row = self._conn().execute(
            "SELECT receipt_json FROM receipts WHERE bank = ? AND reference = ?",
            (bank, reference)).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def put_cached(self, bank, references, receipt):
        """Cache ``receipt`` under every reference it is known by."""
        if not is_cacheable(receipt):
            return False
        body = json.dumps(receipt)
        now = self.clock()
        with self._tx() as conn:
            for ref in {r for r in references if r}:
                conn.execute(
                    "INSERT OR REPLACE INTO receipts (bank, reference, receipt_json, fetched_at)"
                    " VALUES (?, ?, ?, ?)", (bank, ref, body, now))
        return True

    # --- jobs ---------------------------------------------------------------

    def submit(self, key_id, bank, reference, client_ref=None, cached_receipt=None):
        """Create a job, or return the existing one it duplicates.

        Returns ``(job, created)``. A ``client_ref`` already used by this API key
        returns that job; an unfinished job for the same bank+reference returns
        that job, so a double submit costs no extra bank call.
        """
        now = self.clock()
        with self._tx() as conn:
            if client_ref:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE api_key_id = ? AND client_ref = ?",
                    (key_id, client_ref)).fetchone()
                if row:
                    return dict(row), False
            if cached_receipt is None:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE api_key_id = ? AND bank = ? AND reference = ?"
                    " AND status IN ('queued', 'processing') ORDER BY created_at LIMIT 1",
                    (key_id, bank, reference)).fetchone()
                if row:
                    return dict(row), False

            job_id = uuid.uuid4().hex
            if cached_receipt is not None:
                status, result = "completed", json.dumps(cached_receipt)
            else:
                status, result = "queued", None
            conn.execute(
                "INSERT INTO jobs (id, api_key_id, bank, reference, client_ref, status,"
                " next_attempt_at, result_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, key_id, bank, reference, client_ref, status, now, result, now, now))
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row), True

    def get_job(self, job_id, key_id=None):
        sql, args = "SELECT * FROM jobs WHERE id = ?", [job_id]
        if key_id is not None:
            sql += " AND api_key_id = ?"
            args.append(key_id)
        row = self._conn().execute(sql, args).fetchone()
        return dict(row) if row else None

    def queue_position(self, job):
        """How many unfinished jobs for the same bank are ahead of this one."""
        if job["status"] not in ACTIVE:
            return 0
        # rowid follows insert order; created_at can tie within the clock's resolution.
        row = self._conn().execute(
            "SELECT COUNT(*) FROM jobs WHERE bank = ? AND status IN ('queued', 'processing')"
            " AND rowid < (SELECT rowid FROM jobs WHERE id = ?)",
            (job["bank"], job["id"])).fetchone()
        return row[0]

    def due_banks(self, now):
        rows = self._conn().execute(
            "SELECT DISTINCT bank FROM jobs WHERE status = 'queued' AND next_attempt_at <= ?",
            (now,)).fetchall()
        return [r[0] for r in rows]

    def claim_next(self, bank, now):
        """Atomically move the next due job for ``bank`` to ``processing``."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE bank = ? AND status = 'queued'"
                " AND next_attempt_at <= ? ORDER BY next_attempt_at, created_at LIMIT 1",
                (bank, now)).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'processing', attempts = attempts + 1,"
                " first_attempt_at = COALESCE(first_attempt_at, ?), updated_at = ?"
                " WHERE id = ?", (now, now, row["id"]))
            return dict(conn.execute("SELECT * FROM jobs WHERE id = ?",
                                     (row["id"],)).fetchone())

    def requeue(self, job_id, next_at, reason, not_found_count=None):
        now = self.clock()
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'queued', next_attempt_at = ?, reason = ?,"
                " not_found_count = COALESCE(?, not_found_count), updated_at = ?"
                " WHERE id = ?", (next_at, reason, not_found_count, now, job_id))

    def finish(self, job_id, status, result=None, reason=None, notify=False):
        assert status in TERMINAL, status
        now = self.clock()
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, result_json = ?, reason = ?, updated_at = ?,"
                " webhook_state = ?, webhook_next_at = ? WHERE id = ?",
                (status, json.dumps(result) if result is not None else None, reason, now,
                 "pending" if notify else "none", now if notify else None, job_id))

    def recover_stale(self):
        """After a worker crash/restart: re-queue jobs and webhooks left mid-flight."""
        now = self.clock()
        with self._tx() as conn:
            jobs = conn.execute(
                "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'processing'",
                (now,)).rowcount
            hooks = conn.execute(
                "UPDATE jobs SET webhook_state = 'pending' WHERE webhook_state = 'sending'"
            ).rowcount
        return jobs, hooks

    # --- webhooks -----------------------------------------------------------

    def claim_webhooks(self, now, limit=20):
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE webhook_state = 'pending' AND webhook_next_at <= ?"
                " ORDER BY webhook_next_at LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                conn.execute("UPDATE jobs SET webhook_state = 'sending',"
                             " webhook_attempts = webhook_attempts + 1 WHERE id = ?",
                             (row["id"],))
        return [dict(r) for r in rows]

    def webhook_done(self, job_id, delivered, next_at=None, error=None):
        if delivered:
            state = "delivered"
        else:
            state = "pending" if next_at is not None else "failed"
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET webhook_state = ?, webhook_next_at = ?, webhook_error = ?"
                " WHERE id = ?", (state, next_at, error, job_id))

    # --- presentation -------------------------------------------------------

    @staticmethod
    def public(job):
        """The job as returned by the API and sent in webhooks."""
        return {
            "id": job["id"],
            "bank": job["bank"],
            "reference": job["reference"],
            "client_ref": job["client_ref"],
            "status": job["status"],
            "reason": job["reason"],
            "attempts": job["attempts"],
            "receipt": json.loads(job["result_json"]) if job["result_json"] else None,
            "created_at": _iso(job["created_at"]),
            "updated_at": _iso(job["updated_at"]),
        }


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False
