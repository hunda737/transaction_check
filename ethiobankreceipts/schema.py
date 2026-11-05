"""Normalization of per-bank receipt fields into one common shape.

The critical rule encoded here: ``amount`` is always the *settled/transferred*
figure -- what the receiving account actually got -- never the payer's total
debit, which includes the provider's service fee and VAT. Verified live:
telebirr DHS48P7HGQ settled 2,000 but totals 2,006; Zemen settled 4,000.00 but
totals 4,023.00. Crediting the total over-pays every deposit by the fee.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ethiobank_receipts.errors import ReceiptParseError

_NUMBER = re.compile(r"-?[\d,]*\d(?:\.\d+)?")

# Tried in order; the first that parses wins.
_DATE_FORMATS = (
    "%d-%m-%Y %H:%M:%S",      # telebirr:  28-08-2026 16:39:13 (day first)
    "%Y-%m-%d %I:%M:%S %p",   # awash:     2025-08-04 09:25:12 AM
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y, %I:%M:%S %p",  # cbe
    "%b %d, %Y, %I:%M:%S %p",  # dashen
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y",               # zemen:     26-Aug-2025
    "%d/%m/%Y %H:%M:%S",
)


def money(value):
    """Parse a money string such as '2,000 Birr' or 'ETB 4,000.00' to Decimal."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    match = _NUMBER.search(str(value))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def to_iso(value):
    """Parse a bank date string to an ISO 8601 string, or None if unrecognized."""
    if not value:
        return None
    text = str(value).strip()
    # Extractors may already have converted to ISO.
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return None


def _str(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _amount_str(value):
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


# raw-field -> normalized mapping per bank. The `amount` entry is deliberately
# the settled/transferred field and never the payer's total.
_MAP = {
    "tele": {
        "reference": "invoice_no", "amount": "settled_amount", "status": "status",
        "paid_at": "payment_date",
        "payer_name": "payer_name", "payer_account": "payer_number",
        "receiver_name": "credited_party", "receiver_account": "credited_party_number",
        "fees": {"service_fee": "service_fee", "vat": "service_fee_vat",
                 "stamp_duty": "stamp_duty", "discount": "discount",
                 "payer_total_debit": "total_paid"},
    },
    "awash": {
        "reference": "Transaction ID", "amount": "Amount", "status": None,
        "paid_at": "Transaction Time",
        "payer_name": "Sender Name", "payer_account": "Sender Account",
        "receiver_name": "Beneficiary name", "receiver_account": "Beneficiary Account",
        "fees": {"service_fee": "Charge", "vat": "VAT"},
    },
    "zemen": {
        "reference": "Reference No", "amount": "Settled Amount",
        "status": "Transaction Status", "paid_at": "Date",
        "payer_name": "Payer Name", "payer_account": "Payer Account No",
        "receiver_name": "Recipient Name", "receiver_account": "Recipient Account No",
        "fees": {"service_fee": "Service Charge", "vat": "VAT",
                 "payer_total_debit": "Total Amount Paid"},
    },
    "cbe": {
        "reference": "reference_no", "amount": "transferred_amount", "status": "status",
        "paid_at": "payment_date",
        "payer_name": "payer", "payer_account": "payer_account",
        "receiver_name": "receiver", "receiver_account": "receiver_account",
        "fees": {"service_fee": "commission", "vat": "vat_on_commission",
                 "payer_total_debit": "total_debited"},
    },
    "dashen": {
        "reference": "transaction_reference", "amount": "amount", "status": None,
        "paid_at": "transaction_date",
        "payer_name": "sender_name", "payer_account": None,
        "receiver_name": "beneficiary_name", "receiver_account": "beneficiary_account",
        "fees": {"payer_total_debit": "total"},
    },
    "boa": {
        "reference": "Transaction Reference", "amount": "Transferred Amount",
        "status": None, "paid_at": "Transaction Date",
        "payer_name": "Source Account Name", "payer_account": "Source Account",
        "receiver_name": "Receiver's Name", "receiver_account": "Receiver's Account",
        "fees": {"service_fee": "Service Charge", "vat": "VAT",
                 "payer_total_debit": "Total Amount"},
    },
}


def normalize(bank: str, raw: dict, reference: str = None) -> dict:
    """Map a bank's raw extraction output onto the common receipt shape."""
    if not raw:
        raise ReceiptParseError(f"No receipt fields extracted for {bank}")

    mapping = _MAP[bank]
    amount = money(raw.get(mapping["amount"]))
    ref = _str(raw.get(mapping["reference"])) or _str(reference)

    # A receipt with no reference or no amount is not usable for verification,
    # and almost always means the bank changed their layout.
    missing = [n for n, v in (("reference", ref), ("amount", amount)) if v is None]
    if missing:
        raise ReceiptParseError(
            f"{bank} receipt is missing required field(s): {', '.join(missing)}. "
            "The bank may have changed their receipt layout."
        )

    status_key = mapping["status"]
    status = _str(raw.get(status_key)) if status_key else None

    fees = {}
    for out_key, raw_key in mapping["fees"].items():
        parsed = money(raw.get(raw_key))
        if parsed is not None:
            fees[out_key] = _amount_str(parsed)

    return {
        "bank": bank,
        "reference": ref,
        "status": status.upper() if status else None,
        "amount": _amount_str(amount),
        "currency": "ETB",
        "paid_at": to_iso(raw.get(mapping["paid_at"])) if mapping["paid_at"] else None,
        "payer_name": _str(raw.get(mapping["payer_name"])) if mapping["payer_name"] else None,
        "payer_account": _str(raw.get(mapping["payer_account"])) if mapping["payer_account"] else None,
        "receiver_name": _str(raw.get(mapping["receiver_name"])) if mapping["receiver_name"] else None,
        "receiver_account": _str(raw.get(mapping["receiver_account"])) if mapping["receiver_account"] else None,
        "fees": fees,
        "raw": {k: (str(v) if v is not None else None) for k, v in raw.items()},
    }
