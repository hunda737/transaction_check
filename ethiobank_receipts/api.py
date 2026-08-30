"""HTTP service exposing receipt extraction to other applications.

Run:
    ETHIOBANK_API_KEYS=your-key uvicorn ethiobank_receipts.api:app --port 8000

Every endpoint requires an ``X-API-Key`` header. Callers pass a transaction
*reference*, never a URL, so this service cannot be steered at arbitrary hosts.
"""
import logging
import os
import secrets
from typing import Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ethiobank_receipts import get_receipt
from ethiobank_receipts.banks import BANKS
from ethiobank_receipts.errors import (
    BankUnavailable,
    InvalidReference,
    ReceiptNotFound,
    ReceiptParseError,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Ethiopian Bank Receipt Service",
    description=(
        "Extracts structured receipt data from Ethiopian bank receipt pages.\n\n"
        "**`amount` is the settled/transferred figure — what the receiving "
        "account actually got. It excludes the provider's service fee and VAT.** "
        "The payer's total debit is reported separately under `fees`."
    ),
    version="0.2.0",
)


def _api_keys():
    raw = os.environ.get("ETHIOBANK_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    """Validate the X-API-Key header against ETHIOBANK_API_KEYS."""
    keys = _api_keys()
    if not keys:
        # Fail closed: an unconfigured service must not serve requests.
        logger.error("ETHIOBANK_API_KEYS is not set; refusing all requests")
        raise HTTPException(
            status_code=503, detail="Service not configured: no API keys set")
    if not x_api_key or not any(
            secrets.compare_digest(x_api_key, k) for k in keys):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class Fees(BaseModel):
    service_fee: Optional[str] = None
    vat: Optional[str] = None
    stamp_duty: Optional[str] = None
    discount: Optional[str] = None
    payer_total_debit: Optional[str] = Field(
        default=None,
        description="What the payer was charged, including fees. Do NOT credit this.")


class Receipt(BaseModel):
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


@app.get("/health", tags=["service"])
def health():
    return {"status": "ok", "api_keys_configured": bool(_api_keys())}


@app.get("/banks", tags=["service"])
def banks():
    """Supported banks and the reference format each expects."""
    return {
        "banks": [
            {"key": s.key, "name": s.name, "host": s.host,
             "reference_example": s.example, "reference_pattern": s.pattern.pattern}
            for s in BANKS.values()
        ]
    }


@app.get("/receipts/{bank}/{reference}", response_model=Receipt,
         dependencies=[Depends(require_api_key)], tags=["receipts"])
def receipt(
    bank: str,
    reference: str,
    account: Optional[str] = Query(
        default=None,
        description="Retired: CBE FT+account verification is no longer supported."),
):
    """Fetch and normalize one receipt.

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
        return get_receipt(bank, reference)
    except InvalidReference as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReceiptNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BankUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ReceiptParseError as exc:
        # The bank changed their receipt layout: verification is degraded until
        # the parser is updated. Loud on purpose.
        logger.error("Parse failure for %s/%s: %s", bank, reference, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
