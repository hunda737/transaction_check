"""HTTP service exposing receipt extraction to other applications.

Run the API and exactly one worker:
    ETHIOBANK_API_KEYS=your-key uvicorn ethiobank_receipts.api:app --port 8060
    ETHIOBANK_API_KEYS=your-key python -m ethiobank_receipts.worker

Every endpoint requires an ``X-API-Key`` header. Callers pass a transaction
*reference*, never a URL, so this service cannot be steered at arbitrary hosts.

For automated deposit checks use ``POST /verifications``: it answers at once and
the worker calls the bank at a pace the bank tolerates. ``GET /receipts/...``
calls the bank synchronously and is meant for occasional manual checks.
"""
import logging
import os
import secrets
import threading
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ethiobank_receipts import get_receipt
from ethiobank_receipts.banks import BANKS, get_bank, resolve_url
from ethiobank_receipts.errors import (
    BankUnavailable,
    InvalidReference,
    ReceiptNotFound,
    ReceiptParseError,
)
from ethiobank_receipts.logconfig import configure_logging
from ethiobank_receipts.ratelimit import load_rates
from ethiobank_receipts.store import Store, api_key_id

logger = logging.getLogger(__name__)
configure_logging()

app = FastAPI(
    title="Ethiopian Bank Receipt Service",
    description=(
        "Extracts structured receipt data from Ethiopian bank receipt pages.\n\n"
        "**`amount` is the settled/transferred figure — what the receiving "
        "account actually got. It excludes the provider's service fee and VAT.** "
        "The payer's total debit is reported separately under `fees`.\n\n"
        "For automated checks use `POST /verifications` (queued, rate-limited, "
        "results by webhook or `GET /verifications/{id}`).\n\n"
        "Every response carries a boolean `success`. Errors look like "
        "`{\"success\": false, \"detail\": \"...\"}`."
    ),
    version="0.3.0",
)

_store = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"success": False, "detail": jsonable_encoder(exc.errors())},
    )


