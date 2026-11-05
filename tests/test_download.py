"""Retry behaviour of the shared HTTP helper. The session is stubbed -- no network."""
import pytest
import requests

import ethiobank_receipts.download as download


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(download.time, "sleep", sleeps.append)
    return sleeps


def stub(monkeypatch, outcomes):
    """Make session.get return/raise each outcome in turn; return the call log."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(download.session, "get", fake_get)
    return calls


def test_recovers_after_two_connection_errors(monkeypatch, no_sleep):
    calls = stub(monkeypatch, [requests.ConnectionError(), requests.ConnectionError(),
                               FakeResponse(200)])
    assert download.get_with_retry("https://x.test/r").status_code == 200
    assert len(calls) == 3
    assert no_sleep == [0.5, 1.0]


def test_gives_up_after_max_retries(monkeypatch, no_sleep):
    calls = stub(monkeypatch, [requests.ConnectionError()] * 3)
    with pytest.raises(requests.ConnectionError):
        download.get_with_retry("https://x.test/r")
    assert len(calls) == 3


def test_retries_5xx(monkeypatch, no_sleep):
    calls = stub(monkeypatch, [FakeResponse(503), FakeResponse(200)])
    assert download.get_with_retry("https://x.test/r").status_code == 200
    assert len(calls) == 2


def test_returns_last_5xx_after_max_retries(monkeypatch, no_sleep):
    calls = stub(monkeypatch, [FakeResponse(502)] * 3)
    assert download.get_with_retry("https://x.test/r").status_code == 502
    assert len(calls) == 3


def test_does_not_retry_404(monkeypatch, no_sleep):
    calls = stub(monkeypatch, [FakeResponse(404)])
    assert download.get_with_retry("https://x.test/r").status_code == 404
    assert len(calls) == 1
    assert no_sleep == []


def test_does_not_retry_read_timeout(monkeypatch, no_sleep):
    calls = stub(monkeypatch, [requests.ReadTimeout()])
    with pytest.raises(requests.ReadTimeout):
        download.get_with_retry("https://x.test/r")
    assert len(calls) == 1


def test_retries_connect_timeout(monkeypatch, no_sleep):
    calls = stub(monkeypatch, [requests.ConnectTimeout(), FakeResponse(200)])
    assert download.get_with_retry("https://x.test/r").status_code == 200
    assert len(calls) == 2
