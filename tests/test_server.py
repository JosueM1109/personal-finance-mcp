import asyncio
from unittest.mock import MagicMock, patch

import server as srv
from plaid_client import ItemHealth, SecretStr


def test_list_accounts_aggregates_across_items(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.accounts_get.return_value.to_dict.return_value = {
        "accounts": [
            {
                "account_id": "a1",
                "name": "Checking",
                "mask": "0001",
                "type": "depository",
                "subtype": "checking",
                "balances": {"current": 100, "available": 100, "iso_currency_code": "USD"},
            },
        ],
    }
    items = [
        ("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase")),
        ("FIDELITY", SecretStr("t2"), ItemHealth("FIDELITY", "re_auth_required", "ins_9", "Fidelity", reason="ITEM_LOGIN_REQUIRED")),
    ]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._list_accounts_impl()
    assert len(out["accounts"]) == 1
    assert out["accounts"][0]["institution"] == "Chase"
    assert out["accounts"][0]["handle"] == "chase_checking_0001"
    assert len(out["warnings"]) == 1
    assert out["warnings"][0]["institution"] == "Fidelity"
    assert out["warnings"][0]["status"] == "re_auth_required"


def test_list_accounts_surfaces_api_exception_as_warning(fake_env_tokens):
    from plaid.exceptions import ApiException
    fake_api = MagicMock()
    exc = ApiException(status=500, reason="boom")
    exc.body = '{"error_code":"INTERNAL_SERVER_ERROR","error_message":"plaid down"}'
    fake_api.accounts_get.side_effect = exc
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._list_accounts_impl()
    assert out["accounts"] == []
    assert len(out["warnings"]) == 1
    w = out["warnings"][0]
    assert w["institution"] == "Chase"
    assert w["code"] == "INTERNAL_SERVER_ERROR"
    assert w["trace_id"]


def test_list_accounts_registered_as_mcp_tool():
    # Confirm list_accounts appears in the FastMCP tool registry via list_tools().
    tools = asyncio.run(srv.mcp.list_tools())
    tool_names = {getattr(t, "name", None) for t in tools}
    assert "list_accounts" in tool_names


