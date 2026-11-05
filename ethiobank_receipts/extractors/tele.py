import os
from typing import Dict

import requests
from bs4 import BeautifulSoup

from ethiobank_receipts.banks import resolve_url
from ethiobank_receipts.download import get_with_retry, DEFAULT_TIMEOUT
from ethiobank_receipts.errors import BankUnavailable, ReceiptNotFound

# telebirr answers real lookups in ~2-3s. When it is rate limiting it never
# answers at all, so a long read timeout only delays the inevitable.
TELE_TIMEOUT = (
    DEFAULT_TIMEOUT[0],
    float(os.environ.get("ETHIOBANK_TELE_READ_TIMEOUT", 10)),
)

# Labels are bilingual, e.g. "የከፋይ ስም/Payer Name". Match on the English half.
_PAIR_LABELS = {
    "payer name": "payer_name",
    "payer telebirr no.": "payer_number",
    "payer account type": "payer_account_type",
    "credited party name": "credited_party",
    "credited party account no": "credited_party_number",
    "transaction status": "status",
    "stamp duty": "stamp_duty",
    "discount amount": "discount",
    "service fee": "service_fee",
    "service fee vat": "service_fee_vat",
    "total paid amount": "total_paid",
}


def _english(text: str) -> str:
    """'የከፋይ ስም/Payer Name' -> 'payer name'."""
    return (text or "").split("/")[-1].strip().lower()


def _cells(row):
    return [td.get_text(" ", strip=True) for td in row.find_all("td")]


# Longest first: "service fee vat" must win over "service fee".
_LABELS_BY_LENGTH = sorted(_PAIR_LABELS, key=len, reverse=True)


def _match_label(cell: str):
    """Some label cells repeat their own value ('transaction status Completed'),
    so match on prefix rather than equality."""
    label = _english(cell)
    for known in _LABELS_BY_LENGTH:
        if label.startswith(known):
            return _PAIR_LABELS[known]
    return None


def parse_tele_html(html: str) -> Dict[str, str]:
    """Parse a Telebirr receipt page. Pure function -- no network."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    data: Dict[str, str] = {}

    # 1. Label/value pairs sharing a row. The previous implementation used
    #    "find label -> find_next('td')", which walks into the *next label* on
    #    the multi-column table below; that is what corrupted receipt.json.
    for row in rows:
        cells = [c for c in _cells(row) if c]
        if len(cells) != 2:
            continue
        key = _match_label(cells[0])
        if key and key not in data:
            data[key] = cells[1]

    # 2. The invoice table is positional: a header row of three labels followed
    #    by a sibling row holding the three matching values.
    #       ['Invoice No.', 'Payment date',        'Settled Amount']
    #       ['DHS48P7HGQ',  '28-08-2026 16:39:13', '2,000 Birr']
    for row in rows:
        cells = _cells(row)
        if len(cells) == 3 and "invoice no" in _english(cells[0]):
            following = row.find_next_sibling("tr")
            if following:
                values = _cells(following)
                if len(values) == 3:
                    data["invoice_no"] = values[0].strip()
                    data["payment_date"] = values[1].strip()
                    data["settled_amount"] = values[2].strip()
            break

    return data


def extract_tele_receipt_data(url_or_id: str) -> Dict[str, str]:
    """Extract Telebirr receipt details from a reference id (or a telebirr URL).

    Returns raw field names; use ``ethiobank_receipts.get_receipt`` for the
    normalized shape.
    """
    url = resolve_url("tele", url_or_id)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        resp = get_with_retry(url, headers=headers, timeout=TELE_TIMEOUT)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            raise ReceiptNotFound(
                f"No telebirr receipt for {url_or_id!r}") from exc
        raise BankUnavailable(f"telebirr returned HTTP {status}") from exc
    except requests.RequestException as exc:
        raise BankUnavailable(f"Could not reach telebirr: {exc}") from exc

    data = parse_tele_html(resp.text)
    if not data:
        # Telebirr answers 200 with an empty receipt for unknown references.
        raise ReceiptNotFound(f"No telebirr receipt for {url_or_id!r}")
    return data
