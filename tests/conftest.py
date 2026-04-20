import os
import pytest
from unittest.mock import MagicMock

@pytest.fixture
def fake_env_tokens(monkeypatch):
    monkeypatch.setenv("PLAID_TOKEN_CHASE", "access-prod-fake-chase")
    monkeypatch.setenv("PLAID_TOKEN_FIDELITY", "access-prod-fake-fidelity")
    monkeypatch.setenv("PLAID_CLIENT_ID", "client-id-fake")
    monkeypatch.setenv("PLAID_SECRET", "secret-fake")
    monkeypatch.setenv("PLAID_ENV", "sandbox")

@pytest.fixture
def mock_plaid_api():
    return MagicMock()
