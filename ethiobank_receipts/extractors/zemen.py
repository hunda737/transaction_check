"""Zemen Bank receipt extraction (PDF)."""
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict

import pdfplumber

from ethiobank_receipts.banks import resolve_url
from ethiobank_receipts.download import download_pdf_from_url
from ethiobank_receipts.errors import ReceiptNotFound

_MONEY = r"([\d,]+\.\d{2})"

_PATTERNS = {
    "Invoice No": re.compile(r"Invoice No\.?\s*[:፡]\s*(\d+)"),
    # The label uses an Amharic colon (U+1361), not ASCII ':'.
    "Date": re.compile(r"Date\s*[:፡]\s*(\d{1,2}-[A-Za-z]{3}-\d{4})"),
    # Stop at the next label; a greedy [A-Z\s]+ swallowed the 'A' of 'Address'
    # and returned "NAHOM ALEMU NEBEBE A".
    "Payer Name": re.compile(r"Payer name:\s*(.+?)\s*(?:Address|አድራሻ|የከፋይ|$)"),
    "Payer Account No": re.compile(r"Payer account no\.?:\s*([\d\*()X]+)"),
    "Recipient Name": re.compile(r"Recipient name:\s*([A-Za-z0-9\s\.\-]+?)\s*(?:የገንዘብ|Recipient account|$)"),
    "Recipient Account No": re.compile(r"Recipient account no\.?:\s*([\d\*]+)"),
    "Reference No": re.compile(r"Reference No:\s*([A-Z0-9]+)"),
    "Transaction Status": re.compile(r"Transaction status:\s*(\w+)"),
    # The settled amount is the first line item after the "Settled Amount"
    # column header: "1 <detail> ETB 4,000.00". The previous pattern hardcoded
    # "ATM CASH WITHDRAWAL", so it matched only cash withdrawals and returned
    # nothing for transfers.
    "Transaction Detail": re.compile(
        r"Settled Amount\s+\d*\s*(.+?)\s*ETB\s*" + _MONEY),
    "Settled Amount": re.compile(
        r"Settled Amount\s+\d*\s*.+?\s*ETB\s*" + _MONEY),
    "Service Charge": re.compile(r"Service Charge ETB\s*" + _MONEY),
    "VAT": re.compile(r"VAT 15% ETB\s*" + _MONEY),
    "Total Amount Paid": re.compile(r"Total Amount Paid ETB\s*" + _MONEY),
    "Amount in Words": re.compile(r"Total amount in word:\s*([A-Z\s\-]+CENT\(S\))"),
}

_CURRENCY_FIELDS = ("Amount", "Charge", "VAT")


def parse_zemen_text(text: str) -> Dict[str, str]:
    """Parse the text of a Zemen receipt PDF. Pure function -- no network."""
    result: Dict[str, str] = {}
    for field, pattern in _PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1).strip()
        if any(token in field for token in _CURRENCY_FIELDS) and field != "Amount in Words":
            value = f"ETB {value}"
        result[field] = value
    return result


def _pdf_text(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        with ThreadPoolExecutor() as executor:
            pages = list(executor.map(
                lambda page: page.extract_text() or "", pdf.pages))
    return " ".join(pages).replace("\n", " ")


def extract_zemen_receipt_data(url: str) -> Dict[str, str]:
    """Extract a Zemen Bank receipt from a reference (or a Zemen URL).

    Raises rather than returning None on failure: a swallowed error that yields
    a missing amount is a financial bug, not a recoverable condition.
    """
    resolved = resolve_url("zemen", url)
    pdf_path = download_pdf_from_url(resolved)
    data = parse_zemen_text(_pdf_text(pdf_path))
    if not data:
        raise ReceiptNotFound(f"No Zemen receipt for {url!r}")
    return data
