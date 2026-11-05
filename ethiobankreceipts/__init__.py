"""Receipt extraction for Ethiopian banks."""
import requests

from .banks import BANKS, get_bank
from .errors import (
    BankUnavailable,
    EthiobankError,
    InvalidReference,
    ReceiptNotFound,
    ReceiptParseError,
)
from .extractors import cbe, dashen, awash, boa, zemen, tele
from .schema import normalize

EXTRACTORS = {
    "cbe": cbe.extract_cbe_receipt_info,
    "dashen": dashen.extract_dashen_receipt_data,
    "awash": awash.extract_awash_receipt_data,
    "boa": boa.extract_boa_receipt_data,
    "zemen": zemen.extract_zemen_receipt_data,
    "tele": tele.extract_tele_receipt_data,
}

__all__ = [
    "extract_receipt", "get_receipt", "BANKS",
    "EthiobankError", "InvalidReference", "ReceiptNotFound",
    "ReceiptParseError", "BankUnavailable",
]


def extract_receipt(bank: str, url: str):
    """Extract a receipt and return the bank's own raw field names.

    Args:
        bank: One of cbe, dashen, awash, boa, zemen, tele
        url: The transaction reference, or a receipt URL on that bank's host

    Returns:
        dict: raw extracted fields
    """
    spec = get_bank(bank)
    try:
        return EXTRACTORS[spec.key](url)
    except EthiobankError:
        raise
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            raise ReceiptNotFound(f"No {spec.key} receipt for {url!r}") from exc
        raise BankUnavailable(f"{spec.key} returned HTTP {status}") from exc
    except requests.RequestException as exc:
        raise BankUnavailable(f"Could not reach {spec.name}: {exc}") from exc


def get_receipt(bank: str, reference: str) -> dict:
    """Extract a receipt and return it in the normalized cross-bank shape.

    ``amount`` is always the settled/transferred figure, excluding the
    provider's service fee and VAT.
    """
    spec = get_bank(bank)
    raw = extract_receipt(spec.key, reference)
    return normalize(spec.key, raw, reference=reference)
