import json
import pytest
from plaid_client import SecretStr, load_tokens, map_plaid_error, build_api


def test_secretstr_repr_redacts():
    s = SecretStr("access-prod-abc123")
    assert "abc123" not in repr(s)
    assert "abc123" not in str(s)
    assert s.reveal() == "access-prod-abc123"


def test_secretstr_not_in_format_string():
    s = SecretStr("access-prod-xyz")
    assert "xyz" not in f"{s}"
    assert "xyz" not in "{}".format(s)


def test_load_tokens_picks_up_plaid_token_prefix(fake_env_tokens):
    tokens = load_tokens()
    assert set(tokens.keys()) == {"CHASE", "FIDELITY"}
    assert tokens["CHASE"].reveal() == "access-prod-fake-chase"


def test_load_tokens_empty_when_no_env(monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith("PLAID_TOKEN_"):
            monkeypatch.delenv(k)
    assert load_tokens() == {}


class FakeApiException(Exception):
    def __init__(self, body: dict):
        self.body = json.dumps(body)


def test_map_plaid_error_item_login_required():
    exc = FakeApiException({
        "error_type": "ITEM_ERROR",
        "error_code": "ITEM_LOGIN_REQUIRED",
        "error_message": "login required",
        "request_id": "req-123",
    })
    out = map_plaid_error(exc, institution="Chase")
    assert out["error"]["code"] == "ITEM_LOGIN_REQUIRED"
    assert out["error"]["institution"] == "Chase"
    assert "req-123" not in json.dumps(out)  # request_id never leaks
    assert out["error"]["trace_id"]  # uuid present


def test_map_plaid_error_rate_limit():
    exc = FakeApiException({
        "error_type": "RATE_LIMIT_EXCEEDED",
        "error_code": "RATE_LIMIT",
        "error_message": "slow down",
        "request_id": "req-999",
    })
    out = map_plaid_error(exc, institution=None)
    assert out["error"]["code"] == "RATE_LIMIT"
    assert "institution" not in out["error"]


def test_map_plaid_error_malformed_body():
    # Defensive: body isn't JSON, don't crash
    class WeirdExc(Exception):
        body = "not json at all"
    out = map_plaid_error(WeirdExc(), institution=None)
    assert out["error"]["code"] == "UNKNOWN"
    assert out["error"]["trace_id"]


def test_map_plaid_error_non_dict_body():
    class ListBody(Exception):
        body = '["not", "a", "dict"]'
    out = map_plaid_error(ListBody(), institution=None)
    assert out["error"]["code"] == "UNKNOWN"
    assert out["error"]["trace_id"]


def test_build_api_uses_env(fake_env_tokens):
    api = build_api()
    assert api is not None  # smoke — real SDK returns a PlaidApi


import time
from unittest.mock import MagicMock
from plaid_client import get_item_health, ItemHealth, _health_cache, all_items


def test_get_item_health_healthy():
    _health_cache.clear()
    api = MagicMock()
    api.item_get.return_value.to_dict.return_value = {
        "item": {"institution_id": "ins_3", "error": None},
    }
    api.institutions_get_by_id.return_value.to_dict.return_value = {
        "institution": {"name": "Chase"},
    }
    h = get_item_health(api, "CHASE", SecretStr("t"))
    assert h.status == "healthy"
    assert h.institution_name == "Chase"
    assert h.institution_id == "ins_3"


def test_get_item_health_relogin():
    _health_cache.clear()
    api = MagicMock()
    api.item_get.return_value.to_dict.return_value = {
        "item": {
            "institution_id": "ins_3",
            "error": {"error_code": "ITEM_LOGIN_REQUIRED"},
        },
    }
    api.institutions_get_by_id.return_value.to_dict.return_value = {
        "institution": {"name": "Chase"},
    }
    h = get_item_health(api, "CHASE", SecretStr("t"))
    assert h.status == "re_auth_required"
    assert h.reason == "ITEM_LOGIN_REQUIRED"


def test_get_item_health_pending_expiration():
    _health_cache.clear()
    api = MagicMock()
    api.item_get.return_value.to_dict.return_value = {
        "item": {"institution_id": "ins_3",
                 "error": {"error_code": "PENDING_EXPIRATION"}},
    }
    api.institutions_get_by_id.return_value.to_dict.return_value = {
        "institution": {"name": "Chase"},
    }
    h = get_item_health(api, "CHASE", SecretStr("t"))
    assert h.status == "pending_expiration"


def test_get_item_health_unknown_error_on_api_exception():
    _health_cache.clear()
    from plaid.exceptions import ApiException
    api = MagicMock()
    api.item_get.side_effect = ApiException(status=500, reason="boom")
    h = get_item_health(api, "BANK", SecretStr("t"))
    assert h.status == "unknown_error"


def test_health_cache_ttl_hit():
    _health_cache.clear()
    api = MagicMock()
    api.item_get.return_value.to_dict.return_value = {"item": {"institution_id": "ins", "error": None}}
    api.institutions_get_by_id.return_value.to_dict.return_value = {"institution": {"name": "X"}}
    get_item_health(api, "X", SecretStr("t"))
    get_item_health(api, "X", SecretStr("t"))
    assert api.item_get.call_count == 1  # second call used the cache


def test_all_items_iterates_env_tokens(fake_env_tokens):
    _health_cache.clear()
    api = MagicMock()
    api.item_get.return_value.to_dict.return_value = {"item": {"institution_id": "ins", "error": None}}
    api.institutions_get_by_id.return_value.to_dict.return_value = {"institution": {"name": "Bank"}}
    items = all_items(api)
    keys = {env_key for env_key, _tok, _h in items}
    assert keys == {"CHASE", "FIDELITY"}


def test_get_item_health_extracts_error_code_from_api_exception_body():
    _health_cache.clear()
    from plaid.exceptions import ApiException
    exc = ApiException(status=400, reason="Bad Request")
    exc.body = (
        '{"error_type":"INVALID_INPUT","error_code":"INVALID_ACCESS_TOKEN",'
        '"error_message":"bad","request_id":"req-x"}'
    )
    api = MagicMock()
    api.item_get.side_effect = exc
    h = get_item_health(api, "BANK", SecretStr("t"))
    assert h.status == "unknown_error"
    assert h.reason == "INVALID_ACCESS_TOKEN"
    assert "req-x" not in (h.reason or "")


def test_get_item_health_survives_institutions_network_error():
    _health_cache.clear()
    api = MagicMock()
    api.item_get.return_value.to_dict.return_value = {
        "item": {"institution_id": "ins_3", "error": None},
    }
    api.institutions_get_by_id.side_effect = ConnectionError("network down")
    h = get_item_health(api, "CHASE", SecretStr("t"))
    assert h.status == "healthy"
    assert h.institution_id == "ins_3"
    assert h.institution_name is None


# ============================================================================
# Response shaping helpers
# ============================================================================

from plaid_client import shape_account, shape_transaction, shape_holding, make_handle


def test_make_handle_deterministic():
    assert make_handle("Chase", "checking", "1234") == "chase_checking_1234"
    assert make_handle("American Express", "credit card", "5678") == "americanexpress_creditcard_5678"


def test_make_handle_no_mask():
    assert make_handle("Chase", "savings", None) == "chase_savings"
    assert make_handle("Chase", "savings", "") == "chase_savings"


def test_shape_account_trims_and_adds_handle():
    raw = {
        "account_id": "acc_123",
        "persistent_account_id": "pid_SHOULD_NOT_LEAK",
        "name": "Chase Checking",
        "official_name": "Chase Total Checking",
        "mask": "1234",
        "type": "depository",
        "subtype": "checking",
        "balances": {
            "current": 1500.0,
            "available": 1450.0,
            "iso_currency_code": "USD",
        },
    }
    out = shape_account(raw, institution="Chase")
    assert out["handle"] == "chase_checking_1234"
    assert out["mask"] == "1234"
    assert out["institution"] == "Chase"
    assert out["account_id"] == "acc_123"
    assert out["balance"]["current"] == 1500.0
    assert out["balance"]["available"] == 1450.0
    assert out["balance"]["currency"] == "USD"
    assert "persistent_account_id" not in out


def test_shape_account_missing_balances():
    raw = {"account_id": "a", "name": "X", "type": "depository", "subtype": "checking"}
    out = shape_account(raw, institution=None)
    assert out["balance"]["current"] is None
    assert out["balance"]["currency"] is None


def test_shape_transaction_uses_pfc_not_legacy_category():
    raw = {
        "transaction_id": "txn_1",
        "account_id": "acc_1",
        "name": "STARBUCKS",
        "merchant_name": "Starbucks",
        "amount": 5.75,
        "iso_currency_code": "USD",
        "date": "2026-04-15",
        "category": ["Food and Drink"],  # legacy — ignore
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_COFFEE",
        },
        "counterparties": [{"name": "Starbucks"}],
        "pending": False,
    }
    out = shape_transaction(raw)
    assert out["category"]["primary"] == "FOOD_AND_DRINK"
    assert out["category"]["detailed"] == "FOOD_AND_DRINK_COFFEE"
    assert out["merchant"] == "Starbucks"
    assert out["pending"] is False
    assert out["amount"] == 5.75
    assert out["currency"] == "USD"
    assert out["date"] == "2026-04-15"