def _api_keys():
    raw = os.environ.get("ETHIOBANK_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    """Validate the X-API-Key header against ETHIOBANK_API_KEYS; return the key."""
    keys = _api_keys()
    if not keys:
        # Fail closed: an unconfigured service must not serve requests.
        logger.error("ETHIOBANK_API_KEYS is not set; refusing all requests")
        raise HTTPException(
            status_code=503, detail="Service not configured: no API keys set")
    if not x_api_key or not any(
            secrets.compare_digest(x_api_key, k) for k in keys):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return x_api_key


class Fees(BaseModel):
    service_fee: Optional[str] = None
    vat: Optional[str] = None
    stamp_duty: Optional[str] = None
    discount: Optional[str] = None
    payer_total_debit: Optional[str] = Field(
        default=None,
        description="What the payer was charged, including fees. Do NOT credit this.")


class Receipt(BaseModel):
    success: bool = True
    bank: str
    reference: str
    status: Optional[str] = None
    amount: str = Field(
        description="Settled/transferred amount, excluding fees. Credit this.")
    currency: str = "ETB"
    paid_at: Optional[str] = Field(
        default=None, description="ISO 8601, or null if the bank's format was unrecognized.")
    payer_name: Optional[str] = None
    payer_account: Optional[str] = None
    receiver_name: Optional[str] = None
    receiver_account: Optional[str] = None
    fees: Fees = Fees()
    raw: Dict[str, Optional[str]] = {}


class VerificationRequest(BaseModel):
    bank: str = Field(description="One of the keys from GET /banks, e.g. 'tele'.")
    reference: str = Field(min_length=1, max_length=512)
    client_ref: Optional[str] = Field(
        default=None, max_length=128,
        description="Your own deposit id. Re-submitting the same client_ref returns "
                    "the same verification instead of creating a new one.")


class Verification(BaseModel):
    success: bool = True
    id: str
    bank: str
    reference: str
    client_ref: Optional[str] = None
    status: str = Field(description="queued | processing | completed | not_found | needs_review")
    reason: Optional[str] = None
    attempts: int = 0
    receipt: Optional[Dict[str, Any]] = Field(
        default=None, description="The normalized receipt once status is 'completed'.")
    queue_position: Optional[int] = None
    estimated_wait_seconds: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@app.get("/health", tags=["service"])
def health():
    return {"success": True, "status": "ok", "api_keys_configured": bool(_api_keys())}


@app.get("/banks", tags=["service"])
def banks():
    """Supported banks and the reference format each expects."""
    return {
        "success": True,
        "banks": [
            {"key": s.key, "name": s.name, "host": s.host,
             "reference_example": s.example, "reference_pattern": s.pattern.pattern}
            for s in BANKS.values()
        ]
    }


def _verification_body(store, job):
    body = Store.public(job)
    if job["status"] in ("queued", "processing"):
        position = store.queue_position(job)
        _, interval = load_rates().get(job["bank"], (1, 5.0))
        body["queue_position"] = position
        # Rough: one bank call per refill interval, ~3s for the call itself.
        body["estimated_wait_seconds"] = int(position * interval + 3)
    return {"success": True, **body}


@app.post("/verifications", response_model=Verification, status_code=202,
          tags=["verifications"],
          responses={200: {"model": Verification,
                           "description": "Already known: cached receipt or repeated client_ref"}})
def create_verification(body: VerificationRequest, api_key: str = Depends(require_api_key)):
    """Queue a receipt check. Returns at once; the result arrives by webhook.

    202 = queued · 200 = already known (cached receipt, or a repeated client_ref)
    · 400 = malformed reference or unknown bank.
    """
    try:
        spec = get_bank(body.bank)
        resolve_url(spec.key, body.reference)
    except InvalidReference as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = get_store()
    reference = body.reference.strip()
    cached = store.get_cached(spec.key, reference)
    job, created = store.submit(api_key_id(api_key), spec.key, reference,
                                client_ref=body.client_ref, cached_receipt=cached)
    if created:
        logger.info("Queued %s/%s as job %s%s", spec.key, reference, job["id"],
                    " (cache hit)" if cached else "")
    status_code = 202 if job["status"] in ("queued", "processing") else 200
    return JSONResponse(status_code=status_code,
                        content=jsonable_encoder(_verification_body(store, job)))


@app.get("/verifications/{verification_id}", response_model=Verification,
         tags=["verifications"])
def get_verification(verification_id: str, api_key: str = Depends(require_api_key)):
    """Current state of a verification. Use when a webhook was missed."""
    store = get_store()
    job = store.get_job(verification_id, api_key_id(api_key))
    if job is None:
        raise HTTPException(status_code=404, detail="No such verification")
    return _verification_body(store, job)


@app.get("/receipts/{bank}/{reference}", response_model=Receipt,
         dependencies=[Depends(require_api_key)], tags=["receipts"])
def receipt(
    bank: str,
    reference: str,
    account: Optional[str] = Query(
        default=None,
        description="Retired: CBE FT+account verification is no longer supported."),
):
    """Fetch and normalize one receipt synchronously (manual checks).

    Not rate limited against the bank -- use POST /verifications for automated
    or bursty traffic. Receipts already fetched are served from the cache.

    404 = no such reference · 400 = malformed reference · 502 = bank unreachable
    · 500 = the bank changed their layout and parsing broke.
    """
    if bank.lower() == "cbe" and account:
        # CBE retired FT+account verification; the reference is now the app's
        # share token, passed on the path with no account query parameter.
        raise HTTPException(
            status_code=400,
            detail=("CBE no longer supports FT+account verification. Pass the share "
                    "token from the CBE app as the path reference and omit 'account'."))

    try:
        key = get_bank(bank).key
    except InvalidReference as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = _store_or_none()
    if store is not None:
        cached = store.get_cached(key, reference.strip())
        if cached is not None:
            return cached

    try:
        result = get_receipt(bank, reference)
    except InvalidReference as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReceiptNotFound as exc:
        logger.info("Not found %s/%s: %s", bank, reference, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BankUnavailable as exc:
        cause = exc.__cause__
        logger.warning("Bank unavailable %s/%s: %s (cause: %s)", bank, reference, exc,
                       type(cause).__name__ if cause else "none")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ReceiptParseError as exc:
        # The bank changed their receipt layout: verification is degraded until
        # the parser is updated. Loud on purpose.
        logger.error("Parse failure for %s/%s: %s", bank, reference, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if store is not None:
        try:
            store.put_cached(key, [reference.strip(), result.get("reference")], result)
        except Exception:
            logger.exception("Could not cache %s/%s", key, reference)
    return result


def _store_or_none():
    """The sync endpoint keeps working even if the database is unavailable."""
    try:
        return get_store()
    except Exception:
        logger.exception("Receipt cache unavailable")
        return None
