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
    response = client.get("/receipts/tele/TESTREFTELE1")
    assert response.status_code == 401
    assert response.json()["success"] is False
    assert "detail" in response.json()


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
    assert body["success"] is True
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
    response = client.get("/receipts/tele/ABC12345", headers=HEADERS)
    assert response.status_code == status
    assert response.json()["success"] is False
    assert response.json()["detail"]


def test_malformed_reference_is_400(client):
    assert client.get("/receipts/tele/!!!", headers=HEADERS).status_code == 400


def test_unknown_bank_is_400(client):
    assert client.get("/receipts/notabank/ABC12345", headers=HEADERS).status_code == 400


# --- sync endpoint cache ---------------------------------------------------

def test_sync_endpoint_serves_and_fills_cache(client, monkeypatch):
    calls = []

    def fake(bank, ref):
        calls.append(ref)
        return SAMPLE
    monkeypatch.setattr(api, "get_receipt", fake)
    for _ in range(3):
        body = client.get("/receipts/tele/TESTREFTELE1", headers=HEADERS).json()
        assert body["amount"] == "2000.00" and body["success"] is True
    assert calls == ["TESTREFTELE1"]


# --- async verifications ---------------------------------------------------

def post(client, payload, headers=HEADERS):
    return client.post("/verifications", json=payload, headers=headers)


def test_verification_is_queued(client):
    response = post(client, {"bank": "tele", "reference": "DHS48P7HGQ"})
    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True and body["status"] == "queued"
    assert body["queue_position"] == 0 and body["estimated_wait_seconds"] >= 0
    assert body["receipt"] is None

    fetched = client.get(f"/verifications/{body['id']}", headers=HEADERS)
    assert fetched.status_code == 200 and fetched.json()["status"] == "queued"


def test_verification_cache_hit_is_immediate(client, isolated_store):
    isolated_store.put_cached("tele", ["DHS48P7HGQ"], SAMPLE)
    response = post(client, {"bank": "tele", "reference": "DHS48P7HGQ"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed" and body["receipt"]["amount"] == "2000.00"


def test_verification_dedupes(client):
    first = post(client, {"bank": "tele", "reference": "DHS48P7HGQ"}).json()
    second = post(client, {"bank": "tele", "reference": "DHS48P7HGQ"}).json()
    assert first["id"] == second["id"]
    by_ref = post(client, {"bank": "tele", "reference": "OTHERREF01", "client_ref": "d1"}).json()
    again = post(client, {"bank": "tele", "reference": "OTHERREF01", "client_ref": "d1"}).json()
    assert by_ref["id"] == again["id"]


def test_verification_queue_position_grows(client):
    ids = [post(client, {"bank": "tele", "reference": f"REFREF{i:04d}"}).json()
           for i in range(3)]
    assert [b["queue_position"] for b in ids] == [0, 1, 2]


@pytest.mark.parametrize("payload", [
    {"bank": "tele", "reference": "!!!"},
    {"bank": "notabank", "reference": "ABC12345"},
])
def test_verification_rejects_bad_input(client, payload):
    response = post(client, payload)
    assert response.status_code == 400
    assert response.json()["success"] is False


def test_verification_requires_key(client):
    assert post(client, {"bank": "tele", "reference": "DHS48P7HGQ"}, headers={}).status_code == 401


def test_cannot_read_another_keys_verification(monkeypatch):
    monkeypatch.setenv("ETHIOBANK_API_KEYS", f"{KEY},other-key")
    c = TestClient(api.app)
    job = post(c, {"bank": "tele", "reference": "DHS48P7HGQ"}).json()
    response = c.get(f"/verifications/{job['id']}", headers={"X-API-Key": "other-key"})
    assert response.status_code == 404 and response.json()["success"] is False


# --- service endpoints -----------------------------------------------------

def test_health_needs_no_key(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["success"] is True


def test_banks_lists_all_six(client):
    body = client.get("/banks").json()
    assert body["success"] is True
    keys = {b["key"] for b in body["banks"]}
    assert keys == {"tele", "cbe", "awash", "zemen", "dashen", "boa"}