def test_shape_transaction_falls_back_to_name_when_no_merchant():
    raw = {
        "transaction_id": "t",
        "account_id": "a",
        "name": "Random Vendor",
        "amount": 10,
        "date": "2026-04-15",
    }
    out = shape_transaction(raw)
    assert out["merchant"] == "Random Vendor"
    assert out["category"]["primary"] is None


def test_shape_holding_joins_with_securities():
    holding = {
        "account_id": "acc_inv",
        "security_id": "sec_abc",
        "quantity": 10.0,
        "cost_basis": 100.0,
        "institution_value": 1500.0,
        "institution_price": 150.0,
        "iso_currency_code": "USD",
    }
    securities = {
        "sec_abc": {"ticker_symbol": "AAPL", "name": "Apple Inc.", "type": "equity"},
    }
    out = shape_holding(holding, securities)
    assert out["symbol"] == "AAPL"
    assert out["name"] == "Apple Inc."
    assert out["type"] == "equity"
    assert out["quantity"] == 10.0
    assert out["market_value"] == 1500.0
    assert out["cost_basis"] == 100.0
    assert out["price"] == 150.0
    assert out["currency"] == "USD"


def test_shape_holding_missing_security():
    holding = {"account_id": "a", "security_id": "sec_missing", "quantity": 1.0}
    out = shape_holding(holding, securities={})
    assert out["symbol"] is None
    assert out["name"] is None
