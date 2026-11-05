import pytest

import ethiobank_receipts.api as api
from ethiobank_receipts.store import Store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Every test gets its own SQLite file; nothing touches a real database."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("ETHIOBANK_DB_PATH", path)
    store = Store(path)
    monkeypatch.setattr(api, "_store", store)
    return store
