from bs4 import BeautifulSoup
from typing import Dict

from ethiobank_receipts.banks import resolve_url
from ethiobank_receipts.download import DEFAULT_TIMEOUT, get_with_retry
from ethiobank_receipts.errors import ReceiptNotFound

_KEYS_OF_INTEREST = [
    "Transaction Time", "Transaction Type", "Amount", "Charge", "VAT",
    "Sender Name", "Sender Account", "Beneficiary name", "Beneficiary Account",
    "Beneficiary Bank", "Reason", "Transaction ID",
]


def parse_awash_html(html) -> Dict[str, str]:
    """Parse an Awash receipt page. Pure function -- no network."""
    soup = BeautifulSoup(html, "html.parser")

    data = {}
    for row in soup.select("table.info-table tr"):
        cells = row.find_all("td")
        if len(cells) == 3:
            data[cells[0].text.strip().rstrip(":")] = cells[2].text.strip()

    return {k: data.get(k) for k in _KEYS_OF_INTEREST}


def extract_awash_receipt_data(url: str) -> Dict[str, str]:
    """Extract an Awash Bank receipt from a reference (or an Awash URL)."""
    resolved = resolve_url("awash", url)
    response = get_with_retry(resolved, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()

    data = parse_awash_html(response.content)
    if not any(v for v in data.values()):
        raise ReceiptNotFound(f"No Awash receipt for {url!r}")
    return data