def test_get_balances_no_filter_returns_all(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.accounts_balance_get.return_value.to_dict.return_value = {
        "accounts": [
            {
                "account_id": "a1",
                "name": "Checking",
                "mask": "0001",
                "type": "depository",
                "subtype": "checking",
                "balances": {"current": 250.50, "available": 240.50, "iso_currency_code": "USD"},
            },
        ],
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_balances_impl(account_ids=None)
    assert len(out["accounts"]) == 1
    assert out["accounts"][0]["balance"]["current"] == 250.50
    # Called without options when account_ids is None
    call_args = fake_api.accounts_balance_get.call_args
    req = call_args[0][0]
    assert not getattr(req, "options", None) or req.options.account_ids is None or req.options.account_ids == []


def test_get_balances_with_filter_passes_account_ids(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.accounts_balance_get.return_value.to_dict.return_value = {
        "accounts": [
            {"account_id": "a1", "name": "X", "mask": "1", "type": "depository", "subtype": "checking",
             "balances": {"current": 5, "available": 5, "iso_currency_code": "USD"}},
        ],
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_balances_impl(account_ids=["a1"])
    assert len(out["accounts"]) == 1
    req = fake_api.accounts_balance_get.call_args[0][0]
    assert list(req.options.account_ids) == ["a1"]


def test_get_balances_surfaces_invalid_account_id_as_warning(fake_env_tokens):
    from plaid.exceptions import ApiException
    fake_api = MagicMock()
    exc = ApiException(status=400, reason="Bad Request")
    exc.body = '{"error_type":"INVALID_INPUT","error_code":"INVALID_ACCOUNT_ID","error_message":"nope"}'
    fake_api.accounts_balance_get.side_effect = exc
    items = [
        ("CHASE", SecretStr("t1"), ItemHealth("CHASE", "healthy", "ins_3", "Chase")),
        ("FIDELITY", SecretStr("t2"), ItemHealth("FIDELITY", "healthy", "ins_9", "Fidelity")),
    ]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_balances_impl(account_ids=["a1"])
    assert out["accounts"] == []
    assert len(out["warnings"]) == 2  # one per item
    assert all(w["code"] == "INVALID_ACCOUNT_ID" for w in out["warnings"])


def test_get_balances_skips_unhealthy_item(fake_env_tokens):
    fake_api = MagicMock()
    items = [
        ("CHASE", SecretStr("t"), ItemHealth("CHASE", "re_auth_required", "ins_3", "Chase", reason="ITEM_LOGIN_REQUIRED")),
    ]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_balances_impl(account_ids=None)
    assert out["accounts"] == []
    assert out["warnings"][0]["status"] == "re_auth_required"
    fake_api.accounts_balance_get.assert_not_called()


def test_get_transactions_paginates(fake_env_tokens):
    fake_api = MagicMock()
    # Two pages: first returns 2 txns with total=3, second returns 1 txn.
    fake_api.transactions_get.side_effect = [
        MagicMock(to_dict=MagicMock(return_value={
            "transactions": [
                {"transaction_id": "t1", "account_id": "a1", "name": "A",
                 "amount": 1, "iso_currency_code": "USD", "date": "2026-04-01",
                 "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "X"},
                 "pending": False},
                {"transaction_id": "t2", "account_id": "a1", "name": "B",
                 "amount": 2, "iso_currency_code": "USD", "date": "2026-04-02",
                 "personal_finance_category": {"primary": "TRAVEL", "detailed": "Y"},
                 "pending": False},
            ],
            "total_transactions": 3,
        })),
        MagicMock(to_dict=MagicMock(return_value={
            "transactions": [
                {"transaction_id": "t3", "account_id": "a1", "name": "C",
                 "amount": 3, "iso_currency_code": "USD", "date": "2026-04-03",
                 "personal_finance_category": {"primary": "GENERAL_MERCHANDISE", "detailed": "Z"},
                 "pending": False},
            ],
            "total_transactions": 3,
        })),
    ]
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_transactions_impl("2026-03-01", "2026-04-30")
    assert len(out["transactions"]) == 3
    ids = [t["transaction_id"] for t in out["transactions"]]
    assert ids == ["t1", "t2", "t3"]
    # Two API calls (pagination)
    assert fake_api.transactions_get.call_count == 2


def test_get_transactions_filters_by_account_ids(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.transactions_get.return_value.to_dict.return_value = {
        "transactions": [], "total_transactions": 0,
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        srv._get_transactions_impl("2026-04-01", "2026-04-30", account_ids=["a1"])
    req = fake_api.transactions_get.call_args[0][0]
    assert list(req.options.account_ids) == ["a1"]


def test_get_transactions_clips_window_older_than_2_years(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.transactions_get.return_value.to_dict.return_value = {
        "transactions": [], "total_transactions": 0,
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_transactions_impl("2020-01-01", "2026-04-30")
    # A warning should note the window was clipped.
    assert any("clipped" in (w.get("reason") or w.get("message") or "").lower() for w in out["warnings"])


def test_get_transactions_api_error_produces_warning(fake_env_tokens):
    from plaid.exceptions import ApiException
    fake_api = MagicMock()
    exc = ApiException(status=429, reason="Too Many Requests")
    exc.body = '{"error_code":"RATE_LIMIT","error_message":"slow down"}'
    fake_api.transactions_get.side_effect = exc
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_transactions_impl("2026-04-01", "2026-04-30")
    assert out["transactions"] == []
    assert out["warnings"][0]["code"] == "RATE_LIMIT"


# ---------------------------------------------------------------------------
# Task 9: get_recurring_transactions
# ---------------------------------------------------------------------------

def test_get_recurring_transactions_shapes_streams(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.accounts_get.return_value.to_dict.return_value = {
        "accounts": [{"account_id": "a1"}, {"account_id": "a2"}],
    }
    fake_api.transactions_recurring_get.return_value.to_dict.return_value = {
        "inflow_streams": [
            {"stream_id": "i1", "account_id": "a1", "description": "Payroll",
             "merchant_name": "Employer", "average_amount": {"amount": -2500, "iso_currency_code": "USD"},
             "frequency": "BIWEEKLY", "last_date": "2026-04-10", "is_active": True,
             "personal_finance_category": {"primary": "INCOME", "detailed": "INCOME_WAGES"}},
        ],
        "outflow_streams": [
            {"stream_id": "o1", "account_id": "a1", "description": "Netflix",
             "merchant_name": "Netflix", "average_amount": {"amount": 15.99},
             "frequency": "MONTHLY", "last_date": "2026-04-05", "is_active": True,
             "personal_finance_category": {"primary": "ENTERTAINMENT", "detailed": "ENTERTAINMENT_SUB"}},
        ],
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_recurring_transactions_impl()
    assert len(out["inflows"]) == 1
    assert out["inflows"][0]["merchant"] == "Employer"
    assert out["inflows"][0]["average_amount"] == -2500
    assert out["inflows"][0]["frequency"] == "BIWEEKLY"
    assert out["inflows"][0]["category"] == "INCOME"
    assert out["inflows"][0]["institution"] == "Chase"
    assert len(out["outflows"]) == 1
    assert out["outflows"][0]["merchant"] == "Netflix"
    # accounts_get was called before transactions_recurring_get
    assert fake_api.accounts_get.called
    assert fake_api.transactions_recurring_get.called


def test_get_recurring_transactions_skips_unhealthy(fake_env_tokens):
    fake_api = MagicMock()
    items = [("X", SecretStr("t"), ItemHealth("X", "re_auth_required", "ins", "X", reason="ITEM_LOGIN_REQUIRED"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_recurring_transactions_impl()
    assert out["inflows"] == []
    assert out["outflows"] == []
    assert out["warnings"][0]["status"] == "re_auth_required"


# ---------------------------------------------------------------------------
# Task 10: get_liabilities
# ---------------------------------------------------------------------------

def test_get_liabilities_shapes_credit(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.liabilities_get.return_value.to_dict.return_value = {
        "liabilities": {
            "credit": [{
                "account_id": "c1",
                "aprs": [{"apr_percentage": 24.99, "apr_type": "purchase_apr"}],
                "last_payment_amount": 200.0, "last_payment_date": "2026-04-01",
                "last_statement_balance": 500.0, "last_statement_issue_date": "2026-03-28",
                "minimum_payment_amount": 25.0,
                "next_payment_due_date": "2026-04-25",
                "is_overdue": False,
            }],
            "student": [],
            "mortgage": [],
        },
    }
    items = [("AMEX", SecretStr("t"), ItemHealth("AMEX", "healthy", "ins_amex", "Amex"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_liabilities_impl()
    assert len(out["credit"]) == 1
    assert out["credit"][0]["apr_percentage"] == 24.99
    assert out["credit"][0]["minimum_payment_amount"] == 25.0
    assert out["credit"][0]["institution"] == "Amex"


def test_get_liabilities_handles_products_not_supported(fake_env_tokens):
    from plaid.exceptions import ApiException
    exc = ApiException(status=400, reason="Bad Request")
    exc.body = '{"error_code":"PRODUCTS_NOT_SUPPORTED","error_message":"no liabilities"}'
    fake_api = MagicMock()
    fake_api.liabilities_get.side_effect = exc
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_liabilities_impl()
    assert out["credit"] == []
    assert out["warnings"][0]["code"] == "PRODUCTS_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# Task 11: get_investment_holdings
# ---------------------------------------------------------------------------

def test_get_investment_holdings_joins_securities(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.investments_holdings_get.return_value.to_dict.return_value = {
        "holdings": [{
            "account_id": "inv1", "security_id": "sec_a",
            "quantity": 10, "cost_basis": 100.0,
            "institution_value": 1500.0, "institution_price": 150.0,
            "iso_currency_code": "USD",
        }],
        "securities": [{"security_id": "sec_a", "ticker_symbol": "AAPL",
                        "name": "Apple", "type": "equity"}],
    }
    items = [("FIDELITY", SecretStr("t"), ItemHealth("FIDELITY", "healthy", "ins_f", "Fidelity"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_investment_holdings_impl()
    assert len(out["holdings"]) == 1
    assert out["holdings"][0]["symbol"] == "AAPL"
    assert out["holdings"][0]["market_value"] == 1500.0
    assert out["holdings"][0]["institution"] == "Fidelity"


# ---------------------------------------------------------------------------
# Task 12: get_investment_transactions
# ---------------------------------------------------------------------------

def test_get_investment_transactions_paginates_and_shapes(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.investments_transactions_get.side_effect = [
        MagicMock(to_dict=MagicMock(return_value={
            "investment_transactions": [
                {"investment_transaction_id": "it1", "account_id": "inv1",
                 "date": "2026-04-01", "type": "buy", "subtype": "buy",
                 "amount": 1500, "quantity": 10, "price": 150, "fees": 0,
                 "iso_currency_code": "USD", "security_id": "sec_a"},
            ],
            "securities": [{"security_id": "sec_a", "ticker_symbol": "AAPL", "name": "Apple"}],
            "total_investment_transactions": 2,
        })),
        MagicMock(to_dict=MagicMock(return_value={
            "investment_transactions": [
                {"investment_transaction_id": "it2", "account_id": "inv1",
                 "date": "2026-04-15", "type": "sell", "subtype": "sell",
                 "amount": -800, "quantity": -5, "price": 160, "fees": 1,
                 "iso_currency_code": "USD", "security_id": "sec_a"},
            ],
            "securities": [{"security_id": "sec_a", "ticker_symbol": "AAPL", "name": "Apple"}],
            "total_investment_transactions": 2,
        })),
    ]
    items = [("FIDELITY", SecretStr("t"), ItemHealth("FIDELITY", "healthy", "ins_f", "Fidelity"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_investment_transactions_impl("2026-04-01", "2026-04-30")
    assert len(out["investment_transactions"]) == 2
    assert out["investment_transactions"][0]["symbol"] == "AAPL"
    assert out["investment_transactions"][0]["type"] == "buy"
    assert fake_api.investments_transactions_get.call_count == 2


# ---------------------------------------------------------------------------
# Task 13: get_institutions_status
# ---------------------------------------------------------------------------

def test_get_institutions_status_reports_all_items(fake_env_tokens):
    fake_api = MagicMock()
    items = [
        ("CHASE", SecretStr("t1"), ItemHealth("CHASE", "healthy", "ins_3", "Chase")),
        ("FIDELITY", SecretStr("t2"), ItemHealth("FIDELITY", "re_auth_required", "ins_9", "Fidelity", reason="ITEM_LOGIN_REQUIRED")),
    ]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._get_institutions_status_impl()
    assert len(out["items"]) == 2
    keys = {i["env_key"] for i in out["items"]}
    assert keys == {"CHASE", "FIDELITY"}
    chase = next(i for i in out["items"] if i["env_key"] == "CHASE")
    fid = next(i for i in out["items"] if i["env_key"] == "FIDELITY")
    assert chase["status"] == "healthy"
    assert chase["institution"] == "Chase"
    assert fid["status"] == "re_auth_required"
    assert fid["reason"] == "ITEM_LOGIN_REQUIRED"


# ---------------------------------------------------------------------------
# Task 16: search_transactions
# ---------------------------------------------------------------------------

def test_search_transactions_matches_merchant(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.transactions_get.return_value.to_dict.return_value = {
        "transactions": [
            {"transaction_id": "t1", "account_id": "a1", "name": "STARBUCKS",
             "merchant_name": "Starbucks", "amount": 5, "iso_currency_code": "USD",
             "date": "2026-04-01", "personal_finance_category": {"primary": "FOOD", "detailed": "X"},
             "pending": False},
            {"transaction_id": "t2", "account_id": "a1", "name": "Uber Eats",
             "merchant_name": "Uber", "amount": 20, "iso_currency_code": "USD",
             "date": "2026-04-02", "personal_finance_category": {"primary": "FOOD", "detailed": "X"},
             "pending": False},
        ],
        "total_transactions": 2,
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._search_transactions_impl("starbucks", "2026-04-01", "2026-04-30")
    assert len(out["transactions"]) == 1
    assert out["transactions"][0]["merchant"] == "Starbucks"


def test_search_transactions_matches_counterparty(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.transactions_get.return_value.to_dict.return_value = {
        "transactions": [
            {"transaction_id": "t1", "account_id": "a1", "name": "DEBIT",
             "merchant_name": "Unknown Vendor", "amount": 30,
             "iso_currency_code": "USD", "date": "2026-04-01",
             "personal_finance_category": {"primary": "X", "detailed": "Y"},
             "counterparties": [{"name": "Netflix"}], "pending": False},
        ],
        "total_transactions": 1,
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._search_transactions_impl("netflix", "2026-04-01", "2026-04-30")
    assert len(out["transactions"]) == 1
    assert out["transactions"][0]["transaction_id"] == "t1"


def test_search_transactions_case_insensitive_and_no_matches(fake_env_tokens):
    fake_api = MagicMock()
    fake_api.transactions_get.return_value.to_dict.return_value = {
        "transactions": [
            {"transaction_id": "t1", "account_id": "a1", "name": "Random",
             "merchant_name": "Vendor", "amount": 5, "iso_currency_code": "USD",
             "date": "2026-04-01", "personal_finance_category": {"primary": "X", "detailed": "Y"},
             "pending": False},
        ],
        "total_transactions": 1,
    }
    items = [("CHASE", SecretStr("t"), ItemHealth("CHASE", "healthy", "ins_3", "Chase"))]
    with patch.object(srv, "build_api", return_value=fake_api), \
         patch.object(srv, "all_items", return_value=items):
        out = srv._search_transactions_impl("UNMATCHED", "2026-04-01", "2026-04-30")
    assert out["transactions"] == []
