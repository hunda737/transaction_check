"""Offline parser tests against redacted fixtures of real receipts.

The headline assertion in every bank's test is that `amount` is the settled
figure, not the payer's fee-inclusive total.
"""
import io
import json
import os

import pytest

from ethiobank_receipts.banks import resolve_url
from ethiobank_receipts.errors import InvalidReference, ReceiptParseError
from ethiobank_receipts.extractors.awash import parse_awash_html
from ethiobank_receipts.extractors.cbe import map_cbe_receipt
from ethiobank_receipts.extractors.tele import parse_tele_html
from ethiobank_receipts.extractors.zemen import parse_zemen_text
from ethiobank_receipts.schema import money, normalize, to_iso

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    return io.open(os.path.join(FIXTURES, name), encoding="utf-8").read()


def json_fixture(name):
    return json.loads(fixture(name))


# --- the fee bug: amount must exclude service fee and VAT -------------------

def test_tele_amount_excludes_fees():
    receipt = normalize("tele", parse_tele_html(fixture("tele_receipt_1.html")))
    assert receipt["amount"] == "2000.00"          # settled
    assert receipt["fees"]["payer_total_debit"] == "2006.00"  # not the amount


def test_tele_second_receipt_amount_excludes_fees():
    receipt = normalize("tele", parse_tele_html(fixture("tele_receipt_2.html")))
    assert receipt["amount"] == "310.00"
    assert receipt["fees"]["payer_total_debit"] == "312.00"


def test_zemen_amount_excludes_fees():
    receipt = normalize("zemen", parse_zemen_text(fixture("zemen_receipt.txt")))
    assert receipt["amount"] == "4000.00"
    assert receipt["fees"]["payer_total_debit"] == "4023.00"


def test_awash_amount():
    receipt = normalize("awash", parse_awash_html(fixture("awash_receipt.html")))
    assert receipt["amount"] == "20.00"


def test_cbe_amount_excludes_fees():
    receipt = normalize("cbe", map_cbe_receipt(json_fixture("cbe_receipt.json")))
    assert receipt["amount"] == "10500.00"          # settled/credited
    assert receipt["fees"]["payer_total_debit"] == "10503.60"  # not the amount


def test_cbe_normalized_fields():
    receipt = normalize("cbe", map_cbe_receipt(json_fixture("cbe_receipt.json")))
    assert receipt["reference"] == "FT26203953V8"
    assert receipt["status"] == "COMPLETED"
    assert receipt["paid_at"] == "2026-07-22T06:31:00+00:00"  # ISO with tz kept
    assert receipt["currency"] == "ETB"
    assert receipt["fees"]["service_fee"] == "3.00"
    assert receipt["fees"]["vat"] == "0.45"


def test_cbe_ft_account_is_retired():
    from ethiobank_receipts.extractors.cbe import extract_cbe_receipt_info_from_ft
    with pytest.raises(InvalidReference):
        extract_cbe_receipt_info_from_ft("FT26203953V8", "1000141561522")


# --- the parser returned labels instead of values --------------------------

def test_tele_returns_values_not_labels():
    raw = parse_tele_html(fixture("tele_receipt_1.html"))
    assert raw["invoice_no"] == "TESTREFTELE1"
    assert raw["payment_date"] == "28-08-2026 16:39:13"
    # The old find_next('td') logic put the *next label* here.
    assert "Payment date" not in raw["invoice_no"]
    assert "Settled Amount" not in raw["payment_date"]


def test_tele_normalized_fields():
    receipt = normalize("tele", parse_tele_html(fixture("tele_receipt_1.html")))
    assert receipt["reference"] == "TESTREFTELE1"
    assert receipt["status"] == "COMPLETED"
    assert receipt["paid_at"] == "2026-08-28T16:39:13"  # day-first, not Aug 28 -> 08-28
    assert receipt["currency"] == "ETB"


def test_zemen_payer_name_not_truncated_into_next_label():
    raw = parse_zemen_text(fixture("zemen_receipt.txt"))
    # The greedy [A-Z\s]+ pattern used to append the 'A' of "Address".
    assert raw["Payer Name"] == "TEST ZEMEN PAYER"
    assert raw["Date"] == "25-Jul-2025"          # was missed: Amharic colon
    assert raw["Transaction Detail"]              # was missed entirely


def test_zemen_settled_amount_is_not_atm_specific():
    """The old regex hardcoded 'ATM CASH WITHDRAWAL' and matched nothing else."""
    text = fixture("zemen_receipt.txt").replace(
        "Remot On Us - ATM CASH WITHDRAWAL", "MOBILE BANKING TRANSFER")
    assert parse_zemen_text(text)["Settled Amount"] == "ETB 4,000.00"


# --- fail closed -----------------------------------------------------------

def test_empty_page_raises_rather_than_returning_partial():
    with pytest.raises(ReceiptParseError):
        normalize("tele", parse_tele_html("<html><body>nothing</body></html>"))


def test_missing_amount_raises():
    with pytest.raises(ReceiptParseError):
        normalize("tele", {"invoice_no": "X", "status": "Completed"})


# --- SSRF: references only, never arbitrary URLs ---------------------------

@pytest.mark.parametrize("bad", [
    "'; DROP TABLE--", "", "   ", "../../etc/passwd",
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example.com/receipt/DHS48P7HGQ",
])
def test_rejects_non_reference_input(bad):
    with pytest.raises(InvalidReference):
        resolve_url("tele", bad)


def test_builds_url_from_reference():
    assert resolve_url("tele", "DHS48P7HGQ") == \
        "https://transactioninfo.ethiotelecom.et/receipt/DHS48P7HGQ"


def test_accepts_same_host_url():
    url = "https://transactioninfo.ethiotelecom.et/receipt/DHS48P7HGQ"
    assert resolve_url("tele", url) == url


def test_cbe_builds_url_from_token():
    assert resolve_url("cbe", "v2-hfHCxFSFTS8uPZdBBOLz") == \
        "https://mbreciept.cbe.com.et/v2-hfHCxFSFTS8uPZdBBOLz"


def test_cbe_accepts_same_host_url():
    url = "https://mbreciept.cbe.com.et/v2-hfHCxFSFTS8uPZdBBOLz"
    assert resolve_url("cbe", url) == url


@pytest.mark.parametrize("bad", [
    "FT26203953V8", "FT25211G11JQ21827223", "v2-short",
    "https://evil.example.com/v2-hfHCxFSFTS8uPZdBBOLz",
])
def test_cbe_rejects_non_token_input(bad):
    with pytest.raises(InvalidReference):
        resolve_url("cbe", bad)


def test_unsupported_bank():
    with pytest.raises(InvalidReference):
        resolve_url("notabank", "ABC123")


# --- helpers ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2,000 Birr", "2000"), ("ETB 4,000.00", "4000.00"), ("20 ETB", "20"),
    ("0.0 Birr", "0.0"), ("312", "312"), (None, None), ("n/a", None),
])
def test_money_parsing(raw, expected):
    result = money(raw)
    assert (str(result) if result is not None else None) == expected


def test_date_parsing_is_day_first_for_telebirr():
    assert to_iso("28-08-2026 16:39:13") == "2026-08-28T16:39:13"
    assert to_iso("2025-08-04 09:25:12 AM") == "2025-08-04T09:25:12"
    assert to_iso("not a date") is None
