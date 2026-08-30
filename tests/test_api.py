"""API tests. Bank calls are stubbed, so these never touch the network."""
import pytest
from fastapi.testclient import TestClient

import ethiobank_receipts.api as api
from ethiobank_receipts.errors import (
    BankUnavailable, InvalidReference, ReceiptNotFound, ReceiptParseError)

KEY = "test-key"
HEADERS = {"X-API-Key": KEY}

SAMPLE = {
    "bank": "tele", "reference": "TESTREFTELE1", "status": "COMPLETED",
    "amount": "2000.00", "currency": "ETB", "paid_at": "2026-08-28T16:39:13",
    "payer_name": "TEST PAYER ONE", "payer_account": "2519****0001",
    "receiver_name": "TEST RECEIVER ONE", "receiver_account": "2519****0002",
    "fees": {"service_fee": "5.22", "vat": "0.78", "payer_total_debit": "2006.00"},
    "raw": {},
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ETHIOBANK_API_KEYS", KEY)
    return TestClient(api.app)


@pytest.fixture
def ok(monkeypatch):
    monkeypatch.setattr(api, "get_receipt", lambda bank, ref: SAMPLE)


def raises(monkeypatch, exc):
    def boom(bank, ref):
        raise exc
    monkeypatch.setattr(api, "get_receipt", boom)


# --- auth ------------------------------------------------------------------

def test_requires_api_key(client, ok):
    assert client.get("/receipts/tele/TESTREFTELE1").status_code == 401


def test_rejects_wrong_api_key(client, ok):
    response = client.get("/receipts/tele/TESTREFTELE1",
                          headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_accepts_valid_api_key(client, ok):
    assert client.get("/receipts/tele/TESTREFTELE1", headers=HEADERS).status_code == 200


def test_unconfigured_service_fails_closed(monkeypatch):
    monkeypatch.delenv("ETHIOBANK_API_KEYS", raising=False)
    response = TestClient(api.app).get("/receipts/tele/X", headers=HEADERS)
    assert response.status_code == 503


# --- payload ---------------------------------------------------------------

def test_returns_settled_amount_not_total(client, ok):
    body = client.get("/receipts/tele/TESTREFTELE1", headers=HEADERS).json()
    assert body["amount"] == "2000.00"
    assert body["fees"]["payer_total_debit"] == "2006.00"


def test_amount_is_a_string_not_a_float(client, ok):
    body = client.get("/receipts/tele/TESTREFTELE1", headers=HEADERS).json()
    assert isinstance(body["amount"], str)


# --- error mapping ---------------------------------------------------------

@pytest.mark.parametrize("exc,status", [
    (ReceiptNotFound("nope"), 404),
    (InvalidReference("bad"), 400),
    (BankUnavailable("down"), 502),
    (ReceiptParseError("layout changed"), 500),
])
def test_error_mapping(client, monkeypatch, exc, status):
    raises(monkeypatch, exc)
    assert client.get("/receipts/tele/ABC12345", headers=HEADERS).status_code == status


def test_malformed_reference_is_400(client):
    assert client.get("/receipts/tele/!!!", headers=HEADERS).status_code == 400


def test_unknown_bank_is_400(client):
    assert client.get("/receipts/notabank/ABC12345", headers=HEADERS).status_code == 400


# --- service endpoints -----------------------------------------------------

def test_health_needs_no_key(client):
    assert client.get("/health").json()["status"] == "ok"


def test_banks_lists_all_six(client):
    keys = {b["key"] for b in client.get("/banks").json()["banks"]}
    assert keys == {"tele", "cbe", "awash", "zemen", "dashen", "boa"}
